# SL-3000 eMMC 专用 OpenWrt 固件

**本仓库仅服务于 SL-3000 eMMC，维护该型号的固件适配、构建和验证。** 不提供其他路由器、SL-3000 非 eMMC 版本或其他硬件修订版的适配承诺，不是通用固件下载或多设备在线定制项目。

本项目从 [kiddin9/Kwrt](https://github.com/kiddin9/Kwrt) 派生。仓库中保留的其他设备目录、脚本和资源属于上游历史内容，**不代表本仓库维护或支持这些设备**。

## 支持范围

- 设备标识为 `sl,3000-emmc`，MT7981 + MT7531，1 GiB RAM，使用 eMMC 启动。
- 必须匹配[设备说明](devices/sl3000_emmc/README.md)中的 GPT 分区布局；公共 Factory 测试版还要求匹配 SPI-NOR 接线和 Factory 校准区布局。不能只凭外壳型号或“eMMC 版”字样判断兼容。
- 仅更新系统的 `kernel`、`rootfs` 分区，不提供或写入 BL2、FIP、GPT、U-Boot，不要求重刷分区表。
- 当前仍是公共测试固件，不是所有 SL-3000 eMMC 硬件修订版的已认证稳定通刷版。

## 固件内容

- 独立构建固定源码版本的 OpenWrt 25.12.5 / Linux 6.12，不调用上游通用定制流程。
- 内置 Tailscale、PassWall、OpenClash 与 Mihomo 核心、UPnP、网络唤醒，以及常用存储支持。
- 公共 Factory 模式从每台设备自己的只读 Factory 区读取 Wi-Fi 校准，不内置样本设备的私人 EEPROM；保留公开通用 EEPROM 兜底，回退后可能出现信号较差的问题。
- 不内置订阅、节点、账号或 root 密码。首次安装启用 Wi-Fi，使用可公开的临时密码，首次登录后应立即修改管理密码和 Wi-Fi 密码。

## 构建与使用

从 [SL-3000 eMMC 构建页面](https://github.com/qingmuhy744/Kwrt/actions/workflows/sl3000-emmc.yml) 进入，使用 `25.12` 分支。公共校准测试需选择 `factory_test=true`、`public_setup_password=true`，保持 `rf_test=false`、`nor_probe=false`、`publish_release=false`；不要把未选择 `factory_test` 的旧通用 EEPROM 模式当作无线修复版。

完整的硬件条件、构建选项、产物校验、保留配置升级和恢复注意事项见 [SL-3000 eMMC 设备说明](devices/sl3000_emmc/README.md)。刷机前先核对兼容性并保存离线备份，不使用强制升级。

配方位于 [`devices/sl3000_emmc`](devices/sl3000_emmc)，工作流位于 [`.github/workflows/sl3000-emmc.yml`](.github/workflows/sl3000-emmc.yml)。

## Acknowledgments

- [Kwrt](https://github.com/kiddin9/Kwrt)
- [OpenWrt](https://github.com/openwrt/openwrt)
- [Lean's OpenWrt](https://github.com/coolsnowwolf/lede)
- [ImmortalWrt](https://github.com/immortalwrt/immortalwrt)
- [iStoreOS](https://github.com/istoreos)
- [unifreq](https://github.com/unifreq/openwrt_packit)
- [ophub](https://github.com/ophub/amlogic-s9xxx-openwrt)
- [hanwckf](https://github.com/hanwckf/immortalwrt-mt798x)
- [aparcar](https://github.com/openwrt/asu)
- [GitHub](https://github.com)
- [GitHub Actions](https://github.com/features/actions)

