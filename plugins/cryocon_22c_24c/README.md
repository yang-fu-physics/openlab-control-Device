# Cryo-con 22C / 24C

本插件只创建一个 `temperature` 设备和一个 USB VISA 会话：B 是主温度，A 作为同一
快照内的 `2nd Stage` 只读附加值。它不提供磁场。由独立回读电路得到的实际加热输出
（`HTRREAD?`，单位为满量程百分比 `%FS`）和加热量程也随快照写入主 DAT 与
`device_status.dat`。

## 配置

把插件目录复制到核心仓库的 `device_plugins/cryocon_22c_24c/`，然后用下面结构替换
温度设备。VISA 地址和 PID 数值必须取自现场已经验证的 22C/24C 配置，不能照抄示意值。

```toml
[application]
# 前面板约 1 秒刷新一次；Measurement Module 调用 api.devices() 时仍会即时读取。
poll_interval_seconds = 1.0

[[devices]]
id = "temperature"
display_name = "Temperature"
kind = "temperature"
role = "primary"
control_enabled = true
plugin = "cryocon_22c_24c"
unit = "K"
min_value = 2.0
max_value = 400.0
max_rate_per_minute = 10.0
stale_after_seconds = 3.0
operation_timeout_seconds = 10.0
shutdown_timeout_seconds = 3.0
address = "USB0::...::INSTR"
io_timeout_seconds = 1.0
monitor_a_max_temperature = 320.0
hold_rate_per_minute = 1.0

[devices.stability]
tolerance = 0.05
max_slope_per_minute = 0.05
dwell_seconds = 30.0
timeout_seconds = 1800.0
window_seconds = 30.0
stale_after_seconds = 3.0

# 按 temperature 从低到高填写；最后一行必须覆盖 400 K。
# 目标温度 < 第一行 temperature 时用第一组 PID；达到边界后使用下一组。
# 达到最后一行边界时仍使用最后一组 PID。
# 下列尖括号是占位符，不是可直接解析的 TOML 数值。
[[devices.pid_zones]]
temperature = <ZONE_1_BOUNDARY_K>
p_gain = <VALIDATED_P>
i_gain = <VALIDATED_I>
d_gain = <VALIDATED_D>
# heater_range 可省略；省略时保留仪表当前量程。
heater_range = "LOW"

[[devices.pid_zones]]
temperature = 400.0
p_gain = <VALIDATED_P>
i_gain = <VALIDATED_I>
d_gain = <VALIDATED_D>
heater_range = "MID"
```

## 运行规则

- Enable/启动只读身份和状态，不自动写入保存的设置。
- 仅接受 Cryo-con 22C 或 24C、Loop 1、输入 B、`RAMPP`、控制已开启、A/B 单位 K。
- 每次设温依次写入并回读 P、I、D、可选量程、速率、目标温度；任一回读不一致立即
  报 Error，不自动重发。
- A 或 B 的传感器报警、Loop 1 加热故障、A 高于 320 K 都是立即中止 SEQ 的安全错误。
- 普通通讯失联由核心尝试重连 60 秒；恢复时核对仪表实际目标和速率。
- Stop、退出和断开只关闭 VISA 会话或把目标保持在即时 B 读数，不关闭加热输出。

首次连接真实仪表前，请先用伪负载或安全温区核对 PID 区间、USB VISA 地址、B 输入和
Loop 1 的 `RAMPP` 状态，并保留硬件独立的过温保护。

默认仿真配置中的独立 `field` 和 `second_stage` 设备块应从现场配置删除：此接线没有磁场
设备，A 已经由本插件通过同一个 USB 会话显示和记录，不能再为 A 打开第二个设备连接。
