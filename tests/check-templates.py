#!/usr/bin/env python3
"""生成四条新机配置并验证安全边界；可选用实际 Xray -test。不会监听端口或访问面板。"""
import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SH=r'''
source "$1/relay.sh"
SNI=node.example.test
NODE_NAME=offline-test
IN_PORT=30453
HY2_CRT="$2/test.crt"; HY2_KEY="$2/test.key"
api() {
  case "$2" in
    server/getNewX25519Cert) printf '{"success":true,"obj":{"privateKey":"%s","publicKey":"%s"}}\n' "$TEST_ONLY_KEY" "$TEST_ONLY_KEY" ;;
    server/getNewUUID) python3 -c 'import json,uuid;print(json.dumps({"obj":{"uuid":str(uuid.uuid4())}}))' ;;
    *) return 12 ;;
  esac
}
for p in vless hy2; do
  "inbound_body_$p" v4 127.0.0.1
  "inbound_body_$p" v6 ::1
done
'''

def main(xray):
    parent="/run" if os.geteuid()==0 and Path("/run").is_dir() else None
    tmp=Path(tempfile.mkdtemp(prefix="relay-template-test-",dir=parent))
    try:
        env={**os.environ,"TEST_ONLY_KEY":base64.urlsafe_b64encode(bytes(range(1,33))).decode().rstrip("=")}
        p=subprocess.run(["bash","-c",SH,"check",str(ROOT),str(tmp)],env=env,text=True,capture_output=True)
        if p.returncode:raise RuntimeError("模板生成失败："+p.stderr[:180])
        rows=[json.loads(l) for l in p.stdout.splitlines()]
        assert len(rows)==4
        assert {(i["protocol"],i["listen"]) for i in rows}=={(p,ip) for p in ("vless","hysteria") for ip in ("127.0.0.1","::1")}
        ids=set();passwords=set()
        for i in rows:
            s=i["streamSettings"]
            assert i["enable"] and i["port"]==30453
            if i["protocol"]=="vless":
                assert s["security"]=="reality" and s["realitySettings"]["target"]=="127.0.0.1:8443"
                assert s["realitySettings"]["serverNames"]==["node.example.test"] and not i["settings"].get("fallbacks")
                ids.add(i["settings"]["clients"][0]["id"])
            else:
                m=s["hysteriaSettings"]["masquerade"]
                assert m.get("type")=="string" and m["statusCode"]==404 and not m.get("url")
                assert s["tlsSettings"]["serverName"]=="node.example.test"
                passwords.add(i["settings"]["clients"][0]["auth"])
        assert len(ids)==len(passwords)==2 and all(passwords)
        if xray:
            # Parse API-generated data as Xray will receive it; this mode never starts a listener.
            inbounds=[{k:i[k] for k in ("listen","port","protocol","settings","streamSettings")} for i in rows]
            for n,i in enumerate(inbounds):i["tag"]=f"offline-{n}"
            c=json.dumps({"inbounds":inbounds,"outbounds":[{"protocol":"blackhole","tag":"discard"}]}).encode()
            p=subprocess.run([xray,"run","-test","-format","json","-c","stdin:"],input=c,capture_output=True,timeout=8)
            if p.returncode:raise RuntimeError("Xray 对新生成的四条配置返回错误："+p.stdout.decode(errors="replace")[-280:])
        sentinel=tmp/"existing-panel";sentinel.touch()
        p=subprocess.run(["bash","-c",'source "$1/relay.sh"; XUI_BIN=$2; do_init',"check",str(ROOT),str(sentinel)],capture_output=True,text=True,stdin=subprocess.DEVNULL)
        assert p.returncode!=0 and "禁止重新初始化" in p.stdout
        print("通过：双栈 TCP 与 HY2 四条模板、未认证行为、独立凭据、已有面板保护"+("、核心配置解析" if xray else ""))
    finally:
        # Only synthetic fixture certificates; preserve recoverability as per workstation policy.
        dest=Path("/run/relay-retired") if parent else Path.home()/".Trash"
        if dest.is_dir() or parent:
            dest.mkdir(exist_ok=True,mode=0o700)
            shutil.move(str(tmp),str(dest/tmp.name))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--xray");main(p.parse_args().xray)
