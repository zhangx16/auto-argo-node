# 更新记录

## 未发布

- 增加独立订阅管理、节点勾选、到期与停用、订阅地址重置。
- 支持通用 Base64、原始 VLESS 链接、Clash/Mihomo YAML、sing-box JSON。
- 订阅动态读取最新节点配置，保留 TLS/WS/Early Data，排除卸载节点。

## v1.0.0

- 网页生成一次性节点安装命令，节点主动拉取配置并上报。
- VLESS-WS + Cloudflare 临时隧道部署、自定义端口及优选地址。
- 分别显示心跳、WebSocket 健康和实际 VLESS 转发结果。
- 连续故障修复、域名变化同步、版本化端口修改与卸载确认。
- 纵向三模块布局，节点列表与任务列表支持两层折叠。
- 密码与 TOTP 登录、Docker Compose 部署、HTTPS 示例及 CI。
