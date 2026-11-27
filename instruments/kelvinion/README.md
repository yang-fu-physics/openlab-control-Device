# MultiFields Kelvinion Temperature Controller

> **实机验证状态：待真机验证（Beta）**
> 本驱动依据《Kelvinion 控温仪用户手册 Rev 3.1.1》编写，尚未在真实硬件上完成通讯与控温测试。
> 接入真实低温系统前，**请务必先按同目录 [`HARDWARE_TEST_CHECKLIST.md`](HARDWARE_TEST_CHECKLIST.md) 逐项完成真机验证**（编号 `HV-01`–`HV-08`，源码中亦以 `UNVERIFIED_PROTOCOL (HV-XX)` 标注）。

## 功能范围与当前约定

- **通讯接口**：优先支持 **USB-B 虚拟串口（CH343，`115200 8N1`，无流控，`ASRL<n>::INSTR`）**，可选 LAN TCP Socket（`TCPIP0::<ip>::5000::SOCKET`）。**当前版本明确禁用 GPIB**（如选入 `GPIB...` 地址将拒绝连接并提示改用 USB 串口）。
- **双回路独立控制**：默认启用双回路：
  - `Output 1 (A)`（Heater 1，最大 2 A / 100 W）对应控制端点 `output_1`；
  - `Output 2 (B)`（Heater 2，最大 1.414 A / 70.7 W）对应控制端点 `output_2`。
  - 如使用单回路或 **Kelvinion Mini**（仅 2 路输入 A/B + 1 路输出 A），请在 Instrument Scanner 中取消勾选 `Enable Output 2 (Heater B)` 并禁用 `Output 2 (B)`、`Temperatures E-H`、`Output 2 Status` 面板。
- **状态与 PID 策略（参考 Lake Shore 340）**：
  - 不使用软件侧 `pid_file`，直接沿用仪表本机 PID 与硬件 Zone 表（`ZONE1`–`ZONE5`）。
  - `open()` 仅做只读身份、型号能力与回路传感器绑定检查，**绝不主动更改设定值、爬坡率、量程或加热状态**。
  - `[READ:SETP:CH]` 暂按**爬坡工作设定值（working/ramping setpoint）**实现（`HV-03`）：控制器卡片显示最终目标并推算 `moving`/`ready`，辅助状态网格显示仪表原始 `[READ:SETP:CH]` 与 `Ramping` 标志。
- **Instrument Setup 按需配置**：支持按需查看和修改传感器类型（`[READ/SET:T:CH]`）、校准曲线编号（`[READ/SET:W:CH]`）、输出控制回路（`[READ/SET:LOOP:CH]`）、加热量程（`[READ/SET:RANGE:CH]`）、PID 上限（`[READ/SET:LIMIT:CH]`）、硬件 Zone 开关与 `ZONE1`–`ZONE5` 参数，以及显式保存配置（`[SET:SAVE]`）。
- **本版本暂不包含**：`Filter`（R/Q）、`Excitation` 档位、`Reversal` 开关、Relay 继电器与 Analog 模拟输出。

## 安装与 Instrument Scanner 配置步骤

1. 将整个 `instruments/kelvinion/` 目录复制到主程序的 `OpenLabControl/system_instruments/kelvinion/`。
2. 用 USB-B 数据线连接 Kelvinion 与主机，在 Windows 设备管理器中确认 CH343 虚拟串口（COM 口）及 NI-VISA 正常识别，并在仪表面板确认通讯方式为串口（115200 bps）。
3. 关闭 OpenLab Control 主程序，运行 **Instrument Scanner**：
   - 在 **VISA Resources** 页扫描串口；若该 `ASRL<n>::INSTR` 显示 *No IDN response* 属正常现象（通用扫描发送 `*IDN?`，Kelvinion 需要方括号帧 `[*IDN?]`，见 `HV-08`），直接手动选用对应的 `ASRL<n>::INSTR`。
   - 在 **MultiFields Kelvinion Temperature Controller** 页添加实例：
     - 核对 **USB Serial Baud Rate**（默认 `115200`）与 **VISA I/O Timeout (ms)**（默认 `1000`）；
     - 核对 `Enable Output 2 (Heater B)`（双回路保持勾选，Mini 或单回路取消勾选）；
     - 两个未实机确认开关 `SET Returns ACK Frame [HV-01]`（默认 `false`）与 `Append 'K' Suffix on SET:SETP [HV-02]`（默认 `true`）可在真机联调时直接切换，无需修改源码；
     - 在 **Fixed Panels** 中为 `Output 1 (A)` 与 `Output 2 (B)` 选择各自在仪表上实际绑定的传感器输入（`temp_a`–`temp_h`），并将用于 SEQ 主控的一路 **Sequence Role** 设为 `Sample Temp`（另一路保持 `None`）；
     - 核对两路各自的现场安全上下限（`Min` / `Max`）和最大速率（硬件上限 `10 K/min`）。
   - 在 **Review & Save** 页检查预览并保存。
4. 启动 OpenLab Control，在面板和 Run Log 中确认读数正常，再按 [`HARDWARE_TEST_CHECKLIST.md`](HARDWARE_TEST_CHECKLIST.md) 执行真机验证。

## 安全说明

- 每次 `set_target` 写入前都会复查：现场上下限、精度（0.01 K / 0.01 K/min）、硬件爬坡率范围（0.1–10 K/min）、输出回路绑定 `[READ:LOOP:CH]`、自动模式 `[READ:MODE:CH] == A`、硬件 PID 温度上限 `[READ:LIMIT:CH]` 与传感器曲线上限 `[READ:INPUT_LIMIT:CH]`（防止触发 Warning 04 导致加热量程自动切 `OFF`）。
- 写命令超时或回读异常统一报错 `INSTRUMENT_WRITE_RESULT_UNKNOWN`，**绝不盲目重发**；重连前请先到仪表面板核对两路真实状态。
- `hold()` 现读目标回路新鲜温度并写入目标设定值，不发送 `RAMP 0`（见 `HV-04`），不关闭加热量程。
- `close()` 仅释放 VISA 会话，不关闭加热器、不改变设定值。
