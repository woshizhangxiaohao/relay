#!/usr/bin/env python3
"""给现有 3x-ui 添加独立的 HTTPS 管理入口。账号密码始终由面板处理。"""
import argparse
import datetime
import fcntl
import http.client
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import runpy
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from relay_hot import atom

WS=runpy.run_path(str(Path(__file__).resolve().with_name("relay-web.py")))
WEB=WS["WEB"]; CERT=WS["DATA"]; CADDY=WS["LIB"]/("caddy-"+WS["CADDY"])
UNIT="relay-panel-https.service"; UFILE=Path("/etc/systemd/system")/UNIT
BASE=Path("/etc/relay-panel-https"); CONF=BASE/"Caddyfile"; STATE=BASE/"settings.json"
HEADER="# Managed by ununu relay panel HTTPS v1\n"

def run(*args,check=True,env=None):
    return subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=check,timeout=15,env=env)

def node():
    host=WS["hostname"]( (WS["ETC"]/"hostname").read_text().strip() )
    with sqlite3.connect("file:/etc/x-ui/x-ui.db?mode=ro",uri=True,timeout=2) as db:
        d=dict(db.execute("SELECT key,value FROM settings WHERE key IN ('webListen','webPort','webBasePath','webDomain','webCertFile','webKeyFile','twoFactorEnable')"))
        accounts=db.execute("SELECT count(*) FROM users WHERE length(trim(username))>0 AND length(coalesce(password,''))>0").fetchone()[0]
        seed=db.execute("SELECT EXISTS(SELECT 1 FROM settings WHERE key='twoFactorToken' AND length(trim(value))>0)").fetchone()[0]
    path="/"+d.get("webBasePath","").strip("/")+"/"
    if not accounts or not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+",path):
        raise ValueError("需要已配置账号密码的 3x-ui 和非空的独立面板路径")
    port=int(d.get("webPort","0"))
    if not 1<=port<=65535 or d.get("webListen")!="127.0.0.1" or d.get("webCertFile") or d.get("webKeyFile"):
        raise ValueError("只支持监听 127.0.0.1 的本机 HTTP 面板；不会修改原有监听或证书")
    if d.get("webDomain","").strip().lower() not in ("",host):raise ValueError("面板限定域名与节点域名不同，未覆盖")
    return host,path,port,bool(seed and d.get("twoFactorEnable","").lower() in ("true","1"))

def allowed(raw):
    ips=[]
    for value in raw:
        a=ipaddress.ip_address(value)
        if not a.is_global or a.is_multicast or getattr(a,"scope_id",None):raise ValueError("白名单只接受单个公网 IPv4/IPv6 地址")
        ips.append(str(a))
    return sorted(set(ips))

def port_check(p,backend):
    if not 1024<=p<=65535 or p in (backend,8443,62789):raise ValueError("面板公网端口必须独立，默认 9443；不能占用面板、网页或 API 端口")

def caddyfile(host,path,backend,port,ips,loop=False):
    cfg=HEADER+"""{
    admin off
    persist_config off
    auto_https off
    servers {
        protocols h1 h2
        strict_sni_host
    }
}
"""+f"https://{host}:{port} {{\n"
    if loop:cfg+="    bind 127.0.0.1\n"
    cfg+="""    tls {$CREDENTIALS_DIRECTORY}/fullchain.pem {$CREDENTIALS_DIRECTORY}/key.pem {
        protocols tls1.2 tls1.3
    }
    header X-Content-Type-Options nosniff
    @root path /
"""+f"    @panel path {path.rstrip('/')} {path}*\n"
    if ips:cfg+="    @outside not remote_ip "+" ".join(ips)+"\n"
    cfg+="    route {\n"
    if ips:cfg+="        respond @outside 403\n"
    cfg+=f"""        redir @root {path} 302
        reverse_proxy @panel 127.0.0.1:{backend} {{
            header_up -Authorization
            header_up -X-API-Key
            header_up -X-API-Token
        }}
        respond 404
    }}
}}
"""
    return cfg

def unit():
    return HEADER+f"""[Unit]
Description=Relay HTTPS panel (local 3x-ui upstream only)
After=network.target {WEB}
PartOf={WEB}
[Service]
Type=simple
DynamicUser=yes
RuntimeDirectory=relay-panel-https
Environment=XDG_CONFIG_HOME=/run/relay-panel-https/config
Environment=XDG_DATA_HOME=/run/relay-panel-https/data
LoadCredential=fullchain.pem:{CERT}/fullchain.pem
LoadCredential=key.pem:{CERT}/key.pem
ExecStart={CADDY} run --config {CONF} --adapter caddyfile
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
CapabilityBoundingSet=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
[Install]
WantedBy=multi-user.target
"""

def guard_owned():
    marker=BASE/"managed-v1"
    if BASE.is_symlink() or (BASE.exists() and (not marker.is_file() or marker.is_symlink() or marker.read_text()!=HEADER or BASE.stat().st_uid!=0 or BASE.stat().st_mode&0o022 or marker.stat().st_uid!=0 or marker.stat().st_mode&0o022)):
        raise ValueError("同名目录非本工具管理，未覆盖")
    if UFILE.is_symlink() or (UFILE.exists() and (not marker.exists() or not UFILE.read_text().startswith(HEADER))):
        raise ValueError("同名服务不属于本工具，未覆盖")
    if any(f.is_symlink() for f in (CONF,STATE)):raise ValueError("配置异常，停止")
    src=run("systemctl","show",UNIT,"-p","FragmentPath","--value",check=False)
    if src.returncode==0 and src.stdout.strip() and src.stdout.decode().strip()!=str(UFILE):
        raise ValueError("系统已有不同来源的同名服务，未覆盖")

def readstate():
    return json.loads(STATE.read_text()) if STATE.is_file() else {}

def listeners(p):
    text=run("ss","-Hlntp",f"sport = :{p}").stdout.decode()
    return [x for x in text.splitlines() if x.strip()]

def current_pid():
    r=run("systemctl","show","-p","MainPID","--value",UNIT,check=False)
    return int(r.stdout.strip()) if r.returncode==0 and r.stdout.strip().isdigit() else 0

def check_backend(host,path,backend):
    entries=listeners(backend)
    if not entries or any(l.split()[3]!=f"127.0.0.1:{backend}" for l in entries):
        raise ValueError("未确认原面板只在回环地址监听；不会建立公网入口")
    c=http.client.HTTPConnection("127.0.0.1",backend,timeout=4)
    try:
        c.request("GET",path+"panel/inbounds",headers={"Host":host})
        r=c.getresponse()
        if not is_closed(r.status,r.getheader("Location",""),path,host):
            raise ValueError("未登录请求没有被面板拒绝或重定向到登录页；不启用")
    finally:c.close()

def is_closed(status,location,path,host):
    parsed=urlsplit(location)
    return (status in (401,403)) or (status in (301,302,303,307,308) and parsed.path==path and parsed.hostname in (None,host))

class Scripts(HTMLParser):
    def __init__(self):
        super().__init__();self.paths=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="script" and a.get("src"):self.paths.append(a["src"])

def probe(host,port,path,ips):
    # Dial the loopback IP directly while preserving the real TLS name. No proxy or credentials.
    ctx=ssl.create_default_context()
    def get(p):
        c=http.client.HTTPSConnection(host,port,timeout=3,context=ctx)
        try:
            c.sock=ctx.wrap_socket(socket.create_connection(("127.0.0.1",port),timeout=3),server_hostname=host)
            c.request("GET",p);r=c.getresponse()
            return r.status,r.getheader("Location",""),r.read(100000),r.getheader("Content-Type","")
        finally:c.close()
    s,loc,_,_=get("/")
    if ips:
        if s!=403 or get(path)[0]!=403:raise ValueError("不在白名单内的回环客户端未被拒绝")
        return
    if s!=302 or urlsplit(loc).path!=path:raise ValueError("网页入口没有转向实际面板路径")
    s,_,body,_=get(path)
    if s!=200 or not re.search(rb'login|sign in|\xe7\x99\xbb\xe5\xbd\x95',body,re.I):raise ValueError("未看到面板登录页")
    scripts=Scripts();scripts.feed(body.decode(errors="replace"))
    for src in scripts.paths:
        dest=urlsplit(urljoin(path,src))
        if dest.netloc or dest.scheme:continue
        sc,_,_,typ=get(dest.path+("?"+dest.query if dest.query else ""))
        if sc!=200 or "html" in typ.lower():raise ValueError("登录页引用的本地脚本未正确转发")
        break
    s,loc,_,_=get(path+"panel/inbounds")
    if not is_closed(s,loc,path,host) or get("/relay-unknown-resource")[0]!=404:raise ValueError("未登录访问或非面板路径的边界验证失败")

def preview(host,path,backend,port,ips):
    check_backend(host,path,backend)
    if not CADDY.is_file() or not (CERT/"fullchain.pem").is_file() or not (CERT/"key.pem").is_file():
        raise ValueError("请先完成 web 子命令的域名与证书配置")
    local=Path(tempfile.mkdtemp(prefix="relay-panel-preview-",dir="/run"))
    env={**os.environ,"CREDENTIALS_DIRECTORY":str(CERT),"XDG_DATA_HOME":str(local/"data"),"XDG_CONFIG_HOME":str(local/"config")}
    proc=None
    try:
        config=local/"Caddyfile"
        # Validate the exact public configuration without binding its port.
        atom(config,caddyfile(host,path,backend,port,ips).encode())
        run(str(CADDY),"validate","--config",str(config),"--adapter","caddyfile",env=env)
        with socket.socket() as sk:sk.bind(("127.0.0.1",0));testport=sk.getsockname()[1]
        atom(config,caddyfile(host,path,backend,testport,ips,loop=True).encode())
        proc=subprocess.Popen([str(CADDY),"run","--config",str(config),"--adapter","caddyfile"],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for i in range(40):
            if proc.poll() is not None:raise ValueError("本机预演进程未启动")
            try:probe(host,testport,path,ips);break
            except (ConnectionRefusedError,ConnectionResetError):
                if i==39:raise
                time.sleep(.08)
    finally:
        if proc:
            if proc.poll() is None:proc.terminate()
            try:proc.wait(timeout=4)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        retired=Path("/run/relay-retired");retired.mkdir(mode=0o700,exist_ok=True);shutil.move(str(local),str(retired/local.name))

def wait_probe(host,port,path,ips):
    for i in range(40):
        try:probe(host,port,path,ips);return
        except (ConnectionRefusedError,ConnectionResetError):
            if i==39:raise
            time.sleep(.08)

def apply(host,path,backend,port,ips):
    guard_owned();prev=readstate();pid=current_pid()
    occupied=listeners(port)
    if occupied and (not UFILE.exists() or prev.get("port")!=port or not pid or any(f"pid={pid}," not in l for l in occupied)):
        raise ValueError("公网端口已被其他服务占用，未接管")
    newstate={"host":host,"port":port,"allow":ips}
    data={UFILE:unit().encode(),CONF:caddyfile(host,path,backend,port,ips).encode(),STATE:(json.dumps(newstate,indent=2)+"\n").encode()}
    same=all(f.is_file() and f.read_bytes()==v for f,v in data.items())
    if same and occupied and run("systemctl","is-enabled","--quiet",UNIT,check=False).returncode==0:
        wait_probe(host,port,path,ips);print("入口已经匹配，未重载。");return
    BASE.mkdir(mode=0o755,exist_ok=True);BASE.chmod(0o755)
    if not (BASE/"managed-v1").exists():atom(BASE/"managed-v1",HEADER.encode(),0o644)
    backup=CERT/"backups"/("panel-"+datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    for f,v in data.items():
        if f.exists() and f.read_bytes()!=v:
            backup.mkdir(mode=0o700,parents=True,exist_ok=True);shutil.copy2(f,backup/f.name)
    try:
        for f,v in data.items():
            if not f.exists() or f.read_bytes()!=v:atom(f,v,0o600 if f==STATE else 0o644)
        run("systemctl","daemon-reload");run("systemctl","enable","--quiet",UNIT)
        run("systemctl","restart" if pid else "start",UNIT)
        wait_probe(host,port,path,ips)
    except Exception:
        run("systemctl","disable","--now",UNIT,check=False)
        raise RuntimeError("入口未能完成验证，已请求关闭独立的公网面板服务；未操作原面板和代理线路") from None

def main():
    ap=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    ap.add_argument("action",choices=["plan","enable","status","disable"])
    g=ap.add_mutually_exclusive_group()
    g.add_argument("--login",action="store_true",help="允许从任意网络到达原面板登录页")
    g.add_argument("--allow-ip",action="append",default=[],metavar="IP",help="只允许给定的固定公网 IP，可重复")
    ap.add_argument("--port",type=int,default=9443)
    a=ap.parse_args();os.umask(0o077)
    if os.geteuid()!=0:raise ValueError("请在节点上用 root 运行")
    with Path("/run/relay-panel-https.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);guard_owned()
        if a.action=="disable":
            if UFILE.is_file():run("systemctl","disable","--now",UNIT)
            print("独立公网面板入口已关闭；本机面板及所有节点照常运行。");return
        host,path,backend,totp=node()
        if a.action=="status":
            state=readstate()
            if UFILE.exists() and state and current_pid():print(f"已开启：https://{host}:{state['port']}{path}；策略："+("指定来源" if state.get("allow") else "原面板登录"))
            else:print(f"未启用公网面板；原面板 http://127.0.0.1:{backend}{path}")
            print("原面板动态验证码："+("已启用" if totp else "未启用"));return
        if not (a.login or a.allow_ip):raise ValueError("请选择 --login（原面板登录）或 --allow-ip（限制来源）")
        ips=allowed(a.allow_ip);port_check(a.port,backend)
        preview(host,path,backend,a.port,ips)
        print(f"入口：https://{host}:{a.port}{path}；上游固定为 127.0.0.1:{backend}。原面板动态验证码："+("已启用" if totp else "未启用"))
        print("来源："+(", ".join(ips) if ips else "任意网络可到达登录页；沿用本机面板账号和密码"))
        if a.action=="plan":
            print("预演通过；未开放任何公网端口。");return
        apply(host,path,backend,a.port,ips)
        print(f"独立 HTTPS 服务已启用；如有主机防火墙或云安全组，请仅按选定来源放行 TCP {a.port}。")

if __name__=="__main__":
    try:main()
    except (OSError,ValueError,KeyError,sqlite3.Error,http.client.HTTPException,RuntimeError,subprocess.SubprocessError) as e:sys.exit(str(e))
