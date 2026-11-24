#!/usr/bin/env python3
"""把旧 HY2 逐个替换为自有域名、本机 404 的新入口。拒绝触发共享核心重启。"""
import argparse
import base64
import copy
import datetime
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse as url
from pathlib import Path

from relay_hot import DIR, DB, CONF, LOCK, api_tags, atom, call, encode, owned_tags, sockets

SAFE={"type":"string","statusCode":404,"content":"Not Found"}
LINKS=DIR/"relay-udp-links.txt"
ALL=DIR/"relay-links.txt"
CERTROOT=DIR/"relay-udp-identity"
HTML=Path("/usr/local/share/relay-local-web/index.html")

def clone(obj):return copy.deepcopy(obj)

def cert_files(host, scratch, apply):
    home=CERTROOT/host
    if CERTROOT.is_symlink() or home.is_symlink():raise ValueError("证书目录异常")
    if not apply and not (home/"node.crt").exists() and not (home/"node.key").exists():home=scratch/"identity"
    home.mkdir(mode=0o700,parents=True,exist_ok=True)
    if home.stat().st_uid or home.stat().st_mode&0o077:raise ValueError("证书目录必须只允许 root 访问")
    cert,key=home/"node.crt",home/"node.key"
    if cert.is_symlink() or key.is_symlink() or cert.exists()!=key.exists():raise ValueError("证书文件不完整或为链接，未替换")
    if not cert.exists():
        call(["openssl","req","-x509","-newkey","ec","-pkeyopt","ec_paramgen_curve:prime256v1","-nodes","-days","3650","-subj","/CN="+host,"-addext","subjectAltName=DNS:"+host,"-keyout",str(key),"-out",str(cert)])
        key.chmod(0o600);cert.chmod(0o600)
    san=call(["openssl","x509","-in",str(cert),"-noout","-ext","subjectAltName"]).decode()
    if san.strip().splitlines()[-1].strip()!="DNS:"+host:raise ValueError("已有证书与节点域名不符")
    if call(["openssl","x509","-in",str(cert),"-noout","-pubkey"])!=call(["openssl","pkey","-in",str(key),"-pubout"]):raise ValueError("证书与私钥不匹配")
    call(["openssl","x509","-in",str(cert),"-noout","-checkend","86400"])
    pin=hashlib.sha256(call(["openssl","x509","-in",str(cert),"-outform","der"])).hexdigest()
    return str(cert),str(key),pin

def mask_of(s):
    masks=s.get("finalmask",{}).get("udp",[])
    if len(masks)!=1 or masks[0].get("type")!="salamander" or not masks[0].get("settings",{}).get("password"):
        raise ValueError("只接管有独立传输口令的本脚本 HY2 入站")
    return masks[0]["settings"]["password"]

def secured(s,host,identity,mask):
    cert,key,pin=identity
    if s.get("network")!="hysteria" or s.get("security")!="tls" or s.get("hysteriaSettings",{}).get("version")!=2:
        raise ValueError("仅支持 Hysteria2 / TLS")
    out=clone(s);mask_of(out)
    tls=out["tlsSettings"];cs=tls.get("certificates",[])
    if len(cs)!=1 or not all(cs[0].get(k) for k in ("certificateFile","keyFile")) or "h3" not in tls.get("alpn",[]):
        raise ValueError("证书或 HTTP/3 结构与脚本模板不符")
    tls["serverName"]=host;cs[0].update(certificateFile=cert,keyFile=key)
    if isinstance(tls.get("settings"),dict):tls["settings"]["pinnedPeerCertSha256"]=[pin]
    out["hysteriaSettings"]["masquerade"]=dict(SAFE)
    out["finalmask"]["udp"][0]["settings"]["password"]=mask
    return out

def live_entries(xb,api):
    q=json.loads(call([str(xb),"api","lsi","--server="+api]))
    return {i["tag"]:i for i in q.get("inbounds",[])}

def live_safe(entry,host,stream):
    s=entry.get("receiverSettings",{}).get("streamSettings",{})
    tls=[v for v in s.get("securitySettings",[]) if v.get("serverName")==host]
    if len(tls)!=1 or len(tls[0].get("certificate",[]))!=1 or len(s.get("udpmasks",[]))!=1:return False
    crt=tls[0]["certificate"][0]; expected=stream["tlsSettings"]["certificates"][0]["certificateFile"]
    if crt.get("certificatePath")!=expected:return False
    cert_data=crt.get("certificate")
    if not isinstance(cert_data,list) or bytes(cert_data).strip()!=Path(expected).read_bytes().strip():return False
    mask=mask_of(stream)
    if s["udpmasks"][0].get("password") not in (mask,base64.b64encode(mask.encode()).decode()):return False
    ts=[t.get("settings",{}) for t in s.get("transportSettings",[]) if "masqType" in t.get("settings",{})]
    return len(ts)==1 and ts[0].get("masqType")=="string" and ts[0].get("masqString")=="Not Found" and ts[0].get("masqStringStatusCode")==404 and not ts[0].get("masqUrl")

def uri_for(e,host,pin,auth,public4=None):
    a=ipaddress.ip_address(e["listen"]);dst=ipaddress.IPv4Address(public4) if a.version==4 and public4 else a
    if not dst.is_global:raise ValueError("NAT 节点需 --public-v4 指定公网地址")
    q={"sni":host,"alpn":"h3","insecure":"0","pinSHA256":pin,"obfs":"salamander","obfs-password":mask_of(e["streamSettings"])}
    address=f"[{dst}]" if dst.version==6 else str(dst)
    return f"hysteria2://{url.quote(auth,safe='')}@{address}:{e['port']}/?{url.urlencode(q)}#{host}-UDP-HY2-IPv{a.version}"

def udp_users(e):
    users=e.get("settings",{}).get("clients",[])
    if not users or any(not x.get("auth") for x in users):raise ValueError("没有可核验的 HY2 认证账户")
    return [x["auth"] for x in users]

def preflight(xb,entry,host,pin,public4):
    # Both temporary listeners are loopback-only. After authentication the sole permitted destination
    # is this node's existing public static HTTPS page; Xray's private-address protections stay enabled.
    pw=secrets.token_hex(12); marker=HTML.read_bytes()
    addr=ipaddress.ip_address(entry["listen"])
    dst=ipaddress.IPv4Address(public4) if addr.version==4 and public4 else addr
    if not dst.is_global:raise ValueError("本机预演需本节点的公网地址；NAT 机器请指定 --public-v4")
    site_port=entry["port"]
    resolver=f"{host}:{site_port}:"+("["+str(dst)+"]" if dst.version==6 else str(dst))
    site=f"https://{host}:{site_port}/"
    procs=[]
    trace=tempfile.TemporaryFile()
    try:
        original=entry["streamSettings"];src=clone(entry);v6=ipaddress.ip_address(src["listen"]).version==6
        loop="::1" if v6 else "127.0.0.1"
        with socket.socket(socket.AF_INET6 if v6 else socket.AF_INET,socket.SOCK_DGRAM) as u:u.bind((loop,0));uport=u.getsockname()[1]
        with socket.socket() as t:t.bind(("127.0.0.1",0));sport=t.getsockname()[1]
        src.update(tag="test-server",listen=loop,port=uport)
        hm={"network":"hysteria","security":"tls","tlsSettings":{"serverName":host,"alpn":["h3"],"pinnedPeerCertSha256":pin},
            "hysteriaSettings":{"version":2,"auth":udp_users(src)[0]},"finalmask":clone(original["finalmask"])}
        server={"log":{"loglevel":"info"},"inbounds":[src],
                "outbounds":[{"tag":"drop","protocol":"blackhole"},{"tag":"only-fixture","protocol":"freedom","settings":{}}],
                "routing":{"rules":[{"type":"field","inboundTag":["test-server"],"ip":[f"{dst}/{dst.max_prefixlen}"],"port":str(site_port),"network":"tcp","outboundTag":"only-fixture"}]}}
        client={"log":{"loglevel":"info"},"inbounds":[{"tag":"test-socks","listen":"127.0.0.1","port":sport,"protocol":"socks","settings":{"auth":"password","accounts":[{"user":"diag","pass":pw}],"udp":False}}],
                "outbounds":[{"tag":"dial-own","protocol":"hysteria","settings":{"version":2,"address":loop,"port":uport},"streamSettings":hm}]}
        if call(["curl","--noproxy","*","-fsS","--max-time","4","--resolve",resolver,site])!=marker:raise RuntimeError("本节点的 HTTPS 页面或证书校验未通过")
        for cfg in (server,client):
            proc=subprocess.Popen([str(xb),"run","-format","json","-c","stdin:"],stdin=subprocess.PIPE,stdout=trace,stderr=subprocess.STDOUT)
            procs.append(proc);proc.stdin.write(encode(cfg));proc.stdin.close()
        for _ in range(35):
            if any(p.poll() is not None for p in procs):raise RuntimeError("本机认证预演的临时核心未启动")
            try:
                with socket.create_connection(("127.0.0.1",sport),.2):break
            except OSError:time.sleep(.07)
        result=call(["curl","-fsS","--max-time","8","--noproxy","","--proxy",f"socks5://127.0.0.1:{sport}","--proxy-user","diag:"+pw,"--resolve",resolver,site],timeout=11)
        if result!=marker:raise RuntimeError("本机新证书与新凭据的端到端认证预演失败")
    except (RuntimeError,subprocess.SubprocessError):
        trace.seek(0);log=trace.read(70000).decode(errors="replace")
        for value in [pw,*udp_users(src),mask_of(src["streamSettings"])]:log=log.replace(value,"[已隐藏]")
        lines=[re.sub(r'[A-Za-z0-9_+/-]{37,}',"[已隐藏]",l)[:470] for l in log.splitlines() if re.search("outbound|inbound|detour|accepted|handshake|certificate|timeout|failed|rejected|dialing|connection opened|freedom|hysteria",l,re.I)]
        if lines:print("本机预演诊断：","\n".join(lines[-15:]),file=sys.stderr)
        raise
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:proc.terminate()
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        trace.close()

def ping_tcp(sock,host):
    # Same TLS socket before and after the UDP hot swap. Never reconnect silently.
    sock.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n".encode())
    res=http.client.HTTPResponse(sock);res.begin();body=res.read()
    if res.status!=200 or body!=HTML.read_bytes() or res.will_close:raise ValueError("原 TCP 长连接未返回预期内容")
    res.close()

def rewrite_combined(before, replacements,port):
    if before is None:return None
    def match(m):
        raw=m[0];u=url.urlsplit(raw.decode())
        if u.port!=port:return raw
        try:addr=ipaddress.ip_address(u.hostname)
        except ValueError:return raw
        auth=url.unquote(u.username or "")+(":"+url.unquote(u.password) if u.password else "")
        return replacements.get((str(addr),u.port,auth),raw)
    return re.sub(rb'(?:hy2|hysteria2)://[^\s]+',match,before)

def migrate(host,port,family,apply,public4,scratch):
    if len(host)>253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",l) for l in host.split(".")) or Path("/etc/relay-local-web/hostname").read_text().strip()!=host:
        raise ValueError("请使用已完成 TCP 迁移的自有域名")
    if not 1<=port<=65535:raise ValueError("端口错误")
    owned=owned_tags();before=CONF.read_bytes();cfg=json.loads(before)
    bins=[f for f in CONF.parent.glob("xray-linux-*") if f.is_file() and os.access(f,os.X_OK)]
    if len(bins)!=1:raise ValueError("不能唯一定位核心")
    xb=bins[0]
    api=next((f"{i['listen']}:{i['port']}" for i in cfg["inbounds"] if i.get("tag")=="api"),"")
    if "HandlerService" not in cfg.get("api",{}).get("services",[]) or not re.fullmatch(r"127\.0\.0\.1:\d+",api):raise ValueError("没有本机热更新接口；不会全局重启")
    prev_live=live_entries(xb,api);socks=sockets(port)
    tcp_entries=[i for i in cfg["inbounds"] if i.get("tag") in owned and i.get("protocol")=="vless" and i.get("port")==port]
    if not socks or not tcp_entries or any(i["tag"] not in prev_live or i["streamSettings"]["realitySettings"].get("target")!="127.0.0.1:8443" or i["streamSettings"]["realitySettings"].get("serverNames")!=[host] for i in tcp_entries):
        raise ValueError("没有确认可作为备用的安全 TCP 入口；不会改动 UDP")
    db=sqlite3.connect(DB.as_uri()+"?mode="+("rw" if apply else "ro"),uri=True,timeout=5);db.row_factory=sqlite3.Row
    def frozen_tcp():
        return [tuple(r) for r in db.execute("SELECT id,tag,enable,listen,port,settings,stream_settings,remark,sniffing FROM inbounds WHERE protocol='vless' ORDER BY id")]
    frozen=frozen_tcp();tcp_file=DIR/"relay-tcp-links.txt";tcp_links=tcp_file.read_bytes()
    cert,key,pin=cert_files(host,scratch,apply);ident=(cert,key,pin)
    canon={i["tag"]:i for i in cfg["inbounds"] if i.get("tag") in owned and i.get("protocol")=="hysteria" and i.get("port")==port}
    records=[];seen=set()
    for row in db.execute("SELECT id,tag,enable,listen,port,protocol,settings,stream_settings,remark FROM inbounds WHERE enable=1 AND protocol='hysteria' AND port=? ORDER BY id",(port,)):
        tag=row["tag"]
        if tag not in owned:continue
        if tag not in canon:raise ValueError("数据库与生成文件不匹配："+tag)
        entry=canon[tag];addr=ipaddress.ip_address(row["listen"])
        if addr.is_unspecified or addr.is_loopback or entry["listen"]!=str(addr) or addr.version in seen:raise ValueError("仅接管每个地址族单独的、已知的 UDP 入口")
        seen.add(addr.version)
        if sorted(udp_users(entry))!=sorted(c["auth"] for c in json.loads(row["settings"])["clients"] if c.get("enable",True)):
            raise ValueError("账户配置不一致，未触碰账户或入站")
        old=json.loads(row["stream_settings"]);es=entry["streamSettings"];om=mask_of(old)
        if om!=mask_of(es) or old["hysteriaSettings"].get("masquerade")!=es["hysteriaSettings"].get("masquerade"):raise ValueError("持久配置与运行文件不一致："+tag)
        same=old==secured(old,host,ident,om) and es==secured(es,host,ident,om)
        records.append((row,entry,addr.version,same))
    if set(canon)!={r[0]["tag"] for r in records} or not records or family and family not in seen:raise ValueError("目标地址族没有完整的 UDP 入站")
    selected=sorted((r for r in records if not family or family==r[2]),key=lambda r:-r[2])
    combined=ALL.read_bytes() if ALL.exists() else None
    for row,e,ver,same in selected:
        tag=row["tag"];live=live_entries(xb,api)
        if tag not in live and not same:raise ValueError("未知的离线旧入口，未接管："+tag)
        old=json.loads(row["stream_settings"]);mask=mask_of(old) if same else secrets.token_hex(24)
        dbstream=secured(old,host,ident,mask);new=clone(e);new["streamSettings"]=secured(e["streamSettings"],host,ident,mask)
        label=f"{host}-UDP-HY2-IPv{ver}";newcfg=clone(cfg)
        newcfg["inbounds"]=[new if i.get("tag")==tag else i for i in newcfg["inbounds"]]
        call([str(xb),"run","-test","-format","json","-c","stdin:"],body=encode(newcfg))
        already=same and tag in live and live_safe(live[tag],host,new["streamSettings"])
        if not already:preflight(xb,new,host,pin,public4)
        print(f"IPv{ver}：新 HY2 认证预演通过，目标域名 {host}，未认证固定为本机 404。" if not already else f"IPv{ver}：配置和运行内核都已匹配。",flush=True)
        if not apply:continue
        if before!=CONF.read_bytes() or frozen_tcp()!=frozen or sockets(port)!=socks or tcp_file.read_bytes()!=tcp_links:raise ValueError("TCP 或磁盘配置发生并发变化，已停止")
        if already and row["remark"]==label:continue
        addr=next((i["listen"] for i in tcp_entries if ":" not in i["listen"]),tcp_entries[0]["listen"])
        endpoint=(f"[{e['listen']}]" if ver==6 else e["listen"])+f":{port}"
        other_udp=[v for v in sockets(port,udp=True) if v[0]!=endpoint]
        ctx=ssl.create_default_context();ctx.set_alpn_protocols(["http/1.1"])
        with socket.create_connection((addr,port),5) as raw,ctx.wrap_socket(raw,server_hostname=host) as guard:
            guard.settimeout(8);ping_tcp(guard,host)
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup=Path("/root")/f"relay-udp-backup-{stamp}-v{ver}";backup.mkdir(mode=0o700)
            with sqlite3.connect(backup/"x-ui.db") as dst:db.backup(dst)
            atom(backup/"config.before.json",before)
            for f in (ALL,LINKS):
                if f.exists():shutil.copy2(f,backup/(f.name+".before"))
            print("可恢复备份："+str(backup),flush=True)
            with db:
                db.execute("BEGIN IMMEDIATE")
                n=db.execute("UPDATE inbounds SET stream_settings=?,remark=? WHERE id=? AND enable=1 AND stream_settings=? AND remark=? AND settings=?",
                             (json.dumps(dbstream,ensure_ascii=False),label,row["id"],row["stream_settings"],row["remark"],row["settings"])).rowcount
                if n!=1:raise ValueError("面板配置已变，未覆盖")
            before=encode(newcfg);atom(CONF,before);cfg=newcfg
            if not already:
                spec=backup/"activate.json";atom(spec,encode({"inbounds":[new]}))
                if tag in live:call([str(xb),"api","rmi","--server="+api,tag])
                if tag in api_tags(xb,api):raise RuntimeError("旧 UDP 未释放："+tag)
                call([str(xb),"api","adi","--server="+api,str(spec)])
            current=live_entries(xb,api)
            if not live_safe(current.get(tag,{}),host,new["streamSettings"]):raise RuntimeError("热加载后未能确认运行内核中的证书、传输口令和本机 404："+tag)
            udp_now=sockets(port,udp=True)
            if other_udp!=[v for v in udp_now if v[0]!=endpoint] or not any(v[0]==endpoint and v[1] in {s[1] for s in socks} for v in udp_now):
                raise RuntimeError("未确认目标 UDP 监听，或另一个 UDP 套接字发生变化")
            if set(current)-{tag}!=set(live)-{tag} or sockets(port)!=socks or frozen_tcp()!=frozen or tcp_file.read_bytes()!=tcp_links:raise RuntimeError("非目标入口发生变化；停止下一步")
            ping_tcp(guard,host)
            print(f"IPv{ver} 热替换成功，原 TCP 长连接仍可用；TCP 监听及链接未变。",flush=True)
    if not apply:print("仅预演，没有替换线上 UDP。");return
    current=live_entries(xb,api);uris=[];mapping={};complete=set()
    for entry in cfg["inbounds"]:
        if entry.get("protocol")!="hysteria" or entry.get("tag") not in canon:continue
        st=entry["streamSettings"]
        if st!=secured(st,host,ident,mask_of(st)) or not live_safe(current.get(entry["tag"],{}),host,st):continue
        complete.add(entry["tag"])
        addr=ipaddress.ip_address(entry["listen"]);pub=public4 if addr.version==4 and public4 else str(addr)
        for user in udp_users(entry):
            uri=uri_for(entry,host,pin,user,public4);uris.append((addr.version,uri));mapping[(pub,port,user)]=uri.encode()
    if not uris:raise ValueError("没有可交付的新链接")
    result=("\n".join(uri for _,uri in sorted(uris))+"\n").encode()
    if not LINKS.exists() or LINKS.read_bytes()!=result:atom(LINKS,result)
    after=rewrite_combined(combined,mapping,port)
    if after is not None and after!=combined:
        if ALL.read_bytes()!=combined:raise ValueError("综合导出同时被更新，未覆盖")
        atom(ALL,after)
    if complete==set(canon):
        env=DIR/"relay.env";old_env=env.read_bytes()
        new_env,n=re.subn(rb"(?m)^SNI=[^\r\n]*",b"SNI="+host.encode("ascii"),old_env)
        if n==1 and old_env!=new_env:
            stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            base=Path("/root")/("relay-state-backup-"+stamp);base.mkdir(mode=0o700)
            atom(base/"relay.env.before",old_env)
            if env.read_bytes()!=old_env:raise ValueError("默认域名元数据被并发修改，未覆盖")
            atom(env,new_env)
    print("已验收的新 UDP 导入文件："+str(LINKS))

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    p.add_argument("host");p.add_argument("--port",type=int,default=443);p.add_argument("--family",type=int,choices=(4,6));p.add_argument("--public-v4");p.add_argument("--apply",action="store_true")
    v=p.parse_args();os.umask(0o077)
    try:
        if os.geteuid()!=0:raise ValueError("请在节点上以 root 运行")
        with LOCK.open("a") as lk:
            fcntl.flock(lk,fcntl.LOCK_EX|fcntl.LOCK_NB)
            scratch=Path(tempfile.mkdtemp(prefix="relay-udp-preflight-",dir="/run"))
            try:migrate(v.host,v.port,v.family,v.apply,v.public_v4,scratch)
            finally:
                retired=Path("/run/relay-retired");retired.mkdir(mode=0o700,exist_ok=True);shutil.move(str(scratch),str(retired/scratch.name))
    except (OSError,ValueError,KeyError,RuntimeError,http.client.HTTPException,sqlite3.Error,subprocess.SubprocessError) as exc:sys.exit(str(exc))
