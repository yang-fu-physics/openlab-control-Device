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
├─ requirements.lock      # 仅在有框架外依赖时需要
└─ wheels/                # 可选：额外依赖的离线 wheel
```

## 当前内容

- `example_controller`：fail-closed 温度/磁场控制骨架，不含真实命令。
- `example_monitor`：只读 Monitor 骨架，不含真实命令。
- `cryocon_22c_24c`：Cryo-con 22C/24C 的 Beta 实现；使用一个 USB VISA 会话，以 B 为
  主温度、A 为监视温度，并记录加热输出和量程。PID 表必须来自现场验证配置。
- `zlan_6408_compressor`：ZLAN 6408I 的 Beta 实现；使用 Modbus TCP，以 DO1 控制压缩机，
  以 DI1 显示并记录状态，并提供 `Compressor On/Off` SEQ 指令。

真实型号实现必须明确写出真机验证状态。示例骨架不能直接用于真实仪表。

## 手动离线安装

1. 审查目标目录、`instrument.toml` 和所有额外依赖哈希。
2. 把完整目录复制到 `OpenLabControl/system_instruments/<instrument-id>/`。
3. 运行 `tools/instrument_scanner.py`，人工确认物理地址、System Instrument 和需要显示的
   Auxiliary readings，保存 `configs/instruments.local.toml`。唯一匹配的实现会自动选择；
   主读数固定显示，名称、单位和精度来自 `instrument.toml` 的 `[readings]`。
4. 在 `configs/site.local.toml` 的 `[[instruments]]` 中设置
   `resource = "<资源 ID>"`、控制许可、上下限、最大速率和超时。
5. 重启 OpenLab Control，核对类型、ID、版本、绝对路径和内容指纹后确认。

复制目录本身不会连接仪表；只有现场配置实际选择该资源时才会启动。温度和磁场每种最多
一个 `control_enabled = true` 的实例，其他实例只读。

PySide6、QtAwesome、packaging、PyVISA 和 typing_extensions 使用主框架锁定版本，不在各
System Instrument 中重复安装。只有框架没有的额外依赖才需要 `requirements.lock` 和本地
wheel，安装过程完全离线。

每份 `instrument.toml` 必须明确选择底部主面板：

```toml
[panel]
template = "controller" # 温度/磁场：当前值、目标、速率和稳定状态
```

只读监视仪表使用 `template = "readout"`。一个 `readout` 以 2×2 最多显示四个读数，第五个
读数开始放到右侧的下一个面板。控制器的主面板保持原样，辅助读数按相同规则进入
`readout`。简单 0/1 状态和无参数指令按钮使用 `template = "switch"`；按钮直接复用清单的
`sequence_commands`。可控温度/磁场实例必须选择 `controller`；缺少 `[panel]` 的清单会直接
显示为 Invalid，不做旧清单兼容。

## 编写原则

- `__init__` 只保存配置，不连接仪表。
- `open()` 使用有限超时确认型号、固件、单位和关键状态，但不自动改变输出。
- `read_status()` 返回普通字典；需要时用 `read_measurement()` 只读写测量行所需的即时主值。
- `set_target()` 在发送前再次检查现场目标与速率边界，并在发送后回读确认。
- `hold()` 使用同一次操作里的新鲜读数，不能使用缓存值、猜测值或零。
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
