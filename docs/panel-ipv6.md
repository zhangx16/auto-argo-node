# 面板 IPv6 出口

SSH 提示网络不可达时，先分别验证宿主机与面板容器。宿主机存在公网 IPv6 并不代表 Docker 默认桥接网络也可使用 IPv6。

```sh
ip -6 addr show dev eth0 scope global
ip -6 route show default
sysctl net.ipv6.conf.all.forwarding net.ipv6.conf.eth0.accept_ra
```

如果供应商使用 SLAAC，开启转发后 `accept_ra=1` 不再接收路由通告，可对实际出口网卡设置 `accept_ra=2` 和 `autoconf=1`，将两项写入单独的 `/etc/sysctl.d/99z-argo-host-ipv6.conf` 以持久化。按实际网卡名配置；不需要重启 IPv4 网络。静态 IPv6 或 DHCPv6 的主机应遵循供应商配置，不能凭空填写公网地址或网关。

面板容器可接入独立的 IPv6 桥接网络，避免重建包含其他服务的现有网络。下面的 ULA 网段需确认不与已有网络冲突，Docker 负责出站 NAT66；保留现有默认网络以访问 Monitor。

```sh
docker network create --ipv4=false --ipv6 --subnet fd53:a690:2325:1::/64 argo-panel-ipv6
docker network connect argo-panel-ipv6 singbox-mgmt
```

将连接关系合入生产 Compose，确保后续重建仍保留该出口：

```yaml
services:
  singbox-mgmt:
    networks:
      - default
      - panel-ipv6
networks:
  panel-ipv6:
    external: true
    name: argo-panel-ipv6
```

2026-10-25 实机验证：宿主机通过恢复 RA 获取公网地址和默认路由，面板通过独立 IPv6 网络建立外部 SSH 握手。HK-Lumen 当前仅有私网 IPv6，历史公网 IPv6 对应的 SSH 指纹不同，因此该节点使用已校验指纹的 IPv4 地址经其他节点中转接入，未使用历史 IPv6 进行认证。
