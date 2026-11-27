"""MultiFields Technologies Kelvinion 协议层与 VISA 传输封装（Manual Rev 3.1.1）。

本模块仅负责底层通讯、方括号帧 ``[...]`` 编解码、命令组装与响应解析，不依赖 OpenLab Control
生命周期或 GUI。所有尚未在实机上验证的协议细节均使用 ``UNVERIFIED_PROTOCOL (HV-XX)`` 注释
标记，并与同目录 ``HARDWARE_TEST_CHECKLIST.md`` 一一对应。

按当前规划，本版本暂不实现 Filter（``[READ/SET:F:...]``）、Excitation
（``[READ/SET:EXCITATION:...]``）、Reversal（``[READ/SET:REVERSAL:...]``）以及 Relay/Analog。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Protocol


INPUT_CHANNELS_STANDARD: tuple[str, ...] = ("A", "B", "C", "D", "E", "F", "G", "H")
INPUT_CHANNELS_MINI: tuple[str, ...] = ("A", "B")
OUTPUT_CHANNELS_STANDARD: tuple[str, ...] = ("A", "B")
OUTPUT_CHANNELS_MINI: tuple[str, ...] = ("A",)

SENSOR_TYPES: tuple[str, ...] = ("CERNOX", "PT100", "PT1000", "DIODE", "RO105")
HEATER_RANGES: tuple[str, ...] = ("OFF", "LOW", "MED", "HIGH")
ZONE_NAMES: tuple[str, ...] = ("ZONE1", "ZONE2", "ZONE3", "ZONE4", "ZONE5")
ALLOWED_BAUD_RATES: tuple[int, ...] = (115200, 9600, 19200, 38400, 57600)

# 硬件规格限值（Manual v3.1.1 Ch. 3 / Ch. 5）
HARDWARE_MIN_SETPOINT_K = 0.0
HARDWARE_MAX_SETPOINT_K = 999.0
# UNVERIFIED_PROTOCOL (HV-04): 手册 p.40 写爬坡率 0.1–10 K/min（精度 0.01），但 p.110 截图出现 Ramp 0.00。
HARDWARE_MIN_RAMP_K_PER_MIN = 0.1
HARDWARE_MAX_RAMP_K_PER_MIN = 10.0
# UNVERIFIED_PROTOCOL (HV-08): 手册 p.35 写曲线 0–9，AppNote 03 (p.117) 写 MF00–MF99（0–99）。
MIN_CURVE_NUMBER = 0
MAX_CURVE_NUMBER = 99

ZONE_RANGE_FROM_CODE: dict[str, str] = {
    "0": "OFF",
    "1": "LOW",
    "2": "MED",
    "3": "HIGH",
    "OFF": "OFF",
    "LOW": "LOW",
    "MED": "MED",
    "HIGH": "HIGH",
}
ZONE_RANGE_TO_CODE: dict[str, int] = {
    "OFF": 0,
    "LOW": 1,
    "MED": 2,
    "HIGH": 3,
}

# UNVERIFIED_PROTOCOL (HV-05): 手册截图出现 0.000 K（Fig 13/17/63）和 S/C（Fig 35/43），
# 实机断开或短路时的确切线缆返回字符串待确认。
INVALID_SENSOR_TOKENS = frozenset(
    {"S/C", "O/C", "SC", "OC", "OL", "OVER", "UNDER", "OPEN", "SHORT", "ERR", "ERROR", "NAN", "NULL", "NONE", "---", "--"}
)

_ASRL_PATTERN = re.compile(r"^ASRL\d+::INSTR$", re.IGNORECASE)
_TCPIP_SOCKET_PATTERN = re.compile(r"^TCPIP\d*::[^:]+::\d+::SOCKET$", re.IGNORECASE)
_GPIB_PATTERN = re.compile(r"^GPIB", re.IGNORECASE)
_BRACKET_FRAME = re.compile(r"^\[(.*)\]$", re.DOTALL)


class KelvinionProtocolError(RuntimeError):
    """仪表帧格式错误、响应缺失、字段非法或写入回读不匹配。"""


class KelvinionSensorFault(KelvinionProtocolError):
    """输入传感器返回断开、短路、非数值或超范围读数（如 0.000 K / S/C）。"""


class KelvinionTransport(Protocol):
    """Kelvinion 底层同步通讯协议接口，便于离线测试注入伪仪表。"""

    def query(self, frame: str) -> str: ...

    def write(self, frame: str) -> None: ...

    def read(self) -> str: ...

    def close(self) -> None: ...


def validate_visa_address(address: str) -> str:
    """校验 VISA 地址：USB 虚拟串口（ASRL）为主通道，可选 LAN Socket，明确拒绝 GPIB。"""

    normalized = str(address).strip()
    if not normalized:
        raise ValueError("Kelvinion VISA resource address must not be empty")
    if _GPIB_PATTERN.match(normalized):
        raise ValueError(
            "GPIB is currently unavailable for Kelvinion; select the USB virtual serial "
            "VISA address (ASRL<n>::INSTR, 115200 8N1) or LAN socket (TCPIP0::<ip>::5000::SOCKET)."
        )
    if not (_ASRL_PATTERN.match(normalized) or _TCPIP_SOCKET_PATTERN.match(normalized)):
        raise ValueError(
            f"Unsupported Kelvinion VISA resource address {normalized!r}; expected "
            "USB virtual serial (ASRL<n>::INSTR) or LAN socket (TCPIP0::<ip>::5000::SOCKET)."
        )
    return normalized


class PyVisaTransport:
    """基于 PyVISA 的 Kelvinion USB 虚拟串口（CH343, 115200 8N1 无流控）/ LAN Socket 会话。"""

    def __init__(
        self,
        address: str,
        timeout_ms: int,
        *,
        baud_rate: int = 115200,
    ) -> None:
        import pyvisa
        from pyvisa.constants import ControlFlow, Parity, StopBits

        normalized_address = validate_visa_address(address)
        timeout = max(1, int(timeout_ms))
        manager = pyvisa.ResourceManager()
        resource = None
        try:
            resource = manager.open_resource(
                normalized_address,
                open_timeout=timeout,
            )
            resource.timeout = timeout
            # UNVERIFIED_PROTOCOL (HV-08): 手册 p.83 / p.131 说明帧以 \r\n 结尾。
            # 若实机串口在收到结尾 \r\n 时异常，可将 write_termination 调整为 "" 后再测试。
            resource.read_termination = "\r\n"
            resource.write_termination = "\r\n"
            if normalized_address.upper().startswith("ASRL"):
                resource.baud_rate = int(baud_rate)
                resource.data_bits = 8
                resource.parity = Parity.none
                resource.stop_bits = StopBits.one
                resource.flow_control = ControlFlow.none
        except Exception:
            try:
                if resource is not None:
                    resource.close()
            finally:
                manager.close()
            raise
        self._manager = manager
        self._resource = resource

    def query(self, frame: str) -> str:
        return str(self._resource.query(frame))

    def write(self, frame: str) -> None:
        self._resource.write(frame)

    def read(self) -> str:
        return str(self._resource.read())

    def close(self) -> None:
        resource, self._resource = self._resource, None
        manager, self._manager = self._manager, None
        try:
            if resource is not None:
                resource.close()
        finally:
            if manager is not None:
                manager.close()


@dataclass(frozen=True, slots=True)
class KelvinionIdentity:
    """[*IDN?] 解析后的身份信息。"""

    manufacturer: str
    model: str
    serial: str
    firmware: str
    is_mini: bool


@dataclass(frozen=True, slots=True)
class OutputReadback:
    """单个输出通道（A=Output 1, B=Output 2）的单轮状态快照。"""

    output: str
    power_percent: float
    heater_range: str
    working_setpoint_k: float
    ramp_rate_k_per_min: float
    mode: str
    zone_state: str


@dataclass(frozen=True, slots=True)
class ZoneEntry:
    """硬件 Zone 表的一行（ZONE1–ZONE5）。"""

    name: str
    kp: float
    ki: float
    kd: float
    heater_range: str
    boundary_k: float

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kp": self.kp,
            "ki": self.ki,
            "kd": self.kd,
            "heater_range": self.heater_range,
            "boundary_k": self.boundary_k,
        }


def format_frame(command: str) -> str:
    """将裸命令包装为 Kelvinion 方括号帧 ``[CMD]``。"""

    body = str(command).strip()
    if not body:
        raise ValueError("Kelvinion command must not be empty")
    if "[" in body or "]" in body or "\r" in body or "\n" in body:
        raise ValueError(f"Kelvinion command contains forbidden frame characters: {body!r}")
    return f"[{body}]"


def unwrap_frame(raw_response: str, *, context: str) -> str:
    """严格剥离响应外层方括号 ``[...]``，拒绝空帧或非方括号乱码。"""

    text = str(raw_response).strip()
    if not text:
        raise KelvinionProtocolError(f"Empty response for {context}")
    match = _BRACKET_FRAME.match(text)
    if match is None:
        raise KelvinionProtocolError(
            f"Malformed Kelvinion frame for {context}: expected '[...]', got {raw_response!r}"
        )
    inner = match.group(1).strip()
    if not inner:
        raise KelvinionProtocolError(f"Empty bracket payload for {context}: {raw_response!r}")
    return inner


def query_command(transport: KelvinionTransport, command: str) -> str:
    """发送 ``[CMD]`` 并解包返回字符串。"""

    frame = format_frame(command)
    raw = transport.query(frame)
    return unwrap_frame(raw, context=frame)


def write_command(
    transport: KelvinionTransport,
    command: str,
    *,
    expects_ack: bool = False,
) -> str | None:
    """发送写命令 ``[SET:...]``；当 ``expects_ack=True`` 时同步读取并校验回执帧。

    UNVERIFIED_PROTOCOL (HV-01): 手册第 5 章对 ``[SET:...]`` 未列返回帧，但 AppNote 07 中
    Profibus 指令会返回 ``[SET ... OK]``。若实机串口对 ``[SET:...]`` 返回回执且未读取，将污染
    紧随其后的查询响应；通过 ``expects_ack`` 可无改代码切换并在实机测试中确认。
    """

    frame = format_frame(command)
    if not expects_ack:
        transport.write(frame)
        return None
    raw_ack = transport.query(frame)
    ack = unwrap_frame(raw_ack, context=frame)
    upper = ack.upper()
    if "ERR" in upper or "FAIL" in upper or "INVALID" in upper:
        raise KelvinionProtocolError(f"Instrument rejected {frame} with {raw_ack!r}")
    return ack


def parse_number(text: str, *, label: str, allow_units: tuple[str, ...] = ()) -> float:
    """解析有限浮点数，可选去除末尾单位后缀（如 K 或 %）。"""

    cleaned = str(text).strip()
    for suffix in allow_units:
        if cleaned.upper().endswith(suffix.upper()):
            cleaned = cleaned[: -len(suffix)].strip()
            break
    try:
        value = float(cleaned)
    except (TypeError, ValueError) as exc:
        raise KelvinionProtocolError(f"Invalid numeric {label}: {text!r}") from exc
    if not math.isfinite(value):
        raise KelvinionProtocolError(f"Non-finite {label}: {text!r}")
    return value


def identify(transport: KelvinionTransport) -> KelvinionIdentity:
    """查询 ``[*IDN?]`` 并校验制造商与 Kelvinion / Kelvinion Mini 型号。"""

    payload = query_command(transport, "*IDN?")
    parts = [item.strip() for item in payload.split(",")]
    if len(parts) < 2:
        raise KelvinionProtocolError(f"Invalid [*IDN?] payload: {payload!r}")
    manufacturer = parts[0]
    model = parts[1]
    serial = parts[2] if len(parts) > 2 else ""
    firmware = parts[3] if len(parts) > 3 else ""

    normalized_maker = manufacturer.casefold()
    normalized_model = " ".join(model.casefold().split())
    if "multifields" not in normalized_maker or normalized_model not in {
        "kelvinion",
        "kelvinion mini",
    }:
        raise KelvinionProtocolError(f"Unexpected Kelvinion identity: {payload!r}")
    return KelvinionIdentity(
        manufacturer=manufacturer,
        model=model,
        serial=serial,
        firmware=firmware,
        is_mini=(normalized_model == "kelvinion mini"),
    )


def read_temperature_k(transport: KelvinionTransport, channel: str) -> float:
    """查询输入通道开尔文温度 ``[READ:K:CH]``，检测断开/短路/非正读数。

    UNVERIFIED_PROTOCOL (HV-05): 仪表断开或短路探头时在屏幕显示 ``0.000 K`` 或 ``S/C``；
    此处将非数值标记、非有限值、``<= 0.0 K`` 或 ``> 999.0 K`` 统一视为 ``KelvinionSensorFault``。
    """

    ch = _normalize_input_channel(channel)
    payload = query_command(transport, f"READ:K:{ch}")
    token = payload.strip().upper()
    if token in INVALID_SENSOR_TOKENS:
        raise KelvinionSensorFault(f"Input {ch} sensor fault ({payload!r})")
    try:
        value = parse_number(payload, label=f"Temp {ch}", allow_units=("K",))
    except KelvinionProtocolError as exc:
        raise KelvinionSensorFault(
            f"Input {ch} returned non-numeric temperature {payload!r}"
        ) from exc
    if value <= 0.0 or value > HARDWARE_MAX_SETPOINT_K:
        raise KelvinionSensorFault(
            f"Input {ch} temperature {value:g} K is outside valid sensor range (0, {HARDWARE_MAX_SETPOINT_K:g}] K"
        )
    return value


def read_loop_binding(transport: KelvinionTransport, output: str) -> str:
    """查询输出通道绑定的输入传感器 ``[READ:LOOP:CH]`` -> ``A``..``H`` 或 ``NULL``。

    UNVERIFIED_PROTOCOL (HV-06): 手册 Table 9 漏印 ``:CH``，第 96 页正文为 ``[READ:LOOP:CH]``，
    返回值应为 ``A``–``H`` 或 ``NULL``（若实机返回 ``OFF`` 亦兼容归一化为 ``OFF``）。
    """

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:LOOP:{out}").upper()
    if payload in {"NULL", "OFF", "NONE"}:
        return "OFF"
    if payload not in INPUT_CHANNELS_STANDARD:
        raise KelvinionProtocolError(f"Invalid [READ:LOOP:{out}] response: {payload!r}")
    return payload


def read_mode(transport: KelvinionTransport, output: str) -> str:
    """查询输出控制模式 ``[READ:MODE:CH]`` -> ``A``（Auto PID）或 ``M``（Manual）。"""

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:MODE:{out}").upper()
    if payload in {"A", "AUTO"}:
        return "A"
    if payload in {"M", "MANUAL"}:
        return "M"
    raise KelvinionProtocolError(f"Invalid [READ:MODE:{out}] response: {payload!r}")


def read_heater_range(transport: KelvinionTransport, output: str) -> str:
    """查询加热档位 ``[READ:RANGE:CH]`` -> ``OFF`` / ``LOW`` / ``MED`` / ``HIGH``。"""

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:RANGE:{out}").upper()
    normalized = ZONE_RANGE_FROM_CODE.get(payload)
    if normalized is None:
        raise KelvinionProtocolError(f"Invalid [READ:RANGE:{out}] response: {payload!r}")
    return normalized


def read_zone_state(transport: KelvinionTransport, output: str) -> str:
    """查询硬件 Zone 状态 ``[READ:ZONE:STATE:CH]`` -> ``OFF`` 或 ``1``..``5``。

    UNVERIFIED_PROTOCOL (HV-07): 手册说明返回 ``OFF`` 或 ``1``–``5``；若实机返回 ``ZONE1``..``ZONE5``
    亦兼容归一化为 ``Zone 1``..``Zone 5``。
    """

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:ZONE:STATE:{out}").upper()
    if payload in {"OFF", "0"}:
        return "OFF"
    if payload.startswith("ZONE"):
        suffix = payload[4:]
        if suffix in {"1", "2", "3", "4", "5"}:
            return f"Zone {suffix}"
    if payload in {"1", "2", "3", "4", "5"}:
        return f"Zone {payload}"
    raise KelvinionProtocolError(f"Invalid [READ:ZONE:STATE:{out}] response: {payload!r}")


def read_working_setpoint(transport: KelvinionTransport, output: str) -> float:
    """查询输出设定值 ``[READ:SETP:CH]``。

    UNVERIFIED_PROTOCOL (HV-03): 按当前设计假定 ``[READ:SETP:CH]`` 返回爬坡中的工作设定值
    （working/ramping setpoint），待实机验证。
    """

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:SETP:{out}")
    value = parse_number(payload, label=f"Output {out} setpoint", allow_units=("K",))
    if not HARDWARE_MIN_SETPOINT_K <= value <= HARDWARE_MAX_SETPOINT_K:
        raise KelvinionProtocolError(
            f"Output {out} setpoint {value:g} K is outside [0, 999] K"
        )
    return value


def read_ramp_rate(transport: KelvinionTransport, output: str) -> float:
    """查询输出升降温速率 ``[READ:RAMP:CH]``（K/min）。"""

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:RAMP:{out}")
    value = parse_number(payload, label=f"Output {out} ramp rate")
    if value < 0.0 or value > 100.0:
        raise KelvinionProtocolError(f"Output {out} ramp rate {value:g} K/min is invalid")
    return value


def read_power_percent(transport: KelvinionTransport, output: str) -> float:
    """查询输出功率百分比 ``[READ:POWER:CH]``（%）。"""

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:POWER:{out}")
    value = parse_number(payload, label=f"Output {out} power", allow_units=("%",))
    if not 0.0 <= value <= 100.0:
        raise KelvinionProtocolError(
            f"Output {out} power {value:g}% is outside [0, 100]%"
        )
    return value


def read_output_status(transport: KelvinionTransport, output: str) -> OutputReadback:
    """读取单个输出回路的完整运行状态（功率、量程、设定值、爬坡率、模式、Zone 状态）。"""

    out = _normalize_output_channel(output)
    return OutputReadback(
        output=out,
        power_percent=read_power_percent(transport, out),
        heater_range=read_heater_range(transport, out),
        working_setpoint_k=read_working_setpoint(transport, out),
        ramp_rate_k_per_min=read_ramp_rate(transport, out),
        mode=read_mode(transport, out),
        zone_state=read_zone_state(transport, out),
    )


def read_pid_limit_k(transport: KelvinionTransport, output: str) -> float:
    """查询输出硬件 PID 温度上限 ``[READ:LIMIT:CH]``（K）。"""

    out = _normalize_output_channel(output)
    payload = query_command(transport, f"READ:LIMIT:{out}")
    return parse_number(payload, label=f"Output {out} PID limit", allow_units=("K",))


def read_input_limit_k(transport: KelvinionTransport, channel: str) -> float:
    """查询输入通道传感器曲线上限 ``[READ:INPUT_LIMIT:CH]``（K）。"""

    ch = _normalize_input_channel(channel)
    payload = query_command(transport, f"READ:INPUT_LIMIT:{ch}")
    return parse_number(payload, label=f"Input {ch} curve limit", allow_units=("K",))


def format_setpoint_command(
    output: str,
    target_k: float,
    *,
    unit_suffix: bool = True,
) -> str:
    """构造 ``SET:SETP:CH:VALUE`` 命令体。

    UNVERIFIED_PROTOCOL (HV-02): 手册 p.96 示例为 ``[SET:SETP:A:300K]``（带 ``K`` 后缀），
    其余温度写命令均不带 ``K``。通过 ``unit_suffix`` 参数控制，便于实机验证切换。
    """

    out = _normalize_output_channel(output)
    suffix = "K" if unit_suffix else ""
    return f"SET:SETP:{out}:{target_k:.2f}{suffix}"


def write_ramp_and_verify(
    transport: KelvinionTransport,
    output: str,
    rate_k_per_min: float,
    *,
    expects_ack: bool = False,
) -> float:
    """写入 ``[SET:RAMP:CH:VALUE]`` 并立即回读 ``[READ:RAMP:CH]`` 严格确认。"""

    out = _normalize_output_channel(output)
    command = f"SET:RAMP:{out}:{rate_k_per_min:.2f}"
    write_command(transport, command, expects_ack=expects_ack)
    actual = read_ramp_rate(transport, out)
    if not math.isclose(actual, rate_k_per_min, rel_tol=0.0, abs_tol=0.005):
        raise KelvinionProtocolError(
            f"Output {out} ramp readback {actual:.3f} K/min does not match requested {rate_k_per_min:.2f} K/min"
        )
    return actual


def write_setpoint_and_readback(
    transport: KelvinionTransport,
    output: str,
    target_k: float,
    *,
    expects_ack: bool = False,
    unit_suffix: bool = True,
) -> float:
    """写入 ``[SET:SETP:CH:VALUE]`` 并立即回读 ``[READ:SETP:CH]``。

    UNVERIFIED_PROTOCOL (HV-03): 按当前爬坡工作设定值假定，写入最终目标后立即回读到的
    ``[READ:SETP:CH]`` 可能仍处于起始工作设定值或刚起步的中间值，尚未跳变至 ``target_k``；
    因此此处验证通信帧可解析且数值处于合法范围 [0, 999] K，而不强求立即等于 ``target_k``。
    若实机确认 ``[READ:SETP:CH]`` 始终直接返回最终目标值，可将此处收紧为严格相等校验。
    """

    out = _normalize_output_channel(output)
    command = format_setpoint_command(out, target_k, unit_suffix=unit_suffix)
    write_command(transport, command, expects_ack=expects_ack)
    return read_working_setpoint(transport, out)


# ---- Setup / on-demand configuration commands (NO Filter, NO Excitation, NO Reversal) ----


def read_sensor_type(transport: KelvinionTransport, channel: str) -> str:
    """查询输入传感器类型 ``[READ:T:CH]``。"""

    ch = _normalize_input_channel(channel)
    payload = query_command(transport, f"READ:T:{ch}").upper()
    if payload not in SENSOR_TYPES:
        raise KelvinionProtocolError(f"Invalid [READ:T:{ch}] sensor type: {payload!r}")
    return payload


def write_sensor_type_and_verify(
    transport: KelvinionTransport,
    channel: str,
    sensor_type: str,
    *,
    expects_ack: bool = False,
) -> str:
    """写入输入传感器类型 ``[SET:T:CH:VALUE]`` 并回读确认。"""

    ch = _normalize_input_channel(channel)
    requested = str(sensor_type).strip().upper()
    if requested not in SENSOR_TYPES:
        raise ValueError(f"Unsupported Kelvinion sensor type: {sensor_type!r}")
    write_command(transport, f"SET:T:{ch}:{requested}", expects_ack=expects_ack)
    actual = read_sensor_type(transport, ch)
    if actual != requested:
        raise KelvinionProtocolError(
            f"Input {ch} sensor type readback {actual!r} does not match requested {requested!r}"
        )
    return actual


def read_curve_number(transport: KelvinionTransport, channel: str) -> int:
    """查询输入通道校准曲线编号 ``[READ:W:CH]``（0–99）。"""

    ch = _normalize_input_channel(channel)
    payload = query_command(transport, f"READ:W:{ch}")
    try:
        curve = int(payload)
    except ValueError as exc:
        raise KelvinionProtocolError(
            f"Invalid [READ:W:{ch}] curve number: {payload!r}"
        ) from exc
    if not MIN_CURVE_NUMBER <= curve <= MAX_CURVE_NUMBER:
        raise KelvinionProtocolError(
            f"Input {ch} curve number {curve} is outside [{MIN_CURVE_NUMBER}, {MAX_CURVE_NUMBER}]"
        )
    return curve


def write_curve_number_and_verify(
    transport: KelvinionTransport,
    channel: str,
    curve_number: int,
    *,
    expects_ack: bool = False,
) -> int:
    """写入输入通道校准曲线编号 ``[SET:W:CH:VALUE]`` 并回读确认。"""

    ch = _normalize_input_channel(channel)
    if (
        isinstance(curve_number, bool)
        or not isinstance(curve_number, int)
        or not MIN_CURVE_NUMBER <= curve_number <= MAX_CURVE_NUMBER
    ):
        raise ValueError(
            f"Curve number must be an integer between {MIN_CURVE_NUMBER} and {MAX_CURVE_NUMBER}"
        )
    write_command(transport, f"SET:W:{ch}:{curve_number}", expects_ack=expects_ack)
    actual = read_curve_number(transport, ch)
    if actual != curve_number:
        raise KelvinionProtocolError(
            f"Input {ch} curve readback {actual} does not match requested {curve_number}"
        )
    return actual


def write_loop_binding_and_verify(
    transport: KelvinionTransport,
    output: str,
    input_channel: str,
    *,
    expects_ack: bool = False,
) -> str:
    """写入输出绑定输入通道 ``[SET:LOOP:CH:VALUE]``（A–H 或 OFF）并回读确认。

    UNVERIFIED_PROTOCOL (HV-06): 手册 p.97 正文误写 ``A-B``，p.39/p.96 确认支持 ``A-H`` 与 ``OFF``。
    """

    out = _normalize_output_channel(output)
    requested = str(input_channel).strip().upper()
    if requested == "NULL":
        requested = "OFF"
    if requested != "OFF" and requested not in INPUT_CHANNELS_STANDARD:
        raise ValueError(f"Unsupported loop input channel: {input_channel!r}")
    write_command(transport, f"SET:LOOP:{out}:{requested}", expects_ack=expects_ack)
    actual = read_loop_binding(transport, out)
    if actual != requested:
        raise KelvinionProtocolError(
            f"Output {out} loop readback {actual!r} does not match requested {requested!r}"
        )
    return actual


def write_heater_range_and_verify(
    transport: KelvinionTransport,
    output: str,
    heater_range: str,
    *,
    expects_ack: bool = False,
) -> str:
    """写入加热量程 ``[SET:RANGE:CH:OFF|LOW|MED|HIGH]`` 并回读确认。"""

    out = _normalize_output_channel(output)
    requested = str(heater_range).strip().upper()
    if requested not in HEATER_RANGES:
        raise ValueError(f"Unsupported heater range: {heater_range!r}")
    write_command(transport, f"SET:RANGE:{out}:{requested}", expects_ack=expects_ack)
    actual = read_heater_range(transport, out)
    if actual != requested:
        raise KelvinionProtocolError(
            f"Output {out} heater range readback {actual!r} does not match requested {requested!r}"
        )
    return actual


def write_pid_limit_and_verify(
    transport: KelvinionTransport,
    output: str,
    limit_k: float,
    *,
    expects_ack: bool = False,
) -> float:
    """写入硬件 PID 温度上限 ``[SET:LIMIT:CH:VALUE]`` 并回读确认。"""

    out = _normalize_output_channel(output)
    if not math.isfinite(limit_k) or not (0.0 <= limit_k <= HARDWARE_MAX_SETPOINT_K):
        raise ValueError(f"PID limit must be within [0, {HARDWARE_MAX_SETPOINT_K:g}] K")
    write_command(transport, f"SET:LIMIT:{out}:{limit_k:.2f}", expects_ack=expects_ack)
    actual = read_pid_limit_k(transport, out)
    if not math.isclose(actual, limit_k, rel_tol=0.0, abs_tol=0.015):
        raise KelvinionProtocolError(
            f"Output {out} PID limit readback {actual:.3f} K does not match requested {limit_k:.2f} K"
        )
    return actual


def write_zone_enabled_and_verify(
    transport: KelvinionTransport,
    output: str,
    enabled: bool,
    *,
    expects_ack: bool = False,
) -> str:
    """切换硬件 Zone 开关 ``[SET:ZONE:CH:ON|OFF]`` 并回读 ``[READ:ZONE:STATE:CH]`` 确认。"""

    out = _normalize_output_channel(output)
    token = "ON" if enabled else "OFF"
    write_command(transport, f"SET:ZONE:{out}:{token}", expects_ack=expects_ack)
    actual = read_zone_state(transport, out)
    is_on = actual != "OFF"
    if is_on != bool(enabled):
        raise KelvinionProtocolError(
            f"Output {out} Zone state readback {actual!r} does not match requested {token}"
        )
    return actual


def read_zone_entry(
    transport: KelvinionTransport,
    output: str,
    zone_name: str,
) -> ZoneEntry:
    """查询单条硬件 Zone 参数 ``[READ:ZONE:ZONEk:CH]`` -> ``[P,I,D,Range,Boundary]``。

    UNVERIFIED_PROTOCOL (HV-07): 手册 p.95 示例 ``[3.00,2.00,1.00,0,300.00]``，其中 Range 为
    整数 0–3（0=OFF, 1=LOW, 2=MED, 3=HIGH）；此处同时兼容文字枚举。
    """

    out = _normalize_output_channel(output)
    zone = _normalize_zone_name(zone_name)
    payload = query_command(transport, f"READ:ZONE:{zone}:{out}")
    parts = [item.strip() for item in payload.split(",")]
    if len(parts) != 5:
        raise KelvinionProtocolError(
            f"Invalid [READ:ZONE:{zone}:{out}] response (expected 5 fields): {payload!r}"
        )
    kp = parse_number(parts[0], label=f"{zone} KP")
    ki = parse_number(parts[1], label=f"{zone} KI")
    kd = parse_number(parts[2], label=f"{zone} KD")
    heater_range = ZONE_RANGE_FROM_CODE.get(parts[3].upper())
    if heater_range is None:
        raise KelvinionProtocolError(
            f"Invalid [READ:ZONE:{zone}:{out}] range token: {parts[3]!r}"
        )
    boundary = parse_number(parts[4], label=f"{zone} BOUNDARY", allow_units=("K",))
    return ZoneEntry(
        name=zone,
        kp=kp,
        ki=ki,
        kd=kd,
        heater_range=heater_range,
        boundary_k=boundary,
    )


def write_zone_field_and_verify(
    transport: KelvinionTransport,
    output: str,
    zone_name: str,
    field: str,
    value: float | str,
    *,
    expects_ack: bool = False,
) -> ZoneEntry:
    """写入硬件 Zone 单个字段 ``[SET:ZONE:CH:ZONEk:FIELD:VALUE]`` 并回读整行确认。

    UNVERIFIED_PROTOCOL (HV-07):
    1. 手册 p.95 标题漏印 ``CH``，正文示例包含 ``CH``：``[SET:ZONE:A:ZONE1:BOUNDARY:300]``。
    2. ``RANGE`` 写入按手册文字枚举 ``OFF/LOW/MED/HIGH`` 发送（实机需核对是否接受文字或 0–3）。
    """

    out = _normalize_output_channel(output)
    zone = _normalize_zone_name(zone_name)
    key = str(field).strip().upper()
    if key == "RANGE":
        rendered = str(value).strip().upper()
        if rendered not in HEATER_RANGES:
            raise ValueError(f"Unsupported zone range: {value!r}")
    elif key == "KP":
        num = float(value)
        if not math.isfinite(num) or not 0.0 <= num <= 1000.0:
            raise ValueError("Zone KP must be within [0, 1000]")
        rendered = f"{num:.2f}"
    elif key == "KI":
        num = float(value)
        if not math.isfinite(num) or not 0.0 <= num <= 1000.0:
            raise ValueError("Zone KI must be within [0, 1000]")
        rendered = f"{num:.2f}"
    elif key == "KD":
        num = float(value)
        if not math.isfinite(num) or not 0.0 <= num <= 200.0:
            raise ValueError("Zone KD must be within [0, 200]")
        rendered = f"{num:.1f}"
    elif key == "BOUNDARY":
        num = float(value)
        if not math.isfinite(num) or not 0.0 <= num <= HARDWARE_MAX_SETPOINT_K:
            raise ValueError(f"Zone BOUNDARY must be within [0, {HARDWARE_MAX_SETPOINT_K:g}] K")
        rendered = f"{num:.2f}"
    else:
        raise ValueError(f"Unsupported zone field: {field!r}")

    write_command(
        transport,
        f"SET:ZONE:{out}:{zone}:{key}:{rendered}",
        expects_ack=expects_ack,
    )
    entry = read_zone_entry(transport, out, zone)
    if key == "RANGE":
        if entry.heater_range != rendered:
            raise KelvinionProtocolError(
                f"Output {out} {zone} RANGE readback {entry.heater_range!r} != {rendered!r}"
            )
    else:
        actual_num = {
            "KP": entry.kp,
            "KI": entry.ki,
            "KD": entry.kd,
            "BOUNDARY": entry.boundary_k,
        }[key]
        tol = 0.15 if key == "KD" else 0.015
        if not math.isclose(actual_num, float(value), rel_tol=0.0, abs_tol=tol):
            raise KelvinionProtocolError(
                f"Output {out} {zone} {key} readback {actual_num:g} != {float(value):g}"
            )
    return entry


def save_configuration(
    transport: KelvinionTransport,
    *,
    expects_ack: bool = False,
) -> None:
    """发送 ``[SET:SAVE]`` 将当前仪表参数保存到非易失存储器。

    UNVERIFIED_PROTOCOL (HV-08): 需实机确认 ``[SET:SAVE]`` 是否耗时超过 1 s 或返回确认帧。
    """

    write_command(transport, "SET:SAVE", expects_ack=expects_ack)


def _normalize_input_channel(channel: str) -> str:
    ch = str(channel).strip().upper()
    if ch not in INPUT_CHANNELS_STANDARD:
        raise ValueError(f"Invalid Kelvinion input channel: {channel!r}")
    return ch


def _normalize_output_channel(output: str) -> str:
    out = str(output).strip().upper()
    if out not in OUTPUT_CHANNELS_STANDARD:
        raise ValueError(f"Invalid Kelvinion output channel: {output!r}")
    return out


def _normalize_zone_name(zone_name: str) -> str:
    zone = str(zone_name).strip().upper()
    if zone not in ZONE_NAMES:
        raise ValueError(f"Invalid Kelvinion zone name: {zone_name!r}")
    return zone
