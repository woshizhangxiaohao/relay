#!/usr/bin/env python3
"""用合成链接检查私有配置的导出、凭据解析、分流顺序和安全覆盖；不需要真实节点。"""
import argparse
import base64
import hashlib
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse as u
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
M=runpy.run_path(str(ROOT/"relay-clash.py"))

def main(core):
    tmp=Path(tempfile.mkdtemp(prefix="relay-clash-test-",dir="/run" if os.geteuid()==0 and Path("/run").is_dir() else None))
    try:
        # Deliberately non-production IPs, keys, passwords and certificate fingerprint.
        pin=hashlib.sha256(b"public synthetic test certificate").hexdigest()
        pk=base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
        principal="fixture-user:p@ss#word";mask="fixture-mask+ &/#"
        h="node.example.test"
        def link(v6,hysteria):
            addr="[2001:db8::5]" if v6 else "192.0.2.5";family="IPv6" if v6 else "IPv4"
            if hysteria:
                q={"sni":h,"alpn":"h3","pinSHA256":":".join(pin[i:i+2] for i in range(0,64,2)),"insecure":"0","obfs":"salamander","obfs-password":mask}
                return "hysteria2://"+u.quote(principal,safe="")+"@"+addr+":443/?"+u.urlencode(q)+"#"+u.quote("测试-HY2-"+family)
            q={"encryption":"none","security":"reality","flow":"xtls-rprx-vision","type":"tcp","sni":h,"pbk":pk,"sid":"0001020304050607","fp":"chrome","spx":"/"}
            return "vless://"+str(uuid.UUID(int=900+v6))+"@"+addr+":443?"+u.urlencode(q)+"#"+u.quote("测试-VLESS-"+family)
        inp=tmp/"nodes.txt";out=tmp/"test.private.yaml"
        inp.write_text("[HY2-v6] "+link(True,True)+"\n"+link(False,True)+"\n[VLESS-v6] "+link(True,False)+"\n[VLESS-v4] "+link(False,False)+"\n"+link(False,False)+"\n")
        (tmp/"geosite-cn.list").write_text("# domain\n+.example.cn\n+.baidu.com\n")
        (tmp/"geoip-cn.list").write_text("1.0.1.0/24\n2400:3200::/32\n")
        nodes=M["load_nodes"]([inp]);assert len(nodes)==4 and nodes[0]["name"]=="测试-VLESS-IPv4"
        assert nodes[0]["reality-opts"]=={"public-key":pk,"short-id":"0001020304050607"} and nodes[0]["servername"]==h and nodes[0]["client-fingerprint"]=="chrome"
        n=nodes[-1];assert n["server"]=="2001:db8::5" and n["password"]==principal and n["obfs-password"]==mask and n["fingerprint"]==pin and n["skip-cert-verify"] is False
        def rejected(fn):
            try:fn()
            except M["Problem"] as e:
                assert all(x not in str(e) for x in (principal,mask,pk));return
            raise AssertionError("应拒绝危险或不受支持的输入")
        rejected(lambda:M["proxy_from_uri"](link(False,True).replace("insecure=0","insecure=1")))
        rejected(lambda:M["proxy_from_uri"](link(False,True).replace("pinSHA256=","missing-pin=")))
        rejected(lambda:M["read_list"](b"0.0.0.0/0","ipcidr"))
        dup=tmp/"dup.txt";dup.write_text(link(False,True)+"\n"+link(True,True).split("#")[0]+"#"+link(False,True).split("#")[1])
        rejected(lambda:M["load_nodes"]([dup]))
        providers,ref=M["rule_data"]("inline",tmp);cfg=M["make_config"](nodes,providers)
        rules=cfg["rules"]
        assert cfg["mode"]=="rule" and rules[-1]=="MATCH,"+M["GROUP"]
        assert rules.index("RULE-SET,cn-domain,DIRECT")<rules.index("RULE-SET,cn-ip,DIRECT")<rules.index("IP-CIDR6,fc00::/7,DIRECT,no-resolve")<len(rules)-1
        assert cfg["dns"]["direct-nameserver-follow-policy"] and cfg["dns"]["nameserver-policy"]["+.lan"]==["system"]
        assert cfg["proxy-groups"][0]["proxies"]==[x["name"] for x in nodes] and "DIRECT" not in cfg["proxy-groups"][0]["proxies"]
        assert cfg["rule-providers"]["cn-ip"]["payload"]==["1.0.1.0/24","2400:3200::/32"]
        assert cfg["rule-providers"]["cn-domain"]["type"]=="inline"
        live,_=M["rule_data"]("online")
        assert all(v["type"]=="http" and v["url"].startswith(M["RAW"]+"/meta/geo/") and v["proxy"]==M["GROUP"] for v in live.values())
        cmd=["bash",str(ROOT/"relay.sh"),"clash","--links",str(inp),"--rules-dir",str(tmp),"-o",str(out)]
        def execute(extra=()):
            r=subprocess.run(cmd+list(extra),capture_output=True,text=True)
            assert all(x not in r.stdout+r.stderr for x in (principal,mask,pk))
            return r.returncode
        assert execute()==0
        assert out.read_bytes()==M["render"](cfg,ref) and stat.S_IMODE(out.stat().st_mode)==0o600
        try:import yaml
        except ModuleNotFoundError:pass
        else:assert yaml.safe_load(out.read_text())==cfg
        assert execute()==0 and not list(tmp.glob("*.bak-*"))
        original=b"# earlier profile\n";out.write_bytes(original)
        assert execute()!=0 and out.read_bytes()==original
        assert execute(("--replace",))==0
        backups=list(tmp.glob("*.bak-*"))
        assert len(backups)==1 and backups[0].read_bytes()==original and stat.S_IMODE(backups[0].stat().st_mode)==0o600
        sy=tmp/"link.yaml";sy.symlink_to(out)
        rejected(lambda:M["write_profile"](sy,b"test",True))
        rejected(lambda:M["write_profile"](ROOT/"site"/"must-not-write.private.yaml",b"test"))
        if core:
            home=tmp/"core";home.mkdir()
            r=subprocess.run([core,"-t","-d",str(home),"-f",str(out)],capture_output=True,timeout=25)
            if r.returncode:raise RuntimeError("Mihomo 未接受合成配置")
        print("通过：四节点转换、TLS 指纹、规则顺序、私有文件权限、备份与防止误发布"+("、Mihomo 解析" if core else ""))
    finally:
        dest=Path("/run/relay-retired") if tmp.parent==Path("/run") else Path.home()/".Trash"
        if dest.is_dir() or tmp.parent==Path("/run"):
            dest.mkdir(exist_ok=True,mode=0o700);shutil.move(str(tmp),str(dest/tmp.name))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--mihomo");main(p.parse_args().mihomo)
