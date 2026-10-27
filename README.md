# relay：一键中转 / 落地

登录服务器（root），粘贴：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/woshizhangxiaohao/relay/main/relay.sh)
```

| 菜单 | 作用 |
|---|---|
| 1. 初始化 | 安装 3x-ui v3.11.0 + Xray v26.6.27，建好 4 个入站（VLESS+Reality、Hysteria2 × IPv4、IPv6）、落地出站和转发规则，输出节点链接 |
| 2. 更换落地 | 换 SOCKS5 / 落地机链接，或改成不需要落地；节点链接不变 |
| 3. 开关入站 | 4 个入站任意开关一个（面板里点开关效果相同） |
| 4. 输出链接 | 读当前配置重新打印链接，不改任何东西（非本脚本装的 3x-ui 也能用） |

也可以直接带子命令：`relay.sh init | landing | toggle | links`。

## 默认值

- 面板端口 8888，访问路径 `/zxh/`，账号密码运行时输入（脚本不保存）
- 入站端口 443，伪装网站 `calmerry.com`（初始化时会检查 TLS 1.3 + HTTP/2）
- 版本可用环境变量改：`RELAY_XUI_VERSION=v3.11.0`、`RELAY_XRAY_VERSION=v26.6.27`

## 规则

- 初始化建 4 个入站：VLESS+Reality（TCP）和 Hysteria2（UDP）各 IPv4、IPv6 一个，同一端口（默认 443），各用独立密钥，互相不能关联；本机没有公网 IPv6 时只建 IPv4 的两个。全部转发到同一个落地。
- Hysteria2：自签证书 + 客户端固定证书指纹（pinSHA256，不用 insecure）、salamander 混淆、伪装成伪装网站、只用 TLS 1.3。证书在 `/etc/x-ui/relay-hy2.crt`。
- 节点名默认按出口 IP 的国家地区自动生成（如 `美国加州-HY2-v4`），初始化时可改。
- 出口只用 IPv4（direct 出站 `UseIPv4`），网站看到的永远是本机或落地的 IPv4。
- 有落地时，本入站的流量全部走落地；落地不通就连不上，不会改用本机 IP 出去。
- 「不需要落地」的机器输出的 IPv4 链接，可以直接粘贴给另一台中转机当落地。
- 初始化在已装 3x-ui 的机器上会先把 `/etc/x-ui` 和 `/usr/local/x-ui` 打包备份到 `/root/relay-backup-时间.tar.gz`，再清空重建。

## 落地格式

- SOCKS5：`IP|端口|账号|密码|到期`、`IP:端口:账号:密码` 或 `socks5://账号:密码@IP:端口`
- 自己的落地机：那台机器「输出链接」得到的 `vless://` 链接

## 本机文件

- `/etc/x-ui/relay.env`：脚本记录的入站 ID、端口、落地类型等（不含密码）
- `/etc/x-ui/relay-links.txt`：最近一次输出的节点链接（仅 root 可读）
