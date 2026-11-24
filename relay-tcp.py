#!/usr/bin/env python3
"""只热替换此脚本管理的 VLESS TCP 入站；不调用面板的全局重启接口。"""
import argparse
import copy
import datetime
import fcntl
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from relay_hot import DIR, DB, CONF, LOCK, call, atom, encode, owned_tags, api_tags, sockets

TARGET="127.0.0.1:8443"
WEB=Path("/usr/local/share/relay-local-web/index.html")
TCP_LINKS=DIR/"relay-tcp-links.txt"

def snapshot(port):return sockets(port,udp=True)

def local_page(domain,addr,port):
    body=call(["curl","--noproxy","*","-fsS","--proto","=https","--max-time","6","--resolve",
               f"{domain}:{port}:{'['+addr+']' if ':' in addr else addr}",f"https://{domain}:{port}/"])
    if body!=WEB.read_bytes():raise ValueError("未得到本机静态页面："+addr)

def safe_stream(stream,domain):
    s=copy.deepcopy(stream)
    if s.get("security")!="reality" or s.get("network") not in ("tcp","raw"):
        raise ValueError("仅支持现有 REALITY/TCP 入站")
    r=s["realitySettings"]
    r.pop("dest",None);r["target"]=TARGET;r["serverNames"]=[domain]
    if isinstance(r.get("settings"),dict) and r["settings"].get("serverName"):r["settings"]["serverName"]=domain
    return s


def stage_tcp(xb,entry,host):
    e=copy.deepcopy(entry)
    with socket.socket() as tmp:tmp.bind(("127.0.0.1",0));port=tmp.getsockname()[1]
    e.update(tag="relay-local-stage",listen="127.0.0.1",port=port)
    # This instance may only serve the local web page. Even valid clients cannot use it as an exit.
    cfg={"log":{"loglevel":"warning"},"inbounds":[e],"outbounds":[{"tag":"discard","protocol":"blackhole"}]}
    p=subprocess.Popen([str(xb),"run","-format","json","-c","stdin:"],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        p.stdin.write(encode(cfg));p.stdin.close()
        for i in range(30):
            if p.poll() is not None:raise ValueError("本机预演内核启动失败")
            try:
                with socket.create_connection(("127.0.0.1",port),.15):break
            except OSError:time.sleep(.08)
        local_page(host,"127.0.0.1",port)
    finally:
        if p.poll() is None:p.terminate()
        try:p.wait(timeout=4)
        except subprocess.TimeoutExpired:p.kill();p.wait()

def migrate(host,port,apply,public4=None):
    if Path("/etc/relay-local-web/hostname").read_text().strip()!=host:raise ValueError("域名与本机静态站不一致")
    local_page(host,"127.0.0.1",8443)
    if not (1<=port<=65535):raise ValueError("端口错误")
    db=sqlite3.connect(DB.as_uri()+"?mode="+("rw" if apply else "ro"),uri=True,timeout=4)
    db.row_factory=sqlite3.Row
    before=CONF.read_bytes(); cfg=json.loads(before);tags=owned_tags()
    api=next((f"{v['listen']}:{v['port']}" for v in cfg["inbounds"] if v.get("tag")=="api"),"")
    if "HandlerService" not in cfg.get("api",{}).get("services",[]) or not re.fullmatch(r'127\.0\.0\.1:\d+',api):
        raise ValueError("现有核心没有本机热更新 API，不会尝试全局重启")
    bins=[f for f in CONF.parent.glob("xray-linux-*") if f.is_file() and os.access(f,os.X_OK)]
    if len(bins)!=1:raise ValueError("无法唯一定位正在使用的 Xray 安装")
    xb=bins[0]; present=api_tags(xb,api); work=[];links=[];existing_links=DIR/"relay-links.txt"
    combined=existing_links.read_bytes() if existing_links.exists() else None
    names=set();clientmap={};safe_before=set()
    ctx=ssl.create_default_context();ctx.minimum_version=ssl.TLSVersion.TLSv1_3;ctx.set_alpn_protocols(["h2"])
    with socket.create_connection(("127.0.0.1",8443),4) as raw,ctx.wrap_socket(raw,server_hostname=host) as tls:
        if tls.selected_alpn_protocol()!="h2":raise ValueError("本机 TLS/h2 校验未通过")
    sels={i["tag"]:i for i in cfg["inbounds"] if i.get("tag") in tags and i.get("protocol")=="vless" and i.get("port")==port}
    for r in db.execute("SELECT id,tag,enable,protocol,port,listen,stream_settings,settings FROM inbounds ORDER BY id"):
        tag=r["tag"]
        if tag not in tags or r["protocol"]!="vless" or r["port"]!=port or not r["enable"]:continue
        if tag not in sels:raise ValueError("面板与运行文件的活动入站不一致："+tag)
        e=sels[tag]; addr=ipaddress.ip_address(r["listen"])
        if addr.is_unspecified or addr.is_loopback or e["listen"]!=str(addr):raise ValueError("仅接管绑定具体接口的独立 TCP 标签")
        if addr.version in names:raise ValueError("同一地址族有多个候选入站，不自动猜测")
        names.add(addr.version)
        old_dbstream=json.loads(r["stream_settings"])
        dbstream=safe_stream(old_dbstream,host)
        old_stream=e["streamSettings"]
        e["streamSettings"]=safe_stream(old_stream,host)
        if old_dbstream==dbstream and old_stream==e["streamSettings"]:safe_before.add(tag)
        if e.get("settings",{}).get("fallbacks"):raise ValueError("此 VLESS 入站另有 fallback，需单独审核")
        if json.loads(r["settings"]).get("fallbacks"):raise ValueError("面板中另有 fallback")
        if not e.get("settings",{}).get("clients"):raise ValueError("没有可用的已认证客户端")
        rs=dbstream["realitySettings"];pub=rs.get("settings",{}).get("publicKey")
        if not pub or not rs.get("shortIds"):raise ValueError("缺少分享链接所需的公钥或标识，未修改")
        label=f"{host}-TCP-VLESS-IPv{addr.version}"
        public=ipaddress.IPv4Address(public4) if addr.version==4 and public4 else addr
        if not public.is_global:raise ValueError("NAT 机器需要提供公网地址 --public-v4")
        for c in e["settings"]["clients"]:
            q=dict(encryption=c.get("encryption","none"),security="reality",type="tcp",sni=host,fp="chrome",pbk=pub,sid=rs["shortIds"][0],spx="/")
            if c.get("flow"):q["flow"]=c["flow"]
            uri=f"vless://{c['id']}@{('['+str(public)+']') if addr.version==6 else public}:{port}?{urllib.parse.urlencode(q)}#{urllib.parse.quote(label)}"
            links.append(uri);clientmap[c["id"]]=uri
        work.append((tag,r["id"],r["stream_settings"],json.dumps(dbstream,ensure_ascii=False),e))
    if not work or set(sels)!={x[0] for x in work}:raise ValueError("数据库与运行文件没有匹配到完整的 TCP 入站；不会改动")
    if any(x[0] not in present and x[0] not in safe_before for x in work):
        raise ValueError("缺少运行中的旧标签且未找到已持久化的安全配置，不自动接管")
    call([str(xb),"run","-test","-format","json","-c","stdin:"],body=encode(cfg))
    for tag,_,__,___,e in work:
        stage_tcp(xb,e,host);print(f"{tag}：通过本机预演，未认证访问只到 {TARGET}")
    if not apply:print("预览完成；未修改任何入站。");return
    known_good=set()
    for tag,_,__,___,e in work:
        if tag in safe_before and tag in present:
            try:local_page(host,e["listen"],port);known_good.add(tag)
            except (ValueError,RuntimeError,subprocess.SubprocessError):pass
    links_data=("\n".join(links)+"\n").encode()
    if len(known_good)==len(work) and TCP_LINKS.exists() and TCP_LINKS.read_bytes()==links_data:
        print("两端配置与实际页面均已匹配；未重载任何 TCP 或 UDP 标签。" if len(work)==2 else "配置与页面均已匹配；未重载。")
        return
    base=Path("/root")/("relay-tcp-backup-"+datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    base.mkdir(mode=0o700)
    with sqlite3.connect(base/"x-ui.db") as dst:db.backup(dst)
    atom(base/"config.before.json",before,0o600)
    for path in (existing_links,TCP_LINKS):
        if path.exists():shutil.copy2(path,base/(path.name+".before"))
    print("事务备份："+str(base),flush=True)
    udp=snapshot(port)
    def udp_db():
        return [tuple(r) for r in db.execute("SELECT id,enable,listen,port,protocol,settings,stream_settings,sniffing FROM inbounds WHERE protocol='hysteria' ORDER BY id")]
    frozen=udp_db()
    if frozen and not udp:raise ValueError("无法确认当前 UDP 的进程与套接字，停止")
    if api_tags(xb,api)!=present or CONF.read_bytes()!=before:raise ValueError("预览后运行状态有变化，停止")
    # Durable source first. Update only these two stream_settings columns, never replace the DB.
    with db:
        db.execute("BEGIN IMMEDIATE")
        for tag,id,old,new,e in work:
            if db.execute("UPDATE inbounds SET stream_settings=? WHERE id=? AND stream_settings=? AND enable=1",(new,id,old)).rowcount!=1:raise ValueError("配置被并发修改："+tag)
    atom(CONF,encode(cfg),0o600)
    for n,(tag,id,old,new,e) in enumerate(work):
        if tag in known_good:continue
        spec=base/f"activate-{n}.json";atom(spec,encode({"inbounds":[e]}),0o600)
        if tag in present:call([str(xb),"api","rmi","--server="+api,tag])
        if tag in api_tags(xb,api):raise RuntimeError("旧 TCP 监听未释放："+tag)
        call([str(xb),"api","adi","--server="+api,str(spec)])
        if tag not in api_tags(xb,api):raise RuntimeError("TCP 未能热添加："+tag)
        local_page(host,e["listen"],port)
        if snapshot(port)!=udp or udp_db()!=frozen:raise RuntimeError("UDP 状态变化，停止后续步骤")
        print(tag+" 已热替换；UDP 进程、套接字及入站配置保持原状",flush=True)
    atom(TCP_LINKS,links_data,0o600)
    if combined is not None:
        def replace(m):
            uri=m[0].decode();u=urllib.parse.urlsplit(uri)
            return clientmap.get(u.username,uri).encode() if u.port==port else m[0]
        updated=re.sub(rb'vless://[^\s]+',replace,combined)
        if updated!=combined:atom(existing_links,updated,0o600)
    print("TCP 导入文件："+str(TCP_LINKS))

if __name__=="__main__":
    a=argparse.ArgumentParser(description=__doc__,allow_abbrev=False);a.add_argument("host");a.add_argument("--port",type=int,default=443);a.add_argument("--apply",action="store_true");a.add_argument("--public-v4");v=a.parse_args()
    os.umask(0o077)
    try:
        if os.geteuid()!=0:raise ValueError("请在节点上以 root 运行")
        with LOCK.open("a") as lk:
            fcntl.flock(lk,fcntl.LOCK_EX|fcntl.LOCK_NB);migrate(v.host,v.port,v.apply,v.public_v4)
    except (OSError,ValueError,KeyError,RuntimeError,sqlite3.Error,subprocess.SubprocessError) as e:sys.exit(str(e))
