# SL-3000 eMMC 固件 CI 设计

## 目标

为当前 SL-3000 eMMC 设备建立独立、可重复的 GitHub Actions 构建目标。固件基于 OpenWrt 25.12 和 Linux 6.12，预装日常所需网络组件，同时避免当前 23.05 Snapshot 中内核 ABI、软件源和 PassWall 依赖不一致的问题。

构建产物以设备现有的 U-Boot 和 eMMC GPT 布局为兼容目标，须经实机验证；当前内核没有暴露 SPI 分区，不能据此断言引导程序的位置。CI 不构建、不发布，也不写入 BL2、FIP 或 GPT。

## 范围

### 包含

- 独立的 `sl3000_emmc` 构建目标和手动触发工作流。
- 固定 OpenWrt、feeds 和关键第三方组件的源码版本。
- 仅编译 `sl_3000-emmc` 设备镜像。
- 预装 Tailscale、PassWall、OpenClash、Mihomo、UPnP 和网络唤醒组件。
- 包含 PassWall 所需 DNS、nftables 和内核模块依赖。
- LAN 默认地址为 `192.168.21.1`。
- Wi-Fi 默认启用，SSID 为 `SL3000-Setup`。
- Wi-Fi 密码由 GitHub Actions Secret `DEFAULT_WIFI_PASSWORD` 在构建时注入。
- 发布 sysupgrade、initramfs 测试镜像、校验值、最终配置、内核配置和包清单。

### 不包含

- Tailscale 登录状态或认证密钥。
- PassWall/OpenClash 订阅、节点、规则和用户数据。
- 固件内置 root 密码。
- 现有路由器配置备份。
- BL2、FIP、U-Boot 或 GPT 镜像。
- 自动刷写路由器或修改 eMMC 分区表。

## 构建架构

在现有 Kwrt 仓库中新增 SL-3000 专用配置，提取必要的设备支持，不执行会修改 ABI 校验和拉取滚动依赖的通用脚本。

构建流程如下：

1. 用户通过 `workflow_dispatch` 手动触发构建。
2. 工作流检出固定的 OpenWrt 25.12 commit。
3. 工作流检出固定的 feeds、PassWall、OpenClash 和 Mihomo 版本。
4. 仅应用经过核对的 SL-3000 eMMC 设备支持。
5. 合并 SL-3000 专用 `.config` 并运行 `make defconfig`。
6. 从 `DEFAULT_WIFI_PASSWORD` 注入首次启动 Wi-Fi 密码。
7. 断言设备目标、关键软件包和内核模块均为内置状态。
8. 仅编译 `sl_3000-emmc`。
9. 检查镜像元数据、包清单、文件类型和校验值。
10. 上传经过检查的构建产物。

不得依赖构建时自动选择最新 tag 或滚动分支。升级固定版本应通过显式修改 commit 完成，以便审查和回滚。

## 设备与升级边界

目标设备标识为 `sl,3000-emmc`，基于仓库现有 DTS 修正为实际 1 GiB 内存，并移除当前 GPT 不存在的 `factory` NVMEM 引用。当前 vendor Wi-Fi 驱动日志显示使用默认 EEPROM；主线 mt76 校准回退需要实机验证。升级继续采用按标签定位 `kernel` 和 `rootfs` 的 sysupgrade tar 处理逻辑。

设备当前 eMMC GPT 布局为：

| 分区 | 标签 | 大小 | 用途 |
| --- | --- | ---: | --- |
| `/dev/mmcblk0p1` | `kernel` | 32 MB | 内核/FIT |
| `/dev/mmcblk0p2` | `rootfs` | 2 GB | 系统和 overlay |
| `/dev/mmcblk0p3` | `storage` | 约 113.5 GB | `/opt` 和 Docker 数据 |

`sysupgrade.bin` 只能更新系统分区，不得重建 GPT 或覆盖 `storage`。CI 必须拒绝发布名称或类型包含 GPT、BL2、preloader、FIP 或 U-Boot 的文件。

当前磁盘的主 GPT 可用，但备份 GPT 不在磁盘末端。此问题不属于固件构建范围，不应通过刷写 GPT 处理。

## 固件内容

### 基础系统

- OpenWrt 25.12
- Linux 6.12
- LuCI 中文界面
- firewall4 和 nftables
- `dnsmasq-full`
- CA 证书、curl、完整 IP 工具和基础诊断工具

### 代理与远程接入

- Tailscale 及 LuCI 管理界面
- PassWall 及 LuCI 管理界面
- Xray
- sing-box
- ChinaDNS-NG
- dns2socks
- OpenClash
- Mihomo Meta 核心
- `kmod-nft-socket`
- `kmod-nft-tproxy`

PassWall 与 OpenClash 默认均关闭，避免同时接管 DNS 和透明代理。用户配置时一次只启用一个。

### 局域网功能

- `luci-app-upnp`
- `miniupnpd-nftables`
- `luci-app-wol`
- `etherwake`

UPnP 默认关闭，由用户在 LuCI 中按需启用。网络唤醒组件预装但不预置主机条目。

### 设备与存储

- MT7981/MT7915 Wi-Fi 驱动和固件
- MMC/eMMC 支持
- USB 3.0
- ext4、F2FS、block-mount 及检查工具

## 默认网络配置

- LAN IPv4：`192.168.21.1/24`
- DHCP：启用
- Wi-Fi：启用
- SSID：`SL3000-Setup`
- 加密：`sae-mixed`（WPA2-PSK/WPA3-SAE 混合模式）
- 密码：由 `DEFAULT_WIFI_PASSWORD` 注入
- root 密码：未设置，首次登录后由用户设置

工作流在 Secret 缺失、为空或不是 8 至 63 个可打印 ASCII 字符时必须失败。密码不得写入源码、构建日志、`.config` 或 manifest，但必然存在于固件镜像中。用户已选择使用可公开的临时密码，首次登录后必须修改；公开仓库构建须显式确认这一点。

## CI 接口与产物

工作流使用 `workflow_dispatch`，至少提供以下输入：

- `clean_build`：是否清除构建缓存。
- `publish_release`：是否在验证通过后创建 GitHub Release。

CI 产物包括：

- SL-3000 eMMC `sysupgrade.bin`
- SL-3000 eMMC initramfs 测试镜像（上游能够生成时）
- `sha256sums`
- 最终 OpenWrt `.config`
- 最终 kernel `.config`
- package manifest
- 构建版本和固定 commit 清单

普通 artifact 和 Release 都不得包含引导程序或 GPT 文件。

## 验证与失败策略

构建前必须验证：

- 目标设备配置唯一指向 `sl_3000-emmc`。
- `DEFAULT_WIFI_PASSWORD` 符合长度要求。
- 所有固定 commit 均可获取。
- 必需软件包在 feeds 中存在。

`make defconfig` 后必须验证以下关键配置为 `=y`，而不是模块或未选择：

- SL-3000 eMMC 目标
- Tailscale
- PassWall
- OpenClash
- Mihomo
- Xray 和 sing-box
- ChinaDNS-NG 和 dns2socks
- `kmod-nft-socket` 和 `kmod-nft-tproxy`
- nftables UPnP 实现
- WOL 和 etherwake

构建后必须验证：

- `sysupgrade.bin` 包含 `sl,3000-emmc` 支持设备元数据。
- manifest 包含全部必需包。
- SHA-256 校验成功。
- 没有 BL2、FIP、GPT、preloader 或 U-Boot 文件进入发布目录。
- 构建失败或任一验证失败时不发布 Release。

## 首次部署与回滚

首次升级不保留 OpenWrt 23.05 配置，避免迁移旧版防火墙、网络和代理配置。

首次产物应先通过 initramfs 或串口启动验证以下项目：

- eMMC、以太网、Wi-Fi 和 LED 正常。
- LAN 地址为 `192.168.21.1`。
- Wi-Fi 使用 Secret 注入的密码。
- LuCI 可访问。
- 关键内核模块已加载。
- PassWall、OpenClash、Tailscale、UPnP 和 WOL 包存在。

验证后才使用系统升级功能写入 `sysupgrade.bin`。U-Boot Web 恢复入口接受的镜像格式须单独确认，不能假定它接受 sysupgrade tar。首次升级前保留已验证的恢复镜像与恢复入口。整个流程不修改 U-Boot 和 GPT。
