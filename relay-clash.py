#!/usr/bin/env python3
"""将本机 relay 节点链接转换为私有 Clash/Mihomo YAML：大陆域名和 IP 直连，其他目的地走节点。"""
import argparse
import base64
import datetime
import ipaddress
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse as url
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GROUP = "节点选择"
RULES_REPO = "MetaCubeX/meta-rules-dat"
RAW = "https://raw.githubusercontent.com/" + RULES_REPO
RULES = (("cn-domain", "geosite", "domain"), ("cn-ip", "geoip", "ipcidr"))
SERVICE_LINKS = Path("/etc/x-ui/relay-links.txt")
PRIVATE_LIMIT = 4 * 1024 * 1024
PUBLIC_LIMIT = 16 * 1024 * 1024

class Problem(ValueError):
    """Safe to show to an operator: never put a raw URI or credential in the message."""

def safe_text(s):
    return bool(s and len(s) <= 512 and all(c.isprintable() for c in s))

def hostname(s):
    if not safe_text(s): raise Problem("缺少有效的主机名")
    try: a = s.rstrip(".").encode("idna").decode().lower()
    except UnicodeError: raise Problem("主机名无效") from None
    if len(a)>253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", k) for k in a.split(".")):
        raise Problem("主机名无效")
    return a

def proxy_from_uri(text):
    try:
        if not text or len(text)>8192 or any(not c.isprintable() for c in text):
            raise Problem("链接过长或含控制字符")
        u = url.urlsplit(text)
        scheme = u.scheme.lower()
        if scheme not in ("vless","hysteria2","hy2"): raise Problem("仅支持 VLESS/REALITY 和 Hysteria2")
        server = u.hostname or ""
        try:
            ip = ipaddress.ip_address(server)
            if "%" in server: raise Problem("不支持带接口标识的节点地址")
            server = str(ip); suffix = f"-IPv{ip.version}"
        except ValueError:
            server = hostname(server); suffix = ""
        port = u.port if u.port is not None else 443
        if not 1 <= port <= 65535: raise Problem("节点端口不合法")
        if u.path not in ("","/"): raise Problem("不支持此传输路径")
        raw = url.parse_qs(u.query,keep_blank_values=True,strict_parsing=True)
        if any(len(v)!=1 for v in raw.values()): raise Problem("链接有重复参数")
        q = {k:v[0] for k,v in raw.items()}
        sni = hostname(q.get("sni",""))
        name = url.unquote(u.fragment) or (sni + ("-TCP-VLESS" if scheme=="vless" else "-UDP-HY2") + suffix)
        if not safe_text(name) or name in (GROUP,"DIRECT","REJECT","GLOBAL","PASS","MATCH","COMPATIBLE"):
            raise Problem("节点名为空、无效或与策略名冲突")
        common = {"name":name,"type":"vless" if scheme=="vless" else "hysteria2","server":server,"port":port,"udp":True}
        if scheme=="vless":
            if u.password is not None or q.get("security")!="reality" or q.get("type","tcp") not in ("tcp","raw") or q.get("encryption","none")!="none" or q.get("flow")!="xtls-rprx-vision":
                raise Problem("仅支持本脚本生成的 VLESS/REALITY TCP Vision 入站")
            if q.get("spx","/")!="/": raise Problem("不支持自定义 REALITY spider 路径")
            ident = str(uuid.UUID(url.unquote(u.username or "")))
            pbk = q.get("pbk","")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}",pbk) or len(base64.urlsafe_b64decode(pbk+"="))!=32:
                raise Problem("REALITY 公钥不合法")
            sid = q.get("sid","")
            if not re.fullmatch(r"(?:[a-fA-F0-9]{2}){1,8}",sid): raise Problem("REALITY short-id 不合法")
            fp=q.get("fp","chrome")
            if fp not in ("chrome","firefox","safari","ios","android","edge","360","qq","random","randomized"):
                raise Problem("不支持这个客户端 TLS 指纹")
            common.update({"uuid":ident,"network":"tcp","tls":True,"flow":"xtls-rprx-vision","servername":sni,
                           "client-fingerprint":fp,"reality-opts":{"public-key":pbk,"short-id":sid}})
        else:
            auth = url.unquote(u.username or "") + (":"+url.unquote(u.password) if u.password is not None else "")
            pin = q.get("pinSHA256","").replace(":","").lower()
            if not re.fullmatch(r"[0-9a-f]{64}",pin): raise Problem("HY2 缺少有效的 SHA-256 证书指纹")
            if q.get("insecure","0").lower() not in ("0","false"): raise Problem("不接受显式关闭证书验证的链接")
            if not safe_text(auth) or q.get("obfs")!="salamander" or not safe_text(q.get("obfs-password","")):
                raise Problem("HY2 认证或传输口令不完整")
            if q.get("alpn","h3")!="h3": raise Problem("仅支持 h3 的 HY2 节点")
            common.update({"password":auth,"sni":sni,"alpn":["h3"],"skip-cert-verify":False,
                           "fingerprint":pin,"obfs":"salamander","obfs-password":q["obfs-password"]})
        return common
    except Problem: raise
    except (ValueError,TypeError,UnicodeError): raise Problem("地址、UUID 或编码格式无效") from None

def load_nodes(paths):
    nodes={}
    for number,path in enumerate(paths,1):
        source=f"输入文件 {number}"
        if not path.is_file() or path.is_symlink() or path.stat().st_size>PRIVATE_LIMIT:raise Problem(f"{source} 不存在、不是常规文件或过大")
        for n,line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(),1):
            line=line.strip()
            if not line or line.startswith("#"):continue
            m=re.fullmatch(r"(?:\[[^\]\r\n]{1,64}\]\s*)?((?:vless|hysteria2|hy2)://\S+)",line)
            if not m:raise Problem(f"{source} 第 {n} 行不是支持的分享链接")
            try:node=proxy_from_uri(m[1])
            except Problem as e:raise Problem(f"{source} 第 {n} 行：{e}") from None
            name=node["name"]
            if name in nodes and nodes[name]!=node:raise Problem(f"{source} 第 {n} 行与另一个节点重名")
            nodes[name]=node
    if not nodes:raise Problem("未找到节点")
    def rank(p):
        try:num=ipaddress.ip_address(p["server"]).version
        except ValueError:num=5
        return (p["type"]!="vless",num)
    return sorted(nodes.values(),key=rank)

def fetch_public(address,limit=PUBLIC_LIMIT):
    req=urllib.request.Request(address,headers={"User-Agent":"ununu-relay-clash/1"})
    try:
        with urllib.request.urlopen(req,timeout=25) as r:
            dest=url.urlsplit(r.geturl())
            if dest.scheme!="https" or dest.hostname not in ("api.github.com","raw.githubusercontent.com"):raise Problem("公共规则地址发生异常重定向")
            data=r.read(limit+1)
    except urllib.error.HTTPError as e:raise Problem(f"公共规则下载失败（HTTP {e.code}），可改用 --rules-dir 或 --rules online") from None
    except (OSError,TimeoutError):raise Problem("公共规则下载失败，可使用 --rules-dir 或 --rules online") from None
    if not data or len(data)>limit:raise Problem("公共规则数据为空或过大")
    return data

def read_list(data,kind):
    if not data or len(data)>PUBLIC_LIMIT:raise Problem("规则数据为空或过大")
    seen={}
    for line in data.decode("utf-8-sig").splitlines():
        s=line.strip().lower()
        if not s or s.startswith("#"):continue
        if kind=="domain":
            bare=s[2:] if s.startswith(("+.","*.")) else s
            if not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*",bare) or len(bare)>253:raise Problem("公共域名规则格式异常")
        else:
            try:net=ipaddress.ip_network(s,strict=True)
            except ValueError:raise Problem("公共 IP 规则格式异常") from None
            if net.prefixlen==0:raise Problem("拒绝将整个互联网分类为直连")
            s=str(net)
        seen[s]=None
    if not seen:raise Problem("规则列表没有有效项目")
    return list(seen)

def rule_data(mode,folder=None,ref=None):
    if ref is not None and not re.fullmatch(r"[a-f0-9]{40}",ref):raise Problem("--rule-ref 必须是公开规则库的完整提交号")
    if mode=="online" and (folder or ref):raise Problem("在线更新模式不接受本地快照或固定提交号")
    if mode=="inline" and folder is None and ref is None:
        try:ref=json.loads(fetch_public("https://api.github.com/repos/"+RULES_REPO+"/branches/meta",256_000))["commit"]["sha"]
        except (ValueError,KeyError,TypeError) as e:
            if isinstance(e,Problem):raise
            raise Problem("无法确定公共规则版本") from None
        if not isinstance(ref,str) or not re.fullmatch(r"[a-f0-9]{40}",ref):raise Problem("公共规则版本不合法")
    result={}
    for key,sub,kind in RULES:
        if mode=="online":
            result[key]={"type":"http","behavior":kind,"format":"mrs","path":f"./ruleset/relay-{key}.mrs",
                         "url":f"{RAW}/meta/geo/{sub}/cn.mrs","interval":86400,"proxy":GROUP}
        else:
            if folder:
                p=folder/f"{sub}-cn.list"
                if p.is_symlink() or not p.is_file() or p.stat().st_size>PUBLIC_LIMIT:raise Problem("缺少本地常规公共规则文件："+p.name)
                data=p.read_bytes()
            else:data=fetch_public(f"{RAW}/{ref}/geo/{sub}/cn.list")
            result[key]={"type":"inline","behavior":kind,"payload":read_list(data,kind)}
    return result,ref or ("local" if mode=="inline" else "meta")

def make_config(nodes,providers,port=7890):
    if not 1<=port<=65535:raise Problem("本机 mixed-port 必须在 1 至 65535 之间")
    cn_dns=["https://dns.alidns.com/dns-query","https://doh.pub/dns-query"]
    policy={"rule-set:cn-domain":cn_dns,"+.cn":cn_dns}
    local_domains=("localhost","local","lan","home.arpa")
    for name in local_domains:policy["+."+name]=["system"]
    direct=[f"DOMAIN-SUFFIX,{name},DIRECT" for name in (*local_domains,"cn")]
    # Resolve a remaining domain for its region once; then private-IP rules can also see the
    # address of a nonstandard LAN hostname. Literal IP destinations do not trigger DNS here.
    direct.extend(["RULE-SET,cn-domain,DIRECT","RULE-SET,cn-ip,DIRECT"])
    for net in ("0.0.0.0/8","10.0.0.0/8","100.64.0.0/10","127.0.0.0/8","169.254.0.0/16","172.16.0.0/12","192.168.0.0/16","224.0.0.0/4","255.255.255.255/32","::/128","::1/128","fc00::/7","fe80::/10","ff00::/8"):
        direct.append(("IP-CIDR6" if ":" in net else "IP-CIDR")+","+net+",DIRECT,no-resolve")
    return {"mixed-port":port,"allow-lan":False,"bind-address":"127.0.0.1","mode":"rule","log-level":"info","ipv6":True,
            "profile":{"store-selected":True},
            "dns":{"enable":True,"ipv6":True,"enhanced-mode":"fake-ip","fake-ip-range":"198.18.0.1/16",
                   "fake-ip-filter":["+."+n for n in local_domains],"default-nameserver":["223.5.5.5","119.29.29.29"],
                   "proxy-server-nameserver":cn_dns,"nameserver-policy":policy,
                   "nameserver":["https://1.1.1.1/dns-query#"+GROUP,"https://8.8.8.8/dns-query#"+GROUP],
                   "direct-nameserver":cn_dns,"direct-nameserver-follow-policy":True},
            "proxies":nodes,"proxy-groups":[{"name":GROUP,"type":"select","proxies":[p["name"] for p in nodes]}],
            "rule-providers":providers,"rules":direct+["MATCH,"+GROUP]}

def yaml_lines(value,level=0):
    # Only emit the narrow JSON-compatible YAML scalar types used in this file. In particular
    # quote every string, even numeric-looking credentials, hex short IDs, booleans and IPv6.
    pad=" "*level
    def scalar(x):return json.dumps(x,ensure_ascii=False,allow_nan=False)
    if isinstance(value,dict):
        for k,v in value.items():
            field=k if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9-]*",k) else scalar(k)
            if isinstance(v,(dict,list)) and v:
                yield pad+field+":";yield from yaml_lines(v,level+2)
            else:yield pad+field+": "+scalar(v)
    elif isinstance(value,list):
        for v in value:
            if isinstance(v,(dict,list)) and v:
                yield pad+"-";yield from yaml_lines(v,level+2)
            else:yield pad+"- "+scalar(v)

def render(cfg,ref):
    head=("# 私有客户端配置：含节点凭据，请勿上传至公共仓库或在线转换服务。\n"
          "# Mihomo / Clash Meta；目的地分流：大陆域名或大陆 IP 直连，其他流量走节点选择。\n"
          f"# 规则来源：https://github.com/{RULES_REPO} @ {ref}\n")
    return (head+"\n".join(yaml_lines(cfg))+"\n").encode("utf-8")

def private_copy(path,data):
    # Use exclusive creation; never follow an existing symlink or truncate a destination.
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),"wb") as f:
        f.write(data);f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),0o600)

def write_profile(path,data,replace=False):
    path=path.expanduser()
    if path.is_symlink():raise Problem("输出路径是符号链接，未写入")
    path=path.resolve()
    public_roots=(ROOT,Path("/usr/local/share/relay-local-web").resolve())
    if any(path==r or r in path.parents for r in public_roots) or any((p/".git").exists() for p in path.parents):
        raise Problem("私有配置不能导出到 Git 工作区、脚本目录或 relay 静态网站内")
    if not path.parent.is_dir():raise Problem("输出目录不存在")
    if path.suffix.lower() not in (".yaml",".yml"):raise Problem("输出文件需要 .yaml 或 .yml 后缀")
    if path.exists():
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:raise Problem("不覆盖非普通文件或硬链接")
        original=path.read_bytes()
        if original==data:path.chmod(0o600);return path,None
        if not replace:raise Problem("输出文件已有不同内容；使用新文件名，或用 --replace 先备份再替换")
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup=path.with_name(path.name+".bak-"+stamp)
        private_copy(backup,original)
        with tempfile.NamedTemporaryFile(dir=path.parent,prefix=".relay-clash-",delete=False) as f:
            f.write(data);f.flush();os.fchmod(f.fileno(),0o600);os.fsync(f.fileno())
        os.replace(f.name,path)
        return path,backup
    private_copy(path,data)
    return path,None

def main():
    ap=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    ap.add_argument("--links",type=Path,action="append",help="本地节点链接文件；可重复，默认读取 /etc/x-ui/relay-links.txt")
    ap.add_argument("--output","-o",type=Path,help="输出 .yaml 文件；不会输出节点明文到终端")
    ap.add_argument("--mixed-port",type=int,default=7890)
    ap.add_argument("--rules",choices=("inline","online"),default="inline",help="默认单文件内置规则；online 使用每日更新的 MRS")
    ap.add_argument("--rules-dir",type=Path,help="从本地 geosite-cn.list 和 geoip-cn.list 读取，不联网获取规则")
    ap.add_argument("--rule-ref",help="固定用于内置规则的 MetaCubeX/meta-rules-dat 提交")
    ap.add_argument("--replace",action="store_true",help="更新同名私有配置（先备份）")
    a=ap.parse_args();os.umask(0o077)
    inputs=[p.expanduser() for p in (a.links or [SERVICE_LINKS])]
    dest=a.output or (Path.home()/"Downloads/relay-clash.yaml" if a.links else SERVICE_LINKS.with_name("relay-clash.yaml"))
    if any(p.resolve()==dest.expanduser().resolve() for p in inputs):raise Problem("输出文件不能覆盖输入链接")
    nodes=load_nodes(inputs);providers,ref=rule_data(a.rules,a.rules_dir.expanduser() if a.rules_dir else None,a.rule_ref)
    cfg=make_config(nodes,providers,a.mixed_port);data=render(cfg,ref);path,backup=write_profile(dest,data,a.replace)
    print(f"已导出 {len(nodes)} 个节点：{path}（权限 600）")
    if backup:print("旧配置已备份："+str(backup))
    if a.rules=="inline":print(f"已内置 {len(providers['cn-domain']['payload'])} 条域名规则和 {len(providers['cn-ip']['payload'])} 个大陆 IP 网段。")
    else:print("Mihomo 将通过所选节点下载国内规则并每日更新。")

if __name__=="__main__":
    try:main()
    except (Problem,OSError,UnicodeError) as e:
        if isinstance(e,UnicodeError):print("输入文件不是有效的 UTF-8，未导出",file=sys.stderr)
        else:print(str(e),file=sys.stderr)
        sys.exit(1)
