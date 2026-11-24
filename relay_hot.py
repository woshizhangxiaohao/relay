"""面板入站热更新共用的本机 I/O；不打印含节点凭据的 API 内容。"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

DIR=Path("/etc/x-ui")
DB=DIR/"x-ui.db"
CONF=Path("/usr/local/x-ui/bin/config.json")
LOCK=Path("/run/relay-tcp-migration.lock")  # 与已发布的 TCP 迁移器互斥

def call(args,*,body=None,timeout=10):
    p=subprocess.run(args,input=body,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{Path(args[0]).name} {args[1] if len(args)>1 else ''} 返回 {p.returncode}")
    return p.stdout

def atom(path,data,mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent,prefix=".relay-",delete=False) as f:
        f.write(data);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),mode)
    os.replace(f.name,path)

def encode(x):return json.dumps(x,ensure_ascii=False,indent=2).encode()+b"\n"

def owned_tags():
    m=re.search(r'(?m)^INBOUND_TAGS=(?:"([^"]+)"|([^\s]+))$',(DIR/"relay.env").read_text())
    if not m:raise ValueError("找不到现有 relay 的入站清单；不接管不明入站")
    return set((m[1] or m[2]).split(","))

def api_tags(xb,api):
    r=json.loads(call([str(xb),"api","lsi","--server="+api,"--isOnlyTags=true"]))
    return {x["tag"] for x in r.get("inbounds",[])}

def sockets(port,udp=False):
    args=["ss","-H","-ulnpe" if udp else "-tlnpe",f"sport = :{port}"]
    return sorted((l.split()[3],m[1],m[2]) for l in call(args).decode().splitlines() if (m:=re.search(r'pid=(\d+).*?ino:(\d+)',l)))
