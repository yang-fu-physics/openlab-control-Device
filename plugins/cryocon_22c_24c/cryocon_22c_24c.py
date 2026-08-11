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

    def __init__(self, address: str, timeout_seconds: float) -> None:
        import pyvisa

        timeout_ms = max(1, int(timeout_seconds * 1000.0))
        manager = pyvisa.ResourceManager()
        try:
            resource = manager.open_resource(
                address,
                open_timeout=timeout_ms,
            )
            resource.timeout = timeout_ms
            resource.read_termination = "\n"
            resource.write_termination = "\n"
        except Exception:
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
class ControllerReadback:
    """一次复合查询得到的完整控制和监视状态。"""

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


STATUS_QUERY = (
    "INPUT A:TEMPERATURE?;UNITS?;ALARM?;"
    ":INPUT B:TEMPERATURE?;UNITS?;ALARM?;"
    ":LOOP 1:SETPT?;RATE?;RAMP?;RANGE?;HTRREAD?;ERROR?;TYPE?;SOURCE?;"
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


def read_status(transport: QueryTransport) -> ControllerReadback:
    """用一条复合查询读取 A、B 和 Loop 1，确保各值来自同一轮采样。"""

    response = transport.query(STATUS_QUERY)
    parts = [part.strip() for part in response.split(";")]
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
        setpoint=_number(parts[6], "loop setpoint"),
        rate=_number(parts[7], "loop ramp rate"),
        ramping=parts[8],
        heater_range=parts[9],
        heater_output_percent=_number(parts[10], "heater output"),
        loop_error=parts[11],
        loop_type=parts[12],
        loop_source=parts[13],
        control=parts[14],
    )


def read_temperature_b(
    transport: QueryTransport,
) -> tuple[float, str, str]:
    """Hold 前即时读取 B；不依赖前面板缓存，也不要求 A 当前正常。"""

    response = transport.query("INPUT B:TEMPERATURE?;UNITS?;ALARM?")
    parts = [part.strip() for part in response.split(";")]
    if len(parts) != 3:
        raise CryoconProtocolError(
            f"Input B query returned {len(parts)} fields instead of 3: {response!r}"
        )
    return _number(parts[0], "input B temperature"), parts[1], parts[2]


def write_number_and_readback(
    transport: QueryTransport,
    parameter: str,
    value: float,
) -> float:
    """写入浮点参数并立即查询同一参数，返回仪表实际值。"""

    rendered = f"{float(value):.12g}"
    response = transport.query(
        f"LOOP 1:{parameter} {rendered};{parameter}?"
    )
    actual = _number(response, f"{parameter} readback")
    tolerance = max(1e-9, abs(float(value)) * 1e-7)
    if not math.isclose(actual, float(value), rel_tol=1e-7, abs_tol=tolerance):
        raise CryoconProtocolError(
            f"{parameter} readback {actual!r} does not match requested {value!r}"
        )
    return actual


def write_enum_and_readback(
    transport: QueryTransport,
    parameter: str,
    value: str,
) -> str:
    """写入枚举参数并立即查询，大小写无关地验证回读。"""

    requested = str(value).strip().upper()
    response = transport.query(
        f"LOOP 1:{parameter} {requested};{parameter}?"
    ).strip()
    if response.upper() != requested:
        raise CryoconProtocolError(
            f"{parameter} readback {response!r} does not match requested {requested!r}"
        )
    return response


def _number(value: Any, label: str) -> float:
    """严格解析有限数值，同时容许手册响应中的 K 和 % 后缀。"""

    text = str(value).strip()
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
