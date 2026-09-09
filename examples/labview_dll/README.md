# LabVIEW DLL System Instrument 模板

要求 OpenLab Control `0.20.1` 或更新的 `0.20.x` 版本。

仪表已有经过实际使用的 LabVIEW 驱动或控制 VI 时，优先使用本模板复用原实现，而不是重新
用 Python 翻译整套底层指令。复用不会替代安全边界：上下限、有限超时、写后回读和真机
低风险验证仍然必须完成。

这个目录是开发模板，不会被扫描器当成已安装仪表。使用时复制整个目录到
`system_instruments/<新 ID>/`，修改 `instrument.toml`，再把编译好的 64 位
`driver.dll` 和它自己的依赖文件放在 `backend.py` 旁边。

```text
system_instruments/<id>/
├─ instrument.toml
├─ backend.py
├─ labview_dll.py
├─ driver.dll
└─ driver 的其他依赖文件
```

Python 只负责生命周期、安全边界和错误类型转换。DLL 在仪表工作进程启动时加载一次，
之后每次 `read_status`、`set_target` 等操作只调用 DLL 一次。仪表 I/O 的耗时通常远大于
JSON 编解码和一次本地函数调用，因此不要把一次完整查询拆成很多 DLL 导出函数。

## 固定导出接口

DLL 必须以 C 调用约定导出以下四个名称：

- `OLC_Describe`：返回 ABI 类型和 DLL 支持的操作。
- `OLC_Open`：建立仪表连接并完成身份检查。
- `OLC_Invoke`：执行一个高层操作。
- `OLC_Close`：释放包括部分初始化在内的全部资源；允许重复调用。

`OLC_Describe` 必须只返回常量说明，不能连接仪表、启动后台循环或改变输出。模块/仪表窗口
会等它返回后才完成初始化。`OLC_Open` 可把 VISA session 等句柄保存在 LabVIEW functional
global 中，后续 `OLC_Invoke` 复用同一份状态，`OLC_Close` 最后清除它。

四个函数必须与 `openlab_labview_abi.h` 使用完全相同的签名。输入和输出是 UTF-8 JSON
字节数组，不是 LabVIEW String Handle：

```c
int32_t Function(
    const uint8_t *request_json,
    int32_t request_length,
    uint8_t *response_json,
    int32_t response_capacity,
    int32_t *response_length);
```

Python 提供 1 MiB 输出缓冲区。DLL 只能写入 `response_capacity` 以内的字节，并把实际
字节数写入 `response_length`；无需写结尾 `\0`。返回值 `0` 表示 ABI 调用完成，此时必须
返回下面的 JSON 信封。非零返回值只用于坏指针、容量不足等无法生成 JSON 的 ABI 故障，
不能用来表示仪表超时、越界或坏读数。

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {}
}
```

`severity` 只能是 `ok`、`warning` 或 `error`。Warning 允许 SEQ 继续；Error 中止 SEQ。
把 LabVIEW error cluster 映射到 `code/message/context`，不要把错误文本塞进正常读数。
建议 `code` 使用不会随提示文字改变的短名称。

## LabVIEW 端编译

1. 为四个导出函数各建一个很薄的顶层 VI；真实仪表 VI 保持在它们下面。
2. 顶层 VI 使用 U8 数组接收和写回 JSON，并使用 I32 表示长度、容量、返回值。
3. 在 Application Builder 中建立 64 位 Shared Library，导出名称严格使用上面的四个名称，
   调用约定选择 C (`cdecl`)。
4. 对数组参数选择数据指针形式。编译后检查 LabVIEW 生成的头文件；函数参数、顺序、
   位宽、指针层级和调用约定必须与 `openlab_labview_abi.h` 一致，不一致时不要试运行。
5. OpenLab Control、DLL、依赖 DLL 和 LabVIEW Run-Time Engine 必须具有相同位数。运行时
   版本必须能加载构建该 DLL 的 LabVIEW 版本。

## JSON 操作

`OLC_Describe` 收到 `{}`，至少返回：

```json
{
  "severity": "ok",
  "code": "",
  "message": "",
  "context": "",
  "result": {
    "abi_version": 1,
    "kind": "system_instrument",
    "operations": [
      "read_status",
      "read_measurement",
      "set_target",
      "hold",
      "sequence_command",
      "manual_function"
    ],
    "manual_functions": [
      {
        "id": "diagnostic_read",
        "label": "Diagnostic Read",
        "description": "Run one existing LabVIEW diagnostic VI.",
        "inputs": [
          {"name": "mode", "label": "Mode", "type": "choice", "default": "Current", "choices": ["Current", "Voltage"]},
          {"name": "range", "label": "Range", "type": "choice", "default": "Auto", "choices": ["Auto", "Low", "High"]},
          {"name": "level", "label": "Level", "type": "float", "default": 0.001, "minimum": 0.0, "maximum": 1.0, "unit": "A", "decimals": 9},
          {"name": "samples", "label": "Samples", "type": "int", "default": 10, "minimum": 1, "maximum": 1000}
        ],
        "outputs": [
          {"name": "value", "label": "Value", "type": "float", "unit": "V", "decimals": 9},
          {"name": "status", "label": "Status", "type": "int"}
        ]
      }
    ]
  }
}
```

`manual_functions` 是可选项。它直接保存在 DLL 的静态说明里，不需要再修改 TOML 或框架
源码。System Instrument 加载后，OpenLab 在主菜单 `Instrument` 下按这份说明自动生成输入、
输出和 Run 按钮；同一函数可以打开多个窗口。只允许在 SEQ 空闲时运行，返回值显示在窗口并
写入事件日志，不写 DAT。

`OLC_Open` 的请求包含实例 ID、物理地址、`instrument.toml` 声明的专用设置以及核心操作
超时。例如：

```json
{
  "id": "temperature_controller",
  "address": "GPIB0::12::INSTR",
  "settings": {"visa_timeout_ms": 1000},
  "operation_timeout_seconds": 10.0
}
```

`OLC_Invoke` 的统一请求是：

```json
{"operation": "set_target", "parameters": {"control": "main", "value": 10.0, "rate_per_minute": 1.0, "mode": "Settle"}}
```

各操作的 `result`：

- `read_status`：直接返回框架状态字典；至少有 `value`，控制器还应给出
  `target/rate/moving/ready`，其他清单读数放在 `auxiliary`。
- `read_measurement`：直接返回即时测量状态字典；只读测量真正需要的值。
- `set_target`、`hold`、`sequence_command`：成功时通常返回空对象。
- `manual_function`：参数包含 `function_id`、对应的 `parameters` 和总超时；结果字段必须与
  `manual_functions.outputs` 完全一致。
- `OLC_Open`、`OLC_Close`：成功时通常返回空对象。

## 必须修改的清单内容

示例清单故意使用永远匹配不到真实仪表的 discovery pattern。复制后必须修改 ID、名称、
型号匹配、`config_fields`、control、panel、reading、单位和现场安全边界。DLL 的
`OLC_Describe` 在已配置仪表的 worker 启动时运行，不能代替扫描器启动前读取的 `instrument.toml`。

System Instrument 的 SEQ 完成或停止不调用 `OLC_Close`。应用退出时只断开通讯，继续保留
温控/磁场的当前动作，除非该仪表的明确约定要求其他收尾行为。

`set_target` 在进入 DLL 前会按所选 Controller 面板再次检查目标和速率；DLL 仍应使用
仪表本机限制并在写入后回读确认。阻塞 I/O 必须有有限超时。框架超时可以结束工作进程，
但不能保证一个卡死的厂商调用有机会执行 `OLC_Close`，所以硬件联锁不能由 DLL 代替。
