# Cryo-con 22C / 24C

- **驱动版本**：`0.4.0.dev1`
- **实机验证状态**：Cryo-con `22C` 单回路已完成现场读写与升温响应验证（`2026-11-22`，仅覆盖已测接线与配置）；`24C` 双回路联动模式**尚未实机验证**。

一个物理实例共用一个 VISA 会话，在 **Instrument Scanner** 中通过单一开关 **`Controller Model`** 直接选择机型（`22C` / `24C`），并在连接时严格核对 `*IDN?` 型号一致（防止 22C/24C 错配）：

1. **`22C`（默认，单回路）**：
   - 仅有 Loop 1 单回路配置（`[[zones]]`），主温度可选 `Temp A` 或 `Temp B`，不查询也不写入 Loop 2；
   - 在 Scanner 中取消勾选 `Linked Loop Status (24C Only)` 面板即可（即使保留勾选也会安全显示空值且绝不发送 Loop 2 指令）。
2. **`24C`（双回路分段公式联控）**：
   - 默认 **主回路 = Loop 2**（控样品，绑定所选 `Reading` 如 `Temp B`，目标 `x`），**从回路 = Loop 1**（控冷台/热沉，绑定另一路输入如 `Temp A`，目标由分段公式计算）；如需反转主从回路，可在 24C PID 文件顶层可选写 `primary_loop = 1`；
   - 两路**分别下发各自的软件 PID 表**（主回路查 `[[zones]]`，从回路按其目标温度查 `[[linked_pid_zones]]`），且均逐项回读确认；
   - `moving` 在**任一回路处于斜坡（ramping）**时为 `True`，只有**两路均停止爬坡**时才报 `ready = True` 进入稳态计时；
   - `Hold Current` 会一次性现读 A/B 两个传感器即时温度并分别保持各自当前温度，**绝不套用联动公式**。

USB 虚拟串口使用 `ASRL…::INSTR` 地址，8N1、LF 终止符，波特率必须与面板一致，
可选 9600、19200、38400、57600、115200；同时保留 USB、LAN/TCP 和 GPIB VISA 地址支持。
若面板波特率不是默认 9600，Instrument Scanner 自动扫描可能无法识别，可手动填写 `ASRL` 地址并在配置中选择对应波特率。

## 配置

把完整目录放入 System Instruments 目录，在 Instrument Scanner 中选择资源、机型开关（`22C` 或 `24C`）、波特率、主温度读数与监视面板。只有活动回路已绑定到对应输入（A/B）、处于 `RAMPP` 且 `CONTROL ON` 时才允许控制；连接只读验证，不自动切换输入或开启加热。

每个物理实例使用自己的 `configs/pid/<instance-id>.toml`。
`pid.example.toml` 是空模板，必须填入现场已经确认的参数，不能用测试夹具代替。

### 1. `22C` 单回路 PID 表（仅需 `[[zones]]`）

```toml
[[zones]]
temperature = 10.0 # 包含等于（<=）的温区上限
p_gain = 1.0
i_gain = 4.0
d_gain = 1.0
# heater_range = "LOW" # 可选：LOW/MID/HI；不填写则保留仪表当前量程
```

### 2. `24C` 双回路联控表（选择 `model = "24C"` 时必需）

同一份 `configs/pid/<instance-id>.toml` 中配置主回路 PID（`[[zones]]`）、从回路 PID（`[[linked_pid_zones]]`）与分段目标公式（`[[linked_target_zones]]`）：

```toml
# 可选：默认为 2（Loop 2 为主控样品，Loop 1 为从回路）
# primary_loop = 2

# 主回路（Loop 2，样品 B）PID 表：按主目标 x（<= temperature）选段
[[zones]]
temperature = 10.0
p_gain = 0.1
i_gain = 1.0
d_gain = 0.0
# ...

# 从回路（Loop 1，冷台/热沉 A）PID 表：按从回路目标温度（<= temperature）选段
[[linked_pid_zones]]
temperature = 10.0
p_gain = 0.1
i_gain = 1.0
d_gain = 0.0
# ...

# 从回路目标温度分段公式：按主目标 x（<= temperature）选段
# 白名单语法仅支持：x、有限数值、括号、+ - * /、min()、max()
[[linked_target_zones]]
temperature = 310.0 # x <= 310.0 K 时生效
formula = "x - 0.5"

[[linked_target_zones]]
temperature = 400.0 # x > 310.0 K（310.0 < x <= 400.0 K）时生效
formula = "290"
```

- **上限规则统一为包含等于（`<=`）**：上例中 `x = 310.0 K` 命中第一段（`309.5 K`），`x > 310.0 K`（例如 `310.01 K`、`350 K`）命中第二段（恒为 `290 K`）。
- **加载期安全抽检**：加载配置时会自动在上下限及各分段边界（含 `310 + 1e-6 K`）抽检公式结果，若出现非正值、除零、超出 `Temp A Maximum`（当从回路为 Input A 时）或超出 `[[linked_pid_zones]]` 最高温区，会在连接硬件前直接拦截。
- **可选参考模板**：
  - `pid.operator-example.toml`：特定 22C 冷台单回路参考表；
  - `pid.24c-linked-example.toml`：24C 双回路联控（`x <= 310: x - 0.5`，`x > 310: 290`）结构示例，**内含示例 PID 增益，实机使用前务必替换为冷台实测整定值**。

## 读写与运行

- `22C` 完整状态用 15 字段复合查询；`24C` 用 23 字段单条复合查询同时读取 A/B、Loop 1、Loop 2 与 `CONTROL?`，并在 **Linked Loop Status (24C Only)** 面板与 `instrument_status.dat` 中记录从回路设定值、功率、量程与斜坡状态。
- 实验测量行只查询所选主温度。辅助温度和加热读数在该行留空，不使用缓存冒充同步值。
- `Set Temperature`：
  - `22C`：按目标选 PID 段，逐项写入并回读 Loop 1 的 P、I、D、可选量程、速率、目标；
  - `24C`：写前完成两路目标计算、限值校验与两路 PID 选段，再先下发两路的 P/I/D/可选量程/速率，最后下发两路目标，**每一步均即时回读确认**；任一步失败立即报 `INSTRUMENT_WRITE_RESULT_UNKNOWN` 且不重发。
- `Hold` 用即时传感器温度作为各自目标，不关闭加热；`24C` 一次复合查询 A/B 各自温度并分别下发各自 PID 与目标，不套公式。
- A/B 传感器报警、任一活动回路加热故障、Input A 实际温度或目标超过 `Temp A Maximum` 均立即报安全错误。
- **Instrument Setup**：支持 22C（A/B）与 24C（A/B/C/D）按需切换出厂/用户曲线及热切换 `configs/pid/` 下的软件配置表；24C 模式下会在弹窗内只读展示当前主从回路映射、分段公式与关键温度点换算预览，22C 模式下自动隐藏。

请保留冷台现场 PID 文件和硬件独立过温保护。该模块不提供磁场，辅助温度不要再创建第二个连接到同一物理地址的仪表实例。
