# OpenLab Control — System Instruments（系统仪表仓库）

本仓库维护 **OpenLab Control** 配套的 **System Instrument** 驱动，用于管理实验期间常驻的底层环境设备：主温控仪、磁场控制器、压缩机开关以及只读监控仪表。

> 如果你在找电阻表、源表、电桥、锁相放大器等按需启用的测量插件，请前往 [openlab-control-Module](https://github.com/yang-fu-physics/openlab-control-Module)。

## 当前包含的驱动

| 目录 ID | 适用设备 | 通讯方式 | 实机验证状态 |
| --- | --- | --- | --- |
| `lakeshore340` | Lake Shore Model 340 温控仪（`0.2.2.dev1`；Temp A/B、Loop 1/2、**Instrument Setup** 按需切换传感器曲线） | GPIB (VISA) | **已实机验证（2026-11-05）**（仅限已测接线与配置） |
| `zlan_6408_compressor` | ZLAN 6408I（4 路）/ 6842（8 路）压缩机继电器控制与 DI/DO 状态 | Modbus TCP | **已实机验证（2026-11-05）**（仅限已测接线与配置） |
| `cryocon_22c_24c` | Cryo-con 22C / 24C 温控仪（`0.3.1.dev2`；Loop 1 主控可选 A/B，含边界 `<=` 独立 PID 表、**Instrument Setup** 按需切换曲线与软件 PID 表） | VISA（含 USB 虚拟串口） | **22C 现场读写与升温响应验证（2026-11-22）**；24C 未验证 |
| `kelvinion` | MultiFields Technologies Kelvinion / Mini 温控仪（`0.1.0.dev1`；独立双回路 Output 1/2、A–H 通道、340 风格状态、**Instrument Setup** 传感器/输出/Zone 配置） | USB 虚拟串口（VISA 115200 8N1；可选 LAN；暂禁用 GPIB） | **空载串口协议初测（2026-11-27，V2.2.6b6）**；闭环控温与 Setup 写入待验证（详见 [`HARDWARE_TEST_CHECKLIST.md`](instruments/kelvinion/HARDWARE_TEST_CHECKLIST.md)） |
| `example_controller` | 温场控制教学骨架（默认拒绝连接真机） | 无 | 仅作开发模板 |
| `example_monitor` | 只读监控教学骨架（默认拒绝连接真机） | 无 | 仅作开发模板 |

> 接入任何真实硬件前，请先阅读对应目录内的 `README.md` 与主框架的《真实仪表安全清单》。Cryo-con `0.3.1.dev2` 按包含等于（`<=`）的温度上限选择 PID，并附带可选参考文件 [`pid.operator-example.toml`](instruments/cryocon_22c_24c/pid.operator-example.toml)（非通用默认整定值，须核对冷台适用性后显式选用）。

## 离线安装与配置（4 步）

每个 `instruments/<仪表ID>/` 都是独立完整的安装包，按**一种物理型号/协议**组织（同一台设备的多路温度和加热状态共用一个实例和一条连接）：

1. 核对目标目录的 `README.md`、`instrument.toml` 和代码。
2. 将完整目录复制到主程序的 `OpenLabControl/system_instruments/<仪表ID>/`。
3. 关闭主程序，运行 **Instrument Scanner**（发布包双击 `InstrumentScanner.exe`，源码版运行 `tools/instrument_scanner.py`）：
   - **VISA Resources**：扫描确认 GPIB/USB/串口地址；
   - **对应仪表页**：添加物理实例，选定 VISA 地址（或网口仪表的 `host` / `port`），核对型号参数、面板角色（`sample_temp` / `field` / `none`）和现场安全限值；
   - **Review & Save**：排好面板顺序，检查完整预览后保存（不会覆盖或删除已有 `configs/pid/*.toml`）。
4. 重启 OpenLab Control，在底部面板和 Run Log 中确认连接状态与读数。

> 依赖说明：`PySide6`、`QtAwesome`、`packaging`、`PyVISA` 和 `typing_extensions` 均由主框架统一锁定提供，各驱动不重复安装依赖。

## 驱动目录结构与 API v4 速览

```text
instruments/<仪表ID>/
├─ instrument.toml        # API v4 声明：字段、控制端点、固定面板、读数、指令与可选 setup_widget
├─ backend.py             # 继承 SystemInstrument 的同步后台类
├─ instrument.py          # 可选：底层 SCPI / Modbus 命令与解析
├─ frontend.py            # 可选：Instrument Setup 按需配置界面（如曲线 / 软件 PID 表切换）
├─ pid.example.toml       # 可选：首次配置时自动复制的现场 PID 空模板（非默认参考表须显式选用）
└─ README.md              # 接线、参数与验证说明
```

`instrument.toml` 通过五组标准区块驱动扫描器表单与主程序运行：

```toml
[[config_fields]]
id = "visa_timeout_ms"
label = "VISA I/O Timeout (ms)"
type = "integer"
default = 1000
min = 1

[[controls]]
id = "main"
label = "Main Temperature"

[[panels]]
id = "control"
label = "Control"
template = "controller"
control = "main"
reading_options = ["temp_a", "temp_b"]
default_reading = "temp_a"
```

- **单一会话、多面板共享**：同一种型号可在扫描器中配置多个 `[[instances]]`（每台物理仪表一个子进程）；单台物理仪表上的多个面板（`controller` / `readout` / `readout_grid` / `switch`）共用同一个子进程和通讯会话。
- **多回路支持**：只有一个已启用控制端点时，`read_status()` 可在顶层返回 `target/rate/moving/ready`；启用多个不同控制端点时按 `controls[control_id]` 分别返回。

## 开发与安全守则

- `__init__` 只保存配置，不发起通讯；
- `open()` 用有限超时确认身份、单位与关键状态，**不自动改写设定或输出**；
- `set_target(..., control=...)` 发送前复查对应端点的现场上下限，发送后尽量回读确认；
- `hold(control=...)` 必须使用同一次操作里现读的新鲜值，绝不用旧缓存或 0；
- 写命令超时视为结果不确定，绝不盲目重发；重连后先读回真实状态；
- `close()` 必须幂等，能在部分初始化失败后安全释放资源。

## 运行测试

```powershell
<OpenLabControl>\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

软件保护与自动化测试永远不能替代仪表本机限值、硬件联锁和人工急停。
