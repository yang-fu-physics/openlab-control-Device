# Cryo-con 22C / 24C

本 System Instrument 只创建一个 `temperature` 仪表和一个 USB VISA 会话：B 是主温度，A 作为同一
快照内的 `2nd Stage` 只读附加值。它不提供磁场。由独立回读电路得到的实际加热输出
（`HTRREAD?`，单位为满量程百分比 `%FS`）和加热量程也随完整快照写入
`instrument_status.dat`。实验测量行只发送 `INPUT B:TEMPERATURE?`；A、加热输出和量程的
固定列在该行留空，不把上次监视读数伪装成同步测量值。

## 配置

把完整目录复制到核心仓库的 `system_instruments/cryocon_22c_24c/`，然后运行 Instrument
Scanner。选择 USB VISA 资源后，扫描器创建一个 Sample Temperature controller 和一个
Temperature and Heater 监视面板；两者共用同一个物理实例和 USB 会话。

每个物理实例使用自己的 `configs/pid/<instance-id>.toml`。首次保存该实例时，扫描器把
`pid.example.toml` 复制到这个位置；以后再次保存或移除实例都不会覆盖或删除 PID 文件。
内置示例故意只有 `zones = []`，不能直接运行。必须填写该冷却系统已经在现场验证过的 PID
数值：

```toml
[[zones]]
temperature = 10.0 # 严格上边界
p_gain = 1.0
i_gain = 4.0
d_gain = 1.0
heater_range = "LOW" # 可省略；省略时保留当前量程

[[zones]]
temperature = 400.0
p_gain = 2.0
i_gain = 8.0
d_gain = 2.0
heater_range = "MID"
```

温度边界必须严格递增，最后一行必须覆盖 controller 的最大温度；P/I/D 必须在 0–1000，
D 不得超过 I/4，量程只能是 LOW/MID/HI。文件缺失、TOML 损坏、空 zones 或任一安全条件
不满足时，后台在连接仪表前快速失败。TempB 是 controller 的固定主读数；TempA、加热输出
和量程由同一次读取返回，不另开第二个 USB 会话。

## 运行规则

- Enable/启动只读身份和状态，不自动写入保存的设置。
- 仅接受 Cryo-con 22C 或 24C、Loop 1、输入 B、`RAMPP`、控制已开启、A/B 单位 K。
- 每次设温依次写入并回读 P、I、D、可选量程、速率、目标温度；任一回读不一致立即
  报 Error，不自动重发。
- 写测量数据时只读取 TempB；完整控制轮询仍读取 A/B 报警、Loop 1、加热输出和量程。
- A 或 B 的传感器报警、Loop 1 加热故障、A 高于 320 K 都是立即中止 SEQ 的安全错误。
- 普通通讯失联由核心尝试重连 60 秒；恢复时核对仪表实际目标和速率。
- Stop、退出和断开只关闭 VISA 会话或把目标保持在即时 B 读数，不关闭加热输出。

首次连接真实仪表前，请先用伪负载或安全温区核对 PID 区间、USB VISA 地址、B 输入和
Loop 1 的 `RAMPP` 状态，并保留硬件独立的过温保护。

默认仿真配置中的独立 `field` 和 `second_stage` 仪表块应从现场配置删除：此接线没有磁场
仪表，A 已经由本 System Instrument 通过同一个 USB 会话显示和记录，不能再为 A 打开第二个仪表连接。
