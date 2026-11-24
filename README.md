# relay：节点安装与分阶段迁移

每台节点使用自己的子域名和证书，网页来自同一份 [site/index.html](site/index.html)。VLESS/REALITY 的未认证连接只能到本机 `127.0.0.1:8443`；新装 Hysteria2 时，未认证请求只收到本机 404。静态网页不含节点链接、外部资源或反向代理。

**家宽落地是可选项。** 安装时在「落地出口」直接回车，默认走节点服务器自己的公网出口。双栈服务器即使没有家宽，也可以使用 TCP/VLESS 和 UDP/HY2 的 IPv4、IPv6 四条入站。以后有了家宽或其他指定出口，再在维护窗口执行 `bash relay.sh landing` 配置即可；不必重装节点。

请使用**完整且同一版本的仓库目录**，不要只下载或执行单独的 `relay.sh`。可以直接把本地验收过的目录复制到新机器，或运行 `python3 tools/package.py` 生成含网页及公网面板工具的完整安装包（默认放在管理电脑的下载目录，仅包含清单列出的源码、测试和文档）。复制到新机后解压，在 `relay/` 目录运行。Git 发布版建议固定到已审核的提交或标签。下面的命令均在仓库目录中运行。

## 新服务器

1. 准备 Linux 服务器和专用子域名，每台用不同的名称。需要 systemd、Python 3、curl、OpenSSL、iproute2；当前实现面向 amd64/arm64。云安全组允许入站 TCP 80（签发和续期）、TCP 443 与 UDP 443（节点，可自选节点端口）。TCP 80 必须可供 HTTP-01 临时使用；**已有网站占用 80 时脚本会停止，不会接管它**。管理面板和本机 TCP 8443 不对外开放。
2. 在**管理电脑**创建灰云 DNS。把下例变量换成本节点的信息；IPv4-only 机器省略 `--ipv6`，不要给它留旧的 AAAA。

   ```bash
   bash relay.sh dns --zone "$ZONE" --host "$NODE" --ipv4 "$PUBLIC_V4" --ipv6 "$PUBLIC_V6" --token-file "$CF_ENV"
   # 核对预览后，给同一条命令加 --apply
   ```

   `CF_ENV` 是受保护的环境文件路径，工具只读取其中的 `CLOUDFLARE_API_TOKEN`。也可使用已有环境变量或手动添加 A/AAAA（仅 DNS）。工具不改其他主机名，不替换冲突记录，也不把 token 上传到节点。新机上直接执行 `init` 时若已有该环境变量，也可按提示创建 DNS。
3. 在**空白节点**以 root 执行 `bash relay.sh init`。输入节点域名和面板信息，脚本会准备本机网页、申请证书、验证证书与页面、安装面板与核心并创建入站。在双栈节点生成 TCP IPv4、TCP IPv6、HY2 IPv4、HY2 IPv6；没有本地 IPv6 接口时只生成 IPv4 入站。**检测到已有 3x-ui 或数据库时，拒绝初始化。**
4. 从客户端分别导入并实测要使用的线路。端口在监听、出口自检成功或网页能打开，都不能单独代替用有效节点凭据的端到端验收。节点链接包含实际凭据，勿提交到 Git、公开网页或工单。

## 已有服务器：保留正在使用的 UDP，只换 TCP

适用于本安装器管理的、启用了 Xray 本机 HandlerService API 的 VLESS/REALITY。不会重装面板，不改 UDP 入站，不重启共享的 Xray 或 x-ui。没有可用的热更新接口、没有找到对应标签、证书或本机页面不通过时会停止。旧 TCP 会逐条短暂断开。

```bash
bash relay.sh web "$NODE"        # 建/更新网页、证书和独立的续期任务
bash relay.sh tcp-plan "$NODE"   # 临时在回环地址预演，不动现有入站
bash relay.sh tcp-apply "$NODE"  # 备份后持久化配置并逐条热替换
```

默认节点端口是 443；需要时给 TCP 子命令传 `--port`，NAT 节点可用 `--public-v4` 指定分享链接的公网地址。程序使用 SQLite 在线备份（含 WAL 中已提交的数据），保存原运行配置，仅更新 relay 管理的现有 TCP 标签；保留其身份凭据、端口、路由和落地。它同时检查 UDP 的数据库配置、进程和内核套接字是否保持不变。备份在 root 专用的 `/root/relay-tcp-backup-时间戳/`。

新 TCP 链接在 `/etc/x-ui/relay-tcp-links.txt`（权限 600，每行一条），导入名称为 `节点域名-TCP-VLESS-IPv4` 和 `节点域名-TCP-VLESS-IPv6`。链接最后的名称只是客户端备注，不参与连接。现有综合导出文件仅替换匹配到的 TCP 链接。先把新 TCP 添加为独立节点，在实际常用设备、网络上试用；**确认能切到 TCP 之后，再另行安排旧 UDP 的迁移。不要用 `init` 或全局重启来完成分阶段切换。** 如某个 TCP 标签热替换失败，保留用户正在用的 UDP，先核对该标签，勿自动恢复向第三方网站转发的旧配置。

## 已切换到 TCP：用新 UDP 替下旧 UDP

先在日常设备上切到已验收的 TCP 节点，保持它连接。迁移只接管本脚本管理的 HY2 入站，不重启共享的 x-ui 或 Xray。沿用原来的端口、面板账户、流量记录和路由；使用本机新域名的独立固定指纹证书、每条各自的新传输口令。旧 UDP 链接因此失效，未认证的请求只返回本机 404，不访问第三方。

```bash
bash relay.sh udp-plan "$NODE"
bash relay.sh udp-apply "$NODE" --family 6  # 先测新 IPv6 节点
bash relay.sh udp-apply "$NODE" --family 4  # IPv6 测通后再换 IPv4
```

只有 IPv4 的节点只执行 `--family 4`；NAT 服务器可传 `--public-v4`。预演使用回环上的独立测试实例。实际切换前会做 SQLite 在线备份，运行中逐条检查 Xray 的 404 配置、另一条 UDP 的套接字和同一条已建立的 TCP 连接。相同配置重复执行不会轮换口令或替换正在使用的监听。每次从 `/etc/x-ui/relay-udp-links.txt` 取已完成的链接（权限 600），在外部客户端验证；两条都通过后再从手机删除旧条目。若某一条未热加载成功，继续使用 TCP，核对输出中的备份路径，不要重启共享核心或恢复第三方回落。

## 导出 Clash / Mihomo：大陆直连，其他走代理

支持 **Clash Verge Rev、Mihomo Party 和其他使用 Mihomo / Clash.Meta 内核的客户端**。旧版 Clash 内核不支持这套 REALITY / HY2 节点。导出器只读取本地分享链接，不改变服务端、系统代理或正在使用的客户端；无需在线订阅转换。

在节点上运行 `bash relay.sh clash`（菜单 6）。默认读取 `/etc/x-ui/relay-links.txt`，把私有配置写到 `/etc/x-ui/relay-clash.yaml`。如果刚在面板里改过节点，先刷新本地分享链接；已有两份独立的 TCP、UDP 导出时，也可以在管理电脑执行：

```bash
bash relay.sh clash \
  --links "$HOME/Downloads/TCP节点.txt" \
  --links "$HOME/Downloads/UDP节点.txt" \
  --output "$HOME/Downloads/我的节点-Clash.yaml"
```

替换为自己的文件名。在客户端选**导入本地配置文件**，启用该配置并选 **规则（Rule）模式**。「节点选择」里可以切换四条线路，首次默认 TCP/VLESS IPv4，之后会记住所选节点；只有 IPv4 的服务器也能按实际节点导出。配置默认只在本机提供代理端口 7890，可用 `--mixed-port` 更改。不要同时让多个客户端接管系统代理或 TUN。

| 访问目的地 | 走哪里 |
|---|---|
| 本机、局域网、`.cn` 及大陆域名规则命中 | 直连（DIRECT） |
| 目的 IPv4 或 IPv6 命中大陆 IP 网段 | 直连（DIRECT）；未被域名表收录的域名也可以解析后按 IP 判断 |
| 其余目的地 | 走「节点选择」当前选中的节点 |

默认在**生成时**从 [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat) 的同一提交读取大陆域名和 IPv4/IPv6 网段，直接内置进一个 YAML。导入时不需要再下载规则；要更新快照，重新导出即可。大陆域名优先用国内 DNS，其余默认使用经所选节点的加密 DNS。VLESS 保留 Vision、REALITY 公钥和 short-id；HY2 保留认证、传输口令和证书 SHA-256 指纹，不关闭证书校验。

- 想要小文件并自动更新：导出时加 `--rules online`。Mihomo 会通过所选节点获取两个 MRS 规则集并定期更新；首次加载需要节点能够连接规则源。
- 无法在导出设备访问 GitHub：将上游同一版本的 `geo/geosite/cn.list`、`geo/geoip/cn.list` 分别保存为 `geosite-cn.list`、`geoip-cn.list`，用 `--rules-dir 本地目录` 生成离线配置。自动下载时也可用 `--rule-ref 完整提交号` 固定版本。
- **YAML 本身就是节点凭据**，文件权限为 600，不要放在公共网站或仓库。重复运行不会覆盖不同内容；明确使用 `--replace` 才会先做私有备份再替换。安装包只包含通用导出器和合成测试，不包含任何人的节点配置。

## 网页、证书与续期

- 网页只维护 `site/index.html`；每台服务器保存本地副本。要更新页面，在该机重新运行 `bash relay.sh web "$NODE"`。普通访问者看到「服务在线。」；节点使用者的认证信息永远不在页面里。
- 独立的 Caddy 只监听回环 TCP 8443，不监听 UDP，不反向代理。systemd 对该进程限制公网出站。新机的 REALITY 始终使用这个固定的回环目标；证书的域名、服务端名称和 TCP 客户端的 SNI 一致。
- Let’s Encrypt 通过 TCP 80 的 HTTP-01 验证，不需要把 Cloudflare DNS 凭据放在服务器。续期由 `relay-local-cert-renew.timer` 负责，仅在换证时重启静态网页服务；不会重启 Xray 或面板。独立的证书状态和私钥在 `/var/lib/relay-local-cert/`（仅 root 可读）。查看定时器：`systemctl list-timers relay-local-cert-renew.timer`。
- 新机 HY2 使用独立的自签证书，并在客户端固定 SHA-256 指纹，避免网页证书续期导致 HY2 指纹变化。**更新网页或现有 TCP 不会替换老 HY2 的证书。**

## 其他命令

### 浏览器直接访问管理面板（可选）

新机初始化的最后会询问「公网 HTTPS 管理面板」；已有节点用 `bash relay.sh panel` 或菜单 5，不必重装。选「公网 HTTPS，用原面板登录」，之后在任意设备打开 **`https://节点域名:9443/`**，会自动跳到这台机器实际的登录路径。每台服务器沿用自己的面板账号和密码；脚本不会创建第二套账号，也不会收集或复制面板密码。已在面板设置里绑定动态验证码的账号，照常使用验证码。

| 命令（在节点上执行） | 用途 |
|---|---|
| `bash relay.sh panel-plan --login` | 只在回环地址预演：验证证书、登录页、未登录保护，不开放公网 |
| `bash relay.sh panel-enable --login` | 允许任意网络到达 HTTPS 登录页；仍需原面板账号密码 |
| `bash relay.sh panel-enable --allow-ip "$ADMIN_IP"` | 固定来源模式，只让指定的公网 IPv4/IPv6 到达登录页；可重复提供 IP |
| `bash relay.sh panel-status` / `bash relay.sh panel-disable` | 查看或关闭本机的公网入口；不影响节点及本机面板 |

默认不开放，端口可用 `--port` 指定。若有主机防火墙或云厂商安全组，另行按选定来源放行**面板的 TCP 端口**；不要开放后端和静态页的端口。公网入口是独立的 Caddy 服务，只将面板路径代理给回环上的 3x-ui；入口供浏览器登录使用，会去掉传入的 Authorization、X-API-Key 和 X-API-Token 请求头。它复用本节点证书，随静态服务的换证重启重新读取证书；不重启 x-ui 或 Xray。不同节点无需共用管理账号或密码。

| 命令 | 用途 |
|---|---|
| `bash relay.sh links` | 输出已启用的节点链接，并刷新仅 root 可读的综合文件 |
| `bash relay.sh landing` | 更换 SOCKS5 / VLESS 落地，或改为直连 |
| `bash relay.sh toggle` | 开关入站；拒绝重新启用仍有外部回落的旧入站 |

`landing` 和 `toggle` 仍可能重启共享内核，需要维护窗口；不是保留当前连接的迁移命令。3x-ui 后端默认仅监听 `127.0.0.1:8888`；公网管理使用上面独立的 HTTPS 入口。新机安装固定 3x-ui v3.11.0、Xray v26.6.27，下载校验的版本与摘要在脚本中；覆盖版本时必须提供对应的受信任摘要。
