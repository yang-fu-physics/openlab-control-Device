# OpenLab Control System Instruments

本仓库保存 OpenLab Control 的 System Instrument。它们负责主温度、主磁场和长期只读监视，
不负责电阻、电压等按需测量；后者属于 Measurement Module。

每个 `instruments/<id>/` 都按一种物理仪表型号/协议组织，是一个可独立手动安装的完整
目录。不要按 TempA、TempB 或“主/副温度”拆分；同一物理地址只允许一个实例和一个会话：

```text
instruments/<id>/
├─ instrument.toml
├─ backend.py
├─ instrument.py          # 可选：底层命令和响应解析
└─ *.example.toml         # 可选：首次配置时复制的现场模板
```

## 当前内容

- `example_controller`：fail-closed 温度/磁场控制骨架，不含真实命令。
- `example_monitor`：只读 Monitor 骨架，不含真实命令。
- `cryocon_22c_24c`：Cryo-con 22C/24C 的 Beta 实现；使用一个 USB VISA 会话，以 B 为
  主温度、A 为监视温度，并记录加热输出和量程。每个物理实例使用独立 PID 文件。
- `lakeshore340`：Lake Shore 340 的 Beta 实现；使用 GPIB VISA，扫描器可为 controller
  选择 Temp A 或 Temp B 作为主温度。已完成现场温控实机测试。
- `zlan_6408_compressor`：ZLAN 6408I（四路）/ 6842（八路）的 Modbus TCP 压缩机控制；
  开关步骤和延时由扫描器配置，显示并记录选定 DI/DO 位的与条件；提供
  `Compressor On/Off` SEQ 指令。已完成现场压缩机控制实机测试。

以上实机状态由现场于 2026-11-05 确认，仅覆盖实际测试的接线和配置。

真实型号实现必须明确写出真机验证状态。示例骨架不能直接用于真实仪表。

`examples/labview_dll` 是直接加载 LabVIEW Shared Library 的开发模板。它规定固定的
UTF-8 JSON ABI，把输入、输出和 LabVIEW error cluster 映射到现有 System Instrument
生命周期；复制到 `system_instruments/<id>/` 并完成清单和 DLL 后才会被主程序加载。
详细步骤见该目录的 README。

## 手动离线安装

1. 审查目标目录、`instrument.toml` 和后台代码。
2. 把完整目录复制到 `OpenLabControl/system_instruments/<instrument-id>/`。
3. 运行 `tools/instrument_scanner.py`。第一页确认 VISA 资源；后续每页配置一种 System
   Instrument；最后一页检查所有面板并调整全局顺序。
4. 保存后，未分配给 System Instrument 的 VISA 地址写入
   `configs/visa.resources.toml`，实例写入 `configs/instruments/<instrument-id>.toml`。
   System Instrument 使用的 VISA 地址不会重复进入资源文件。ZLAN 等专用 TCP 仪表直接由
   自己的 `config_fields` 提供 Host/Port，不参加通用 TCP 或 VISA 扫描。
5. 重启 OpenLab Control，检查连接状态、前面板和 Run Log。

保存会完整重写扫描器预览中的实例配置。每种 System Instrument 可以有多个物理实例；一个
物理实例只创建一个后台进程和通讯会话，多个面板或控制端点都复用它。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 使用主框架锁定版本，不在各
System Instrument 中重复安装。需要新 Python 包时更新核心锁定依赖、完成全部测试并重新
构建发布包。

System Instrument API v4 不读取旧 `[panel]`、`[[panel.sections]]`、`[settings]`、
`main_reading` 或 `main_readings`。清单用以下三个命名集合驱动扫描表单和运行时：

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

`config_fields` 只声明型号专用输入；通用控制上下限、速率与稳定参数由 `controller` 面板
给出初值，并由操作者在扫描器中确认。`controls` 是后端可控制端点。`panels` 的 `id` 是稳定
短标识，实例配置只引用这个 ID，并保存 `enabled/order/role/reading` 和现场控制参数，不复制
面板模板实现。`controller` 引用一个 control；`readout`/`readout_grid` 声明读数；`switch`
声明状态读数和无参数命令。面板可关闭和全局排序，不能临时新增或删除模板。

同一物理实例可以声明多个独立 control，仍只建立一个 worker 和通讯会话。只有一个不同的
已启用 control 时，`read_status()` 可在顶层返回 `target/rate/moving/ready`；启用了多个
不同 control 时，必须返回 `controls[control_id]`，让每个回路分别提供这四项。回路当前值
仍来自其 Controller 面板选择的 `value` 或 `auxiliary` 读数。`set_target` 和 `hold` 必须按
传入的 `control` 选择真实回路，并从对应面板配置复查该回路的现场安全限制。

## 编写原则

- `__init__` 只保存配置，不连接仪表。
- `open()` 使用有限超时确认型号、固件、单位和关键状态，但不自动改变输出。
- `read_status()` 返回普通字典；需要时用 `read_measurement()` 只读写测量行所需的即时主值。
- `set_target(..., control=<id>)` 在发送前再次检查对应控制端点的现场目标与速率边界，并在
  发送后回读确认。
- `hold(control=<id>)` 使用同一次操作里的新鲜读数，不能使用缓存值、猜测值或零。
- 写命令超时后不自动重发；重新连接后先读取实际状态。
- `close()` 可重复调用，并能清理只完成一半的初始化。
- 同一通讯口只由一个实例持有；地址读取 `config.address`。
- 辅助温度和加热状态放在 `auxiliary` 字典中；名称、单位和精度只在清单声明一次。
- 不同物理资源各有独立进程，连接和轮询会并发进行。

## 测试

```text
<OpenLabControl>\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真机前至少测试身份不符、损坏响应、I/O 超时、上下限边界、写后回读、写超时不重放、Hold
新鲜读数、断线恢复、重复断开、部分初始化清理和进程退出。软件保护不能替代仪表本机限制、
硬件联锁或人工急停。
