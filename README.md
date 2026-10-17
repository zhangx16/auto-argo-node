# Auto Argo Node

通过网页生成一条安装命令，在自己的服务器上部署 **VLESS + WebSocket + Cloudflare 临时隧道**，集中查看节点、编辑参数、测活及卸载。

节点服务器主动连接面板，不需要向面板提供 SSH 密码，也不需要为隧道配置 NAT 入站端口映射。节点侧复用系统 cron，每分钟执行一次 Shell 脚本，检查完毕后退出。

## 功能

- 自定义名称、本地监听端口、优选域名/IP、客户端 HTTPS 端口、UUID 和 WebSocket 路径。
- 一次性安装凭据，有效期 24 小时；安装完成后换成仅对应节点可用的运行凭据。
- 自动同步临时隧道域名及 VLESS 链接，可勾选节点生成订阅，不生成节点二维码。
- 编辑节点参数；修改监听端口时排队执行，确认成功后才更新面板实际端口。
- 连续异常检测、隧道重建、离线提示及面板侧实际 VLESS 转发测试。
- 远端卸载成功后删除面板记录；同机其他节点及仍在使用的共用服务保留。
- 登录密码 + TOTP 两步验证及备用恢复码。
- 配置部署、已部署节点、部署任务从上到下排列；节点和任务模块均支持两层折叠，自动刷新保留展开状态。

## 环境要求

**面板服务器：** Docker Engine、Docker Compose，以及一个节点服务器能访问的 HTTPS 域名。默认只监听 `127.0.0.1:18899`，通过 Nginx 等反向代理提供 HTTPS。

**节点服务器：** root 权限、systemd 或 OpenRC，以及可访问面板、GitHub 和 Cloudflare 的出站网络。

| 项目 | 支持范围 |
| --- | --- |
| 系统适配 | Alpine、Debian、Ubuntu |
| 架构适配 | AMD64、ARM64、ARMv7，依赖上游提供对应二进制 |
| 本地监听 | `127.0.0.1:1024–65535` |
| 客户端端口 | `443`、`2053`、`2083`、`2087`、`2096`、`8443` |
| 节点依赖 | Shell、curl、jq、flock、cron，由安装命令安装必要依赖 |

已在隔离 **Alpine / AMD64 / OpenRC** 环境验证完整安装、真实转发、修复、端口修改及卸载。Debian/systemd 和 ARM 变体已做适配，仍需在对应机器验证。监控脚本不常驻，sing-box 和 cloudflared 本身仍需持续运行。

## 部署面板

```sh
git clone https://github.com/zhangx16/auto-argo-node.git
cd auto-argo-node
cp .env.example .env
```

编辑 `.env`，将 `PANEL_URL` 改为实际访问地址，例如 `https://panel.example.com`。不要填写节点地址；这是面板自己的公开地址。

```sh
docker compose up -d --build
curl -fsS http://127.0.0.1:18899/health
```

构建时会从上游下载并校验固定版本的安装脚本，需要访问 GitHub。应用状态保存在 `./data/`。

配置 HTTPS 反向代理，可参考 [Nginx 示例](docs/nginx.conf.example)。替换域名、证书路径；如果更改了 `.env` 中的 `PANEL_PORT`，同时修改代理目标端口。示例不自动申请证书，请使用已有效的证书或通过自己的证书工具配置 HTTPS。

浏览器登录需要 HTTPS，应用的登录 Cookie 带有 `Secure` 属性。域名和 HTTPS 配置好后访问面板。

### 首次登录

默认账号 `admin`，密码随机生成。在面板服务器本地查看：

```sh
sudo cat data/credentials.txt
```

首次登录按提示绑定验证器，并离线保存备用恢复码。此处的验证器二维码仅用于两步验证，不是节点二维码。`ADMIN_USER`、`ADMIN_PASSWORD` 仅在空数据目录首次初始化时生效，修改 `.env` 不会覆盖已有账号密码。

`data/credentials.txt` 包含首次初始化的明文密码，登录完成并妥善保存后可删除；不要公开该文件、`.env` 或整个 `data/` 目录。

### 配置项

| 变量 | 说明 |
| --- | --- |
| `PANEL_URL` | 必填，节点可访问的面板 HTTPS 地址 |
| `PANEL_BIND` | 宿主机监听地址，默认 `127.0.0.1` |
| `PANEL_PORT` | 宿主机监听端口，默认 `18899` |
| `TZ` | 时区，默认 `Asia/Shanghai` |
| `ADMIN_USER` | 首次初始化账号，默认 `admin` |
| `ADMIN_PASSWORD` | 首次初始化密码，留空自动生成 |
| `ORIGIN_IP` | 旧版 Reality 安装回调可选直连地址，正常 Argo 管理无需填写 |

## 部署和管理节点

1. 在“配置部署”填写参数，点击“生成安装命令”。
2. 在目标节点服务器以 root 执行该命令。
3. 展开“已部署节点”，再点击对应节点查看详情、监控结果及 VLESS 链接。
4. 展开“部署任务”，查看具体任务、执行进度或日志。

优选地址默认留空，直接使用当前临时隧道域名，也可填写自己的优选域名或 IP。优选地址只改变客户端连接目标；TLS SNI 和 WebSocket Host 使用当前临时隧道域名。

- **修改名称、优选地址、客户端端口：** 立即更新面板链接。
- **修改本地监听端口：** 节点下次连接时执行；新隧道验证后停止旧隧道，失败尝试恢复旧配置。临时域名会变化，请重新复制链接。
- **重新生成安装命令：** 使尚未使用的旧命令失效；命令实际执行完成绑定后，旧运行凭据失效。
- **已有旧版 SSH 部署节点：** 先点击“接入监控”，到该节点执行一次接入命令，再通过主动上报方式管理。保留的 SSH API 用于兼容旧部署，不是网页默认流程。
- **删除节点：** 确认后排队卸载对应隧道、监听配置、监控凭据及 cron。收到成功确认后删除记录。离线节点保持“卸载中”，恢复连接后处理；无安装内容的未执行任务可直接删除。正在执行配置的任务需要等待结束。

不要在远端节点尚未卸载时直接销毁面板数据，否则面板会失去继续调度清理的凭据。

## 订阅管理

展开主页“订阅管理”并点击“创建订阅”，填写名称，勾选要导出的已部署节点。支持多个独立订阅，每个订阅只包含明确选择的节点，新部署节点不会自动加入。

| 格式 | 用途 |
| --- | --- |
| 通用 Base64 | Base64 编码的 VLESS URI 列表 |
| 原始 VLESS 链接 | 每行一条，可继续导入 Sub-Store 等工具处理 |
| Clash / Mihomo | 包含节点、选择组和基础全局代理规则的 YAML，需支持 VLESS 的 Mihomo 客户端 |
| sing-box | 包含 mixed 入站、选择出站、DoH DNS 及基础路由的 JSON（sing-box 1.12+） |

sing-box 基础配置使用 Cloudflare `1.1.1.1` 的 HTTPS DNS，并通过直连出站解析节点域名；可按自己的网络环境调整 DNS。

Clash/Mihomo 与 sing-box 基础配置的本地代理监听端口为 `127.0.0.1:7890`；Clash 使用 `allow-lan: false`。下载配置会替换客户端导入的相应配置，复杂分流规则可通过客户端或其他管理工具另行配置。

每个订阅可选择格式复制地址、预览内容、下载配置、修改节点选择、停用、设置到期时间或重置地址。重置后旧地址立即失效；删除订阅不会删除节点。允许先保存空订阅，尚无可导出节点时返回错误，不会生成静默直连配置。

订阅地址是不需要面板登录的访问凭据，持有者可读取该订阅的节点配置。它只允许读取对应订阅，不具备面板管理能力；不要公开分享到公共仓库。

订阅内容在每次请求时读取节点最新数据，优选地址、SNI、WS Host、路径与 Early Data 参数会保留。临时隧道域名更新后，客户端需刷新订阅才能取得新配置。卸载中或已删除节点自动排除；短暂离线节点保留在选择中，避免网络波动改变客户端配置。

这一功能参考 [妙妙屋](https://github.com/iluobei/miaomiaowu) 的节点选择/生成订阅方式及 [Sub-Store](https://github.com/sub-store-org/Sub-Store) 的多格式订阅管理思路，独立实现于本项目中，无需部署第三方转换服务。转换字段参照 [Mihomo VLESS 文档](https://wiki.metacubex.one/en/config/proxies/vless/) 和 [sing-box VLESS 文档](https://sing-box.sagernet.org/configuration/outbound/vless/)。

## 测活与自动修复

节点脚本每分钟检查 cloudflared 进程、本地 WS 握手及公网隧道 WS 握手并上报。连续 3 次失败、距上次修复至少 300 秒，才尝试修复；服务器整体出站网络异常时暂不反复重建隧道。

本地服务异常先重启 sing-box；公网隧道异常尝试重建 cloudflared。临时域名变化会更新节点元数据并同步面板链接。新域名可能需要额外一轮检查才能正常访问。

- 超过 180 秒未收到心跳，显示“心跳超时”。
- 面板每 5 分钟独立进行一次实际 VLESS-over-WS + TLS + HTTPS 转发测试，最多并发 2 个；也可手动测活。
- 失败的常规配置任务不会每分钟反复重装，需要手动点击“重试任务”；卸载失败会保留记录和日志，等待下一次连接重试。

心跳、WS 检查、真实转发结果分别显示，不能保证所有客户端地区的可用性。临时隧道不保证域名固定或长期可用。

## 升级与备份

先确认没有正在执行的部署/卸载任务。更新前备份 `.env` 和完整 `data/`；其中包括数据库、会话密钥、TOTP 信息及节点凭据哈希。

简单一致性备份方式是短暂停止面板，再归档数据：

```sh
docker compose stop
sudo tar -czf ../auto-argo-node-backup.tar.gz .env data
docker compose start
```

备份会包含敏感信息，应保存在私有位置。不要将生产数据提交 GitHub。恢复时需使用同一份数据库与 `secret.key`，并保留文件权限。

```sh
git pull --ff-only
docker compose up -d --build
docker compose ps
```

更新面板不会主动替换所有节点上的监控脚本；需要更新节点脚本时，重新生成接入命令并到对应服务器执行。已有记录和 UUID 会保留。

## 开发与验证

详细步骤见 [测试说明](docs/testing.md)。Docker 构建、34 项后端/HTTP 测试，以及 Python/Shell/JavaScript 语法检查已在整理发布时验证；GitHub Actions 提供相同的后端验证流程。

```text
app/                 FastAPI、SQLite、登录、节点任务、Shell 代理和网页
scripts/             固定版本上游安装器获取与 SHA-256 校验
tests/               SSH、HTTP、代理契约、浏览器和隔离集成验证
docs/                HTTPS 代理示例及测试说明
.github/workflows/   CI 验证
```

安装器来源、固定提交及第三方说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本仓库不提交上游安装器源码、服务器凭据、数据库、日志或备份。
