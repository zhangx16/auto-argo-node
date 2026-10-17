# 测试说明

所有验证使用临时数据或测试服务器，不要让集成脚本连接生产节点。

## 后端 / HTTP

```sh
docker build -t auto-argo-node:test .
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" \
  -e PYTHONPATH=/app:/app/tests \
  auto-argo-node:test \
  python -m unittest discover -s tests -p 'test_*.py' -v
```

涵盖 SSH 主机密钥与交互认证、敏感字段不回显、版本化配置、一次性接入凭据、回调重放、域名同步、测活状态及卸载确认。测试会启动本机临时 SSH 和 HTTP 服务；不使用生产数据库或真实 SSH 凭据。

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

启动固定本地端口 18909 / 18910，使用虚构数据检查命令生成、节点状态、编辑、任务、折叠详情和桌面/手机布局；截图输出到被忽略的 `artifacts/`，结束后关闭浏览器并删除临时配置目录。

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
