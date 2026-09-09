# SL3000 设备限速方案考察

核对日期：2026-09-09。此次构建仅加入 vnStat，没有安装限速插件或修改设备限速规则。

## 候选结论

| 方案 | 按设备硬限速 | 星期、时间段 | 结论 |
| --- | --- | --- | --- |
| Bandix Plus | 支持按 MAC 与接口匹配，分别限制 IPv4/IPv6 上下行 | 有 LuCI 界面和后端调度，支持跨午夜 | 最接近需求，适合下一步在单台测试设备上验证 |
| SQM / CAKE | 主要解决拥塞延迟和设备间公平分配 | 没有原生的逐设备时间表 | 适合改善抢网、延迟，不直接满足固定设备限额 |
| nft-qos / luci-app-nft-qos | 曾提供 IP/MAC 限速 | 需另加调度 | 已被官方移除，不采用旧版本重新引入 |
| 自建 tc + LuCI 调度 | 可把同一设备 IPv4/IPv6 放入同一限速类 | 可实现 | 适合必须统一总速率上限的需求，但开发维护量更大 |

官方移除 nft-qos 的原因是多个未解决故障、维护者失联以及长期缺乏维护：[删除提交](https://github.com/openwrt/packages/commit/14bff6e90ef8ebe22d72b4ede398e306ea3b46da)。本配方锁定的 packages 和 LuCI feed 已不包含该插件。

## Bandix Plus 的实现与边界

[LuCI Bandix Plus](https://github.com/timsaya/luci-app-bandix-plus) 提供设备选择、星期、起止时间和限速编辑。后端 [bandix-plus v0.1.2](https://github.com/timsaya/bandix-plus/releases/tag/v0.1.2) 是正式发布版，Rust 与 eBPF 源码公开；OpenWrt 包配方使用带 SHA-256 的架构对应二进制。

调度规则以“接口 + 设备 MAC + 星期 + 时间段”为单位。[后端策略代码](https://github.com/timsaya/bandix-plus/blob/v0.1.2/bandix-plus/src/policy.rs) 使用路由器本地时间，周期性更新 eBPF 限速表；多条生效规则按各方向的更严格非零限额合并，0 表示不限速。跨午夜的后半段归属开始日。更换设备随机 MAC 会改变设备身份，规则需要跟随更新。

[内核侧实现](https://github.com/timsaya/bandix-plus/blob/v0.1.2/bandix-plus-ebpf/src/main.rs) 是 TC/eBPF 令牌桶，超限丢包，不是排队整形。上下行、IPv4/IPv6 分别使用独立额度：分别设置 IPv4 和 IPv6 为 10 Mbit/s，不能承诺两者合计仍只有 10 Mbit/s。若必须限制设备所有 IP 流量的合计速率，应增加共享令牌桶，或将两种地址族归入同一个 tc 限速类。

发现一个时间语义差异：当前 LuCI 源码将相同起止时间显示为所选日全天有效，而 v0.1.2 后端只匹配这一分钟。集成前应修正或禁止相同起止时间；全天规则可明确设置为 00:00–23:59。界面显示状态还需核对异地浏览器与路由器时区不同时的行为。

## 对当前 SL3000 的适配要求

- 当前使用 `phy1-sta0` 无线 WAN，设备在 LAN 侧识别；Passwall 透明代理会终止并重新发起连接，不能只凭 WAN 上 NAT 后的源 IP 做逐设备限速。
- 上游要求关闭硬件流量卸载 / Turbo ACC。实机当前没有 nft flowtable，mt7915e 的 WED 开关为 `N`；本次未改变这些设置。
- 需要核对并补齐 TC/eBPF 所需内核配置和模块，再验证 LAN 网桥与无线设备的挂载点。源码支持不等于已经在这台设备上跑通。
- 验证应覆盖 IPv4/IPv6、上下行、TCP/UDP、Passwall 流量、跨午夜、重启后恢复，以及局域网传输是否被纳入限制。先以单台客户端测试，不直接开启全网默认限额。
- 集成时锁定前后端版本及校验值，保留规则和数据的备份路径，并纳入现有软件安全检查。此次固件不加入该插件。
