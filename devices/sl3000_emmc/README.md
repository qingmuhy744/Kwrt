# SL-3000 eMMC 专用固件

**本仓库仅服务于 SL-3000 eMMC。** 不维护其他路由器或 SL-3000 非 eMMC 版本；仓库中其他设备目录仅为上游历史保留，不属于支持范围。即使同为 SL-3000 eMMC，也必须核对下列硬件与分区条件，不能视为所有硬件修订版通用。

本目录独立构建 OpenWrt **25.12.5 / Linux 6.12**。不调用 Kwrt 通用定制脚本，不关闭内核 ABI 校验，不拉取滚动版本。

**首版属于待实机验证版本，不是已认证的稳定固件。不要仅凭 CI 通过就直接刷入。**

## 适用设备

- 设备标识：`sl,3000-emmc`，1 GiB RAM，MT7981 + MT7531。
- 已有 GPT：`mmcblk0p1=kernel`（起始扇区 8192，65536 扇区），`mmcblk0p2=rootfs`（起始 73728，4096000 扇区），`mmcblk0p3=storage`（起始 4169728）。
- 系统升级只定位并更新 `kernel`、`rootfs`；不格式化 `storage`，也不默认挂载或运行其中的旧 Docker 数据。
- 不提供或写入 BL2、FIP、GPT、U-Boot。不要重刷分区表。
- 禁用在线自动固件升级入口，避免自定义设备跳转到不匹配的官方镜像；更新内核和驱动请重新编译整套固件，不要强制安装其他 ABI 的 kmod。

2026-09-05 首次只读检查发现：设备 GPT 没有 `factory`，当时的 vendor Wi-Fi 驱动报告 EEPROM 无效并使用默认文件。默认构建移除不存在的 NVMEM 引用，包含与锁定 mt76 源码配套的通用 EEPROM。它**不能恢复每台设备的原厂射频校准**。后续实测该默认构建存在 Wi-Fi 覆盖和吞吐问题；不能当作已修复版本使用。以太网和 AP MAC 从设备 eMMC CID 派生，避免所有机器共享默认 EEPROM 的 MAC；升级后 MAC 可能与旧固件不同。

2026-09-06 对正常运行的同一设备完成 NOR 只读诊断：独立的 32 MiB SPI-NOR 内存在 Factory，位置为 `0x180000`、长度 `0x200000`，其前 4096 字节是本机 EEPROM；驻留旧固件设备树也确认了该布局。新增的 `factory_test` 公共测试模式读取这个区域，不再把该设备的运行时 EEPROM 放进镜像。此发现仅验证了样本设备的布局和数据，尚不能代表所有同名机型已通过实机验收。

## 内置内容

- Tailscale 与官方 feed 的 `luci-app-tailscale-community`。
- PassWall、Xray、sing-box、ChinaDNS-NG、dns2socks、dnsmasq-full、nft socket/tproxy 模块。
- OpenClash 与已内置的 AArch64 Mihomo Meta 核心，首次配置不需要另行下载核心。
- 保留 OpenClash 所需的 Ruby/YAML，关闭可选的 Ruby YJIT，避免因此从源码构建 Rust/LLVM 工具链。配置校验拒绝重新开启 YJIT，产物校验检查未生成 Rust 主机工具链；不改变代理核心、无线驱动或 EEPROM。
- nftables UPnP、LuCI 网络唤醒与 etherwake。
- MMC、USB 3、ext4、F2FS、block-mount 和检查工具。

PassWall、OpenClash、Tailscale、UPnP 默认关闭。没有订阅、节点、账号、Tailscale 登录状态或 root 密码。两种透明代理一次只启用一个。

PassWall 的系统服务保留启用，以便 LuCI 的“保存并应用”能够触发服务重载；代理主开关和访问控制仍默认关闭，不会自动接管网络。

## 初始网络

- LAN：`192.168.21.1/24`，DHCP 开启，后台 `http://192.168.21.1`。
- 实体网口为 `lan1`、`lan2`、`lan3` 和 `wan`。在上游 `02_network` 中一次性登记，后续脚本只设置逐机 MAC，不再追加端口列表。
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

上述不选测试模式的构建仍是旧的通用 EEPROM bootstrap。要测试本机 Factory 读取，请按下面的公共测试选项触发，避免误下载旧模式。

产物只包括设备 sysupgrade、initramfs FIT、包清单、OpenWrt/kernel 配置、来源锁定清单、构建信息和 SHA-256。不会上传完整构建目录、注入密码的脚本或未筛选的 `bin/targets` 目录。失败构建不发布镜像。

所有来源见 `sources.lock.json`，官方 feeds 与 25.12.5 发布的 `feeds.buildinfo` 一致。第三方插件也按 commit 固定；Mihomo 使用上游发布的压缩二进制并验证 SHA-256。升级这些来源需要显式修改锁文件及相应包配方后重跑验证。固定源码不代表比特级可重复，也不能替代安全更新。

### 公共 Factory 校准测试

此模式只面向匹配上述 eMMC GPT、SPI2 接线、32 MiB NOR / Factory 布局的 SL-3000，首轮样本为 MT7981 / MT7976C A-die v1。它不是所有 SL-3000 硬件修订的稳定通刷版。

- 开启 `factory_test=true`，关闭 `rf_test`、`nor_probe`、`publish_release`，确认 `public_setup_password=true`；`clean_build=false` 保留下载缓存复用。模式互斥和禁止 Release 由预检强制执行。
- 每台路由器启动时从自己的 NOR Factory 前 4096 字节读取 EEPROM，通过标准 NVMEM `eeprom` 单元交给 mt76。仓库和产物中不包含样本设备的 NOR、EEPROM、MAC 或其他私人校准数据；公共构建步骤不接收私人校准或加密 Secret。
- 按用户选择保留原版 mt76 的加载逻辑及配套的**公开通用 EEPROM 兜底文件**。正常顺序是设备树提供的 Factory 数据、驱动原有 eFuse 尝试、在适用错误条件下回退到通用文件；未改驱动来保证所有硬件故障都能启动。触发通用文件回退时，日志会出现 `eeprom load fail, use default bin`，覆盖效果可能退回此前信号较差的状态。
- 不更换锁定的内核、mt76 或 MCU 版本，不修改射频算法、功率表、信道、国家码、以太网和 AP 的 MAC 派生策略。Factory 内容与私有运行时 EEPROM 并非逐字节相同，需要实机确认双频连接和覆盖；不要求测速。
- 只暴露 `factory` 这一个只读 MTD 分区，不暴露整个 NOR 或可写引导环境。禁用额外分区主设备，保留 `CONFIG_MTD_SPI_NOR_SWP_KEEP=y`。这是 Linux 分区访问限制和保留原保护状态的策略，不是修改芯片永久锁，也不限制 eMMC 上保存配置或正常系统升级。
- CI 检查 sysupgrade 与 initramfs 的实际设备树、Factory 范围、只读标志、EEPROM 单元大小和连接，以及内核 NVMEM / NOR 选项；分别检查两种镜像内的兜底文件与公开锁定 SHA-256 一致，拒绝私有测试标记。
- 下载 `sl3000-emmc-factory-test-<run id>`，普通解压即可，**没有 GPG 密码**。仍校验包内 `sha256sums`。`build-info.json` 的 `wifi_profile` 为 `public-factory-eeprom-test`，`nor_profile` 为 `read-only-factory-nvmem`；系统内的 `/etc/sl3000-factory-test.json` 只标记设计意图，不是运行时成功证明。
- 当前同一 25.12.5 基线可按下文用 LuCI 的 sysupgrade 镜像保留配置升级，提前保存离线备份和正常固件，准备有线管理。不要刷 GPT / BL2 / FIP，不需要重新刷 U-Boot。CI 不自动刷机，不创建 Release。
- 刷后先只读检查 `factory` 分区标志、内核日志，再在本地比对 mt76 debugfs 的运行时 EEPROM 与本机 Factory 前 4096 字节及 ROM 中公开兜底文件，确定实际加载来源。不要只凭信号变好或没有回退日志就认定成功，不把原始校准内容上传到公开仓库。再检查双频连接和原先弱信号位置；至少需要另一台匹配设备的反馈后，才考虑扩大支持范围。

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
2. 上传对应模式的 `*-sl_3000-emmc-squashfs-sysupgrade.bin`，勾选“保留配置”；私有模式需先解密，公共 Factory 测试版直接解压。不要选择 `initramfs.itb`，也不要用 U-Boot 刷写来替代这条保留配置的升级路径。
3. 如果兼容性或分区检查失败，停止升级，不使用强制选项。升级前保留现在正常镜像的离线副本，并准备有线管理方式。
4. 本版本在检测到 OpenWrt 正在恢复 sysupgrade 配置时，跳过本项目全部首启默认值，不重置 Wi-Fi、LAN、DNS、PassWall/OpenClash 开关或系统设置。全新安装仍应用公开临时 Wi-Fi 等默认值。

另有独立的 `98-sl3000-ports` 升级迁移：旧版本先登记四个 LAN、再追加三个 LAN，造成概览重复和不存在的 `lan4`。新版本只对 `sl,3000-emmc` 的已知错误组合进行修正。旧 `board.json` 的七项列表改为三个真实 LAN；网络配置只有同时符合旧七项桥端口列表、匿名纯 MAC 设备节及原 MAC 的特征时，才删除重复设备节和虚构端口。自定义端口列表、MAC、设备选项、命名设备节、对 `lan4` 的 VLAN/接口引用不强制迁移；其他网络选项保留。检测到尚未提交的 network 变更时延后迁移并记日志。

迁移前的 `board.json` 和 `network` 备份保存在路由器 `/etc/sl3000-port-backup-v1/`，目录权限 0700、文件 0600，不上传。脚本可重复运行，不重建整份网络配置，也不主动重启网络；它在升级后正常启动流程中执行。CI 使用同一锁定版本的原生 UCI/ucode 测试旧配置迁移、重复运行、自定义设置和异常输入，并检查两种公共镜像包含修复文件。

“NOR 只读”只约束诊断对象；sysupgrade 仍会更新 eMMC 上的系统。保留配置和离线备份都需要，不能把实验升级视为零风险。

### 缓存与编译并发

- 下载目录 `dl` 与编译缓存 `.ccache` 分别恢复、保存，不缓存整个源码树、`build_dir`、`staging_dir`、注入密码的脚本或成品固件。
- 下载成功后立即保存源码缓存，即使后续编译失败也能复用。优先匹配相同配方，未命中时回退到同一 runner 环境的下载缓存，仍执行源码下载校验。普通构建命中相同配方时不重复上传；干净重建总是尝试保存新快照。
- 编译缓存只在编译成功并完成缓存统计后保存，不依赖后续固件上传成功。缓存键隔离 runner 系统、架构和完整构建配方；包含 run ID 与重跑次数，避免覆盖不可变缓存。ccache 按编译器内容识别兼容性，并限制为 2 GiB。缓存服务失败不会绕过构建与产物校验，也不应单独导致固件构建失败。
- 编译缓存还按 generic / rf-test / nor-probe / factory-test 模式隔离；私有 RF/NOR 测试仍不保存编译缓存。公共 Factory 模式不注入私人校准，因此允许保存该模式的编译缓存。
- 编译任务数取 CPU 数和内存预算的较小值：从可用内存中预留 1 GiB，每个 make 任务按 3 GiB 预算，至少为 1。公开仓库的标准 `ubuntu-24.04` runner 为 4 CPU / 16 GB，内存充足时使用 `-j4`；内存不足时自动降低。这个预算不是硬性内存限制，Go、Rust 和链接阶段仍需要观察实际占用。下载仍使用 `-j8`。
- Actions 日志记录实际 CPU、可用内存和下载耗时，摘要记录编译耗时、并发数、恢复的缓存键、ccache 命中统计和缓存大小。ccache 主要加速 C/C++，暂不增加 Go/Rust 编译缓存，不承诺固定提速比例。
- 工作流更新仅作用于使用新提交启动的构建；已经运行的任务不会自动获得这些改动。
- 可在 `compare_run` 填入同一工作流、同一分支的基准构建编号。构建完成后，摘要自动对比两轮成功的 `Compile` 步骤耗时，列出节省或增加的时间和百分比，不计排队、下载、校验和上传时间。仅读取 GitHub 步骤元数据，不读取或上传私人校准数据；对照报告失败不会影响固件产物。
- 首轮关闭 YJIT 的对照基准为 NOR 诊断构建 `34017054854`（提交 `238df703`）。两轮保持 `rf_test=true`、`nor_probe=true`、`clean_build=false`、不发布 Release；Go、编译并发、缓存策略和源码锁定均不变。基准未命中编译缓存，新配方的首次构建也没有匹配的编译缓存；两轮均允许下载缓存。最终仍需检查实际 runner 和缓存统计，不能把虚拟机性能波动当作确定的优化收益。

## 首次验收与升级

1. 在原系统之外保存配置和数据备份，准备经过验证的旧固件恢复镜像，确认现有 U-Boot 的恢复方式及支持的镜像格式。
2. 优先通过**已确认可用的 RAM 启动方式**测试 initramfs；不要把 initramfs 写入 GPT 或引导分区。不能假定 U-Boot Web 接受 sysupgrade tar。
3. 验证 1 GiB 内存、全部 LAN/WAN 端口、eMMC 三个分区、USB、LuCI、双频 Wi-Fi、休眠后重连和负载下持续运行。检查 `dmesg` 的 EEPROM/mt76/以太网错误。
4. 配置一个代理后验证 DNS、Google、YouTube 和 Emby；同一目标分别测试 IPv4/IPv6、TCP/UDP。换固件不会自动修复节点、分流规则或客户端 DNS 绕行问题。
5. 验收通过后才考虑系统升级，首次从旧 23.05 升级**不保留设置**；先做镜像兼容性检查，不使用强制升级。旧系统的升级代码不包含本固件新增的分区检查，第一次刷写必须人工核对布局。
6. `storage` 数据保留不等于免备份；本工作流不刷写路由器，也不修复备份 GPT。

## 安全更新与 Telegram 提醒

独立工作流 **SL-3000 Security Monitor** 每天北京时间 09:17 检查，只将当前固件已包含组件、尚未纳入的明确安全修复推送 Telegram。通知按组件合并，列出当前版本、修复链接和重新构建刷入的处理方式。日常仅推送新增或变化的待处理项；周一只汇总仍待处理的修复，没有待处理项则保持安静。手动 `force_report=true` 可以验证通知链路。它不会修改锁文件、构建或刷写固件。

- 检查 `sources.lock.json` 中的官方同系列分支、第三方分支及正式版本，同时读取脚本所列上游仓库公开的 GitHub 安全公告。官方公告之外的邮件列表、传递依赖等未全部覆盖；版本差异和关键词匹配只是待核实线索，报告不会声称“固件无漏洞”。
- 按维护约定，默认假定设备运行 GitHub 默认分支当前配方构建的固件。`security-baseline.json` 保存已校验构建 34033726009 的公开包清单；监控会校验锁文件和固件配方输入摘要，不能将不匹配的旧清单当成新固件。后续公共构建会额外上传小型 `sl3000-security-inventory-*` 构建产物，监控自动读取匹配配方的清单并缓存。找不到匹配构建时发送检查失败提醒，不报告“没有漏洞”。可设置 Actions Variable `SL3000_DEPLOYED_RECIPE` 为指定配方完整 SHA；仍需要匹配的构建清单。
- 根据真实 APK 名称过滤未安装包和 TLS 库变体，并排除已核实的功能范围、其他操作系统及已修复的代理组件公告。额外跟踪 uhttpd、cgi-io、odhcpd、rpcd、ubus、netifd、umdns 的精确上游提交。无法映射、只有模糊关键词或涉及本地回移补丁的项目保留在完整报告待核实，不发送漏洞告警。构建包存在且缺少上游安全修复不等于所有漏洞触发条件都已在实机验证；静态依赖、运行配置和完整漏洞覆盖仍有边界。
- 在仓库 Actions Secrets 中设置 `TELEGRAM_TOKEN` 和 `TELEGRAM_CHAT_ID`，仅通知步骤读取它们。个人私聊需先向机器人发送 `/start`。凭据不写入代码、固件、报告或附件，发送保留 TLS 校验，超时/临时错误有限重试，限流按 `retry_after` 处理。
- 默认分支估算 **45、55、59 天无提交**时分别提醒，有待处理修复的周报也列出无提交天数。GitHub 的公开仓库定时任务可能在 60 天无仓库活动后停用；提交时间只是保守参考，其他仓库活动和推送时间可能影响实际期限。发现新的默认分支提交后重新计时，不自动创建保活提交。
- 定时工作流只在默认分支（当前为 `25.12`）启用。GitHub 调度可能延迟或跳过；工作流停用后无法自行发出提醒，需要在 Actions 页面重新启用，应保留独立的上游公告订阅。
- 完整 Markdown/JSON 报告写入 Actions 摘要与保留 30 天的附件。仓库关闭 Issues，不创建 Issue。通过 Actions cache 保存已送达告警状态；缓存丢失可能导致重复提醒。发送失败不确认送达，下次重试；网络超时存在重复送达的可能。
- 数据源查询失败会明确标记“检查不完整”并通知，最近完整检查时间不前进。TG 最终发送失败或查询不完整都会令任务失败，完整报告仍保留在可生成报告的运行中。

监测发现需要升级后，再审查修复、更新锁文件及必要的包配方，构建、验证并安装新固件。只有确认路由器已升级，才能更新所登记的部署基准。

## 本地验证

```sh
python3 -m unittest discover -s devices/sl3000_emmc/tests -v
sh -n devices/sl3000_emmc/firstboot.sh
actionlint .github/workflows/sl3000-emmc.yml
```

在 Linux 的新构建目录运行 `prepare.py <openwrt-dir>`，随后执行 `make -C <openwrt-dir> defconfig` 和 `verify.py config <openwrt-dir>`。已有准备完成的目录不能再次运行 prepare；它会拒绝重复覆盖。
