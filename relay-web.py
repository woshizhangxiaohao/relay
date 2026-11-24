#!/usr/bin/env python3
"""为单台节点准备可自动续期的公信证书与本机静态 HTTPS。不会操作 x-ui、Xray 或 UDP。"""
import argparse
import datetime
import fcntl
import hashlib
import io
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ETC = Path("/etc/relay-local-web")
DATA = Path("/var/lib/relay-local-cert")
LIB = Path("/usr/local/lib/relay-local-web")
ASSETS = Path("/usr/local/share/relay-local-web")
UNITS = Path("/etc/systemd/system")
WEB = "relay-local-web.service"
PORT = 8443
ACME_SHA = "35bf6b947478666be137f938f77c5089954f120798220e6fff390949c5ea5692"
CADDY = "2.11.9"
HASHES = {"amd64":"5b8cb7faf8726a2028ad96a66e54e6e81e8f361bc6175e5d22bbd997cc5177fb",
          "arm64":"5e377cff994fc9362a6fcf695c53f226cfca3a3ef85bd3a3687cf252d6310165"}

def hostname(s):
    if len(s)>253 or len(s.split("."))<3 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",l) for l in s.split(".")):
        raise ValueError("请填写自己控制的专用子域名（小写 ASCII 域名）")
    return s

def write(path,data,mode):
    data = data.encode() if isinstance(data,str) else data
    if path.is_symlink():
        raise ValueError("拒绝覆盖符号链接："+str(path))
    if path.is_file() and path.read_bytes()==data and stat.S_IMODE(path.stat().st_mode)==mode:
        return False
    if path.exists():
        b = DATA/"backups"/datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        b.mkdir(mode=0o700,parents=True)
        shutil.copy2(path,b/path.name)
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix=".relay-",delete=False) as f:
        f.write(data); f.flush(); os.fsync(f.fileno()); os.fchmod(f.fileno(),mode)
    os.replace(f.name,path)
    return True

def fetch(url,digest):
    r=subprocess.run(["curl","--fail","--silent","--show-error","--proto","=https","--proto-redir","=https","--location","--connect-timeout","8","--max-time","150",url],stdout=subprocess.PIPE,check=True)
    if hashlib.sha256(r.stdout).hexdigest()!=digest:
        raise ValueError("官方发布包摘要与固定版本不符，停止安装")
    return r.stdout

def programs():
    arch={"x86_64":"amd64","aarch64":"arm64","arm64":"arm64"}.get(platform.machine())
    if not arch: raise ValueError("目前支持 Linux amd64/arm64")
    c=LIB/("caddy-"+CADDY)
    if not c.exists():
        buf=fetch(f"https://github.com/caddyserver/caddy/releases/download/v{CADDY}/caddy_{CADDY}_linux_{arch}.tar.gz",HASHES[arch])
        with tarfile.open(fileobj=io.BytesIO(buf)) as a:
            member=next(m for m in a if m.isfile() and m.name.lstrip("./")=="caddy")
            write(c,a.extractfile(member).read(),0o755)
    ac=LIB/"acme.sh"
    if not ac.exists() or hashlib.sha256(ac.read_bytes()).hexdigest()!=ACME_SHA:
        existing=Path("/root/.acme.sh/acme.sh")
        data=existing.read_bytes() if existing.is_file() else b""
        if hashlib.sha256(data).hexdigest()!=ACME_SHA:
            data=fetch("https://raw.githubusercontent.com/acmesh-official/acme.sh/3.1.7/acme.sh",ACME_SHA)
        write(ac,data,0o700)
    return c,ac

def install(host):
    if os.geteuid()!=0 or platform.system()!="Linux":raise ValueError("请在 Linux 节点上以 root 运行")
    site=(HERE/"site/index.html").read_bytes()
    for p,mode in ((ETC,0o755),(DATA,0o700),(LIB,0o755),(ASSETS,0o755)):
        p.mkdir(parents=True,exist_ok=True,mode=mode)
        if p.is_symlink() or p.stat().st_uid != 0:
            raise ValueError("节点服务目录不属于 root："+str(p))
        p.chmod(mode)  # root 的 umask 常为 077；动态用户需要穿越公开资源目录
    lock=(DATA/"provision.lock").open("a"); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    marker=ETC/"hostname"
    if marker.exists() and marker.read_text().strip()!=host:raise ValueError("此实例已有不同的节点域名，未覆盖")
    if not marker.exists() and any(x.exists() for x in (UNITS/WEB,ETC/"Caddyfile",ASSETS/"index.html")):
        raise ValueError("已有同名但非本脚本管理的网页，未覆盖")
    managed=marker.exists()
    if not managed or subprocess.run(["systemctl","is-active","--quiet",WEB]).returncode!=0:
        with socket.socket() as sk:
            try: sk.bind(("127.0.0.1",PORT))
            except OSError: raise ValueError("本机 8443 已被其他服务占用")
    write(marker,host+"\n",0o644)
    c,ac=programs()
    pem, key = DATA/"fullchain.pem",DATA/"key.pem"
    conf=ETC/"Caddyfile"
    config="""{
    admin off
    auto_https off
    persist_config off
    servers {
        protocols h1 h2
    }
}
https://HOST:8443 {
    bind 127.0.0.1
    tls {$CREDENTIALS_DIRECTORY}/fullchain.pem {$CREDENTIALS_DIRECTORY}/key.pem {
        protocols tls1.3
    }
    root * /usr/local/share/relay-local-web
    file_server
}
""".replace("HOST",host)
    cchange=write(conf,config,0o644)
    write(ASSETS/"index.html",site,0o644)
    wchange=write(UNITS/WEB,f"""[Unit]
Description=Relay local-only HTTPS static page
After=network.target
[Service]
DynamicUser=yes
RuntimeDirectory=relay-local-web
Environment=XDG_CONFIG_HOME=/run/relay-local-web/config
Environment=XDG_DATA_HOME=/run/relay-local-web/data
LoadCredential=fullchain.pem:{pem}
LoadCredential=key.pem:{key}
ExecStart={c} run --config {conf} --adapter caddyfile
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_UNIX AF_INET6 AF_NETLINK
IPAddressDeny=any
IPAddressAllow=localhost
[Install]
WantedBy=multi-user.target
""",0o644)
    args=[str(ac),"--home",str(DATA/"acme"),"--config-home",str(DATA/"acme"),"--cert-home",str(DATA/"certs"),"--server","letsencrypt"]
    for f in (DATA/"acme",DATA/"certs"):f.mkdir(exist_ok=True,mode=0o700)
    timer="relay-local-cert-renew"
    # --cron only reads this dedicated ACME home; it cannot renew unrelated certificates.
    write(UNITS/(timer+".service"),"[Unit]\nDescription=Renew relay HTTPS certificate\nAfter=network-online.target\n[Service]\nType=oneshot\nUMask=0077\nExecStart="+" ".join(args+["--cron","--no-color"])+"\n",0o644)
    write(UNITS/(timer+".timer"),"[Unit]\nDescription=Check relay certificate daily\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=3h\nPersistent=true\n[Install]\nWantedBy=timers.target\n",0o644)
    subprocess.run(["systemctl","daemon-reload"],check=True)
    issued=(DATA/"certs"/(host+"_ecc")/"fullchain.cer")
    def valid(f,days):
        if not f.is_file(): return False
        return subprocess.run(["openssl","x509","-in",str(f),"-noout","-checkend",str(86400*days)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
    def acme(a,allow_skip=False):
        r=subprocess.run(args+a+["--no-color"],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        if r.returncode and not (allow_skip and r.returncode==2 and valid(issued,1)):
            # Logs may include ACME account metadata; keep them root-only.
            write(DATA/"last-acme-error.log",r.stdout,0o600)
            raise ValueError(f"证书操作失败（{r.returncode}），详见 {DATA}/last-acme-error.log；未更改任何 Xray 入站")
    if not valid(issued,30):
        if subprocess.run(["ss","-H","-ltn","sport = :80"],capture_output=True,text=True,check=True).stdout.strip():
            raise ValueError("HTTP-01 验证需要 TCP 80，目前已被占用；不会接管已有网站")
        print("向 Let's Encrypt 申请证书（仅临时监听 TCP 80）…",flush=True)
        acme(["--issue","--standalone","-d",host,"--keylength","ec-256"],True)
    if not (valid(pem,30) and key.exists() and issued.exists() and pem.read_bytes()==issued.read_bytes()):
        acme(["--install-cert","--ecc","-d",host,"--key-file",str(key),"--fullchain-file",str(pem),"--reloadcmd","systemctl try-restart relay-local-web.service"])
        key.chmod(0o600)
    op="restart" if (cchange or wchange) else "start"
    subprocess.run(["systemctl","enable","--quiet",WEB,timer+".timer"],check=True)
    subprocess.run(["systemctl","start",timer+".timer"],check=True)
    subprocess.run(["systemctl",op,WEB],check=True)
    import time
    for i in range(25):
        try:
            ctx=ssl.create_default_context();ctx.minimum_version=ssl.TLSVersion.TLSv1_3;ctx.set_alpn_protocols(["h2"])
            with socket.create_connection(("127.0.0.1",PORT),2) as raw, ctx.wrap_socket(raw,server_hostname=host) as tls:
                if tls.selected_alpn_protocol()!="h2":raise ValueError("本机网页未协商 HTTP/2")
            break
        except OSError:
            if i==24:raise
            time.sleep(.2)
    page=subprocess.check_output(["curl","--noproxy","*","--fail","--silent","--show-error","--max-time","5","--resolve",f"{host}:{PORT}:127.0.0.1",f"https://{host}:{PORT}/"])
    if page!=site: raise ValueError("静态页面内容与模板不一致")
    print(f"通过：{host} 的公信证书、TLS 1.3/h2、本机页面和续期定时器。")

if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("host");a=ap.parse_args()
    try:install(hostname(a.host))
    except (OSError,ValueError,subprocess.SubprocessError) as e:sys.exit(str(e))
