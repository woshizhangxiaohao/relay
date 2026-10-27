#!/usr/bin/env bash
# 一键中转 / 落地：安装 3x-ui 并配好 VLESS + Reality 入站、落地出站和转发规则。
# 用法：bash <(curl -Ls https://raw.githubusercontent.com/woshizhangxiaohao/relay/main/relay.sh)
# 本脚本不含任何密码或密钥；面板账号密码运行时输入，只写进本机 3x-ui。
set -uo pipefail

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

need_root() { [[ $EUID -eq 0 ]] || die "请用 root 运行"; }

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
    || die "接口 $p 返回失败：$(printf '%s' "$out" | head -c 300)"
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
  python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extract("xray", sys.argv[2])' "$tmp/x.zip" "$tmp" || die "解压 Xray 失败"
  systemctl stop x-ui
  install -m 755 "$tmp/xray" "$XUI_DIR/bin/xray-linux-${bin_arch}"
  rm -rf "$tmp"
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
  echo "是否需要落地？"
  echo "  1. 不需要（本机直接出去，也可以给别的中转机当落地）"
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

# 改 Xray 模板：出口只用 IPv4；本入站全部走落地（无落地则走 direct）；落地不通不回退
apply_routing() {
  local inbound_tag="$1" tpl new
  tpl=$(jget "$(api POST xray/)" obj.xraySetting)
  new=$(python3 - "$tpl" "$inbound_tag" "${LANDING_JSON:-}" "$LANDING_TAG" "$RULE_TAG" <<'PY'
import json, sys
c = json.loads(sys.argv[1]); itag, landing, ltag, rtag = sys.argv[2:6]
obs = [o for o in c.get("outbounds", []) if o.get("tag") != ltag]
direct = next((o for o in obs if o.get("protocol") == "freedom" and o.get("tag") == "direct"), None)
if not direct:
    direct = {"tag": "direct", "protocol": "freedom", "settings": {}}; obs.insert(0, direct)
direct.setdefault("settings", {})["domainStrategy"] = "UseIPv4"
if landing: obs.append(json.loads(landing))
c["outbounds"] = obs
r = c.setdefault("routing", {}); rules = [x for x in r.get("rules", []) if x.get("ruleTag") != rtag]
mine = {"type": "field", "ruleTag": rtag, "inboundTag": [itag], "outboundTag": ltag if landing else "direct"}
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
  ip=$(curl -s -m 15 --socks5-hostname "127.0.0.1:$sp" https://api64.ipify.org)
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; rm -rf "$tmp"
  if [[ -z "$ip" ]]; then red "自检失败：经出口访问外网不通（落地不可用？）"; return 1; fi
  if [[ "$ip" == *:* ]]; then red "自检警告：出口用的是 IPv6（$ip），网站看到的不是 IPv4"; return 1; fi
  if [[ "$LANDING_TYPE" != none && "$ip" == "$(pub_ip4)" ]]; then red "自检失败：出口是本机 IP，没走落地"; return 1; fi
  green "自检通过：出口 IP = $ip"
}

# ---------- 入口类型 ----------
ask_entry() {
  echo "入口用哪种？"
  echo "  1. 仅 IPv4（默认）"
  echo "  2. 仅 IPv6（IPv4 被封时用）"
  echo "  3. 两个都开"
  local c; ask c "请选择 [1]: " 1
  case "$c" in 1) ENTRY=v4 ;; 2) ENTRY=v6 ;; 3) ENTRY=both ;; *) die "无效选择" ;; esac
  if [[ $ENTRY != v4 && -z "$(local_ip6)" ]]; then die "本机没有公网 IPv6，请先在服务商面板开启 IPv6"; fi
}
entry_listen() {
  case "$ENTRY" in
    v4) local_ip4 ;;
    v6) local_ip6 ;;
    both) echo "" ;;
  esac
}

# ---------- 输出链接（直接读运行中的 Xray 配置，脚本外装的 3x-ui 也能用） ----------
print_links() {
  [[ -f "$XRAY_CONF" ]] || die "找不到 $XRAY_CONF，3x-ui 没装？"
  local xb v4 v6 name
  xb=$(xray_bin); v4=$(pub_ip4); v6=$(local_ip6); name="${RELAY_NAME:-$(hostname -s)}"
  python3 - "$XRAY_CONF" "$xb" "$v4" "$v6" "$name" "$(local_ip4)" <<'PY' | tee "$LINKS"
import json, subprocess, sys, urllib.parse as u
conf, xb, v4, v6, name, lip4 = sys.argv[1:7]
c = json.load(open(conf))
for ib in c.get("inbounds", []):
    ss = ib.get("streamSettings") or {}
    if ib.get("protocol") != "vless" or ss.get("security") != "reality": continue
    rs = ss["realitySettings"]
    pub = subprocess.run([xb, "x25519", "-i", rs["privateKey"]], capture_output=True, text=True).stdout
    pbk = next((l.split(":", 1)[1].strip() for l in pub.splitlines() if l.lower().startswith(("public", "password"))), "")
    listen = ib.get("listen") or ""
    hosts = []
    if listen in ("", "0.0.0.0", "::"):
        if v4: hosts.append(("v4", v4))
        if v6: hosts.append(("v6", v6))
    elif ":" in listen: hosts.append(("v6", listen))
    elif v4: hosts.append(("v4", v4))
    for cl in ib["settings"].get("clients", []):
        for kind, h in hosts:
            q = {"encryption": "none", "flow": cl.get("flow", ""), "security": "reality",
                 "sni": (rs.get("serverNames") or [""])[0], "fp": "chrome", "pbk": pbk,
                 "sid": (rs.get("shortIds") or [""])[0], "spx": "/", "type": ss.get("network", "tcp")}
            q = {k: v for k, v in q.items() if v != ""}
            hh = f"[{h}]" if ":" in h else h
            print(f"[{kind}] vless://{cl['id']}@{hh}:{ib['port']}?{u.urlencode(q)}#{u.quote(name + '-' + kind)}")
PY
  chmod 600 "$LINKS"
}

save_state() {
  umask 077
  cat > "$STATE" <<EOF
PANEL_PORT=$PANEL_PORT
IN_PORT=$IN_PORT
SNI=$SNI
ENTRY=$ENTRY
LANDING_TYPE=$LANDING_TYPE
INBOUND_ID=$INBOUND_ID
INBOUND_TAG=$INBOUND_TAG
EOF
}

open_firewall() {
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$1"/tcp >/dev/null; ufw allow "$2"/tcp >/dev/null
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$1"/tcp; firewall-cmd -q --permanent --add-port="$2"/tcp; firewall-cmd -q --reload
  fi
}

show_panel() {
  local h; h=$(pub_ip4); [[ -n "$h" ]] || h="[$(local_ip6)]"
  green "面板：http://${h}:${PANEL_PORT}/${WEB_PATH}/"
}

# ---------- 1. 初始化 ----------
do_init() {
  local user pass pass2
  ask user "面板账号: "; [[ -n "$user" ]] || die "账号不能为空"
  read -r -s -p "面板密码: " pass </dev/tty; echo
  read -r -s -p "再输一次密码: " pass2 </dev/tty; echo
  [[ -n "$pass" && "$pass" == "$pass2" ]] || die "两次密码不一致或为空"
  ask PANEL_PORT "面板端口 [8888]: " 8888
  ask IN_PORT "入站端口 [443]: " 443
  ask SNI "伪装网站 [calmerry.com]: " calmerry.com
  ask_landing
  ask_entry

  if [[ -x "$XUI_BIN" ]]; then
    yellow "本机已装 3x-ui。初始化会清空现有面板配置（节点、账号都会重建）。"
    local ok; ask ok "输入 yes 继续（会先备份）: "
    [[ "$ok" == yes ]] || die "已取消"
    local bk="/root/relay-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar czf "$bk" /etc/x-ui "$XUI_DIR" 2>/dev/null || die "备份失败"
    green "已备份到 $bk"
    systemctl stop x-ui
    rm -f /etc/x-ui/x-ui.db "$STATE" "$LINKS"
  fi
  port_busy "$IN_PORT" && die "入站端口 $IN_PORT 已被占用：$(ss -Hltnp "sport = :$IN_PORT" | head -1)"
  port_busy "$PANEL_PORT" && die "面板端口 $PANEL_PORT 已被占用"

  green "安装 3x-ui $XUI_VERSION ..."
  curl -fLs -m 60 "https://raw.githubusercontent.com/MHSanaei/3x-ui/${XUI_VERSION}/install.sh" -o /tmp/xui-install.sh || die "下载 3x-ui 安装脚本失败"
  XUI_NONINTERACTIVE=1 XUI_USERNAME="$user" XUI_PASSWORD="$pass" XUI_PANEL_PORT="$PANEL_PORT" \
    XUI_WEB_BASE_PATH="$WEB_PATH" XUI_SSL_MODE=none XUI_ENABLE_FAIL2BAN=false \
    bash /tmp/xui-install.sh "$XUI_VERSION" </dev/null >/tmp/xui-install.log 2>&1 \
    || { grep -vF -- "$pass" /tmp/xui-install.log > /tmp/xui-install.log.1; mv /tmp/xui-install.log.1 /tmp/xui-install.log
         die "3x-ui 安装失败，日志：/tmp/xui-install.log"; }
  # 官方安装日志会打印明文账号密码，成功后删掉
  rm -f /tmp/xui-install.sh /tmp/xui-install.log /etc/x-ui/install-result.env
  # 已装过的机器安装脚本会沿用旧设置，这里强制写一遍
  "$XUI_BIN" setting -username "$user" -password "$pass" -port "$PANEL_PORT" -webBasePath "$WEB_PATH" >/dev/null
  unset pass pass2
  systemctl restart x-ui; wait_panel
  api_init

  install_xray

  green "检查伪装网站 $SNI ..."
  local scan; scan=$(api POST server/scanRealityTarget --data-urlencode "target=$SNI:443")
  if [[ $(jget "$scan" obj.feasible) != True ]]; then
    red "伪装网站不合格：$(jget "$scan" obj.reason)"
    local go; ask go "仍然继续？[y/N]: " n; [[ "$go" == y ]] || die "已取消，换个伪装网站再来"
  fi

  local keys uuid priv sid body res
  keys=$(api GET server/getNewX25519Cert); priv=$(jget "$keys" obj.privateKey)
  uuid=$(jget "$(api GET server/getNewUUID)" obj.uuid 2>/dev/null || python3 -c 'import uuid;print(uuid.uuid4())')
  sid=$(openssl rand -hex 8)
  body=$(python3 - "$IN_PORT" "$(entry_listen)" "$uuid" "$SNI" "$priv" "$sid" <<'PY'
import json, sys
port, listen, uid, sni, priv, sid = sys.argv[1:7]
print(json.dumps({"enable": True, "remark": "relay", "listen": listen, "port": int(port), "protocol": "vless",
  "expiryTime": 0, "total": 0,
  "settings": {"clients": [{"id": uid, "email": "relay", "flow": "xtls-rprx-vision", "enable": True}],
               "decryption": "none", "fallbacks": []},
  "streamSettings": {"network": "tcp", "security": "reality", "tcpSettings": {"header": {"type": "none"}},
    "realitySettings": {"show": False, "xver": 0, "target": f"{sni}:443", "serverNames": [sni],
                        "privateKey": priv, "shortIds": [sid]}},
  "sniffing": {"enabled": False}}))
PY
)
  res=$(api POST inbounds/add -H 'Content-Type: application/json' --data "$body")
  INBOUND_ID=$(jget "$res" obj.id); INBOUND_TAG=$(jget "$res" obj.tag)

  apply_routing "$INBOUND_TAG"
  restart_xray
  open_firewall "$IN_PORT" "$PANEL_PORT"
  save_state
  self_test || yellow "（自检没过，配置已保存，可以用「更换落地」重新设置）"
  echo; show_panel
  echo "节点链接（同时保存在 $LINKS）："; print_links
  [[ $ENTRY == both ]] && yellow "两条都导入 Shadowrocket，建「故障转移」策略组：IPv4 排第一、IPv6 排第二。"
}

# ---------- 2. 更换落地 ----------
do_landing() {
  [[ -f "$STATE" ]] || die "这台机器不是用本脚本初始化的，请先选「初始化」"
  api_init
  ask_landing
  apply_routing "$INBOUND_TAG"
  restart_xray
  sed -i "s/^LANDING_TYPE=.*/LANDING_TYPE=$LANDING_TYPE/" "$STATE"
  self_test
}

# ---------- 3. 切换入口类型 ----------
do_entry() {
  [[ -f "$STATE" ]] || die "这台机器不是用本脚本初始化的，请先选「初始化」"
  api_init
  ask_entry
  local ib body
  ib=$(jget "$(api GET "inbounds/get/$INBOUND_ID")" obj)
  body=$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); d["listen"]=sys.argv[2]
d.pop("clientStats", None); print(json.dumps(d))' "$ib" "$(entry_listen)")
  api POST "inbounds/update/$INBOUND_ID" -H 'Content-Type: application/json' --data "$body" >/dev/null
  restart_xray
  sed -i "s/^ENTRY=.*/ENTRY=$ENTRY/" "$STATE"
  echo "新的节点链接："; print_links
}

main() {
  need_root; ensure_deps
  case "${1:-}" in
    init) do_init; exit ;; landing) do_landing; exit ;; entry) do_entry; exit ;; links) print_links; exit ;;
  esac
  echo "==== 一键中转（3x-ui $XUI_VERSION / Xray $XRAY_VERSION）===="
  echo "1. 初始化"
  echo "2. 更换落地"
  echo "3. 切换入口类型（仅IPv4 / 仅IPv6 / 都开）"
  echo "4. 输出链接"
  local c; ask c "请选择: "
  case "$c" in 1) do_init ;; 2) do_landing ;; 3) do_entry ;; 4) print_links ;; *) die "无效选择" ;; esac
}
main "$@"
