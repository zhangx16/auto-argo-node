# 流媒体检测方案

## 参考与选择

- [MediaUnlockTest](https://github.com/HsukqiLee/MediaUnlockTest) 提供多个服务的地区检测思路和 HTTP/SOCKS 代理参数，适合作为检测路径和字段参考。
- [Subs-Check](https://github.com/sinspired/subs-check) 使用代理 HTTP 客户端检查 Netflix、Disney 和 YouTube；本项目沿用经指定代理检测的原则，未引入其订阅处理、测速服务或程序。
- [流媒体检测介绍](https://github.com/link-kuajing/awesome-network-optimization/blob/main/guides/streaming-unlock-test.md) 提供使用场景。
- [mmw-local](https://github.com/zhangx16/mmw-local) 与 [妙妙屋 X](https://github.com/iluobei/miaomiaowuX) 的公开发行仓库没有完整检测后端源码；参考产品入口和结果分层，不复制发行程序或编译前端。

实现为本项目的独立 Python 适配器，使用公开服务入口、响应字段和匿名设备协议。没有打包上述项目源码、二进制或前端。Disney 公共浏览器客户端标识不是账号凭据；匿名会话令牌只存在于本机检测工作线程内，不保存或返回。

## 资源与判定边界

复用已有 sing-box 探测内核，临时客户端只监听本机回环地址；检测 HTTP 客户端显式 CONNECT 到该端口，固定服务主机和 HTTPS 跳转白名单，无任意目标输入。网络和结果处理在面板进行，不对节点执行安装命令或升级脚本。请求本身会经过节点并消耗少量转发流量。

同一时刻一个任务，单任务 60 秒，单响应 1 MiB，最多两项服务并行，避免高并发放大内存和节点连接数；达到总时限的项目独立标为未完成。每节点保留 5 条可见历史、全局 200 条持久记录；持久结果只含状态、地区、说明、时间和连接身份摘要，不含原页面、凭据、匿名会话令牌。配置变化后结果失效，取消和进程异常均清理临时内核。

通用 HTTP 200 不足以证明解锁，HTTP 403 也不足以证明地区限制。未知、网络异常、风控、区域拒绝分开呈现。通过地区接口或抽样页面也不等于付费账号播放验证。

## 客户端导入

订阅分享页面使用 [sing-box 官方 URL Scheme](https://sing-box.sagernet.org/zh/clients/general/) 和 [Clash Verge Rev URL Scheme](https://www.clashverge.dev/guide/url_schemes.html)。一键导入需要本机安装并注册相应协议的客户端，复制与下载不依赖客户端。

## 扩展项目

新增 Apple TV、Spotify、普通 YouTube、TikTok、ChatGPT、Gemini、Claude、Grok，普通 YouTube 与 Premium 分开。AI 的免登录入口可达单独显示，既不使用可能过期的国家名单作肯定判定，也不把 Cloudflare trace 的地区视为账号可用性证明。识别服务返回的明确地区拒绝、登录页、验证码和风控。不发对话、不调用付费 API。Apple 通用定位与 TV 支持分开；Spotify 只使用 GET，不提交注册。
