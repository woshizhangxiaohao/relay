#!/usr/bin/env bash
# 一键中转 / 落地：安装 3x-ui 并配好 VLESS + Reality 入站、落地出站和转发规则。
# 用法：下载完整仓库后运行 bash relay.sh；DNS/证书/网页见 README。
# 本脚本不含任何密码或密钥；面板账号密码运行时输入，只写进本机 3x-ui。
set -euo pipefail
umask 077
RELAY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

XUI_VERSION="${RELAY_XUI_VERSION:-v3.11.0}"
XRAY_VERSION="${RELAY_XRAY_VERSION:-v26.6.27}"
WEB_PATH="zxh"
XUI_DIR="/usr/local/x-ui"
XUI_BIN="$XUI_DIR/x-ui"
XRAY_CONF="$XUI_DIR/bin/config.json"
STATE="/etc/x-ui/relay.env"
LINKS="/etc/x-ui/relay-links.txt"
LANDING_TAG="landing"
RULE_TAG="relay"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "错误：$*"; exit 1; }
ask() { local __v="$1" __p="$2" __d="${3:-}" __a; read -r -p "$__p" __a </dev/tty; printf -v "$__v" '%s' "${__a:-$__d}"; }

# 同一个版本的脚本、网页与迁移工具须成套使用。
require_bundle() {
  local x
  for x in relay-web.py relay-dns.py relay-tcp.py relay-udp.py relay-panel.py relay_hot.py site/index.html; do
    [[ -f "$RELAY_DIR/$x" ]] || die "需要完整的 relay 目录（缺少 $x），请按 README 下载同一版本。"
  done
}

prepare_new_web() {
  ask SNI "本节点的自有子域名（例如 lax1.ununu.dev）: "
  [[ -n "$SNI" ]] || die "必须填写自己的节点域名"
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    local zone ip4 ip6
    ask zone "Cloudflare zone（例如 ununu.dev）: "
    ip4=$(pub_ip4); ip6=$(pub_ip6 || true)
    local record_args=(--host "$SNI" --zone "$zone" --ipv4 "$ip4" --apply)
    if [[ -n "$ip6" ]]; then record_args+=(--ipv6 "$ip6"); fi
    python3 "$RELAY_DIR/relay-dns.py" "${record_args[@]}" || die "DNS 未就绪"
    unset CLOUDFLARE_API_TOKEN
  else
    echo "使用管理端已建的灰云记录；还没有记录时先用 relay.sh dns。"
  fi
  if command -v ufw >/dev/null && ufw status | grep -q 'Status: active'; then
    ufw allow 80/tcp >/dev/null
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port=80/tcp; firewall-cmd -q --add-port=80/tcp
  fi
  python3 "$RELAY_DIR/relay-web.py" "$SNI" || die "证书或本机静态站未通过；不创建入站"
}

need_root() { [[ $EUID -eq 0 ]] || die "请用 root 运行"; }

verify_file() {
  python3 -c 'import hashlib,sys; sys.exit(0 if hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()==sys.argv[2] else 1)' "$1" "$2" || die "文件摘要未通过：$1"
}

# 暂存文件转入仅 root 可读的运行时回收目录，系统重启后自然清理。
retire_tmp() {
  local dest; install -d -m 700 /run/relay-retired
  dest=$(mktemp -d /run/relay-retired/old.XXXXXXXX)
  local item; for item in "$@"; do [[ ! -e "$item" ]] || mv -- "$item" "$dest/"; done
}

ensure_deps() {
  local miss=()
  for c in curl python3 openssl ss; do command -v "$c" >/dev/null || miss+=("$c"); done
  [[ ${#miss[@]} -eq 0 ]] && return
  yellow "安装依赖：${miss[*]}"
  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq curl python3 openssl iproute2 >/dev/null
  elif command -v dnf >/dev/null; then dnf install -y -q curl python3 openssl iproute
  elif command -v yum >/dev/null; then yum install -y -q curl python3 openssl iproute
  else die "无法自动安装依赖：${miss[*]}"; fi
}

pub_ip4() { curl -4 -s -m 6 https://api.ipify.org || curl -4 -s -m 6 https://4.ident.me; }
pub_ip6() { curl -6 -s -m 6 https://api6.ipify.org || curl -6 -s -m 6 https://6.ident.me; }
# 入站绑定用的本机地址（云服务器 IPv4 常是内网地址，经 NAT 映射公网）
local_ip4() { ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}'; }
local_ip6() { ip -6 route get 2001:4860:4860::8888 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="src"){print $(i+1); exit}}'; }

xray_bin() { ls "$XUI_DIR"/bin/xray-linux-* 2>/dev/null | head -1; }
port_busy() { ss -Hltn "sport = :$1" | grep -q .; }

# ---------- 3x-ui API ----------
api_init() {
  # shellcheck disable=SC1090
  source "$STATE" 2>/dev/null || true
  PANEL_PORT="${PANEL_PORT:-$("$XUI_BIN" setting -show 2>/dev/null | awk -F': ' '/^port/{print $2}')}"
  [[ -n "$PANEL_PORT" ]] || die "读不到面板端口"
  API_BASE="http://127.0.0.1:${PANEL_PORT}/${WEB_PATH}/panel/api"
  TOKEN=$("$XUI_BIN" setting -getApiToken -tokenName relay-script 2>/dev/null | grep -Eo 'apiToken: .+' | awk '{print $2}')
  [[ -n "$TOKEN" ]] || die "拿不到 3x-ui API 令牌"
}
# api METHOD PATH [curl 参数...]，成功时输出响应 JSON，失败退出
api() {
  local m="$1" p="$2"; shift 2
  local out
  out=$(curl -s -m 120 -X "$m" -H "Authorization: Bearer $TOKEN" "$API_BASE/$p" "$@") || die "接口 $p 请求失败"
  python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d.get("success") else 1)' "$out" 2>/dev/null \
    || die "接口 $p 返回失败（响应可能含凭据，不回显）"
  printf '%s' "$out"
}
# 取 JSON 字段；3x-ui 有的接口把 obj 再编码成字符串，遇到字符串先解开
jget() { python3 -c 'import json,sys; d=json.loads(sys.argv[1])
for k in sys.argv[2].split("."):
    if isinstance(d,str): d=json.loads(d)
    d=d[int(k)] if isinstance(d,list) else d[k]
print(d if not isinstance(d,(dict,list)) else json.dumps(d))' "$1" "$2"; }

wait_panel() {
  for _ in $(seq 1 30); do curl -s -m 2 -o /dev/null "http://127.0.0.1:${PANEL_PORT}/" && return 0; sleep 1; done
  die "面板没起来，查看：journalctl -u x-ui -n 50"
}

# 直接从 Xray 官方发布下载指定版本替换内核（3x-ui 的安装接口只认最近 30 个版本，旧版本会被拒）
install_xray() {
  local a zip_arch bin_arch tmp
  a=$(uname -m)
  case "$a" in
    x86_64|amd64) zip_arch=64; bin_arch=amd64 ;;
    aarch64|arm64) zip_arch=arm64-v8a; bin_arch=arm64 ;;
    *) die "不支持的架构：$a" ;;
  esac
  green "安装 Xray $XRAY_VERSION ..."
  tmp=$(mktemp -d)
  curl -fLs -m 180 -o "$tmp/x.zip" "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/Xray-linux-${zip_arch}.zip" \
    || die "下载 Xray $XRAY_VERSION 失败"
  local pinned="${RELAY_XRAY_SHA256:-}"
  if [[ -z "$pinned" && "$XRAY_VERSION" == v26.6.27 ]]; then
    if [[ "$bin_arch" == amd64 ]]; then pinned=b3e5902d06d6282fe53cfa2fc426058b9aeaa429b2c812e20887cd47f26d08bf
    else pinned=13a251379bea366c2cf10363ad71e75734193d401f26f518bf0c25e5c8f8c931; fi
  fi
  [[ -n "$pinned" ]] || die "非默认 Xray 版本需要提供 RELAY_XRAY_SHA256"
  verify_file "$tmp/x.zip" "$pinned"
  python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract("xray", sys.argv[2])' "$tmp/x.zip" "$tmp" || die "解压 Xray 失败"
  systemctl stop x-ui
  install -m 755 "$tmp/xray" "$XUI_DIR/bin/xray-linux-${bin_arch}"
  retire_tmp "$tmp"
  systemctl start x-ui; wait_panel
  "$(xray_bin)" version | head -1
}

restart_xray() {
  api POST server/restartXrayService >/dev/null
  sleep 3
  systemctl is-active --quiet x-ui || die "x-ui 没在运行"
  pgrep -f "$(basename "$(xray_bin)")" >/dev/null || die "Xray 没起来：$(api GET xray/getXrayResult | head -c 400)"
}

# ---------- 落地 ----------
# 解析落地输入为 Xray 出站 JSON；空输入 = 不需要落地
parse_landing() {
  python3 - "$1" "$LANDING_TAG" <<'PY'
import json, sys, urllib.parse as u
s, tag = sys.argv[1].strip(), sys.argv[2]
def out(o): print(json.dumps(o)); sys.exit(0)
if s.startswith("vless://"):
    p = u.urlparse(s); q = dict(u.parse_qsl(p.query))
    host = p.hostname; port = p.port
    if not (p.username and host and port): sys.exit("vless 链接缺少 UUID、地址或端口")
    sec = q.get("security", "none")
    st = {"network": q.get("type", "tcp"), "security": sec}
    if sec == "reality":
        st["realitySettings"] = {"serverName": q.get("sni", ""), "publicKey": q.get("pbk", ""),
            "shortId": q.get("sid", ""), "fingerprint": q.get("fp", "chrome"), "spiderX": q.get("spx", "/")}
    elif sec == "tls":
        st["tlsSettings"] = {"serverName": q.get("sni", host), "fingerprint": q.get("fp", "chrome")}
    out({"tag": tag, "protocol": "vless", "settings": {"address": host, "port": port, "id": p.username,
         "encryption": q.get("encryption", "none"), "flow": q.get("flow", "")}, "streamSettings": st})
if s.startswith(("socks5://", "socks://")):
    p = u.urlparse(s)
    host, port, user, pw = p.hostname, p.port, u.unquote(p.username or ""), u.unquote(p.password or "")
else:
    parts = s.split("|") if "|" in s else s.split(":")
    if len(parts) < 2: sys.exit("看不懂这个落地格式")
    host, port = parts[0], parts[1]
    user, pw = (parts[2], parts[3]) if len(parts) >= 4 else ("", "")
try: port = int(port)
except Exception: sys.exit("端口不是数字")
srv = {"address": host, "port": port}
if user: srv["users"] = [{"user": user, "pass": pw}]
out({"tag": tag, "protocol": "socks", "settings": {"servers": [srv]}})
PY
}

ask_landing() {
  echo "要另外配置落地出口吗？（可选）"
  echo "  1. 本机直出（默认，不需要家宽；也可以给别的中转机当落地）"
  echo "  2. SOCKS5（粘贴卖家给的那一行，如 IP|端口|账号|密码|到期）"
  echo "  3. 自己的落地机（粘贴那台机器输出的 vless:// 链接）"
  local c; ask c "请选择 [1]: " 1
  case "$c" in
    1) LANDING_JSON=""; LANDING_TYPE="none" ;;
    2|3) local line; ask line "粘贴落地信息: "
         LANDING_JSON=$(parse_landing "$line") || die "落地信息解析失败"
         LANDING_TYPE=$([[ $c == 2 ]] && echo socks || echo vless) ;;
    *) die "无效选择" ;;
  esac
}

# 改 Xray 模板：出口只用 IPv4；本脚本的入站（逗号分隔的 tag）全部走落地（无落地则走 direct）；落地不通不回退
apply_routing() {
  local inbound_tags="$1" tpl new
  tpl=$(jget "$(api POST xray/)" obj.xraySetting)
  new=$(python3 - "$tpl" "$inbound_tags" "${LANDING_JSON:-}" "$LANDING_TAG" "$RULE_TAG" <<'PY'
import json, sys
c = json.loads(sys.argv[1]); itags, landing, ltag, rtag = sys.argv[2:6]
obs = [o for o in c.get("outbounds", []) if o.get("tag") != ltag]
direct = next((o for o in obs if o.get("protocol") == "freedom" and o.get("tag") == "direct"), None)
if not direct:
    direct = {"tag": "direct", "protocol": "freedom", "settings": {}}; obs.insert(0, direct)
direct.setdefault("settings", {})["domainStrategy"] = "UseIPv4"
if landing: obs.append(json.loads(landing))
c["outbounds"] = obs
r = c.setdefault("routing", {}); rules = [x for x in r.get("rules", []) if x.get("ruleTag") != rtag]
mine = {"type": "field", "ruleTag": rtag, "inboundTag": itags.split(","), "outboundTag": ltag if landing else "direct"}
pos = next((i + 1 for i, x in enumerate(rules) if x.get("outboundTag") == "api"), 0)
rules.insert(pos, mine); r["rules"] = rules
print(json.dumps(c, ensure_ascii=False, indent=2))
PY
) || die "改写 Xray 模板失败"
  api POST xray/update --data-urlencode "xraySetting=$new" >/dev/null
}

# 用临时 Xray 实例走同一个出口访问外网，确认出口 IP
self_test() {
  local xb sp tmp ip ob
  xb=$(xray_bin); sp=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
  tmp=$(mktemp -d)
  ob=${LANDING_JSON:-'{"tag":"landing","protocol":"freedom","settings":{"domainStrategy":"UseIPv4"}}'}
  python3 - "$ob" "$sp" > "$tmp/c.json" <<'PY'
import json, sys
print(json.dumps({"log": {"loglevel": "warning"},
  "inbounds": [{"listen": "127.0.0.1", "port": int(sys.argv[2]), "protocol": "socks", "settings": {"udp": False}}],
  "outbounds": [json.loads(sys.argv[1])]}))
PY
  "$xb" run -c "$tmp/c.json" >"$tmp/log" 2>&1 & local pid=$!
  sleep 2
  ip=$(curl --noproxy '' -fsS -m 15 --socks5-hostname "127.0.0.1:$sp" https://api64.ipify.org) || ip=""
  kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; retire_tmp "$tmp"
  if [[ -z "$ip" ]]; then red "自检失败：经出口访问外网不通（落地不可用？）"; return 1; fi
  python3 -c 'import ipaddress,sys;sys.exit(0 if ipaddress.IPv4Address(sys.argv[1]).is_global else 1)' "$ip" 2>/dev/null || { red "出口没有返回可验证的公网 IPv4"; return 1; }
  if [[ "$LANDING_TYPE" != none && "$ip" == "$(pub_ip4)" ]]; then red "自检失败：出口是本机 IP，没走落地"; return 1; fi
  EXIT_IP=$ip
  green "自检通过：出口 IP = $ip"
}

# 出口 IP 的中文「国家+地区」，如 美国加利福尼亚；查不到输出空
region_name() {
  curl -s -m 8 "http://ip-api.com/json/$1?lang=zh-CN&fields=status,country,regionName" | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
if d.get("status") == "success":
    r = d.get("regionName", "")
    # 只给长名去后缀（加利福尼亚州→加利福尼亚），「加州」这类两个字的保持原样
    print(d.get("country", "") + (re.sub(r"(特别行政区|特别市|广域市|州|省|市)$", "", r) if len(r) > 2 else r))' 2>/dev/null
}

# ---------- 入站：VLESS+Reality（TCP）、Hysteria2（UDP）各 IPv4、IPv6 一个，同端口、各用独立密钥 ----------
# 输出 "id tag"
add_inbound() {
  local proto="$1" kind="$2" listen="$3" res payload
  payload=$(inbound_body_$proto "$kind" "$listen") || die "生成 $proto / $kind 入站失败"
  res=$(api POST inbounds/add -H 'Content-Type: application/json' --data "$payload") || die "创建 $proto / $kind 入站失败"
  echo "$(jget "$res" obj.id) $(jget "$res" obj.tag)"
}

inbound_body_vless() {
  local kind="$1" listen="$2" keys priv pub uuid sid
  keys=$(api GET server/getNewX25519Cert); priv=$(jget "$keys" obj.privateKey); pub=$(jget "$keys" obj.publicKey)
  uuid=$(jget "$(api GET server/getNewUUID)" obj.uuid 2>/dev/null || python3 -c 'import uuid;print(uuid.uuid4())')
  sid=$(openssl rand -hex 8)
  # realitySettings.settings.publicKey 不进 Xray 配置，但 3x-ui 面板导出分享链接要用它，缺了导出的链接连不上
  python3 - "$IN_PORT" "$listen" "$uuid" "$SNI" "$priv" "$sid" "$pub" "$kind" "$NODE_NAME" <<'PY'
import json, sys
port, listen, uid, sni, priv, sid, pub, kind, name = sys.argv[1:10]
print(json.dumps({"enable": True, "remark": f"{name}-TCP-VLESS-IP{kind}", "listen": listen, "port": int(port), "protocol": "vless",
  "expiryTime": 0, "total": 0,
  "settings": {"clients": [{"id": uid, "email": f"relay-vless-{kind}", "flow": "xtls-rprx-vision", "enable": True}],
               "decryption": "none", "fallbacks": []},
  "streamSettings": {"network": "tcp", "security": "reality", "tcpSettings": {"header": {"type": "none"}},
    "realitySettings": {"show": False, "xver": 0, "target": "127.0.0.1:8443", "serverNames": [sni],
                        "privateKey": priv, "shortIds": [sid],
                        "settings": {"publicKey": pub, "fingerprint": "chrome", "serverName": "", "spiderX": "/"}}},
  "sniffing": {"enabled": False}}))
PY
}

# 自签证书（ECDSA P-256），客户端用证书指纹固定（pinSHA256）验证，不用 insecure
HY2_CRT=/etc/x-ui/relay-hy2.crt
HY2_KEY=/etc/x-ui/relay-hy2.key
ensure_hy2_cert() {
  [[ -s $HY2_CRT && -s $HY2_KEY ]] && return
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=$SNI" -addext "subjectAltName=DNS:$SNI" -keyout "$HY2_KEY" -out "$HY2_CRT" >/dev/null 2>&1 || die "生成 hy2 证书失败"
  chmod 600 "$HY2_KEY"
}
cert_pin() { openssl x509 -in "$1" -outform der | openssl dgst -sha256 -hex | awk '{print $NF}'; }

inbound_body_hy2() {
  local kind="$1" listen="$2"
  ensure_hy2_cert
  # 未认证 HTTP/3 只返回固定的本机 404；网页证书的续期不影响 HY2 指纹。
  python3 - "$IN_PORT" "$listen" "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" "$SNI" "$(cert_pin $HY2_CRT)" \
    "$HY2_CRT" "$HY2_KEY" "$kind" "$NODE_NAME" <<'PY'
import json, sys
port, listen, auth, obfs, sni, pin, crt, key, kind, name = sys.argv[1:11]
print(json.dumps({"enable": True, "remark": f"{sni}-UDP-HY2-IP{kind}", "listen": listen, "port": int(port), "protocol": "hysteria",
  "expiryTime": 0, "total": 0,
  "settings": {"version": 2, "clients": [{"auth": auth, "email": f"relay-hy2-{kind}", "enable": True}]},
  "streamSettings": {"network": "hysteria", "security": "tls",
    "tlsSettings": {"serverName": sni, "minVersion": "1.3", "alpn": ["h3"],
                    "certificates": [{"certificateFile": crt, "keyFile": key}],
                    "settings": {"fingerprint": "chrome", "pinnedPeerCertSha256": [pin]}},
    "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60,
                         "masquerade": {"type": "string", "statusCode": 404, "content": "Not Found"}},
    "finalmask": {"udp": [{"type": "salamander", "settings": {"password": obfs}}]}},
  "sniffing": {"enabled": False}}))
PY
}

# ---------- 3. 开关入站 ----------
do_toggle() {
  [[ -f "$STATE" ]] || die "这台机器不是用本脚本初始化的，请先选「初始化」"
  api_init
  local ids=() i=0 id ib c
  for id in $INBOUND_IDS; do
    ib=$(jget "$(api GET "inbounds/get/$id")" obj)
    ids+=("$id"); i=$((i + 1))
    echo "  $i. $(jget "$ib" remark)  监听 $(jget "$ib" listen):$(jget "$ib" port)  $([[ $(jget "$ib" enable) == True ]] && echo 已开启 || echo 已关闭)"
  done
  ask c "切换哪一个的开关？[1-$i]: "
  [[ "$c" =~ ^[0-9]+$ && $c -ge 1 && $c -le $i ]] || die "无效选择"
  id=${ids[$((c - 1))]}
  # inbounds/update 会保留原 enable，开关要走 setEnable
  local on; on=$(jget "$(api GET "inbounds/get/$id")" obj.enable)
  if [[ $on != True ]]; then
    local entry; entry=$(api GET "inbounds/get/$id")
    python3 -c 'import json,sys
v=json.load(sys.stdin)["obj"];s=v.get("streamSettings",{})
if isinstance(s,str): s=json.loads(s)
p=v.get("protocol");r=s.get("realitySettings",{});h=s.get("hysteriaSettings",{}).get("masquerade",{})
ok=(p=="vless" and s.get("security")=="reality" and r.get("target",r.get("dest"))=="127.0.0.1:8443" and r.get("serverNames")==[open("/etc/relay-local-web/hostname").read().strip()]) or (p=="hysteria" and h.get("type","") in ("","string"))
sys.exit(0 if ok else 1)' <<<"$entry" || die "旧入站未满足本机响应规则，不直接启用"
  fi
  api POST "inbounds/setEnable/$id" -H 'Content-Type: application/json' \
    --data "{\"enable\": $([[ $on == True ]] && echo false || echo true)}" >/dev/null
  restart_xray
  echo "当前开启的节点链接："; print_links
}

# ---------- 输出链接（直接读运行中的 Xray 配置，脚本外装的 3x-ui 也能用） ----------
print_links() {
  [[ -f "$XRAY_CONF" ]] || die "找不到 $XRAY_CONF，3x-ui 没装？"
  local xb v4 v6 name
  # shellcheck disable=SC1090
  [[ -f "$STATE" ]] && source "$STATE"
  xb=$(xray_bin); v4=$(pub_ip4); v6=$(local_ip6 || true); name="${RELAY_NAME:-${NODE_NAME:-$(hostname -s)}}"
  python3 - "$XRAY_CONF" "$xb" "$v4" "$v6" "$name" <<'PY' | tee "$LINKS"
import json, subprocess, sys, urllib.parse as u
conf, xb, v4, v6, name = sys.argv[1:6]
c = json.load(open(conf))
def hosts_of(listen):
    if listen in ("", "0.0.0.0", "::"):
        return [h for h in (("v4", v4), ("v6", v6)) if h[1]]
    if ":" in listen: return [("v6", listen)]
    return [("v4", v4)] if v4 else []
for ib in c.get("inbounds", []):
    ss = ib.get("streamSettings") or {}
    st = ib.get("settings") or {}
    for kind, h in hosts_of(ib.get("listen") or ""):
        hh = f"[{h}]" if ":" in h else h
        if ib.get("protocol") == "vless" and ss.get("security") == "reality":
            rs = ss["realitySettings"]
            out = subprocess.run([xb, "x25519", "-i", rs["privateKey"]], capture_output=True, text=True).stdout
            pbk = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.lower().startswith(("public", "password"))), "")
            for cl in st.get("clients", []):
                q = {"encryption": "none", "flow": cl.get("flow", ""), "security": "reality",
                     "sni": (rs.get("serverNames") or [""])[0], "fp": "chrome", "pbk": pbk,
                     "sid": (rs.get("shortIds") or [""])[0], "spx": "/", "type": ss.get("network", "tcp")}
                q = {k: v for k, v in q.items() if v != ""}
                label = f"{name}-TCP-VLESS-IP{kind}"
                print(f"[VLESS-{kind}] vless://{cl['id']}@{hh}:{ib['port']}?{u.urlencode(q)}#{u.quote(label)}")
        elif ib.get("protocol") == "hysteria":
            ts = ss.get("tlsSettings") or {}
            crt = ((ts.get("certificates") or [{}])[0]).get("certificateFile", "")
            der = subprocess.run(["openssl", "x509", "-in", crt, "-outform", "der"], capture_output=True).stdout if crt else b""
            import hashlib
            pin = hashlib.sha256(der).hexdigest() if der else ""
            obfs = next((m.get("settings", {}).get("password", "") for m in (ss.get("finalmask") or {}).get("udp", [])
                         if m.get("type") == "salamander"), "")
            for cl in st.get("clients", []) or st.get("users", []):
                q = {"sni": ts.get("serverName", ""), "alpn": ",".join(ts.get("alpn") or []), "insecure": "0", "pinSHA256": pin}
                if obfs: q.update({"obfs": "salamander", "obfs-password": obfs})
                q = {k: v for k, v in q.items() if v != ""}
                m=(ss.get("hysteriaSettings") or {}).get("masquerade") or {}
                label=(f"{ts['serverName']}-UDP-HY2-IP{kind}" if ts.get("serverName") and m.get("type")=="string" and m.get("statusCode")==404 else f"{name}-HY2-{kind}")
                print(f"[HY2-{kind}] hysteria2://{u.quote(cl['auth'], safe='')}@{hh}:{ib['port']}/?{u.urlencode(q)}#{u.quote(label)}")
PY
  chmod 600 "$LINKS"
}

save_state() {
  umask 077
  [[ "$INBOUND_TAGS" =~ ^[a-zA-Z0-9_.,:-]+$ ]] || die "入站标签不合法"
  {
    printf 'PANEL_PORT=%q\nIN_PORT=%q\nSNI=%q\nLANDING_TYPE=%q\nNODE_NAME=%q\nINBOUND_IDS=%q\n' "$PANEL_PORT" "$IN_PORT" "$SNI" "$LANDING_TYPE" "$NODE_NAME" "$INBOUND_IDS"
    printf 'INBOUND_TAGS=%s\n' "$INBOUND_TAGS"
  } > "$STATE"
}

# 新机只开放节点端口；面板和本机网页不对公网开放。
open_firewall() {
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$1"/tcp >/dev/null; ufw allow "$1"/udp >/dev/null
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$1"/tcp; firewall-cmd -q --permanent --add-port="$1"/udp
    firewall-cmd -q --add-port="$1"/tcp; firewall-cmd -q --add-port="$1"/udp
  fi
}

show_panel() {
  green "面板只监听 127.0.0.1:${PANEL_PORT}/${WEB_PATH}/"
  echo "可以另开独立的公网 HTTPS 入口；也可以通过 SSH 转发访问本机面板。"
}

panel_choice() {
  local choice ipline
  echo "公网 HTTPS 管理面板（原账号密码，不修改四条节点）"
  echo "  0. 保持当前设置（新机默认不开放）"
  echo "  1. 公网 HTTPS，用原面板登录"
  echo "  2. 公网 HTTPS，只让指定公网 IP 访问登录页"
  echo "  3. 查看当前入口"
  echo "  4. 关闭公网面板，保留服务器本机访问"
  ask choice "请选择 [0]: " 0
  case "$choice" in
    0) return 0 ;;
    1) python3 "$RELAY_DIR/relay-panel.py" enable --login ;;
    2) ask ipline "允许登录的公网 IP（多个用空格隔开）: "
       local ips=() opts=() x; read -r -a ips <<<"$ipline"
       for x in "${ips[@]}"; do opts+=(--allow-ip "$x"); done
       [[ ${#opts[@]} -gt 0 ]] || die "白名单不能为空"
       python3 "$RELAY_DIR/relay-panel.py" enable "${opts[@]}" ;;
    3) python3 "$RELAY_DIR/relay-panel.py" status ;;
    4) python3 "$RELAY_DIR/relay-panel.py" disable ;;
    *) die "无效选择" ;;
  esac
}

# ---------- 1. 初始化 ----------
do_init() {
  local user pass pass2 setup
  [[ ! -e "$XUI_BIN" && ! -e /etc/x-ui/x-ui.db ]] || die "已有面板：禁止重新初始化。保留 UDP 迁移请用 tcp-plan / tcp-apply。"
  ask user "面板账号: "; [[ -n "$user" ]] || die "账号不能为空"
  read -r -s -p "面板密码: " pass </dev/tty; echo
  read -r -s -p "再输一次密码: " pass2 </dev/tty; echo
  [[ -n "$pass" && "$pass" == "$pass2" ]] || die "两次密码不一致或为空"
  ask PANEL_PORT "面板端口 [8888]: " 8888
  ask IN_PORT "入站端口 [443]: " 443
  [[ "$IN_PORT" =~ ^[1-9][0-9]{0,4}$ && "$PANEL_PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( IN_PORT <= 65535 && PANEL_PORT <= 65535 )) || die "端口必须在 1 到 65535 之间"
  [[ "$IN_PORT" != "$PANEL_PORT" && "$IN_PORT" != 80 && "$IN_PORT" != 8443 && "$PANEL_PORT" != 80 && "$PANEL_PORT" != 8443 ]] || die "端口冲突（80 用于续证；8443 为本机静态页）"
  prepare_new_web
  ask_landing
  ask NODE_NAME "节点名称（回车 = 按出口 IP 的国家地区自动命名，如 美国加利福尼亚）: "
  [[ -n "$(local_ip6 || true)" ]] || yellow "本机没有可用 IPv6，只建 IPv4 入站；要双栈请先给空服务器配置公网 IPv6。"

  # 只在空服务器初始化，绝不清空已有面板。

  port_busy "$IN_PORT" && die "入站端口 $IN_PORT 已被占用：$(ss -Hltnp "sport = :$IN_PORT" | head -1)"
  ss -Hlun "sport = :$IN_PORT" | grep -q . && die "入站端口 $IN_PORT 的 UDP 已被占用：$(ss -Hlunp "sport = :$IN_PORT" | head -1)"
  port_busy "$PANEL_PORT" && die "面板端口 $PANEL_PORT 已被占用"

  green "安装 3x-ui $XUI_VERSION ..."
  setup=$(mktemp -d /run/relay-installer.XXXXXXXX)
  curl -fLs -m 60 "https://raw.githubusercontent.com/MHSanaei/3x-ui/${XUI_VERSION}/install.sh" -o "$setup/install.sh" || die "下载 3x-ui 安装脚本失败"
  local ui_hash="${RELAY_XUI_INSTALL_SHA256:-}"
  [[ -n "$ui_hash" || "$XUI_VERSION" != v3.11.0 ]] || ui_hash=9684bae4f769b2127fcceceb4f200045657381311867529e6ba76c6b58e2825d
  [[ -n "$ui_hash" ]] || die "非默认面板版本需要提供 RELAY_XUI_INSTALL_SHA256"
  verify_file "$setup/install.sh" "$ui_hash"
  XUI_NONINTERACTIVE=1 XUI_USERNAME="$user" XUI_PASSWORD="$pass" XUI_PANEL_PORT="$PANEL_PORT" \
    XUI_WEB_BASE_PATH="$WEB_PATH" XUI_SSL_MODE=none XUI_ENABLE_FAIL2BAN=false \
    bash "$setup/install.sh" "$XUI_VERSION" </dev/null >/dev/null 2>&1 \
    || { retire_tmp "$setup"; die "3x-ui 安装器失败；上游输出含凭据，不记录"; }
  retire_tmp "$setup" /etc/x-ui/install-result.env
  # 明确新面板的管理监听地址。
  "$XUI_BIN" setting -username "$user" -password "$pass" -port "$PANEL_PORT" -webBasePath "$WEB_PATH" >/dev/null
  "$XUI_BIN" setting -listenIP 127.0.0.1 >/dev/null
  unset pass pass2
  systemctl restart x-ui; wait_panel
  api_init

  install_xray

  # 不接受外部回落；本机站点已经通过 TLS 和内容校验。
  self_test || die "出口校验失败；未创建或开放节点入站"

  if [[ -z "$NODE_NAME" ]]; then
    [[ -z "${EXIT_IP:-}" ]] || NODE_NAME=$(region_name "$EXIT_IP" || true)
    NODE_NAME=${NODE_NAME:-节点}
  fi
  green "节点名称：$NODE_NAME"

  install -d -m 700 "/etc/x-ui/relay-udp-identity/$SNI"
  HY2_CRT="/etc/x-ui/relay-udp-identity/$SNI/node.crt"
  HY2_KEY="/etc/x-ui/relay-udp-identity/$SNI/node.key"
  local r ip6 proto kind listen
  INBOUND_IDS=""; INBOUND_TAGS=""
  ip6=$(local_ip6 || true)
  for proto in vless hy2; do
    for kind in v4 v6; do
      if [[ $kind == v4 ]]; then listen=$(local_ip4); else listen=$ip6; [[ -n "$ip6" ]] || continue; fi
      r=$(add_inbound "$proto" "$kind" "$listen")
      INBOUND_IDS="${INBOUND_IDS:+$INBOUND_IDS }${r%% *}"; INBOUND_TAGS="${INBOUND_TAGS:+$INBOUND_TAGS,}${r#* }"
    done
  done

  apply_routing "$INBOUND_TAGS"
  restart_xray
  open_firewall "$IN_PORT" "$PANEL_PORT"
  save_state
  echo; show_panel
  echo "节点链接（同时保存在 $LINKS）："; print_links
  yellow "每条链接是独立节点（密钥各不相同）。都导入客户端，哪条能用就用哪条。"
  green "新机入站已创建，请从客户端验收每个地址族。"
  panel_choice
}

# ---------- 2. 更换落地 ----------
do_landing() {
  [[ -f "$STATE" ]] || die "这台机器不是用本脚本初始化的，请先选「初始化」"
  api_init
  ask_landing
  apply_routing "$INBOUND_TAGS"
  restart_xray
  sed -i "s/^LANDING_TYPE=.*/LANDING_TYPE=$LANDING_TYPE/" "$STATE"
  self_test
}

main() {
  require_bundle
  case "${1:-}" in
    dns) shift; exec python3 "$RELAY_DIR/relay-dns.py" "$@" ;;
    web) shift; need_root; exec python3 "$RELAY_DIR/relay-web.py" "$@" ;;
    panel) shift; need_root; panel_choice; return ;;
    panel-plan|panel-enable|panel-status|panel-disable)
      local action="${1#panel-}"; shift; need_root; exec python3 "$RELAY_DIR/relay-panel.py" "$action" "$@" ;;
    tcp-plan) shift; need_root; local arg; for arg in "$@"; do [[ "$arg" != --apply ]] || die "tcp-plan 不允许 --apply"; done; exec python3 "$RELAY_DIR/relay-tcp.py" "$@" ;;
    tcp-apply) shift; need_root; exec python3 "$RELAY_DIR/relay-tcp.py" "$@" --apply ;;
    udp-plan) shift; need_root; local arg; for arg in "$@"; do [[ "$arg" != --apply ]] || die "udp-plan 不允许 --apply"; done; exec python3 "$RELAY_DIR/relay-udp.py" "$@" ;;
    udp-apply) shift; need_root; exec python3 "$RELAY_DIR/relay-udp.py" "$@" --apply ;;
  esac
  need_root; ensure_deps
  case "${1:-}" in
    init) do_init; exit ;; landing) do_landing; exit ;; toggle) do_toggle; exit ;; links) print_links; exit ;;
  esac
  echo "==== 一键中转（3x-ui $XUI_VERSION / Xray $XRAY_VERSION）===="
  echo "1. 初始化"
  echo "2. 更换落地"
  echo "3. 开关入站（VLESS / HY2 × IPv4 / IPv6）"
  echo "4. 输出链接"
  echo "5. 公网 HTTPS 管理面板（沿用面板账号）"
  local c; ask c "请选择: "
  case "$c" in 1) do_init ;; 2) do_landing ;; 3) do_toggle ;; 4) print_links ;; 5) panel_choice ;; *) die "无效选择" ;; esac
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
