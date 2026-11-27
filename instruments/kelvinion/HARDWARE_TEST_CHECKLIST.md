# Kelvinion 驱动真机待测试文档（Hardware Verification Checklist）

> **适用版本**：`kelvinion 0.1.0.dev1`（基于手册《Kelvinion User's Manual Rev 3.1.1》编写）
> **当前进展（2026-11-27）**：已在标准 Kelvinion（S/N `1005`，固件 `V2.2.6b6`，USB CH343 `COM6 / ASRL6::INSTR`，`115200 8N1` 无流控，读写 `\r\n`）完成**空载只读与部分空载写入协议验证**（2360 条连续查询零错位，中位 3.0 ms，最大 14.3 ms）；**闭环控温、有负载爬坡、启用传感器后的断路/短路故障编码、Zone 开启与 Range 联动、Setup 写入与 `[SET:SAVE]` 仍待实机验证**。
> **已按实测修复的兼容问题**：① `[READ:T:CH]` 空通道返回 `[OFF]`（已纳入 `SENSOR_TYPES`）；② `[READ:W:A]=104`、`[READ:W:B]=412`（`read_curve_number` 改为接受任意非负整数，UI SpinBox 扩展至 `0–9999` 且绝不改写未动通道）。
> **现场注意点**：空闲输入 C 执行曲线切换 `0 -> 12 -> 0` 后，其 `INPUT_LIMIT` 从 `800 K` 变为 `360 K`（前面板同步显示 `360 K`），原因与恢复方式尚未确认；测试曲线切换时务必留意 `INPUT_LIMIT` 变化，未确认前勿执行 `[SET:SAVE]`。

---

## 一、待确认项总览（与源码标记一一对应）

| 编号 | 待确认问题 | 2026-11-27 空载实测结果（V2.2.6b6） | 仍待验证的边界 | 源码标记位置 |
| --- | --- | --- | --- | --- |
| **HV-01** | `[SET:...]` 是否返回 ACK 回执帧 | 已测 `[SET:RAMP]`（及 `SETP`/`LOOP`/`ZONE`/`W`）500 ms 内**无 ACK**，紧跟查询无帧错位（`write_expects_ack = false` 正常） | 尚未验证 `[SET:SAVE]` 等全部写命令是否均无 ACK | [`kelvinion.py`](kelvinion.py) `write_command`；[`instrument.toml`](instrument.toml) `write_expects_ack` |
| **HV-02** | `[SET:SETP:CH:VALUE]` 是否需要 `K` 后缀 | **已确认**：`[SET:SETP:A:2.00K]` 与 `[SET:SETP:B:3.00K]` 均接受并正确回读（`setpoint_unit_suffix = true` 可用） | 未测无 `K` 写法（无需切换） | [`kelvinion.py`](kelvinion.py) `format_setpoint_command`；[`instrument.toml`](instrument.toml) `setpoint_unit_suffix` |
| **HV-03** | `[READ:SETP:CH]` 返回爬坡工作设定值还是最终目标 | Range=`OFF` 空载下写后 0/5/15/30 s 均立即读回最终目标（A=2、B=3，功率 0） | **有负载闭环有效爬坡时的行为未验证**，尚不能认定工作设定值跟踪算法已通过 | [`kelvinion.py`](kelvinion.py) `read_working_setpoint` / `write_setpoint_and_readback`；[`backend.py`](backend.py) `_evaluate_loop_motion` |
| **HV-04** | `[SET:RAMP:CH:VALUE]` 边界与 `0` 行为 | A/B 均接受并回读 `0.10`、`10.00`、`0.00` K/min | 未确认 `0.00` 在真实爬坡中的物理含义及硬件真实极限；保持 `[0.1, 10.0]` 与 `hold()` 不发 `RAMP 0` | [`kelvinion.py`](kelvinion.py) `HARDWARE_MIN_RAMP_K_PER_MIN`；[`backend.py`](backend.py) `hold` / `_validate_rate_value` |
| **HV-05** | 断开/短路探头时 `[READ:K:CH]` 线缆返回 | 空输入（类型 `OFF`）串口返回 `[0.000000]`（前面板显示 `O.L.`），已由 `<= 0.0` 拦截为探头故障 | **传感器类型启用（非 `OFF`）后的物理断路/短路返回字串未验证** | [`kelvinion.py`](kelvinion.py) `INVALID_SENSOR_TOKENS` / `read_temperature_k` |
| **HV-06** | `[READ/SET:LOOP:CH]` 在 `A–H` 与 `NULL`/`OFF` 的格式 | **已确认**：B 切 `C`、`OFF`、`B` 均成功，`OFF` 回读 `[NULL]`（与 `NULL -> OFF` 归一化一致） | 无（已空载验证） | [`kelvinion.py`](kelvinion.py) `read_loop_binding` / `write_loop_binding_and_verify` |
| **HV-07** | `ZONE` 读写格式及 Zone 开启时 `[READ:RANGE:CH]` 联动 | 两路 `STATE` 与 `Zone1–5` 可读；A `Zone5` 写 `RANGE LOW/OFF`、`BOUNDARY 2/0` 均成功并恢复 | **Zone 置 `ON` 后与 `[READ:RANGE:CH]` 的联动未验证** | [`kelvinion.py`](kelvinion.py) `read_zone_state` / `read_zone_entry` / `write_zone_field_and_verify` |
| **HV-08** | USB `\r\n`、`*IDN?`、节拍、传感器类型、曲线号与 `[SET:SAVE]` | CRLF 可用、带框/裸 IDN 均正常、2360 条串行查询 0 错位；实测 `[READ:T:CH]` 返回 `[OFF]`、`[READ:W:A/B]` 返回 `104`/`412` | 曲线完整范围/编码、`INPUT_LIMIT` 随曲线变更规则、`[SET:T:CH:OFF]` 写入及 `[SET:SAVE]` 耗时/持久化未验证 | [`kelvinion.py`](kelvinion.py) `SENSOR_TYPES` / `read_curve_number` / `save_configuration` |

---

## 二、准备条件

1. 硬件接线：仅连接 **USB-B 线** 到电脑（GPIB 暂不连接）；确认 Windows 设备管理器出现 CH343 虚拟串口（记录 `COMx` 及 NI-VISA 中的 `ASRLx::INSTR`）。
2. 仪表设置：在前面板确认通讯接口已切换到串口（115200 bps），记录当前固件版本号与传感器接线（哪些输入口接了探头、哪些未接）。
3. 建议先用 PyVISA 或串口助手（115200, 8N1, 无流控）执行下方**阶段一（纯只读）**，确认响应格式后再进入 OpenLab Control 联调。

---

## 三、阶段一：USB 串口纯只读探针测试（零风险，不修改任何参数）

可在 `openlab-control\.venv\Scripts\python.exe` 下运行以下单次只读探针（把 `ASRL3::INSTR` 换成实际串口号），记录每条命令的**原始返回字节（`repr`）**：

```python
import pyvisa
from pyvisa.constants import ControlFlow, Parity, StopBits

ADDR = "ASRL3::INSTR"  # 替换为实机 COM 口对应的 VISA 地址
rm = pyvisa.ResourceManager()
inst = rm.open_resource(
    ADDR,
    open_timeout=1500,
    timeout=1500,
    baud_rate=115200,
    data_bits=8,
    parity=Parity.none,
    stop_bits=StopBits.one,
    flow_control=ControlFlow.none,
    read_termination="\r\n",
    write_termination="\r\n",
)
for cmd in [
    "[*IDN?]",
    "[READ:K:A]", "[READ:K:B]", "[READ:K:C]", "[READ:K:H]",
    "[READ:T:A]", "[READ:W:A]", "[READ:INPUT_LIMIT:A]",
    "[READ:LOOP:A]", "[READ:LOOP:B]",
    "[READ:MODE:A]", "[READ:MODE:B]",
    "[READ:RANGE:A]", "[READ:RANGE:B]",
    "[READ:POWER:A]", "[READ:POWER:B]",
    "[READ:SETP:A]", "[READ:SETP:B]",
    "[READ:RAMP:A]", "[READ:RAMP:B]",
    "[READ:LIMIT:A]", "[READ:LIMIT:B]",
    "[READ:ZONE:STATE:A]", "[READ:ZONE:STATE:B]",
    "[READ:ZONE:ZONE1:A]", "[READ:ZONE:ZONE1:B]",
]:
    try:
        print(cmd, "->", repr(inst.query(cmd)))
    except Exception as exc:
        print(cmd, "-> ERROR:", exc)
inst.close()
rm.close()
```

### 阶段一验收记录表

- [ ] **HV-08a（串口建链与 `\r\n`）**：`[*IDN?]` 正常返回 `[MultiFields Technologies,Kelvinion,...]`（或 `Kelvinion Mini`），无超时、无乱码。
  - 实测原始响应：`____________________________________________`
  - 若超时，改为 `write_termination=""` 再试并记录结果：`____________`
- [ ] **HV-08b（扫描器 `*IDN?` 对比）**：发送裸 `*IDN?`（无方括号）是否有响应？
  - 实测结果：`[ ] 无响应（需在 Scanner 手选 ASRL）` / `[ ] 有响应：____________`
- [ ] **HV-05（空通道 / 断线读数）**：至少查一个**未接探头**的输入通道（如 `C` 或 `H`），记录 `[READ:K:CH]` 的原始返回值。
  - 实测未接通道返回值：`________________________`（如 `[0.000]` / `[S/C]` / 其他）
- [ ] **HV-06a（LOOP 读取）**：`[READ:LOOP:A]` 与 `[READ:LOOP:B]` 分别返回单字母 `[A]`–`[H]` 或 `[NULL]`，且与前面板一致。
  - 实测返回：`A -> ________`，`B -> ________`
- [ ] **HV-07a（ZONE 读取）**：`[READ:ZONE:STATE:A]` 与 `[READ:ZONE:ZONE1:A]` 原始返回格式符合预期（5 字段逗号分隔）。
  - 实测返回：`STATE -> ________`，`ZONE1 -> ________________________`
- [ ] **HV-08c（连续查询稳定性）**：连续循环执行 20 轮上述查询，无丢帧、无粘包、无错位。
  - 实测结果：`[ ] 通过` / `[ ] 异常：____________`

---

## 四、阶段二：受控小幅写命令验证（先将 Heater Range 置 `OFF`）

> **安全建议**：先在前面板将 Output 1 (A) 与 Output 2 (B) 的 **Heater Range 均设为 `OFF`**（仅验证协议状态机与设定值变化，不实际加热样品！），且当前 Mode 为 `Auto (A)`。

### 1. 测试 `HV-01`（写命令是否返回 ACK）
发送一次无害写命令 `[SET:RAMP:A:1.00]`（先记录原值以便恢复），立刻尝试 `inst.read()` 一次（timeout=500 ms）：
- [ ] 若 `inst.read()` 超时且紧接着 `[READ:RAMP:A]` 返回 `[1.000]` -> **无 ACK**（保持 `write_expects_ack = false`）。
- [ ] 若 `inst.read()` 返回了 `[OK]` 或类似帧 -> **有 ACK**（记录原始帧 `____________`，并在 Scanner 勾选 `write_expects_ack = true`）。

### 2. 测试 `HV-02`（`SET:SETP` 是否接受/需要 `K` 后缀）
在当前温度附近选一个安全值（例如比当前设定值高 `1.00 K`）：
- [ ] 发送 `[SET:SETP:A:<val>.00K]` 后查 `[READ:SETP:A]` 与前面板设定值，观察是否生效：
  - 实测结果：`[ ] 生效` / `[ ] 被拒绝或无变化`
- [ ] 若带 `K` 不生效，改发 `[SET:SETP:A:<val>.00]`（不带 `K`）并观察是否生效：
  - 实测结果：`[ ] 生效（需关闭 setpoint_unit_suffix）` / `[ ] 异常：____________`

### 3. 测试 `HV-03`（`[READ:SETP:CH]` 是否为爬坡工作设定值）
设定爬坡率 `[SET:RAMP:A:1.00]`，将设定值从 `T0` 改为 `T0 + 2.00 K`，在随后的 0 s、5 s、15 s、30 s 连续读取 `[READ:SETP:A]` 并核对前面板显示：
- [ ] `t=0 s`：`________`，`t=5 s`：`________`，`t=15 s`：`________`，`t=30 s`：`________`
- 结论：
  - `[ ]` `[READ:SETP:A]` 随时间从 `T0` 逐步爬升到 `T0 + 2.00`（**确认为爬坡工作设定值**，与当前实现完全一致！）
  - `[ ]` `[READ:SETP:A]` 在 `t=0 s` 立即跳变为 `T0 + 2.00`（**为最终目标设定值**，需通知开发收紧写后校验）

### 4. 测试 `HV-04`（爬坡率边界与 `0`）
- [ ] 分别写入 `0.10` 与 `10.00` K/min 并回读 `[READ:RAMP:A]`：`[ ] 均接受` / `[ ] 异常：________`
- [ ] 写入 `0`（`[SET:RAMP:A:0]`）并回读 `[READ:RAMP:A]` 及观察前面板：
  - 实测结果：`[ ] 被拒绝/报错` / `[ ] 接受且读回 0（说明：________）`
- [ ] 测试完毕后恢复原 `RAMP` 与 `SETP`。

### 5. 测试 `HV-06`、`HV-07`、`HV-08`（Setup 写入项，仅在空载/安全通道测试）
- [ ] **HV-06b**：临时切换一个空闲输出（或 Heater Range=`OFF` 的 Output B）的 `[SET:LOOP:B:C]` 与 `[SET:LOOP:B:OFF]` 再切回原输入，确认回读一致。
- [ ] **HV-07b**：修改一条备用 Zone（如 `ZONE5`）的 `BOUNDARY` 与 `RANGE`（`[SET:ZONE:A:ZONE5:RANGE:LOW]`），回读 `[READ:ZONE:ZONE5:A]` 确认字段生效；开启 Zone 后读取 `[READ:RANGE:A]` 与 `[READ:ZONE:STATE:A]`，记录两者是否同步变化。
  - Zone 开启时 `[READ:RANGE:A]` 返回：`________`，`[READ:ZONE:STATE:A]` 返回：`________`
- [ ] **HV-08d**：对一个空闲通道读取/写入曲线编号 `> 9`（例如 `12`，再恢复原值），确认固件曲线号上限是 `9` 还是 `99`：`________`。
- [ ] **HV-08e**：执行一次 `[SET:SAVE]`，记录耗时及之后立即执行 `[*IDN?]` 是否在 `1000 ms` 内返回：`________ ms`。

---

## 五、阶段三：OpenLab Control 端到端集成测试

完成阶段一、二并调整（如有需要）`HV-01` / `HV-02` 选项后，在 OpenLab Control 中完成以下闭环检查：

1. **Instrument Scanner 配置**：
   - [ ] 添加 `kelvinion` 实例，选择 USB `ASRL<n>::INSTR`，同时启用 `Output 1 (A)` 与 `Output 2 (B)` 两个控制面板；
   - [ ] 将 `Output 1 (A)` 角色设为 `Sample Temp`，`Output 2 (B)` 角色设为 `None`；保存配置并重启主程序。
2. **连接与只读轮询**：
   - [ ] 启动后无报错，两路控制卡片与 `Output 1 Status` / `Output 2 Status` 网格均正常刷新；
   - [ ] 核对两路设定值、爬坡率、功率百分比、加热档位、Mode、Zone 状态与仪表面板一致；
   - [ ] 若有未接探头的监控通道（如 `Temp E`–`H`），显示为缺失（`—`）而不触发主控错误；若主控探头断线，正确触发 `KELVINION_SENSOR_FAULT`。
3. **双回路独立控制与 Hold**：
   - [ ] 分别在 `Output 1 (A)` 与 `Output 2 (B)` 卡片上做小幅目标变更（如 `+0.50 K`，`1.00 K/min`），确认两路互不干扰、爬坡期间 `Ramping` 变为 `Ramping`、到位后恢复 `Idle` 且进入稳态判定（若工作设定值停滞不前，应保持原目标、`ready=False` 且显示 `Stalled`，绝不误判稳定）；
   - [ ] 爬坡中途点击 **Hold Current**，确认仅对该回路写入现读温度并停止爬坡，加热档位保持不变；
   - [ ] 在仪表面板将某路切为 Manual (`M`) 模式，确认 OpenLab 拒绝向该路写入目标（`KELVINION_MODE_MISMATCH`）；设回 Auto (`A`) 后恢复正常。
4. **Instrument Setup 窗口**：
   - [ ] 打开 **Instrument Setup -> Kelvinion**，检查 **Sensors / Outputs / Zones** 三页加载正确；
   - [ ] 确认界面**不包含** Filter、Excitation、Reversal 项；
   - [ ] 修改一项非关键参数点 **Apply**，确认回读成功；点击 **Save to Instrument**，确认无超时报错。
5. **关闭与断开安全**：
   - [ ] 退出 OpenLab Control，核对仪表面板确认两路加热档位、设定值、爬坡率**未被改动或关闭**。

---

## 六、测试结果反馈记录

测试完成后请将阶段一/二的实际原始返回粘贴回对话，我们将据此：
1. 固化 `HV-01`–`HV-08` 的真实协议行为并移除已验证项的 `UNVERIFIED_PROTOCOL` 标注；
2. 更新 `README.md` 中的实机验证状态与日期。
