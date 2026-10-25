# 测试说明

所有验证使用临时数据或测试服务器，不要让集成脚本连接生产节点。

## 后端 / HTTP

```sh
docker build -t auto-argo-node:test .
docker build -f tests/Dockerfile -t auto-argo-node:test-runtime .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  -e PYTHONPATH=/app:/app/tests \
  auto-argo-node:test-runtime \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

测试镜像额外安装 jq/bash 以运行真实 Shell 场景，生产镜像不增加测试依赖。涵盖维护继承/到期、修复授权/冷却/上限、升级验证与失败暂停、面板健康及 HTTP 权限；同时覆盖 SSH 主机密钥与交互认证、敏感字段不回显、版本化配置、一次性接入凭据、回调重放、域名同步、测活状态及卸载确认。测试会启动本机临时 SSH 和 HTTP 服务；不使用生产数据库或真实 SSH 凭据。

## 本机开发

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
sh scripts/fetch-installer.sh
export SINGBOX_MGMT_DATA="$PWD/data"
export PANEL_URL=https://panel.example.com
uvicorn app.main:app --host 127.0.0.1 --port 18899
```

浏览器登录需要在应用前提供 HTTPS 代理。测试运行时自行生成隔离数据目录，下载的上游安装器被 `.gitignore` 排除。

## 界面

需要 Node.js 22+ 和 Chromium；浏览器路径默认 `/usr/bin/chromium`，可通过 `CHROMIUM_BIN` 指定。

```sh
node tests/ui-smoke.mjs
# 或 CHROMIUM_BIN=/path/to/chromium node tests/ui-smoke.mjs
```

启动固定本地端口 18909 / 18910，使用虚构数据检查命令生成、节点状态、编辑、任务、节点详情、单模块切换、运维中心和桌面/手机布局；截图输出到被忽略的 `artifacts/`，结束后关闭浏览器并删除临时配置目录。

## 完整节点集成

`tests/integration_agent.py` 是手动集成验证，需要可访问 GitHub、Cloudflare 的隔离 Docker 网络，以及两个固定名称的测试容器。该流程会实际安装服务和临时隧道，需要数分钟，受外网和 DNS 传播影响，不在默认 CI 中执行。

确保下列容器名和网络名未被使用，再创建：

```sh
docker network create auto-argo-test
docker run -d --init --name sb-agent-panel --network auto-argo-test \
  -e PANEL_URL=http://sb-agent-panel:18899 auto-argo-node:test
docker run -d --init --name sb-agent-node --network auto-argo-test \
  alpine:latest tail -f /dev/null
docker exec sb-agent-node sh -c \
  'apk add --no-cache openrc && mkdir -p /run/openrc && touch /run/openrc/softlevel'
python3 tests/integration_agent.py
```

`--init` 负责回收测试容器里的子进程；不要使用省略该选项的容器验证 OpenRC 服务停止行为。测试代理通过明文 HTTP 访问隔离网络内面板，仅用于测试，正式面板应配置 HTTPS。

测试包含首次部署、实际转发、冻结 cloudflared 后检测与修复、修复冷却、端口修改、重新绑定、无常驻代理和删除后远端清理。结束后清理仅此次创建的测试资源：

```sh
docker container rm --force sb-agent-panel sb-agent-node
docker network rm auto-argo-test
```

发布整理时另用隔离 Alpine 环境验证了完整卸载，以及同机保留其他 inbound 时不停止共用服务。Debian/systemd、ARM64 和 ARMv7 的真实机器验证仍待补充。

## SSH 隔离集成

`tests/integration_ssh.py` 只用于自行创建的临时 SSH 容器，要求 `SSH_TEST_HOST` 和测试专用 `SSH_TEST_PASSWORD`。不得指向生产服务器。使用 `tests/Dockerfile.ssh`（基于测试运行镜像的 Debian）或 `tests/Dockerfile.ssh-alpine` 创建 SSH peer；在独立 Docker 网络内由测试运行镜像执行，挂载当前 app/tests，设置 PYTHONPATH=/app:/app/tests。

该测试会在临时 SSH peer 内写入测试节点配置、修改测试 root 的 authorized_keys/crontab、写有界任务日志，结束后应删除仅本次创建的 SSH peer 和网络。已验证断连后不重复执行、未知执行标记、管理脚本实际 SSH 标记、密钥失败回退及轮换撤销，并验证大于日志上限的正常文件仍可写入。

2026-10-25：172 项后端全量测试、后续 27 项 SSH / 节点专项和浏览器测试通过，补充中转通道的指纹拒绝、资源释放、失效不回退、同机节点限制，以及重装命令生成不执行、执行后保存身份、一次性凭据、执行前再次检查配置任务、固定隧道重装选择完整安装器与 SSH 模式脚本校验。IPv6 出口属于部署环境配置，见 [面板 IPv6](panel-ipv6.md)。

## 服务器工作区与恢复测试

`test_server_workflows.py` 覆盖旧库迁移保留数据、显式主备来源、跨来源 bootstrap 拒绝、统一档案脱敏、同机端口冲突与复用、恢复确认和配置互斥、真实任务回调与新转发门槛、结果未知不重放、旧排队任务的显式替换、客户端预算与嵌套中转生命周期，以及订阅隔离的次数/时间、重复样本、维护/过期、配置变化、逐订阅独立性及链路完整性。`test_server_api.py` 检查新入口认证、来源校验、参数限制和默认策略关闭。

SSH 隔离集成补充能力检测、退出前的增量输出、主备管理地址更新及大日志轮换，在 Debian 和 Alpine 测试 peer 上执行。浏览器覆盖服务器档案、同机新增节点、重装向导、步骤日志、统一任务筛选与订阅隔离设置，桌面和手机截图保存在 artifacts。生产验证仅做只读诊断和能力检查，不以测试为由重装在线节点或开启订阅隔离。

### 集中式服务器弹窗与独立 Monitor 页面

- 后端全量 202 项通过，新增验证服务器任务与升级批次隔离、整机所属节点流量与日历史范围、`/monitor` 登录保护与独立页面响应。
- UI smoke 覆盖删除任务中心入口、放大的弹窗、完整节点操作、内嵌 SSH、服务器维护、任务 server_id、资源与流量挂载，以及独立看板搜索 / 状态筛选、单机管理链接与手机 / 桌面布局。
- 线上只读浏览器检查通过：KR-Shlii 的两个协议节点、SSH 关联、单机资源 / 任务 / 流量范围，及 `/monitor` 15 台主机、额度弹窗与移动端溢出检查。没有通过本轮检查执行远端重装或升级。

### 移除独立已部署节点页面

- 全量后端 203 项通过；节点通知定向链接补充检查通过，历史节点路由映射到服务器工作区。
- UI smoke 验证待关联节点的完整操作、归档到已有服务器、链接不变、全局协议 / 名称 / 标签筛选、节点定位、服务器内安装命令以及新旧深链接。
- 线上只读检查验证独立页面 / 导航已删除，KR-Shlii 两节点搜索与定位、单机完整操作、`/monitor`、桌面 / 手机布局正常。更新前后 16 个节点、1 份订阅数量及节点链接 / 订阅关联摘要一致。

### Monitor 主题、通知迁移与原生延迟

- 全量后端 212 项通过；新增压缩包边界 / 恶意路径、配置和管理员权限、只读数据过滤、探测配置去重与部分失败重试测试。最后的地区 / 分组适配与主题预览另外通过定向检查。
- UI smoke 验证主面板不再存在全局流量 / TG 表单，旧入口迁移、独立页面完整通知设置、默认三列卡片、明暗配色、主题配置与预览。
- 用真实官方默认 1.4.2、Rakugaki 1.18.1 发布包做隔离浏览器测试：安装、切换、监控数据、静态字体 / 国旗、详情、只读沙箱、手机显示与回退。
- 实际 Rust hub + 现有 agent 集成测试验证三条 TCP 探测及服务凭据权限；部署后 15 台主机均回报全部三条安徽线路。
- 主题 iframe 与大文件上传在 Nginx 使用独立 location 适配，主面板原 CSP 不变。

生产浏览器最终验证通过：服务器顶部重复分类已移除，全局流量与 TG 表单只存在于 `/monitor`，旧流量入口正确跳转；三列卡片、已安装主题、Rakugaki 预览 / 地区与国旗 / 详情 / 安徽三网曲线、手机布局与预览退出均正常。测试保留用户当前站点主题选择。更新前后 16 个节点链接、1 份订阅的节点关联，以及 Telegram 配置摘要一致。

### 删除独立探针并恢复节点脚本（当前）

- 本轮最终全量 **207 项测试通过**，前端 smoke 通过。覆盖恢复 / 卸载脚本的顺序约束、节点脚本真实心跳、重装恢复必须等待心跳、SSH 管理仍保留心跳 cron，以及主面板流量 / TG 配置、单机流量和桌面 / 手机布局。
- 线上完成 15 台服务器的 `legacy_restore` / `monitor_remove` 任务；全部成功回执后停止并删除 hub。16 个节点心跳、资源采样与实际转发正常，节点 ID / 链接与备份完全一致。
- Monitor 页面、主题 / 延迟接口、探针二进制下载和发布脚本已禁用 / 删除；旧 `/monitor` 跳转主面板。没有遗留 Monitor 环境变量或运行数据目录。
- 备份 `/root/backups/auto-argo-monitor-20261025T110152Z/`，SQLite 完整性与 SHA256 清单检查通过，仅 root 可读。未同步 GitHub。

### 外部订阅 IPv4 整理

211 项后端测试通过，前端 smoke 验证来源编辑 / 转换规则及上游用量展示；线上验证 29 个导出节点、12 组已验证 IPv4 对照、无残留纯 IPv6、URL 不回显、客户端流量头和专属页面 / 手机布局。上游 IPv4 使用临时面板代理读取，再以候选 IPv4 实际连接验证，节点端不安装任何新进程。

## 汇总订阅与紧凑节点管理

2026-10-25：后端全套 225 项测试通过；新增本地引用 / 嵌套去重、账户隔离、循环及祖先深度拦截、来源停用 / 到期 / 删除、选定用量及到期响应头、继承链式节点防绕过、原订阅隔离恢复、公有专属页信息边界，以及批量删除鉴权、整批回滚和来源排除验证。`node tests/ui-smoke.mjs` 验证引用选择与回填、按来源用量、手机端编辑器、两列紧凑节点网格、已归入订阅节点不再重复平铺，以及批量删除影响范围。

生产验证使用临时汇总订阅：两份来源的 65 条引用合并为 47 份不同连接配置，四种格式数量一致；检查指定 / 自动用量响应头、编辑回填、按来源统计、手机两列网格、专属页及 JavaScript 错误。临时订阅完成后删除，原订阅名称、地址、直接成员及可导出数量、全部自建与外部节点链接均通过前后比对，SQLite 完整性检查通过。
