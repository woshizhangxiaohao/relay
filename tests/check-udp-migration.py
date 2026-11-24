#!/usr/bin/env python3
"""验证 UDP 迁移不重写无关链接、不带走外部回落，重跑也不换当前传输口令。仅用合成资料。"""
import json
import runpy
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
m=runpy.run_path(str(ROOT/"relay-udp.py"))
old={"network":"hysteria","security":"tls","tlsSettings":{"serverName":"retired.example.test","alpn":["h3"],"minVersion":"1.3","certificates":[{"certificateFile":"/test/old.pub","keyFile":"/test/old.key"}],"settings":{"pinnedPeerCertSha256":["old"]}},
     "hysteriaSettings":{"version":2,"udpIdleTimeout":60,"masquerade":{"type":"proxy","url":"https://retired.example.test","rewriteHost":True}},
     "finalmask":{"udp":[{"type":"salamander","settings":{"password":"old-mask"}}]}}
saved=json.dumps(old,sort_keys=True);args=("node.example.test",("/test/new.pub","/test/new.key","1"*64),"new-mask")
new=m["secured"](old,*args)
assert json.dumps(old,sort_keys=True)==saved and m["secured"](new,*args)==new
assert new["hysteriaSettings"]=={"version":2,"udpIdleTimeout":60,"masquerade":{"type":"string","statusCode":404,"content":"Not Found"}}
assert new["tlsSettings"]["serverName"]=="node.example.test" and new["tlsSettings"]["certificates"][0]["keyFile"]=="/test/new.key"
assert new["tlsSettings"]["settings"]["pinnedPeerCertSha256"]==["1"*64] and m["mask_of"](new)=="new-mask"
tcp=b"[VLESS-v4] vless://test@192.0.2.14:443/#keep\n"
v4=b"[HY2-v4] hysteria2://demo@192.0.2.14:443/?sni=legacy#keep-v4\n"
other=b"[other] hysteria2://different@outside.example.test:443/#keep\n"
v6=b"hysteria2://p%3Aa@[2001:db8::6]:443/?sni=legacy#retired"
repl=b"hysteria2://p%3Aa@[2001:db8::6]:443/?sni=node.example.test#new"
mapping={("2001:db8::6",443,"p:a"):repl}
assert m["rewrite_combined"](tcp+v4+other+v6+b"\n",mapping,443)==tcp+v4+other+repl+b"\n"
try:
 m["mask_of"]({"finalmask":{"udp":[]}})
except ValueError:pass
else:raise AssertionError("错误模板应拒绝")
print("通过：原输入未被更改、复用已生成凭据、固定 404、只替换匹配到的 UDP 链接")
