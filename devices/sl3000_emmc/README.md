# SL-3000 eMMC 专用固件

**本仓库仅服务于 SL-3000 eMMC。** 不维护其他路由器或 SL-3000 非 eMMC 版本；仓库中其他设备目录仅为上游历史保留，不属于支持范围。即使同为 SL-3000 eMMC，刷入前仍须核对下列硬件与分区条件。

本目录独立构建固定来源的正式版 OpenWrt **25.12.6 / Linux 6.12.116**（锁定 `openwrt-25.12` 分支安全基线提交 `94701b14e00e`）。不调用 Kwrt 通用定制脚本，不关闭内核 ABI 校验，不拉取滚动版本。

## 适用设备与硬件前提

- 设备标识：`sl,3000-emmc`，1 GiB RAM，MT7981 + MT7531，板载 32 MiB SPI-NOR（`spi2`）含偏移 `0x180000`、长度 `0x200000` 的原厂 `Factory` 校准区（MT7981 / MT7976C A-die v1）。
- 已有 eMMC GPT：`mmcblk0p1=kernel`（起始扇区 8192，65536 扇区），`mmcblk0p2=rootfs`（起始 73728，4096000 扇区），`mmcblk0p3=storage`（起始 4169728）。
- 系统升级只定位并更新 `kernel`、`rootfs`；不格式化 `storage`，也不默认挂载或运行其中的旧 Docker 数据。
- 不提供或写入 BL2、FIP、GPT、U-Boot。不要重刷分区表。
- 禁用在线自动固件升级入口，避免自定义设备跳转到不匹配的官方镜像；更新内核和驱动请重新编译整套固件，不要强制安装其他 ABI 的 kmod。

## 无线与射频架构（公用驱动 + 逐机原厂校准 + AP+STA 自愈）

正式固件默认采用 **开源主线 `mac80211/mt76` 驱动 + 本机 SPI-NOR `Factory` 只读 NVMEM 校准直读 + 同射频 AP+STA 断线恢复补丁**：

- **本机原厂校准直读（`factory-nvmem`）**：设备树（`mt7981b-sl-3000-emmc-factory.dtsi`）通过 `spi2`（10 MHz 单线模式）只暴露 `0x180000` 处长度 `0x200000` 的单个只读 `factory` MTD 分区，并通过标准 `nvmem-layout` 将其前 4096 字节（`eeprom@0`）绑定到 `mt7981-wmac`。内核固定启用 `CONFIG_NVMEM=y`、`CONFIG_NVMEM_LAYOUTS=y` 与 `CONFIG_MTD_SPI_NOR_SWP_KEEP=y`，禁用分区主设备暴露，不暴露引导分区或可写 U-Boot 环境。
- **零私有校准入库与通用兜底**：仓库代码与公开固件镜像中不包含任何单机的 NOR 转储、运行时 EEPROM 或硬件 MAC；公共构建预检显式拒绝注入私人校准或加密 Secret。镜像内同时保留与锁定 `mt76` 源码配套的公开通用 EEPROM（`sl3000-default-eeprom`）作为异常回退保底；若硬件读取异常触发回退，内核日志会打印 `eeprom load fail, use default bin`。
- **MAC 地址策略**：无线 `phy0` / `phy1` 硬件直接读取本机 `Factory` EEPROM 内的出厂无线 MAC；有线 `lan`、`wan` 由 `03_sl3000-network` 根据每台设备唯一的 eMMC CID 哈希派生，全新首启生成的默认 AP 接口亦基于该派生地址偏移，避免万一触发通用 EEPROM 回退时出现多机 MAC 冲突。保留配置升级不会覆盖用户已有的无线配置。
- **转发路径**：保持主线 `mt7915e` 默认关闭无线 WED 硬件卸载（`wed_enable=N`），不默认开启与同射频 WISP 中继或透明代理冲突的硬件卸载路径。

### 同射频无线中继（AP+STA）断线后的客户端自愈

在 5 GHz 单射频同时承载 STA 上联（WISP）与本地 AP 时，若上游路由器偶发异常 beacon（如丢失 VHT 字段触发 `mac80211` `appears to change mode`）导致 STA 短暂重连，内核 `stop_ap` 会清空无线站点表及密钥，但上游 OpenWrt `hostapd` 的 ucode 停止路径（`uc_hostapd_iface_stop`）原本仍将旧终端保留为已关联、已授权状态，导致下游设备陷入“显示已连接但无法收发包”的假死，直到漫长的 inactivity 超时或手动开关 Wi-Fi 才能恢复。

锁文件在官方 `hostapd`（`2025.08.26~ca266cc2-r8`）基础上纳入本地补丁 `patches/hostapd/900-apsta-reset-clients.patch`（`origin: local`，校验 SHA-256）：

- 在 `hostapd_drv_stop_ap()` 移除内核密钥前，先向当前 BSS 已关联客户端逐台发送解除认证通知（支持 PMF 受保护帧），随后立即调用 `hostapd_free_stas()` 清空 `hostapd` 用户态残留客户端表，使终端在 1–3 秒内随 AP 恢复立即干净重连。
- 补丁不改变 PMF、换钥、信道选择、驱动或 STA 重连状态机；它修正断线后的状态同步，不会消除上游 AP 自身的信标波动。若上游 5 GHz AP 存在 Wi-Fi 6/VHT 信标抖动，同射频中继可将 `radio1` 设为 `channel='auto'` 与 `htmode='VHT80'` 以稳定跟随上联信道。
- CI 在锁定源码上用原生编译器编译实际 `uc_hostapd_iface_stop` 函数执行状态机回归测试（覆盖多 BSS、重复停止与 deauth 发送失败），并从最终 SquashFS 的 `wpad` 二进制校验修复标记。

## 内置内容

- Tailscale（`1.104.6`）与官方 feed 的 `luci-app-tailscale-community`。
- PassWall、Xray、sing-box、ChinaDNS-NG、dns2socks、dnsmasq-full、nft socket/tproxy 模块。
- OpenClash 与已内置的 AArch64 Mihomo Meta 核心（`1.19.33`），首次配置不需要另行下载核心。
- 保留 OpenClash 所需的 Ruby/YAML，关闭可选的 Ruby YJIT，避免从源码构建 Rust/LLVM 工具链。配置校验拒绝开启 YJIT，产物校验确认未生成 Rust 主机工具链。
- nftables UPnP、LuCI 网络唤醒与 etherwake、vnStat 流量统计。
- MMC、USB 3、ext4、F2FS、block-mount 和分区/文件系统检查工具。

PassWall、OpenClash、Tailscale、UPnP 默认关闭。镜像内不包含任何订阅、节点、账号、Tailscale 登录状态或 root 密码。两种透明代理一次只启用一个。PassWall 系统服务保持 enable 以便 LuCI“保存并应用”触发重载，但其全局主开关与访问控制默认均为关闭。

## 初始网络（仅全新安装生效）

- LAN：`192.168.21.1/24`，DHCP 开启，后台 `http://192.168.21.1`。
- 实体网口：`lan1`、`lan2`、`lan3` 和 `wan`（在 `02_network` 中一次性登记，`03_sl3000-network` 仅设置逐机派生 MAC）。
- 默认 Wi-Fi：开启，SSID `SL3000-Setup`，`sae-mixed`，国家码 `CN`。
- 初始 Wi-Fi 密码来自 GitHub Secret `DEFAULT_WIFI_PASSWORD`，要求 8 至 63 个可打印 ASCII 字符；为空或非法则拒绝构建。
- `root` 未设置初始密码，首次登录后请立即设置管理密码并修改默认 Wi-Fi 密码。**默认 Wi-Fi 密码会写入只读 ROM**，请仅配置专用的临时初始口令，切勿复用家庭日常 Wi-Fi 或管理密码。

## GitHub Actions 构建与发布

1. 在仓库 `Settings → Secrets and variables → Actions` 中配置 `DEFAULT_WIFI_PASSWORD`（如需默认推送 Telegram 产物，同时配置 `TELEGRAM_TOKEN` 与 `TELEGRAM_CHAT_ID`）。
2. 打开 `Actions → SL-3000 eMMC (Pinned OpenWrt) → Run workflow`：
   - `clean_build`（默认 `false`）：勾选后跳过恢复旧下载与编译缓存，成功后仍保存新缓存；每次均在全新目录编译。
   - `publish_release`（默认 `false`）：勾选后创建正式版 GitHub Release（标签 `sl3000-25.12.6-<run_number>`）。
   - `telegram_artifacts`（默认 `true`）：将校验通过的固件归档（超 45 MiB 自动分卷 ZIP，附 `SHA256SUMS.txt` 与 `RESTORE.txt`）发送至 Telegram。
3. 下载 `sl3000-emmc-<run_id>` 或 Release 附件，解压后执行 `sha256sum -c sha256sums`（macOS 用 `shasum -a 256 -c sha256sums`）核验完整性。

正式构建的 `build-info.json` 固定标记 `status: "release"`、`wifi_profile: "factory-nvmem"`、`nor_profile: "read-only-factory-nvmem"`、`eeprom_fallback: "pinned-public-generic"`、`contains_private_calibration: false`。CI 对 sysupgrade 与 initramfs FIT 中的设备树节点、只读标志、NVMEM `eeprom@0` 连接、内核配置、兜底 EEPROM SHA-256、网络脚本及软件包清单逐项校验，任一校验失败均拒绝上传或发布。

### 缓存与编译并发

- `dl` 下载缓存与 `.ccache` 编译缓存分开管理，不缓存源码树、`build_dir`、`staging_dir`、注入密码的脚本或成品镜像；`.ccache` 上限 2 GiB，按完整构建配方与模式隔离。
- 编译并发取 CPU 核心数与可用内存预算（预留 1 GiB 后按每任务 3 GiB 估算，至少为 1）的较小值；下载固定 `-j8`。

## 来源锁定与安全修复基线

所有上游仓库、分支与 40 位提交 SHA 均锁定在 `sources.lock.json`：

- **OpenWrt 主树与官方 Feeds**：锁定 `openwrt-25.12` 提交 `94701b14e00e`（`25.12.6` 后续维护基线，内核 **Linux `6.12.116`**、`mac80211` `6.18.55`、`mt76` `2026.03.19~39c960c3-r3`）及配套官方 `packages`（`b192b111cde1`）、`luci`（`b2e0be4b46ec`）、`routing`（`625f9d844680`）、`telephony`（`c58ec5a67039`）、`video`（`094bf58da668`）。此前单独回移的 `musl`（`CVE-2026-107891`）、`ucode`、`uhttpd`（`2026.11.05~93b05e0b-r1`）、`rpcd`（`2026.10.04~d99f703e-r1`）、`openssl`（`3.5.9-r1`）、`mbedtls`（`3.6.7-r1`）、`ca-certificates`（`20260816-r2`）、`cgi-io`、`nghttp2`（`1.70.0-r1`）、`bind`（`9.20.30-r1`）、`curl`（`8.23.0-r1`）、`miniupnpd`（`2.3.9-r2`）及 `hostapd` RRM 信标报告修复均已由该基线直接原生包含。
- **单独固定的包更新（`package_updates`）与内置组件**：

| 组件 | 锁定版本 | 说明 |
| --- | --- | --- |
| Tailscale | `1.104.6-r1` | 导入官方 `packages` 配方 `407eb224739a`，固定上游稳定版 `v1.104.6` 源码包 SHA-256 |
| Go 1.26 / Go 1.27 构建工具链 | `1.26.11` / `1.27.4` | 同时跟踪并校验 `golang`（供 `xray-core`、`sing-box` 编译）与 `golang1.27`（满足 Tailscale `1.104.6` `go.mod` 要求）；编译器不安装到路由器，CI 收集安全清单时逐一核验实际主机编译器版本 |
| ubus | `2026.06.28~24864e78-r3` | 在基线源码上回移 `3930745d8593`、`5331c00f37f7`、`955ff3f7030c`、`bde609c2400e` 四个上游修复并递增发布号 |
| hostapd / wpad | `2025.08.26~ca266cc2-r8` | 在基线 `-r7` 源码上应用本地 `900-apsta-reset-clients.patch` 并递增发布号至 `-r8` |
| Mihomo | `1.19.33-r1` | 固定上游正式版 `v1.19.33` AArch64 二进制并校验 SHA-256 与 ELF 架构 |
| LuCI 挂载权限加固 | `GHSA-v5f9-62c7-cw29` | `hardening.py` 移除 `luci-mod-system-mounts` 对 `/etc/crontabs/root` 的多余写权限，产物校验从 SquashFS 实读 ACL 复核 |

CI 仍从锁定的 `uhttpd` 与 `libubox` 源码编译原生测试程序，验证 `/ubus` list 参数类型、长度、512 项上限、去重及响应大小边界。

## 保留配置系统升级（Sysupgrade）

适用于已在运行本项目的 SL-3000 eMMC（相同 eMMC GPT 布局）：

1. 升级前若启用了 vnStat，可先执行 `/etc/init.d/vnstat stop` 使最新统计落盘，再在 LuCI `系统 → 备份/升级` 下载一份离线配置备份到本地电脑（跨内核小版本如 `6.12.94 → 6.12.116` 升级前务必保留离线备份与旧版可用固件，并备好有线连接方式）。
2. 在 LuCI 上传解压并校验过 `sha256sums` 的 `openwrt-mediatek-filogic-sl_3000-emmc-squashfs-sysupgrade.bin`，勾选“保留配置”。不要上传 ZIP 分卷，也不要把 `initramfs.itb` 当作系统升级镜像刷入。
3. 若兼容性检查报错，立即停止，切勿勾选强制升级。
4. `firstboot.sh` 检测到 sysupgrade 配置恢复归档时会自动跳过全部首启默认值，保留现有的 Wi-Fi（含 WISP 中继设置）、LAN、防火墙、PassWall/OpenClash/Tailscale 及管理员密码；额外手动安装过的第三方软件包不会随固件自动重装。
5. 镜像继续保留幂等的 `98-sl3000-ports` 升级迁移脚本：仅在检测到早期固件遗留的重复桥端口与虚构 `lan4` 特定组合时自动修正并备份至 `/etc/sl3000-port-backup-v1/`（权限 `0700`），正常配置不做改动。

首次从旧版 23.05 或其他第三方固件切入时，请先核对 eMMC GPT 分区表与 SPI-NOR 布局，优先通过已验证的 RAM 启动方式测试 `initramfs.itb`，正式刷入时**不要保留旧系统配置**。

## vnStat 流量统计

固件内置 vnStat 2.13、`vnstati2` 及 LuCI 中文页面，默认统计 `wan`、`phy1-sta0` 与 `tailscale0`（Tailscale 流量经过物理/无线 WAN，两者不可直接相加作为总外网流量）。数据库位于 `/etc/vnstat/`（权限 `0700`），每 15 分钟及正常停机时落盘，并通过 `/lib/upgrade/keep.d/sl3000-vnstat` 纳入保留配置升级与备份。

## 安全更新监控与 Telegram 提醒

独立工作流 **SL-3000 Security Monitor** 每天北京时间 09:17 运行，自动比对 `sources.lock.json`、GitHub 安全公告与 Tailscale 官方安全 RSS，并结合匹配配方的 `sl3000-security-inventory-*` 构建清单（包含 `golang` 与 `golang1.27` 双工具链溯源）筛除未安装组件或已包含的修复，仅对尚未纳入的明确安全更新发送 Telegram 提醒。

- `security-baseline.json` 保留历史构建 `34033726009` 的只读基准记录；每次更新 `sources.lock.json` 或构建配方后，需运行一次 **SL-3000 eMMC (Pinned OpenWrt)** 生成新的 `sl3000-security-inventory-*` 供监控自动匹配（或通过 Actions Variable `SL3000_DEPLOYED_RECIPE` 指定实际在用配方的 commit SHA）。
- `security-policy.json` 记录明确的使用边界（当前声明无委派 LuCI 子管理员账号；未显式声明为 `false` 的 Tailscale 子功能保持未知，不作激进排除）。
- 公开仓库超过 45/55/59 天无默认分支提交时自动提醒，防止 GitHub 在 60 天无活动后静默停用定时任务。

## 归档诊断模式（仅供单机排障，日常无需使用）

代码库保留 `rf_test`（`rf_test.py`）与 `nor_probe`（`nor_probe.py`）两项私有排障支持，默认关闭：

- 仅用于历史单机运行时 EEPROM A/B 对照或全盘只读 NOR 探测；触发时强制要求提供私有校准与随机 32 字节加密口令 Secret，仅输出 AES-256 加密归档，禁止发布 Release，也不保存编译缓存。日常固件构建与升级请始终保持两者为默认的 `false`。

## 本地验证

```sh
python3 -m unittest discover -s devices/sl3000_emmc/tests -v
sh -n devices/sl3000_emmc/firstboot.sh
sh -n devices/sl3000_emmc/files/sl3000-upgrade.sh
sh -n devices/sl3000_emmc/files/03_sl3000-network
sh -n devices/sl3000_emmc/files/98-sl3000-ports
```

在 Linux 的新构建目录运行 `prepare.py <openwrt-dir>`，随后执行 `make -C <openwrt-dir> defconfig` 和 `verify.py config <openwrt-dir>`。已有准备完成的目录不能再次运行 `prepare.py`；它会拒绝重复覆盖。
