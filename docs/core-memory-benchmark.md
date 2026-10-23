# 核心内存对比测试 · 2026-10-23

本机隔离测试确认：同版本 sing-box 保留三种所需协议能力、裁剪可选功能后，运行内存有明显下降。直接协议库路线也有进一步潜力，但本次 Keel 对照只覆盖 Reality 单协议，不能直接替换现有三协议方案。

测试不连接现有节点，不加载生产凭据，不替换线上核心。VLESS-WS 项只测 Argo 的本地协议入站，不包括 Cloudflare 公网链路或 cloudflared 进程。

## 条件与校验

- Linux amd64；sing-box v1.14.3，源码提交 `7054cac5124465ffdb145870d7c01885566711b1`；Go 1.26.8、CGO 关闭，运行时 GOMAXPROCS=1、GOGC 默认、GOMEMLIMIT 未设置。
- 基线为面板现有官方探测二进制的独立副本。候选使用相同源码/编译器，保留 `with_quic,with_utls,badlinkname,tfogo_checklinkname0`；保留上游 `release/LDFLAGS`，关闭未用的可选特性。它仍是 sing-box 框架，不是三协议专用重写核心。
- 每种协议、每个版本单独启动服务端，3 轮，第二轮交换版本执行顺序；共 18 组。每组 8 个并发 HTTPS 客户端，每个下载和上传各 8 MiB，共 128 MiB 有效负载；每连接每方向主动限制约 2 MiB/s，避免占用本机全部带宽。
- 所有流量走回环地址，下载和上传均核对 SHA-256；验证 SOCKS UDP 回显；另验证客户端保持运行时，停掉被测服务端后新请求失败，防止把直连误算成代理成功。
- 客户端使用独立文件副本，避免服务端/客户端共享同一可执行文件页面影响 PSS。每 100 ms 读取服务端 `/proc/PID/smaps_rollup`，采集 RSS/PSS；表中“传输中”是各轮采样中位数，再取 3 轮中位数。原始 JSON 含采样峰值，它不是持续硬件采样的绝对瞬时峰值。
- 启动后约 1 秒记录冷启动值；小请求和 UDP 预热后等待 5 秒记录暖机空闲；正式传输完成后等待 15 秒，再关闭客户端（包含 Hysteria2 的持久 QUIC 会话），等待 5 秒分别记录内存。
- 另外为全部 6 种协议/版本组合做预热后关闭客户端、空闲 150 秒的观察。每个实例用不同可执行文件 inode；这一步独立于传输阶段，并非数小时耐久测试。

## 三协议结果

单位 MiB，以下只含服务端协议核心。官方文件大小 77.57 MiB，精简文件大小 38.55 MiB；文件体积与运行内存分别统计。

| 协议 | 官方传输中 RSS | 精简传输中 RSS | 减少 | 降幅 |
| --- | ---: | ---: | ---: | ---: |
| VLESS-WS（Argo 本地核心） | 55.29 | 35.47 | 19.82 | 35.8% |
| Reality Vision | 56.91 | 37.67 | 19.25 | 33.8% |
| Hysteria2 | 60.33 | 40.33 | 20.00 | 33.2% |

其他阶段（MiB）：

| 协议 / 版本 | 冷启动 RSS | 暖机空闲 RSS | 传输中 PSS | 传输结束 15 秒 RSS | 客户端关闭 5 秒 RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| VLESS-WS（Argo 本地核心） / 官方标准版 | 53.55 | 54.42 | 54.28 | 55.83 | 55.83 |
| VLESS-WS（Argo 本地核心） / 精简编译版 | 33.20 | 34.07 | 35.46 | 35.73 | 35.73 |
| Reality Vision / 官方标准版 | 53.29 | 55.09 | 55.90 | 57.28 | 57.28 |
| Reality Vision / 精简编译版 | 32.94 | 35.45 | 37.66 | 37.88 | 37.88 |
| Hysteria2 / 官方标准版 | 54.33 | 56.29 | 59.32 | 61.42 | 61.43 |
| Hysteria2 / 精简编译版 | 33.84 | 36.30 | 40.32 | 41.41 | 41.42 |

预热后空闲 150 秒（各组合 1 次，不混入上表 3 轮中位数）：

| 协议 | 官方 RSS | 精简 RSS | 差值 |
| --- | ---: | ---: | ---: |
| VLESS-WS（Argo 本地核心） | 58.73 | 36.77 | 21.96 |
| Reality Vision | 58.41 | 38.17 | 20.24 |
| Hysteria2 | 61.39 | 38.70 | 22.69 |

两分钟后内存差距仍存在。Go 的回收、页面归还及后台活动不保证 RSS 随时间单调下降，所以表中部分空闲值反而略高，未对数据做平滑或“回收后必下降”的假设。

## 直接协议库的 Reality 参照

使用 Keel 提交 `dc0bf728febb261f51b9af3d651e39478ead249a`，Go 1.26.8，以 `vless,with_utls` 仅编译 Reality。未连接 Keel master，使用临时本地配置。其服务端参数、库版本和内部功能与 sing-box 不完全一致，不能把全部差距归因于单一因素。

同样 8 并发、每轮 128 MiB、3 轮 HTTPS/UDP/停服断流校验全部通过：

| 暖机空闲 RSS | 传输中 RSS | 传输中 PSS | 二进制大小 |
| ---: | ---: | ---: | ---: |
| 9.49 MiB | 11.76 MiB | 11.76 MiB | 8.99 MiB |

这为“用协议库写更小的服务端”提供了实测参照，但本轮没有实现 Keel 的 VLESS-WS/Argo 或 Hysteria2，也未验证其与当前安装、配置重载、事件、修复和回滚流程的完整集成。

## 结论与适用范围

1. 精简 sing-box 是改动较小且已验证三种所需核心协议的方向，适合进一步进行单节点 A/B 试点。
2. 直接协议库路线的 Reality 内存潜力更大；要替代当前核心，仍需补齐 VLESS-WS 与 Hysteria2、CLI/配置接口、监控修复及多客户端兼容性。
3. 上述比例仅相对本机同版本官方构建成立。线上节点可能使用不同版本/架构、已有 GOMEMLIMIT、容器限制、不同负载或页面回收状态，实际 RSS 可能低于本表，不能逐台承诺减少约 20 MiB。
4. Argo 的独立 cloudflared 仍占用额外内存；本测试不证明整套节点内存按同一比例减少。
5. 本轮未覆盖 ARM 实机、全部客户端、真实 Cloudflare 网络、长期连接稳定性或公网吞吐；固定限速的传输耗时不用于性能排名。

## 复现与数据

脚本：[`scripts/benchmark-core-memory.py`](../scripts/benchmark-core-memory.py)。仅需 Linux /proc、Python 标准库、OpenSSL，以及指定二进制；使用随机临时凭据，退出时清理自己启动的进程和配置。

候选构建条件：固定上述 sing-box 提交，在 Go 1.26.8 容器内运行（可将容器限制为 2 CPU / 2 GiB 内存）：

```sh
CGO_ENABLED=0 GOTOOLCHAIN=local GOMAXPROCS=2 go build -p 2 -trimpath \
  -tags with_quic,with_utls,badlinkname,tfogo_checklinkname0 \
  -ldflags "$(cat release/LDFLAGS) -s -w -X github.com/sagernet/sing-box/constant.Version=1.14.3" \
  -o /bench/bin/minimal ./cmd/sing-box
```

```sh
python3 scripts/benchmark-core-memory.py \
  --baseline /bench/bin/official --candidate /bench/bin/minimal \
  --client /bench/bin/client --repeats 3 --output results.json

python3 scripts/benchmark-core-memory.py \
  --baseline /bench/bin/official --candidate /bench/bin/minimal \
  --client /bench/bin/client --idle-seconds 150 --output idle-results.json

python3 scripts/benchmark-core-memory.py \
  --baseline /bench/bin/official --candidate /bench/bin/minimal \
  --keel /bench/bin/keel-reality --variants keel --protocols reality \
  --client /bench/bin/client --repeats 3 --output keel-results.json
```

- [三协议原始数据](benchmarks/core-memory-2026-10-23-results.json)
- [150 秒空闲原始数据](benchmarks/core-memory-2026-10-23-idle-results.json)
- [Keel Reality 原始数据](benchmarks/core-memory-2026-10-23-keel-results.json)
- [sing-box 官方构建说明](https://sing-box.sagernet.org/installation/build-from-source/)
- [Keel Reality 源码](https://github.com/kiwi0719/keel/blob/dc0bf728febb261f51b9af3d651e39478ead249a/cmd/keel-node/proto_vless.go)

原始数据记录了二进制 SHA-256 和版本标签，全部是隔离测试数据，不含生产节点信息。所有测试二进制均为临时参照，未替换生产节点核心。
