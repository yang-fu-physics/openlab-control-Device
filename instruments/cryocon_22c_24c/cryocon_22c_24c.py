"""Cryo-con 22C/24C 的 VISA 会话和 SCPI 指令。

本文件只处理底层通讯、命令拼接和响应拆分，不包含 OpenLab Control 生命周期。
所有修改命令都使用“写入；查询回读”的复合命令，避免未读响应留在队列中影响下一次
读取，也使上层能确认仪表实际接受的值。这里故意不提供裸 ``write`` 接口。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Protocol


class CryoconProtocolError(RuntimeError):
    """仪表响应缺失、格式错误或写入回读不一致。"""


class QueryTransport(Protocol):
    """驱动所需的最小通讯接口，便于用伪仪表做协议测试。"""

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


class PyVisaTransport:
    """具有有限 I/O 超时并使用 LF 终止符的 PyVISA 会话。"""

    def __init__(self, address: str, timeout_seconds: float, *, baud_rate: int = 9600) -> None:
        import pyvisa

        timeout_ms = max(1, int(timeout_seconds * 1000.0))
        manager = pyvisa.ResourceManager()
        resource = None
        try:
            resource = manager.open_resource(
                address,
                open_timeout=timeout_ms,
            )
            resource.timeout = timeout_ms
            resource.read_termination = "\n"
            resource.write_termination = "\n"
            if address.strip().upper().startswith("ASRL"):
                resource.baud_rate = baud_rate
                resource.data_bits = 8
                resource.parity = pyvisa.constants.Parity.none
                resource.stop_bits = pyvisa.constants.StopBits.one
        except Exception:
            try:
                if resource is not None:
                    resource.close()
            finally:
                manager.close()
            raise
        self._manager = manager
        self._resource = resource

    def query(self, command: str) -> str:
        return str(self._resource.query(command)).strip()

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
class LoopReadback:
    """单个控制回路（Loop 1 或 Loop 2）的即时状态。"""

    loop: int
    setpoint: float
    rate: float
    ramping: str
    heater_range: str
    heater_output_percent: float
    loop_error: str
    loop_type: str
    loop_source: str


@dataclass(frozen=True, slots=True)
class ControllerReadback:
    """单回路复合查询得到的完整控制和监视状态（默认兼容 22C Loop 1）。"""

    temperature_a: float
    unit_a: str
    alarm_a: str
    temperature_b: float
    unit_b: str
    alarm_b: str
    setpoint: float
    rate: float
    ramping: str
    heater_range: str
    heater_output_percent: float
    loop_error: str
    loop_type: str
    loop_source: str
    control: str


@dataclass(frozen=True, slots=True)
class DualLoopReadback:
    """24C 双回路联动模式下一次复合查询得到的 A/B 与 Loop 1/2 完整状态。"""

    temperature_a: float
    unit_a: str
    alarm_a: str
    temperature_b: float
    unit_b: str
    alarm_b: str
    loop_1: LoopReadback
    loop_2: LoopReadback
    control: str

    def loop(self, number: int) -> LoopReadback:
        if number == 1:
            return self.loop_1
        if number == 2:
            return self.loop_2
        raise ValueError(f"Unsupported Cryo-con loop number: {number!r}")


STATUS_QUERY = (
    "INPUT? A;:INPUT A:UNITS?;ALARM?;"
    ":INPUT? B;:INPUT B:UNITS?;ALARM?;"
    ":LOOP 1:SETPT?;RATE?;RAMP?;RANGE?;HTRREAD?;ERROR?;TYPE?;SOURCE?;"
    ":CONTROL?"
)

DUAL_LOOP_STATUS_QUERY = (
    "INPUT? A;:INPUT A:UNITS?;ALARM?;"
    ":INPUT? B;:INPUT B:UNITS?;ALARM?;"
    ":LOOP 1:SETPT?;RATE?;RAMP?;RANGE?;HTRREAD?;ERROR?;TYPE?;SOURCE?;"
    ":LOOP 2:SETPT?;RATE?;RAMP?;RANGE?;HTRREAD?;ERROR?;TYPE?;SOURCE?;"
    ":CONTROL?"
)

TEMPERATURES_AB_QUERY = (
    "INPUT? A;:INPUT A:UNITS?;ALARM?;"
    ":INPUT? B;:INPUT B:UNITS?;ALARM?"
)

MEASUREMENT_TEMPERATURE_QUERY = "INPUT? B"


def status_query_for_loop(loop: int = 1) -> str:
    """构造单回路状态查询；Loop 1 保持与历史 ``STATUS_QUERY`` 逐字一致。"""

    _validate_loop_number(loop)
    if loop == 1:
        return STATUS_QUERY
    return (
        "INPUT? A;:INPUT A:UNITS?;ALARM?;"
        ":INPUT? B;:INPUT B:UNITS?;ALARM?;"
        f":LOOP {loop}:SETPT?;RATE?;RAMP?;RANGE?;HTRREAD?;ERROR?;TYPE?;SOURCE?;"
        ":CONTROL?"
    )


def identify(transport: QueryTransport) -> tuple[str, str, str, str]:
    """读取并拆分标准身份字符串。"""

    response = transport.query("*IDN?")
    parts = tuple(part.strip() for part in response.split(","))
    if len(parts) < 2:
        raise CryoconProtocolError(f"Invalid *IDN? response: {response!r}")
    padded = (*parts, "", "")[:4]
    return padded[0], padded[1], padded[2], padded[3]


def read_status(transport: QueryTransport, *, loop: int = 1) -> ControllerReadback:
    """用一条复合查询读取 A、B 和指定单回路，确保各值来自同一轮采样。"""

    response = transport.query(status_query_for_loop(loop))
    parts = [part.strip() for part in response.strip().rstrip(";").split(";")]
    if len(parts) != 15:
        raise CryoconProtocolError(
            f"Status query returned {len(parts)} fields instead of 15: {response!r}"
        )
    return ControllerReadback(
        temperature_a=_number(parts[0], "input A temperature"),
        unit_a=parts[1],
        alarm_a=parts[2],
        temperature_b=_number(parts[3], "input B temperature"),
        unit_b=parts[4],
        alarm_b=parts[5],
        setpoint=_number(parts[6], f"loop {loop} setpoint"),
        rate=_number(parts[7], f"loop {loop} ramp rate"),
        ramping=parts[8],
        heater_range=parts[9],
        heater_output_percent=_number(parts[10], f"loop {loop} heater output"),
        loop_error=parts[11],
        loop_type=parts[12],
        loop_source=parts[13],
        control=parts[14],
    )


def read_dual_loop_status(transport: QueryTransport) -> DualLoopReadback:
    """用一条 23 字段复合查询同时读取 A、B、Loop 1、Loop 2 与 CONTROL 状态。"""

    response = transport.query(DUAL_LOOP_STATUS_QUERY)
    parts = [part.strip() for part in response.strip().rstrip(";").split(";")]
    if len(parts) != 23:
        raise CryoconProtocolError(
            f"Dual-loop status query returned {len(parts)} fields instead of 23: {response!r}"
        )
    loop_1 = _parse_loop_fields(1, parts[6:14])
    loop_2 = _parse_loop_fields(2, parts[14:22])
    return DualLoopReadback(
        temperature_a=_number(parts[0], "input A temperature"),
        unit_a=parts[1],
        alarm_a=parts[2],
        temperature_b=_number(parts[3], "input B temperature"),
        unit_b=parts[4],
        alarm_b=parts[5],
        loop_1=loop_1,
        loop_2=loop_2,
        control=parts[22],
    )


def read_temperature(
    transport: QueryTransport, channel: str,
) -> tuple[float, str, str]:
    """Hold 前即时读取所选输入，不依赖前面板缓存。"""

    response = transport.query(f"INPUT? {channel};:INPUT {channel}:UNITS?;ALARM?")
    parts = [part.strip() for part in response.strip().rstrip(";").split(";")]
    if len(parts) != 3:
        raise CryoconProtocolError(
            f"Input {channel} query returned {len(parts)} fields instead of 3: {response!r}"
        )
    return _number(parts[0], f"input {channel} temperature"), parts[1], parts[2]


def read_temperatures_ab(
    transport: QueryTransport,
) -> dict[str, tuple[float, str, str]]:
    """双回路 Hold 前用一条复合查询即时读取 A、B 两个输入通道的温度、单位与报警。"""

    response = transport.query(TEMPERATURES_AB_QUERY)
    parts = [part.strip() for part in response.strip().rstrip(";").split(";")]
    if len(parts) != 6:
        raise CryoconProtocolError(
            f"Input A/B query returned {len(parts)} fields instead of 6: {response!r}"
        )
    return {
        "A": (_number(parts[0], "input A temperature"), parts[1], parts[2]),
        "B": (_number(parts[3], "input B temperature"), parts[4], parts[5]),
    }


def read_measurement_temperature(transport: QueryTransport, channel: str) -> float:
    """只读取写实验数据所需的主温度，不附带状态或其他通道查询。"""

    return _number(
        transport.query(f"INPUT? {channel}"),
        f"input {channel} measurement temperature",
    )


def write_number_and_readback(
    transport: QueryTransport,
    parameter: str,
    value: float,
    *,
    loop: int = 1,
) -> float:
    """写入浮点参数并立即查询同一参数，返回仪表实际值。"""

    _validate_loop_number(loop)
    rendered = f"{float(value):.12g}"
    response = transport.query(
        f"LOOP {loop}:{parameter} {rendered};{parameter}?"
    )
    actual = _number(response, f"LOOP {loop} {parameter} readback")
    tolerance = max(1e-9, abs(float(value)) * 1e-7)
    if not math.isclose(actual, float(value), rel_tol=1e-7, abs_tol=tolerance):
        raise CryoconProtocolError(
            f"LOOP {loop} {parameter} readback {actual!r} does not match requested {value!r}"
        )
    return actual


def write_enum_and_readback(
    transport: QueryTransport,
    parameter: str,
    value: str,
    *,
    loop: int = 1,
) -> str:
    """写入枚举参数并立即查询，大小写无关地验证回读。"""

    _validate_loop_number(loop)
    requested = str(value).strip().upper()
    response = transport.query(
        f"LOOP {loop}:{parameter} {requested};{parameter}?"
    ).strip().rstrip(";").strip()
    normalized_response = _normalize_range_token(response) if parameter.upper() == "RANGE" else response.upper()
    normalized_requested = _normalize_range_token(requested) if parameter.upper() == "RANGE" else requested
    if normalized_response != normalized_requested:
        raise CryoconProtocolError(
            f"LOOP {loop} {parameter} readback {response!r} does not match requested {requested!r}"
        )
    return response


def _optional_index(value: Any, label: str) -> int | None:
    text = str(value).strip().rstrip(";").strip()
    if not text or text.upper() in {"NONE", "--", "OFF", "N/A"}:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise CryoconProtocolError(f"Invalid {label}: {value!r}") from exc
    if parsed < 0:
        return None
    return parsed


def read_channel_sensor_indices(
    transport: QueryTransport,
    channel: str,
) -> tuple[int | None, int | None]:
    """查询指定输入通道的出厂传感器索引 (ISENix) 与用户曲线索引 (USENix)。"""

    ch = str(channel).strip().upper()
    response = transport.query(f"INPUT {ch}:ISENIX?;USENIX?")
    parts = [part.strip() for part in response.strip().rstrip(";").split(";")]
    if len(parts) != 2:
        raise CryoconProtocolError(
            f"Input {ch} sensor index query returned {len(parts)} fields instead of 2: {response!r}"
        )
    factory_index = _optional_index(parts[0], f"input {ch} factory sensor index")
    user_index = _optional_index(parts[1], f"input {ch} user curve index")
    return factory_index, user_index


def write_channel_sensor_index_and_readback(
    transport: QueryTransport,
    channel: str,
    kind: str,
    index: int,
) -> int:
    """为指定输入通道写入出厂曲线 (ISENix) 或用户曲线 (USENix) 并立即回读确认。"""

    ch = str(channel).strip().upper()
    command_key = {"factory": "ISENIX", "user": "USENIX"}.get(kind)
    if command_key is None:
        raise ValueError(f"Unsupported sensor curve kind: {kind!r}")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError(f"Sensor index must be a non-negative integer: {index!r}")
    response = transport.query(
        f"INPUT {ch}:{command_key} {index};{command_key}?"
    ).strip().rstrip(";").strip()
    actual = _optional_index(response, f"input {ch} {command_key} readback")
    if actual != index:
        raise CryoconProtocolError(
            f"Input {ch} {command_key} readback {response!r} does not match requested {index}"
        )
    return actual


def _parse_loop_fields(loop: int, fields: list[str]) -> LoopReadback:
    return LoopReadback(
        loop=loop,
        setpoint=_number(fields[0], f"loop {loop} setpoint"),
        rate=_number(fields[1], f"loop {loop} ramp rate"),
        ramping=fields[2],
        heater_range=fields[3],
        heater_output_percent=_number(fields[4], f"loop {loop} heater output"),
        loop_error=fields[5],
        loop_type=fields[6],
        loop_source=fields[7],
    )


def _validate_loop_number(loop: int) -> None:
    if isinstance(loop, bool) or loop not in (1, 2):
        raise ValueError(f"Unsupported Cryo-con loop number: {loop!r}")


def _normalize_range_token(token: str) -> str:
    upper = str(token).strip().upper()
    return "HI" if upper == "HIGH" else upper


def _number(value: Any, label: str) -> float:
    """严格解析有限数值，同时容许手册响应中的 K 和 % 后缀。"""

    text = str(value).strip().rstrip(";").strip()
    if text.endswith(("K", "k", "%")):
        text = text[:-1].strip()
    try:
        result = float(text)
    except (TypeError, ValueError) as exc:
        raise CryoconProtocolError(
            f"Invalid {label}: {value!r}"
        ) from exc
    if not math.isfinite(result):
        raise CryoconProtocolError(f"Non-finite {label}: {value!r}")
    return result
