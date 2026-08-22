"""OpenLab Control System Instrument：Cryo-con 22C/24C 温度控制器。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Callable, Mapping
from typing import Any

from labcontrol.instruments.base import InstrumentError, SystemInstrument, SafetyViolation

from . import cryocon_22c_24c as instrument


@dataclass(frozen=True, slots=True)
class PIDZone:
    """PID 表的一行；``temperature`` 是选择这一行的严格上边界。"""

    temperature: float
    p_gain: float
    i_gain: float
    d_gain: float
    heater_range: str | None = None


class Cryocon22C24CController(SystemInstrument):
    """以 B 为主温度、A 为只读二级冷头温度的单连接控制器。"""

    def __init__(
        self,
        config,
        *,
        transport_factory: Callable[
            [str, float], instrument.QueryTransport
        ] = instrument.PyVisaTransport,
    ) -> None:
        super().__init__(config)
        self._transport_factory = transport_factory
        self._transport: instrument.QueryTransport | None = None
        self._status_validated = False
        self._address = config.address
        self._auxiliary_readings = frozenset(config.auxiliary_readings)
        self._io_timeout = self._positive_number(
            config.extras.get("io_timeout_seconds", 2.0),
            "io_timeout_seconds",
        )
        self._monitor_max = self._finite_number(
            config.extras.get("monitor_a_max_temperature", 320.0),
            "monitor_a_max_temperature",
        )
        if not self._address.upper().startswith("USB"):
            raise ValueError(
                "cryocon_22c_24c.address must be a USB VISA resource"
            )
        if self._monitor_max <= 0:
            raise ValueError("monitor_a_max_temperature must be positive")
        operation_timeout = self._positive_number(
            config.operation_timeout_seconds,
            "operation_timeout_seconds",
        )
        minimum_operation_timeout = self._io_timeout * 7.0 + 0.5
        if operation_timeout <= minimum_operation_timeout:
            raise ValueError(
                "operation_timeout_seconds must exceed seven VISA I/O timeouts "
                "plus 0.5 s so Hold can finish its fresh read and confirmed writes"
            )
        self._pid_zones = self._parse_pid_zones(
            config.extras.get("pid_zones"),
            float(config.max_value),
        )

    def open(self) -> None:
        """建立 VISA 会话并只读验证型号、传感器和 Loop 1 状态。"""

        if self._transport is not None:
            return
        transport: instrument.QueryTransport | None = None
        try:
            transport = self._transport_factory(
                self._address,
                self._io_timeout,
            )
            manufacturer, model, _serial, _firmware = instrument.identify(
                transport
            )
            maker = manufacturer.casefold().replace("-", "").replace(" ", "")
            if "cryocon" not in maker or model.strip().upper() not in {
                "22C",
                "24C",
            }:
                raise SafetyViolation(
                    f"Unexpected instrument identity: {manufacturer}, {model}",
                    "CRYOCON_IDENTITY_MISMATCH",
                    self._address,
                )
            self._transport = transport
            # 首次状态检查也不得修改已经手动完成的仪表设置。
            self.read_status()
        except Exception:
            self._transport = None
            if transport is not None:
                transport.close()
            raise

    def close(self) -> None:
        """只关闭通讯句柄；不关闭输出、不更改设定值或 Loop 类型。"""

        transport, self._transport = self._transport, None
        self._status_validated = False
        if transport is not None:
            transport.close()

    def read_status(self) -> dict[str, object]:
        """即时读取 B 主温度，以及 A、加热输出和量程附加读数。"""

        transport = self._require_transport()
        try:
            status = instrument.read_status(transport)
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con communication failed: {exc}",
                "CRYOCON_COMMUNICATION_FAILED",
                self._address,
            ) from exc
        self._validate_status(status)
        ramping = status.ramping.strip().upper() == "ON"
        auxiliary = {
            "temp_a": status.temperature_a,
            "heater_output": status.heater_output_percent,
            "heater_range": status.heater_range.strip().upper(),
        }
        self._status_validated = True
        return {
            "value": status.temperature_b,
            "target": status.setpoint,
            "rate": status.rate,
            "moving": ramping,
            "ready": not ramping,
            "auxiliary": {
                key: value
                for key, value in auxiliary.items()
                if key in self._auxiliary_readings
            },
        }

    def read_measurement(self) -> dict[str, object]:
        """测量行只向仪表查询 TempB，其余状态留给常规安全轮询。"""

        transport = self._require_transport()
        if not self._status_validated:
            raise InstrumentError(
                "Cryo-con has no validated full status snapshot",
                "CRYOCON_STATUS_UNAVAILABLE",
                self._address,
            )
        try:
            temperature_b = instrument.read_measurement_temperature_b(
                transport
            )
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con measurement read failed: {exc}",
                "CRYOCON_COMMUNICATION_FAILED",
                self._address,
            ) from exc
        return {"value": temperature_b}

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
    ) -> None:
        """先按目标选择并确认 PID，再确认速率，最后写入并确认目标。"""

        del mode
        target = self._bounded_target(value)
        rate = self._bounded_rate(rate_per_minute)
        self._apply_pid_and_target(target, rate)

    def hold(self) -> None:
        """以即时 B 读数作为目标；保持输出开启和 Ramp-P 控制。"""

        transport = self._require_transport()
        try:
            current, unit, alarm = instrument.read_temperature_b(transport)
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con communication failed during hold: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                self._address,
            ) from exc
        self._validate_channel("B", current, unit, alarm)
        target = self._bounded_target(current)
        # Hold 仍使用正速率；设定点等于当前值后仪表会停止继续爬升。
        rate = self._bounded_rate(
            float(self.config.extras.get("hold_rate_per_minute", 1.0))
        )
        self._apply_pid_and_target(target, rate)

    def _apply_pid_and_target(self, target: float, rate: float) -> None:
        transport = self._require_transport()
        zone = next(
            (
                zone
                for zone in self._pid_zones
                if target < zone.temperature
            ),
            self._pid_zones[-1],
        )
        operations: list[tuple[str, float | str]] = [
            ("PGAIN", zone.p_gain),
            ("IGAIN", zone.i_gain),
            ("DGAIN", zone.d_gain),
        ]
        if zone.heater_range is not None:
            operations.append(("RANGE", zone.heater_range))
        operations.extend((("RATE", rate), ("SETPT", target)))
        try:
            for parameter, requested in operations:
                if isinstance(requested, str):
                    instrument.write_enum_and_readback(
                        transport,
                        parameter,
                        requested,
                    )
                else:
                    instrument.write_number_and_readback(
                        transport,
                        parameter,
                        requested,
                    )
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con write or readback confirmation failed: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                f"{self._address}:{parameter}",
            ) from exc

    def _validate_status(
        self,
        status: instrument.ControllerReadback,
    ) -> None:
        self._validate_channel(
            "A",
            status.temperature_a,
            status.unit_a,
            status.alarm_a,
        )
        self._validate_channel(
            "B",
            status.temperature_b,
            status.unit_b,
            status.alarm_b,
        )
        if status.temperature_a > self._monitor_max:
            raise SafetyViolation(
                f"Input A is {status.temperature_a:g} K, above the "
                f"software limit {self._monitor_max:g} K",
                "CRYOCON_INPUT_A_HIGH_LIMIT",
                "A",
            )
        if status.loop_source.strip().upper() not in {"B", "CHB"}:
            self._configuration_fault(
                f"Loop 1 source is {status.loop_source!r}, expected input B"
            )
        if status.loop_type.strip().upper() != "RAMPP":
            self._configuration_fault(
                f"Loop 1 type is {status.loop_type!r}, expected RAMPP"
            )
        if status.control.strip().upper() != "ON":
            self._configuration_fault("Controller reports CONTROL OFF")
        if status.ramping.strip().upper() not in {"ON", "OFF"}:
            self._configuration_fault(
                f"Invalid Loop 1 ramp state {status.ramping!r}"
            )
        heater_range = status.heater_range.strip().upper()
        if heater_range not in {"LOW", "MID", "HI"}:
            self._configuration_fault(
                f"Invalid heater range {status.heater_range!r}"
            )
        if not 0.0 <= status.heater_output_percent <= 100.0:
            self._configuration_fault(
                f"Invalid heater output {status.heater_output_percent!r}%"
            )
        normalized_error = "".join(status.loop_error.casefold().split())
        if "htrok" not in normalized_error:
            raise SafetyViolation(
                f"Loop 1 reports {status.loop_error!r}",
                "CRYOCON_HEATER_FAULT",
                "loop1",
            )
        self._bounded_target(status.setpoint)
        self._bounded_rate(status.rate)

    def _validate_channel(
        self,
        channel: str,
        temperature: float,
        unit: str,
        alarm: str,
    ) -> None:
        if unit.strip().upper() != "K":
            self._configuration_fault(
                f"Input {channel} unit is {unit!r}, expected K"
            )
        if not math.isfinite(temperature):
            raise SafetyViolation(
                f"Input {channel} returned a non-finite temperature",
                "CRYOCON_SENSOR_FAULT",
                channel,
            )
        if alarm.strip() != "--":
            raise SafetyViolation(
                f"Input {channel} alarm is {alarm!r}",
                "CRYOCON_SENSOR_ALARM",
                channel,
            )

    def _configuration_fault(self, message: str) -> None:
        raise SafetyViolation(
            message,
            "CRYOCON_CONFIGURATION_FAULT",
            self._address,
        )

    def _bounded_target(self, value: Any) -> float:
        target = self._finite_number(value, "target")
        if not float(self.config.min_value) <= target <= float(
            self.config.max_value
        ):
            raise SafetyViolation(
                f"Target {target:g} K is outside the configured "
                f"{self.config.min_value:g}–{self.config.max_value:g} K range",
                "CRYOCON_TARGET_LIMIT",
                self.config.id,
            )
        return target

    def _bounded_rate(self, value: Any) -> float:
        rate = self._positive_number(value, "rate")
        if rate > float(self.config.max_rate_per_minute):
            raise SafetyViolation(
                f"Rate {rate:g} K/min exceeds the configured "
                f"{self.config.max_rate_per_minute:g} K/min limit",
                "CRYOCON_RATE_LIMIT",
                self.config.id,
            )
        return rate

    def _require_transport(self) -> instrument.QueryTransport:
        if self._transport is None:
            raise InstrumentError(
                "Cryo-con is not connected",
                "CRYOCON_NOT_CONNECTED",
                self._address,
            )
        return self._transport

    @classmethod
    def _parse_pid_zones(
        cls,
        raw_zones: Any,
        configured_maximum: float,
    ) -> tuple[PIDZone, ...]:
        if not isinstance(raw_zones, list) or not raw_zones:
            raise ValueError(
                "cryocon_22c_24c requires a non-empty pid_zones list; "
                "copy the PID values already validated for this cryostat"
            )
        zones: list[PIDZone] = []
        previous_maximum = float("-inf")
        for index, raw_zone in enumerate(raw_zones, start=1):
            if not isinstance(raw_zone, Mapping):
                raise ValueError(f"pid_zones[{index}] must be a table")
            maximum = cls._finite_number(
                raw_zone.get("temperature"),
                f"pid_zones[{index}].temperature",
            )
            p_gain = cls._finite_number(
                raw_zone.get("p_gain"),
                f"pid_zones[{index}].p_gain",
            )
            i_gain = cls._finite_number(
                raw_zone.get("i_gain"),
                f"pid_zones[{index}].i_gain",
            )
            d_gain = cls._finite_number(
                raw_zone.get("d_gain"),
                f"pid_zones[{index}].d_gain",
            )
            if maximum <= previous_maximum:
                raise ValueError("pid_zones temperature values must increase")
            if min(p_gain, i_gain, d_gain) < 0.0 or max(
                p_gain,
                i_gain,
                d_gain,
            ) > 1000.0:
                raise ValueError("PID gains must be within the instrument 0–1000 range")
            if d_gain > i_gain / 4.0:
                raise ValueError(
                    f"pid_zones[{index}].d_gain must not exceed i_gain / 4"
                )
            raw_range = raw_zone.get("heater_range")
            heater_range = (
                None
                if raw_range is None or not str(raw_range).strip()
                else str(raw_range).strip().upper()
            )
            if heater_range not in {None, "LOW", "MID", "HI"}:
                raise ValueError(
                    f"pid_zones[{index}].heater_range must be LOW, MID, or HI"
                )
            zones.append(
                PIDZone(
                    maximum,
                    p_gain,
                    i_gain,
                    d_gain,
                    heater_range,
                )
            )
            previous_maximum = maximum
        if zones[-1].temperature < configured_maximum:
            raise ValueError(
                "The final PID temperature must cover the configured maximum temperature"
            )
        return tuple(zones)

    @staticmethod
    def _finite_number(value: Any, label: str) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a finite number") from exc
        if not math.isfinite(result):
            raise ValueError(f"{label} must be a finite number")
        return result

    @classmethod
    def _positive_number(cls, value: Any, label: str) -> float:
        result = cls._finite_number(value, label)
        if result <= 0.0:
            raise ValueError(f"{label} must be positive")
        return result
