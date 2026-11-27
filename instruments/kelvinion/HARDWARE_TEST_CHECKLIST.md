# Kelvinion 驱动真机待测试文档（Hardware Verification Checklist）

> **适用版本**：`kelvinion 0.1.0.dev1`（基于手册《Kelvinion User's Manual Rev 3.1.1》编写）
> **当前进展（2026-11-27）**：已在标准 Kelvinion（S/N `1005`，固件 `V2.2.6b6`，USB CH343 `COM6 / ASRL6::INSTR`，`115200 8N1` 无流控，读写 `\r\n`）完成三轮空载真机测试：
> 1. **空载串口协议初测**：完成只读与部分空载写入协议验证（2360 条连续查询零错位，中位 ~3.0 ms，最大 14.3 ms）；
> 2. **端到端只读验收（19:30–19:34，PR #8 `8a8f026` + 主框架 `main 2d58579 / 0.20.10`，只读传输包装，零写命令）**：完成 Scanner 独立配置、20 轮 `read_status`/`read_setup` 连续测试（2133 条 / 59 种命令，零超时/解码错/错位，中位 3.31 ms，最大 13.48 ms）、Instrument Setup 三页只读加载、真实 `RuntimeService` + 仪表子进程 `10 s Wait` 运行与归档，以及正常退出后 59 条全量回读与初始快照完全一致；
> 3. **空载底层协议补测（19:46–19:51，直接 PyVISA，零 OpenLab、零 `[SET:SAVE]`、前后 59 条快照 0 差异）**：实测确认 Output A `Zone5` `KP/KI/KD` 读写、Output B `PID Limit`（`325 -> 320 -> 325 K`）读写、空闲通道 `H` 全部 6 种传感器类型（`CERNOX/PT100/PT1000/DIODE/RO105/OFF`）写入与开路读数（`W:H=0` 下均返回 `[0.000000]`，且 `W:H`/`INPUT_LIMIT:H` 不变）；全部 14 条写命令均无 ACK。因 Output B 全部 `Zone1–5`（及 A `Zone2–4`）预存非 `OFF` 量程，为安全起见跳过 `[SET:ZONE:B:ON]`。
> **仍待真机验证**：有负载闭环控温与爬坡（`HV-03`）、`RAMP=0` 真实爬坡行为与极限（`HV-04`）、带校准曲线（`W != 0`）的物理断路与短路（`HV-05`）、Zone 置 `ON` 与 Range 联动（`HV-07`）、完整曲线号规则与 `INPUT_LIMIT` 变化原因（`HV-08`）、`[SET:SAVE]` 耗时/ACK/持久化（`HV-01`/`HV-08`）、阶段三双回路设温/`Hold`/`Manual` 拒写/Setup `Apply`/`Save` 真机写入，以及人工实体前面板逐项核对与有负载故障演练。
> **已按实测修复的兼容问题**：① `[READ:T:CH]` 空通道返回 `[OFF]`（已纳入 `SENSOR_TYPES`，且实测 `[SET:T:H:OFF]` 可写）；② `[READ:W:A]=104`、`[READ:W:B]=412`（`read_curve_number` 改为接受任意非负整数，UI SpinBox 扩展至 `0–9999` 且绝不改写未动通道）；③ **传感器类型回读别名不对称**：实测 `[SET:T:H:CERNOX]` 回读为 `[NTCRTD]`、`[SET:T:H:RO105]` 回读为 `[RO]`，已在 `read_sensor_type` / `write_sensor_type_and_verify` 加入别名归一化（`NTCRTD -> CERNOX`、`RO -> RO105`）。
> **现场注意点**：初测时对空闲输入 C 执行曲线切换 `0 -> 12 -> 0` 后，其 `INPUT_LIMIT` 从 `800 K` 变为 `360 K`（前面板同步显示 `360 K`，后续复读仍为 `360 K`）；而补测中对 `H` 仅切换传感器类型 `T:H` 并未改变 `INPUT_LIMIT:H`（始终 `800 K`）。测试曲线切换时务必留意 `INPUT_LIMIT` 变化，未确认前勿执行 `[SET:SAVE]`。

---

## 一、待确认项总览（与源码标记一一对应）

| 编号 | 待确认问题 | 2026-11-27 空载实测结果（V2.2.6b6） | 仍待验证的边界 | 源码标记位置 |
| --- | --- | --- | --- | --- |
| **HV-01** | `[SET:...]` 是否返回 ACK 回执帧 | 已测 `RAMP`/`SETP`/`LOOP`/`LIMIT`/`T`/`W`/`ZONE(KP,KI,KD,RANGE,BOUNDARY)` 均在 500 ms 内**无 ACK**，紧跟查询无帧错位（`write_expects_ack = false` 正常） | 尚未验证 `[SET:SAVE]` 及 `[SET:ZONE:CH:ON|OFF]` 是否同样无 ACK | [`kelvinion.py`](kelvinion.py) `write_command`；[`instrument.toml`](instrument.toml) `write_expects_ack` |
| **HV-02** | `[SET:SETP:CH:VALUE]` 是否需要 `K` 后缀 | **已确认**：`[SET:SETP:A:2.00K]` 与 `[SET:SETP:B:3.00K]` 均接受并正确回读（`setpoint_unit_suffix = true` 可用） | 未测无 `K` 写法（无需切换） | [`kelvinion.py`](kelvinion.py) `format_setpoint_command`；[`instrument.toml`](instrument.toml) `setpoint_unit_suffix` |
| **HV-03** | `[READ:SETP:CH]` 返回爬坡工作设定值还是最终目标 | Range=`OFF` 空载下写后 0/5/15/30 s 均立即读回最终目标（A=2、B=3，功率 0） | **有负载闭环有效爬坡时的行为未验证**，尚不能认定工作设定值跟踪算法已通过 | [`kelvinion.py`](kelvinion.py) `read_working_setpoint` / `write_setpoint_and_readback`；[`backend.py`](backend.py) `_evaluate_loop_motion` |
| **HV-04** | `[SET:RAMP:CH:VALUE]` 边界与 `0` 行为 | A/B 均接受并回读 `0.10`、`10.00`、`0.00` K/min | 未确认 `0.00` 在真实爬坡中的物理含义及硬件真实极限；保持 `[0.1, 10.0]` 与 `hold()` 不发 `RAMP 0` | [`kelvinion.py`](kelvinion.py) `HARDWARE_MIN_RAMP_K_PER_MIN`；[`backend.py`](backend.py) `hold` / `_validate_rate_value` |
| **HV-05** | 断开/短路探头时 `[READ:K:CH]` 线缆返回 | 空载开路下（`W=0`），`OFF` 及全部 5 种启用类型（`CERNOX/PT100/PT1000/DIODE/RO105`）均返回 `[0.000000]`，驱动均拦截为 `KelvinionSensorFault`/缺失 | **带校准曲线（`W != 0`）的物理断路与真实物理短路返回字串未验证** | [`kelvinion.py`](kelvinion.py) `INVALID_SENSOR_TOKENS` / `read_temperature_k` |
| **HV-06** | `[READ/SET:LOOP:CH]` 在 `A–H` 与 `NULL`/`OFF` 的格式 | 协议读写已确认：A=`[A]`、B=`[B]`；B 切 `C`、`OFF`（回读 `[NULL]`）、`B` 均成功 | 实体前面板逐项人工核对尚未执行 | [`kelvinion.py`](kelvinion.py) `read_loop_binding` / `write_loop_binding_and_verify` |
| **HV-07** | `ZONE` 读写格式及 Zone 开启时 `[READ:RANGE:CH]` 联动 | 两路 `STATE` 与全部 `Zone1–5` 读取解析通过；A `Zone5` 全部 5 字段（`KP`/`KI`/`KD`/`RANGE`/`BOUNDARY`）读写均已验证并恢复 | **Zone 置 `ON` 后 `[READ:ZONE:STATE:CH]` 返回值及与 `[READ:RANGE:CH]` 的联动未验证**（因 B `Zone1–5` 预存 `HIGH`/`MED` 量程而跳过） | [`kelvinion.py`](kelvinion.py) `read_zone_state` / `read_zone_entry` / `write_zone_field_and_verify` |
| **HV-08** | USB `\r\n`、`*IDN?`、节拍、传感器类型、曲线号与 `[SET:SAVE]` | CRLF、带框/裸 IDN、连续 2360+2133 条查询均正常；实测 `T` 写入 `OFF/PT100/PT1000/DIODE/CERNOX/RO105` 生效，且 **`CERNOX -> [NTCRTD]`、`RO105 -> [RO]`**；`LIMIT` 读写通过；曲线 `104/412` | 曲线完整范围/编码、`INPUT_LIMIT` 随曲线变更规则及 `[SET:SAVE]` 耗时/持久化未验证 | [`kelvinion.py`](kelvinion.py) `SENSOR_TYPE_ALIASES` / `read_sensor_type` / `read_curve_number` / `save_configuration` |

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

- [x] **HV-08a（串口建链与 `\r\n`）**：`[*IDN?]` 正常返回身份帧，无超时、无乱码（2026-11-27 两轮均通过）。
  - 实测原始响应：`[MultiFields Technologies,Kelvinion,1005,V2.2.6b6]`（`ASRL6::INSTR`，`115200 8N1`，无流控，CRLF）
- [x] **HV-08b（扫描器 `*IDN?` 对比）**：2026-11-27 初测确认 `V2.2.6b6` 下带框 `[*IDN?]` 与裸 `*IDN?` 均有正常响应（其他固件版本仍建议保留 Scanner 手选 `ASRL` 兜底说明）。
- **HV-05（空通道 / 断线读数）**：
  - [x] **空输入与 `W=0` 开路读数**：`OFF` 及在空闲通道 `H`（`W:H=0`）上依次切 `CERNOX` / `PT100` / `PT1000` / `DIODE` / `RO105` 时，串口 `[READ:K:CH]` 均立即及 +1 s 返回 `[0.000000]`（驱动拦截为 `KelvinionSensorFault` / 缺失，不记 `0 K`；2026-11-27 19:30–19:51 验证）。
  - [ ] **带校准曲线（`W != 0`）的物理断路与真实物理短路读数**：尚未验证故障字串与 `KELVINION_SENSOR_FAULT` 实机演练。
- **HV-06a（LOOP 读取）**：
  - [x] **协议读取**：`[READ:LOOP:A]` -> `[A]`，`[READ:LOOP:B]` -> `[B]`（Output A -> Input A，Output B -> Input B，2026-11-27 两轮一致）。
  - [ ] **实体前面板逐项人工核对**：19:30–19:34 只读验收未逐项对照实体前面板，待人工核对。
- [x] **HV-07a（ZONE 读取）**：两路 `[READ:ZONE:STATE:A/B]`（`OFF`）与全部 `Zone1–5` 参数均成功读取与解析（2026-11-27 两轮通过）。
- [x] **HV-08c（连续查询稳定性）**：
  - 初测：20 轮 × 2 组（100 ms 间隔与零额外间隔，各 1180 条，共 2360 条）0 错位，中位 ~3.0 ms，最大 14.3 ms；
  - 驱动只读验收（19:30–19:34）：20 轮完整 `read_status`/`read_setup`，主轨迹合计 **2133 条查询、59 种命令**，0 超时 / 0 解码错 / 0 响应错位，中位 **3.3084 ms**，最大 **13.4826 ms**（含本机调度与日志开销）。

---

## 四、阶段二：受控小幅写命令验证（先将 Heater Range 置 `OFF`）

> **安全建议**：先在前面板将 Output 1 (A) 与 Output 2 (B) 的 **Heater Range 均设为 `OFF`**（仅验证协议状态机与设定值变化，不实际加热样品！），且当前 Mode 为 `Auto (A)`。

### 1. 测试 `HV-01`（写命令是否返回 ACK）
- [x] **已测空载写命令无 ACK**（2026-11-27 初测）：`[SET:RAMP]` 同值写入 `5.00` 后 500 ms 内无 ACK，紧跟回读正确；其余已测写命令（`SETP`/`LOOP`/`ZONE`/`W`）后紧跟查询均无帧错位（保持 `write_expects_ack = false`）。
- [ ] **剩余写命令 ACK 行为（尤其是 `[SET:SAVE]`）**：尚未验证。

### 2. 测试 `HV-02`（`SET:SETP` 是否接受/需要 `K` 后缀）
- [x] **带 `K` 后缀已确认可用**（2026-11-27 初测）：`[SET:SETP:A:2.00K]` 与 `[SET:SETP:B:3.00K]` 均接受并正确回读（已恢复 `0`；`setpoint_unit_suffix = true` 可用；未再测无 `K` 写法）。

### 3. 测试 `HV-03`（`[READ:SETP:CH]` 是否为爬坡工作设定值）
- [x] **空载 `Range=OFF` 观测**（2026-11-27 初测）：`0 / 5 / 15 / 30 s` 均立即回读最终值（`A=2.00`、`B=3.00`，功率始终 `0`）。
- [ ] **有负载闭环有效爬坡验证（关键待测）**：在接入有效探头与加热负载并开启 Heater Range 后，验证有效爬坡期间 `[READ:SETP:CH]` 是逐步爬升的工作设定值还是直接跳变到最终目标。

### 4. 测试 `HV-04`（爬坡率边界与 `0`）
- [x] **空载接受值回读**（2026-11-27 初测）：A/B 均接受并回读 `0.10`、`10.00`、`0.00` K/min，并已恢复原值 `5.00`。
- [ ] **`RAMP=0` 在真实爬坡中的物理含义与硬件真实速率极限**：尚未验证（当前代码继续限制 `[0.1, 10.0]` 且 `hold()` 不发送 `RAMP 0`）。

### 5. 测试 `HV-06`、`HV-07`、`HV-08`（Setup 写入项，仅在空载/安全通道测试）
- [x] **HV-06b（LOOP 切换）**：2026-11-27 初测 Output B 切 `C`、`OFF`（回读 `[NULL]`）、恢复 `B` 均成功，A 保持 `A`。
- [x] **HV-07b（Zone 全部 5 字段与 PID Limit 读写，`Zone=OFF`）**：2026-11-27 初测 + 19:50 补测 Output A `Zone5` 写入 `RANGE LOW/OFF`、`BOUNDARY 2/0`、`KP 12.50`、`KI 3.25`、`KD 1.5` 均无 ACK、精确回读并恢复 `[0.000,0.000,0.000,0,0.000]`；Output B `[SET:LIMIT:B:320.00]` 读写并恢复 `[325.000000]` 通过。
- [ ] **HV-07b（Zone 置 `ON` 与 Range 联动）**：因实机 Output B `Zone1–5` 预存 `HIGH`/`MED` 量程（且 A `Zone2–4` 预存 `HIGH`/`MED`/`LOW`），19:50 安全跳过 `[SET:ZONE:B:ON]`；开启 Zone 后 `[READ:ZONE:STATE:CH]` 返回值与 `[READ:RANGE:CH]` 联动尚未验证。
- [x] **HV-08（传感器类型全量读写与回读别名确认）**：2026-11-27 19:50 空闲通道 `H` 实测 `[SET:T:H:CERNOX|PT100|PT1000|DIODE|RO105|OFF]` 全部无 ACK 生效，且 **`CERNOX` 回读 `[NTCRTD]`、`RO105` 回读 `[RO]`**，`W:H`（`0`）与 `INPUT_LIMIT:H`（`800`）均未变化（已在驱动加入别名归一化）。
- [x] **HV-08d（现场曲线号读取与空闲通道写入）**：实机读到 `A=104`、`B=412`，空闲 C 写入曲线 `12` 并恢复 `0`（注：恢复后 C 的 `INPUT_LIMIT` 由 `800 K` 变为 `360 K`，前面板同步显示 `360 K`）。
- [ ] **HV-08d（完整曲线编号含义与 `INPUT_LIMIT` 规则）**：完整曲线号范围/编码含义及 `INPUT_LIMIT` 变更与恢复方式尚未确认。
- [ ] **HV-08e（`[SET:SAVE]`）**：`[SET:SAVE]` 耗时、ACK 行为及断电持久化尚未执行。

---

## 五、阶段三：OpenLab Control 端到端集成测试

完成阶段一、二并调整（如有需要）`HV-01` / `HV-02` 选项后，在 OpenLab Control 中完成以下检查：

1. **Instrument Scanner 配置**：
   - [x] **独立配置生成、保存与重载（只读验收）**：2026-11-27 19:30–19:34 基于 PR #8 `8a8f026` 与主框架 `main 2d58579 / 0.20.10` 在独立项目生成配置并保存重载，无不完整实例（两路控制面板 `role` 均为 `none`）；
   - [ ] **控温角色联调配置**：将 `Output 1 (A)` 角色设为 `Sample Temp`、`Output 2 (B)` 角色设为 `None`，并在有负载闭环场景下运行。
2. **连接、只读轮询与运行归档**：
   - [x] **真实运行时连接、轮询与 `Wait` 序列归档**：使用真实 `RuntimeService` 与仪表子进程（未启用测量模块），执行 `T Wait For 10.0 secs / T End Sequence` 正常 `completed`；`instrument_status.dat` 共 10 行，连接均为 `connected`，两路 Heater Range 均为 `OFF`；`events.dat` 共 6 条 `info`，无 `warning`/`error`；`sequence.seq`、`configuration/general.toml`、`visa.resources.toml` 与仪表配置快照均正常归档；
   - [x] **`OFF` 空输入缺失处理**：A–H（类型 `OFF`，返回 `[0.000000]`）在驱动与归档中均显示/记录为缺失，未当作有效 `0 K`；
   - [ ] **实体前面板逐项核对与真实探头验证**：逐项核对实体前面板读数/状态、验证真实温度精度，以及启用主控探头后的物理断线触发 `KELVINION_SENSOR_FAULT`。
3. **双回路独立控制与 Hold（真机写入，全部待测）**：
   - [ ] 分别在 `Output 1 (A)` 与 `Output 2 (B)` 卡片上做小幅目标变更（如 `+0.50 K`，`1.00 K/min`），确认两路互不干扰、爬坡期间 `Ramping` 变为 `Ramping`、到位后恢复 `Idle` 且进入稳态判定（若工作设定值停滞不前，应保持原目标、`ready=False` 且显示 `Stalled`，绝不误判稳定）；
   - [ ] 爬坡中途点击 **Hold Current**，确认仅对该回路写入现读温度并停止爬坡，加热档位保持不变；
   - [ ] 在仪表面板将某路切为 Manual (`M`) 模式，确认 OpenLab 拒绝向该路写入目标（`KELVINION_MODE_MISMATCH`）；设回 Auto (`A`) 后恢复正常。
4. **Instrument Setup 窗口**：
   - [x] **只读加载与界面核对（2026-11-27 19:30–19:34）**：真实仪表加载 **Sensors / Outputs / Zones** 三页，A/B 曲线号 `104` / `412` 正确保留，未编辑时变更集合为空且 `Apply` 禁用；确认界面**不包含** Filter、Excitation、Reversal 项（已基于同一份真机捕获数据加载 Segoe UI 重新渲染三页逐页核对）；
   - [ ] **真机写入与保存**：修改一项非关键参数点 **Apply** 确认回读成功；点击 **Save to Instrument**（`[SET:SAVE]`）确认无超时报错且持久化符合预期。
5. **关闭与断开安全**：
   - [x] **只读运行退出与全量回读验证（2026-11-27 19:30–19:34）**：运行时和子进程会话均正常退出，重新打开执行 59 条全量回读，结果与初始快照完全一致（本轮未发送任何写命令）；
   - [ ] **有负载/写操作后的断开与前面板人工复核**：确认退出 OpenLab Control 后两路加热档位、设定值、爬坡率**未被改动或关闭**。

---

## 六、2026-11-27 真机实测快照与后续待办

- **只读验收环境（19:30–19:34）**：`ASRL6::INSTR`（USB CH343，`115200 8N1`，无流控，CRLF），身份 `[MultiFields Technologies,Kelvinion,1005,V2.2.6b6]`；驱动 PR #8 `8a8f0264cc44b0978aacf93b96cc5896663313e5`（`0.1.0.dev1`）；主框架 `main 2d58579d55aab3effbb0bba1ab3966a51e4f9912`（`0.20.10`）；只读传输包装仅允许 `[*IDN?]` / `[READ:...]`，未发送任何写命令。
- **底层协议补测（19:46–19:51）**：直接 PyVISA（未启动 OpenLab Control，未发 `[SET:SAVE]`），发送 14 条写命令覆盖 `Zone5 KP/KI/KD`、`LIMIT:B` 及 `T:H` 全部 6 种类型，均 `ack=None`，测后恢复，**59 条前后全量快照 0 差异**。
- **实机初始/结束一致快照**：
  - 输入 A–H：传感器类型全部 `OFF`，温度响应全部 `[0.000000]`（无有效测温）；
  - 曲线号：`A=104`，`B=412`，`C–H=0`；
  - `INPUT_LIMIT`：`A/B=350 K`，`C=360 K`，`D–H=800 K`（C 通道 `360 K` 与初测后现场状态一致，原因仍待确认）；
  - 输出 A/B：`Mode=A`（Auto），`Range=OFF`，`Power=0`，`Setpoint=0 K`，`Ramp=5 K/min`，`PID Limit=325 K`，`Zone State=OFF`。
- **原始证据说明**：本轮本地 JSONL 轨迹、快照 JSON 与 Setup 渲染截图保存在本地工作区 `tmp/`，不随代码提交入库。
- **后续待办**：完成上述剩余 `[ ]` 的有负载闭环控温、真机写入与人工前面板核对后，据此固化 `HV-01`–`HV-08` 真实协议行为、移除已验证项的 `UNVERIFIED_PROTOCOL` 标注，并更新 `README.md` 验证状态。
