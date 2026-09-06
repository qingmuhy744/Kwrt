# SL-3000 eMMC 专用固件

本目录独立构建 OpenWrt **25.12.5 / Linux 6.12**。不调用 Kwrt 通用定制脚本，不关闭内核 ABI 校验，不拉取滚动版本，不影响其他设备的构建。

**首版属于待实机验证版本，不是已认证的稳定固件。不要仅凭 CI 通过就直接刷入。**

## 适用设备

- 设备标识：`sl,3000-emmc`，1 GiB RAM，MT7981 + MT7531。
- 已有 GPT：`mmcblk0p1=kernel`（起始扇区 8192，65536 扇区），`mmcblk0p2=rootfs`（起始 73728，4096000 扇区），`mmcblk0p3=storage`（起始 4169728）。
- 系统升级只定位并更新 `kernel`、`rootfs`；不格式化 `storage`，也不默认挂载或运行其中的旧 Docker 数据。
- 不提供或写入 BL2、FIP、GPT、U-Boot。不要重刷分区表。
- 禁用在线自动固件升级入口，避免自定义设备跳转到不匹配的官方镜像；更新内核和驱动请重新编译整套固件，不要强制安装其他 ABI 的 kmod。

2026-09-05 首次只读检查发现：设备 GPT 没有 `factory`，当时的 vendor Wi-Fi 驱动报告 EEPROM 无效并使用默认文件。默认构建移除不存在的 NVMEM 引用，包含与锁定 mt76 源码配套的通用 EEPROM。它**不能恢复每台设备的原厂射频校准**。后续实测该默认构建存在 Wi-Fi 覆盖和吞吐问题；不能当作已修复版本使用。以太网和 AP MAC 从设备 eMMC CID 派生，避免所有机器共享默认 EEPROM 的 MAC；升级后 MAC 可能与旧固件不同。

## 内置内容

- Tailscale 与官方 feed 的 `luci-app-tailscale-community`。
- PassWall、Xray、sing-box、ChinaDNS-NG、dns2socks、dnsmasq-full、nft socket/tproxy 模块。
- OpenClash 与已内置的 AArch64 Mihomo Meta 核心，首次配置不需要另行下载核心。
- nftables UPnP、LuCI 网络唤醒与 etherwake。
- MMC、USB 3、ext4、F2FS、block-mount 和检查工具。

PassWall、OpenClash、Tailscale、UPnP 默认关闭。没有订阅、节点、账号、Tailscale 登录状态或 root 密码。两种透明代理一次只启用一个。

PassWall 的系统服务保留启用，以便 LuCI 的“保存并应用”能够触发服务重载；代理主开关和访问控制仍默认关闭，不会自动接管网络。

## 初始网络

- LAN：`192.168.21.1/24`，DHCP 开启，后台 `http://192.168.21.1`。
- Wi-Fi：开启，SSID `SL3000-Setup`，`sae-mixed`，国家码 CN。
- 密码来自 GitHub Secret `DEFAULT_WIFI_PASSWORD`，要求 8 至 63 个可打印 ASCII 字符；为空或非法则拒绝构建。
- root 未设置密码，首次登录后立即设置。

**Wi-Fi 密码必然存在于镜像中，也会保留在只读 ROM 中。** 本项目已选择可公开的临时密码，不能复用家庭 Wi-Fi、路由器管理或其他账号密码。首次连接后立即更改运行配置中的 Wi-Fi 密码。不要将该镜像当作含秘密的私人备份。

## GitHub Actions

1. 在仓库 Settings → Secrets and variables → Actions 中设置 `DEFAULT_WIFI_PASSWORD`，仅使用准备公开的临时密码。
2. 打开 Actions → **SL-3000 eMMC (Pinned OpenWrt)** → Run workflow。
3. 公开仓库必须勾选 `public_setup_password`。`clean_build` 只跳过旧缓存恢复，成功阶段仍保存新缓存供下次使用；每次都在新目录编译。
4. `publish_release` 默认关闭；启用后也只创建 prerelease，不能表示硬件验收通过。
5. 下载 `sl3000-emmc-<run id>`，解压后用 `sha256sum -c sha256sums`（macOS 可用 `shasum -a 256 -c sha256sums`）验证。

产物只包括设备 sysupgrade、initramfs FIT、包清单、OpenWrt/kernel 配置、来源锁定清单、构建信息和 SHA-256。不会上传完整构建目录、注入密码的脚本或未筛选的 `bin/targets` 目录。失败构建不发布镜像。

所有来源见 `sources.lock.json`，官方 feeds 与 25.12.5 发布的 `feeds.buildinfo` 一致。第三方插件也按 commit 固定；Mihomo 使用上游发布的压缩二进制并验证 SHA-256。升级这些来源需要显式修改锁文件及相应包配方后重跑验证。固定源码不代表比特级可重复，也不能替代安全更新。

### 本机射频数据 A/B 测试

`rf_test` 默认关闭。这是**只供原始采集设备使用的实验镜像**，不是其他 SL-3000 的通用校准文件，也不代表问题已经解决。

- 正常工作的 ImmortalWrt / mt_wifi 7.6.6.1 固件使用了不同的 iPA/iLNA EEPROM，并合并了本机 A-die 校准值。通用公开 iPA/iLNA 文件与该固件 ROM 数据也不完全相同，因此首轮直接测试该设备的实际运行时数据。
- `rf_test.py capture` 通过已认证的 SSH 连接，只发送不含 `=` 的 `iwpriv ... e2p` 读取命令，连续读取两遍完整 4096 字节。仅接受 MT7981 / MT7976C / A-die v1，预校准标志必须为零；相对 ROM 的差异必须全部落在已确认的芯片校准位置。不会写 EEPROM、eFuse、闪存或重启 Wi-Fi。
- 采集产生的 `runtime-eeprom.bin`、`calibration-secret.json`、`artifact-passphrase.txt` 只保存在本地私有目录，禁止提交。JSON 设置为 Secret `SL3000_RF_TEST_CALIBRATION`，随机 32 字节十六进制解密口令设置为 Secret `SL3000_RF_TEST_PASSPHRASE`。不能用日常账号密码替代该随机口令。
- 本次测试只在 rootfs overlay 中替换 mt76 加载的默认 EEPROM 文件，不改锁定的 OpenWrt、内核、mt76、MCU 固件或分区布局，也不添加厂商驱动；不额外调整无线信道或 UCI 发射功率设置。公开 `sources.lock.json` 仍记录公共源码；私人数据的校验值记录在加密包内的镜像标记 `/etc/sl3000-rf-test.json`，`build-info.json` 标记测试类型。
- 构建时选 `rf_test=true`、`publish_release=false`、`public_setup_password=true`，`clean_build=false` 可复用旧下载和编译缓存。测试构建不保存新的编译缓存；下载缓存只含公开源码。
- 固件校验仍检查包、分区容量、首启设置、账号状态，并逐字节确认 sysupgrade 内嵌 EEPROM 与输入一致。仅上传 `sl3000-emmc-rf-test-<run id>` 中的 AES-256 加密归档和密文 SHA-256，不上传明文固件，不允许发布 Release。
- 下载后使用本地保留的 `artifact-passphrase.txt` 解密 `sl3000-rf-test.tar.gpg`，再检查包内 `sha256sums`。解密后的镜像包含本机数据，不能转发。丢失口令将无法解密该次构建，应妥善备份。
- 此构建不会自动刷机。实际测试前必须备好正常固件、独立互联网和有线管理方式；RAM 启动方式未经验证前不应假定 initramfs 能安全临时启动。实测双频、近距离和原先信号差的位置，再决定是否实现长期的逐机校准加载。

采集示例（SSH ControlMaster 应先手动认证；不要在命令或仓库中写路由器密码）：

```sh
python3 devices/sl3000_emmc/rf_test.py capture \
  --control /path/to/ssh-control-socket \
  --reference /private/path/MT7981_iPAiLNA_EEPROM.bin \
  --output /private/path/new-rf-test-directory
```

### 本机 NOR 只读诊断

`nor_probe=true` 只能与 `rf_test=true` 一起使用，仍禁止 Release 和明文上传。它保留当前已验证有效的本机运行时 EEPROM，**不把 NOR 的 factory 数据接入 Wi-Fi**。

- 参考 OpenWrt PR [24172](https://github.com/openwrt/openwrt/pull/24172) 的 SPI2 接线，新增 10 MHz、单线 SPI-NOR 识别。只暴露一个预期大小为 32 MiB、名为 `sl3000-nor-probe` 的只读原始分区，不预先认定 factory 的实际内容有效。
- 不写 NOR 存储内容，不添加可写 U-Boot 环境分区。禁用分区主设备的额外暴露，内核选择 `CONFIG_MTD_SPI_NOR_SWP_KEEP=y`，避免探测时解除原有块写保护。SPI 初始化仍可能发送正常的易失性状态或寻址模式命令，“只读”不是不初始化控制器。
- 源码、mt76 和无线 MCU 版本不变；设备树和上述 NOR 保护策略是本轮内核相关变化。eMMC 布局、升级写入范围、Wi-Fi 校准文件及其加载方式不变。
- CI 验证 sysupgrade 和 initramfs FIT 中的实际设备树、NOR 只读标志、SPI 接线、内核保护选项，以及原有私有 EEPROM 的逐字节一致性。系统内 `/etc/sl3000-nor-probe.json` 标记诊断类型，不含校准内容。
- Actions 选择 `rf_test=true`、`nor_probe=true`、`publish_release=false`、`public_setup_password=true`。产物名为 `sl3000-emmc-nor-probe-<run id>`；内部仍使用 `sl3000-rf-test.tar.gpg` 和现有私有解密口令。
- 不自动采集或上传 NOR 内容。安装后再经 SSH 只读检查 JEDEC/SFDP、实际容量、候选校准区域及 MTD 写保护状态。此步骤不要求测速。

#### 从当前本机实验固件保留配置升级

仅适用于当前同一 OpenWrt 25.12.5 基线、同一设备和已有 GPT 布局；不能推广到旧厂商固件或跨大版本迁移。

1. 在 LuCI 的“系统 → 备份/升级”先下载配置备份，保存在本机；备份包含私人配置，不上传到公开仓库。确认重要的自定义文件在备份列表内，额外安装的软件不会因“保留配置”自动重新安装。
2. 上传解密后的 `*-sl_3000-emmc-squashfs-sysupgrade.bin`，勾选“保留配置”。不要选择 `initramfs.itb`，也不要用 U-Boot 刷写来替代这条保留配置的升级路径。
3. 如果兼容性或分区检查失败，停止升级，不使用强制选项。升级前保留现在正常镜像的离线副本，并准备有线管理方式。
4. 本版本在检测到 OpenWrt 正在恢复 sysupgrade 配置时，跳过本项目全部首启默认值，不重置 Wi-Fi、LAN、DNS、PassWall/OpenClash 开关或系统设置。全新安装仍应用公开临时 Wi-Fi 等默认值。

“NOR 只读”只约束诊断对象；sysupgrade 仍会更新 eMMC 上的系统。保留配置和离线备份都需要，不能把实验升级视为零风险。

### 缓存与编译并发

- 下载目录 `dl` 与编译缓存 `.ccache` 分别恢复、保存，不缓存整个源码树、`build_dir`、`staging_dir`、注入密码的脚本或成品固件。
- 下载成功后立即保存源码缓存，即使后续编译失败也能复用。优先匹配相同配方，未命中时回退到同一 runner 环境的下载缓存，仍执行源码下载校验。普通构建命中相同配方时不重复上传；干净重建总是尝试保存新快照。
- 编译缓存只在编译成功并完成缓存统计后保存，不依赖后续固件上传成功。缓存键隔离 runner 系统、架构和完整构建配方；包含 run ID 与重跑次数，避免覆盖不可变缓存。ccache 按编译器内容识别兼容性，并限制为 2 GiB。缓存服务失败不会绕过构建与产物校验，也不应单独导致固件构建失败。
- 编译任务数取 CPU 数和内存预算的较小值：从可用内存中预留 1 GiB，每个 make 任务按 3 GiB 预算，至少为 1。公开仓库的标准 `ubuntu-24.04` runner 为 4 CPU / 16 GB，内存充足时使用 `-j4`；内存不足时自动降低。这个预算不是硬性内存限制，Go、Rust 和链接阶段仍需要观察实际占用。下载仍使用 `-j8`。
- Actions 日志记录实际 CPU、可用内存和下载耗时，摘要记录编译耗时、并发数、恢复的缓存键、ccache 命中统计和缓存大小。ccache 主要加速 C/C++，暂不增加 Go/Rust 编译缓存，不承诺固定提速比例。
- 工作流更新仅作用于使用新提交启动的构建；已经运行的任务不会自动获得这些改动。

## 首次验收与升级

1. 在原系统之外保存配置和数据备份，准备经过验证的旧固件恢复镜像，确认现有 U-Boot 的恢复方式及支持的镜像格式。
2. 优先通过**已确认可用的 RAM 启动方式**测试 initramfs；不要把 initramfs 写入 GPT 或引导分区。不能假定 U-Boot Web 接受 sysupgrade tar。
3. 验证 1 GiB 内存、全部 LAN/WAN 端口、eMMC 三个分区、USB、LuCI、双频 Wi-Fi、休眠后重连和负载下持续运行。检查 `dmesg` 的 EEPROM/mt76/以太网错误。
4. 配置一个代理后验证 DNS、Google、YouTube 和 Emby；同一目标分别测试 IPv4/IPv6、TCP/UDP。换固件不会自动修复节点、分流规则或客户端 DNS 绕行问题。
5. 验收通过后才考虑系统升级，首次从旧 23.05 升级**不保留设置**；先做镜像兼容性检查，不使用强制升级。旧系统的升级代码不包含本固件新增的分区检查，第一次刷写必须人工核对布局。
6. `storage` 数据保留不等于免备份；本工作流不刷写路由器，也不修复备份 GPT。

## 本地验证

```sh
python3 -m unittest discover -s devices/sl3000_emmc/tests -v
sh -n devices/sl3000_emmc/firstboot.sh
actionlint .github/workflows/sl3000-emmc.yml
```

在 Linux 的新构建目录运行 `prepare.py <openwrt-dir>`，随后执行 `make -C <openwrt-dir> defconfig` 和 `verify.py config <openwrt-dir>`。已有准备完成的目录不能再次运行 prepare；它会拒绝重复覆盖。
