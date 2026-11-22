# Cryo-con 22C / 24C

一个物理实例共用一个 VISA 会话，只控制 Loop 1。主温度可选 Temp A 或 Temp B；
默认 CH A 为主温度，CH B 为只读辅助温度。不查询或写入 Loop 2。

USB 虚拟串口使用 `ASRL…::INSTR` 地址，8N1、LF 终止符，波特率必须与面板一致，
可选 9600、19200、38400、57600、115200；同时保留 USB、LAN/TCP 和 GPIB VISA 地址支持。
若面板波特率不是默认 9600，Instrument Scanner 自动扫描可能无法识别，可手动填写 `ASRL` 地址并在配置中选择对应波特率。

## 配置

把完整目录放入 System Instruments 目录，在 Instrument Scanner 中选择资源、
波特率、Loop 1 主温度和监视读数。只有 Loop 1 已使用所选输入、处于 RAMPP 且
CONTROL ON 时才允许控制；连接只读验证，不自动切换输入或开启加热。

每个物理实例使用自己的 `configs/pid/<instance-id>.toml`。
`pid.example.toml` 是空模板，必须填入现场已经确认的参数，不能用测试夹具代替。

```toml
[[zones]]
temperature = 10.0 # 严格上边界
p_gain = 1.0
i_gain = 4.0
d_gain = 1.0
# heater_range = "LOW" # 可选：LOW/MID/HI；不填写则保留仪表当前量程
```

温度边界必须严格递增，最后一行覆盖 controller 最大温度。P/I/D 必须为有限值且
在远程命令的 0–1000 范围内。手册建议 D 不超过 I/4；软件保留现场确认的原值，
不再以这个比例拒绝或钳位参数。空表、非法数值或非法量程仍快速失败。

## 读写与运行

- 完整状态用复合查询读取 A/B、Loop 1、控制状态、加热回读和量程；使用固件支持的
  `INPUT? A/B` 温度查询，斜坡状态支持 ON/OFF 和 YES/NO。
- 实验测量行只查询所选主温度。辅助温度和加热读数在该行留空，不使用缓存冒充同步值；
  完整状态仍写入 `instrument_status.dat`。
- Set Temperature 按目标温度选择 PID 段，逐项写入并回读 P、I、D、可选量程、速率，
  最后写入并回读目标。任一不匹配即报错，不自动重发。
- Hold 用即时主温度作为目标，不关闭加热。断开和退出只关闭 VISA 会话。
- A/B 传感器报警、Loop 1 加热故障、A 超过配置的 Temp A Maximum 均报错。
  Temp A Maximum 始终限制物理输入 A，与主温度选择无关；主温度为 A 时，设温、Hold 与状态校验中的目标均不得高于 Temp A Maximum。
- 通信失联沿核心重连机制处理；恢复后核对实际目标和速率。
- **传感器曲线与软件 PID 表切换（Instrument Setup）**：通过主菜单 **Instrument → Instrument Setup…**（或右键点击底部温控卡片）按需打开独立窗口，不占用额外底部卡片也不在后台轮询。22C 支持切换 A/B、24C 支持切换 A/B/C/D 通道的出厂曲线（`ISENix`）或用户曲线（`USENix`），每次写入均回读确认；同时支持在 `configs/pid/` 目录内校验并切换软件侧 PID 表（自动持久化回 `configs/instruments/`，在下一次 `Set Temperature` / `Hold` 生效，不中断当前控温）。

请保留冷台现场 PID 文件和硬件独立过温保护。该模块不提供磁场，辅助温度不要再创建
第二个连接到同一物理地址的仪表实例。
