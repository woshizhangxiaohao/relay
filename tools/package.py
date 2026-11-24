#!/usr/bin/env python3
"""生成可复制到任意节点的完整安装包，不打包在线凭据、配置或导入链接。"""
import argparse
import datetime
import gzip
import hashlib
import io
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES=("README.md","relay.sh","relay-dns.py","relay-web.py","relay-tcp.py","relay-udp.py",
       "relay-panel.py","relay_hot.py","site/index.html","tests/check-templates.py",
       "tests/check-udp-migration.py","tools/package.py")

def package(dest):
    dst=Path(dest).expanduser();dst.mkdir(parents=True,exist_ok=True)
    buf=io.BytesIO()
    with gzip.GzipFile(fileobj=buf,mode="wb",filename="",mtime=0) as gz:
        with tarfile.open(fileobj=gz,mode="w") as tar:
            for name in FILES:
                p=ROOT/name
                if p.is_symlink() or not p.is_file():raise ValueError("缺少正式文件或文件为符号链接："+name)
                b=p.read_bytes();i=tarfile.TarInfo("relay/"+name);i.size=len(b);i.mode=0o755 if name=="relay.sh" else 0o644;i.mtime=0
                tar.addfile(i,io.BytesIO(b))
    data=buf.getvalue();sha=hashlib.sha256(data).hexdigest()
    path=dst/f"relay-{datetime.date.today():%Y%m%d}-{sha[:12]}.tar.gz"
    if path.exists():
        if path.is_symlink() or path.read_bytes()!=data:raise ValueError("已有同名但内容不同的文件")
    else:
        with path.open("xb") as f:f.write(data)
    print(sha);print(path)

if __name__=="__main__":
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument("--dest",default="~/Downloads");package(ap.parse_args().dest)
