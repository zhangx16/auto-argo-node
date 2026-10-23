# 轻量资源与本地诊断

现有 Shell + cron 每分钟采样一次，无新增常驻服务，不增加 Python、Node、Go、监控数据库等节点依赖。读取 /proc/stat、meminfo、loadavg、uptime、进程 comm/status 和 df 根分区；CPU 根据相邻采样差值计算，首次为空。RSS 只读取 sing-box/cloudflared 同名进程，不读取可能含凭据的命令行或环境。

数据随既有心跳上传。面板记录最近一次完整样本及最近 24 小时的 5 分钟桶（每桶最后值，每节点最多 288 条），以面板接收时间判断超过 180 秒过期及清理历史。展示整机系统口径；容器 / NAT 的 /proc 可能与容器配额不同。多个节点共用服务器时，整机指标及共享进程 RSS 会重复，不应按节点相加。不会调整资源配额或自动停止协议进程。

本地命令：`sh /usr/local/lib/argo-deploy/diagnose.sh <节点ID>`。只输出安全字段：协议、端口、应用版本、当前核心进程/监听/cron 状态、最近健康及资源样本、最近心跳 HTTP 状态。没有网络诊断请求，不执行修复，不输出 UUID、路径、隧道 token、面板鉴权或原始日志。命令和指标脚本通过原有 bootstrap/upgrade 下发。旧节点可在资源看板升级监控；在线节点按正常队列升级，不重启核心或改写协议配置。

节点仅保留前次 CPU 计数与最近本地诊断记录（root-only）；退出后无监控工作进程。真实资源开销取决于进程数量与现有 jq/awk 实现；本机单次采集约 0.04 秒，此数不是其他节点的内存或耗时保证。

# 直接使用协议库评估（2026-10-23）

三种协议在技术上都能通过 Go 协议库与传输实现组合构建精简服务端；Keel 当前可执行程序没有实现所需全部组合，不能直接替换。

| 现有类型 | 可用组件与需要补齐的部分 |
| --- | --- |
| VLESS-WS + Argo | SagerNet/sing-vmess/vless 提供 VLESS 服务；仍需 WebSocket 传输、路径和 early-data 兼容（可参考 sing-box transport/v2raywebsocket）；Argo 需继续保留独立 cloudflared 临时/固定隧道管理。 |
| VLESS TCP Reality Vision | sing-vmess/vless 提供 Vision；必须额外接入 Reality TLS 握手、连接包装和兼容的直通能力。Keel proto_vless.go 已展示这种组合，但其实现及依赖不能视为无需适配的稳定 SDK。 |
| Hysteria2 | SagerNet/sing-quic/hysteria2 提供 Service 与认证、QUIC 相关实现；需接入 TLS/证书、UDP 转发、拥塞控制与带宽、连接生命周期。 |

核对的独立库源码 HEAD：sing-vmess `a0c58542bd9d99b00ea0c3cfacce6f03cf8a4017`；sing-quic `7e0a5869a5f49ed0a685143345dfda0448bbc708`。只做源码核对，没有将这些移动 HEAD 引入生产。生产如选此路线，必须固定版本与校验、审查许可证及协议兼容性。

- VLESS/Vision：https://github.com/SagerNet/sing-vmess/tree/main/vless
- Hysteria2：https://github.com/SagerNet/sing-quic/tree/main/hysteria2
- 官方协议配置：https://sing-box.sagernet.org/configuration/inbound/vless/ 、https://sing-box.sagernet.org/configuration/inbound/hysteria2/
- WebSocket：https://sing-box.sagernet.org/configuration/shared/v2ray-transport/
- Reality：https://sing-box.sagernet.org/configuration/shared/tls/
- 构建裁剪：https://sing-box.sagernet.org/installation/build-from-source/
- Argo：https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/

## 内存收益边界与建议

直接协议库可能减少未用的路由、规则、API、DNS 和配置框架的常驻对象及二进制大小，但 Go 链接器本来会移除不可达代码，未启用功能也不等于分配运行期内存。因此可执行文件变小不等于 RSS 同比例下降。新核心仍是 Go 程序，TLS、QUIC、连接缓冲及并发产生的堆依然存在。Hysteria2 的 QUIC 开销和 Argo 的独立 cloudflared 不会因裁剪业务框架自动消失；把监控改成常驻 Go Agent 还可能抵消收益。不能用 Keel Reality/AnyTLS 的基准推算本项目全部三种协议。

优先保留当前核心与一次性监控，先用新看板建立基线。下一步可独立构建禁用不需要可选特性的 sing-box，保留 Reality/WebSocket/Hysteria2 所需能力（特别是 QUIC）；构建标签不表示可以裁掉所有无关协议。若仍有明显节省空间，再制作三协议专用核心进行并排实测。未得到用户进一步指令前，不替换生产核心。

有效内存比较必须使用同版本 Go/依赖、相同 GOGC/GOMEMLIMIT、系统架构、证书、连接数与数据量，区分：核心 RSS/PSS、cloudflared RSS/PSS、整套总量、空闲/并发/连接释放后，以及延迟、吞吐和修复恢复行为。单次 RSS 不能证明长期节省，不能给出未经实测的百分比承诺。

## 后续实测

已完成同版本官方/精简构建的三协议对比和 Keel Reality 参照，详见[核心内存测试报告](core-memory-benchmark.md)。本机结果确认精简方向有收益，但不能直接预测每台线上节点的实际降幅。
