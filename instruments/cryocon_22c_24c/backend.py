"""OpenLab Control System Instrument：Cryo-con 22C/24C 温度控制器。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Callable, Mapping
from pathlib import Path
import tomllib
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
    """Loop 1 控制所选主温度，A/B 共享同一个通信会话。"""

    def __init__(
        self,
        config,
        *,
        transport_factory: Callable[..., instrument.QueryTransport] = instrument.PyVisaTransport,
    ) -> None:
        super().__init__(config)
        self._transport_factory = transport_factory
        self._transport: instrument.QueryTransport | None = None
        self._status_validated = False
        self._model = "24C"
        self._address = config.address
        primary_input = {"temp_a": "A", "temp_b": "B"}.get(config.main_reading)
        if primary_input is None:
            raise ValueError(
                f"Unsupported Cryo-con main_reading {config.main_reading!r}"
            )
        self._primary_input = primary_input
        self._baud_rate = self._parse_baud_rate(config.extras.get("baud_rate", 9600))
        self._auxiliary_readings = frozenset(config.auxiliary_readings)
        self._io_timeout = self._positive_number(
            config.extras["io_timeout_seconds"],
            "io_timeout_seconds",
        )
        self._monitor_max = self._finite_number(
            config.extras["monitor_a_max_temperature"],
            "monitor_a_max_temperature",
        )
        if not str(self._address).strip():
            raise ValueError(
                "cryocon_22c_24c.address must not be empty"
            )
        if self._monitor_max <= 0:
            raise ValueError("monitor_a_max_temperature must be positive")
        self._hold_rate = self._bounded_rate(
            config.extras["hold_rate_per_minute"]
        )
        operation_timeout = self._positive_number(
            config.operation_timeout_seconds,
            "operation_timeout_seconds",
        )
        minimum_operation_timeout = self._io_timeout * 7.0 + 0.5
        if operation_timeout <= minimum_operation_timeout:
            raise InstrumentError(
                f"Instrument I/O timeout setting is too high: "
                f"seven VISA I/O timeouts of {self._io_timeout:g} s plus 0.5 s "
                f"need {minimum_operation_timeout:g} s for Hold; "
                f"must be less than {operation_timeout:g} s. "
                "Reduce the I/O timeout in Instrument Scanner.",
                "INSTRUMENT_IO_TIMEOUT_TOO_HIGH",
                "io_timeout_seconds",
            )
        self._max_value = float(config.max_value)
        self._pid_file = Path(config.extras["pid_file"]).resolve()
        self._pid_directory = self._pid_file.parent
        self._pid_zones = self._load_pid_zones_from_path(
            self._pid_file,
            self._max_value,
        )
        self._staged_pid: tuple[Path, tuple[PIDZone, ...]] | None = None

    def open(self) -> None:
        """建立 VISA 会话并只读验证型号、传感器和 Loop 1 状态。"""

        if self._transport is not None:
            return
        transport: instrument.QueryTransport | None = None
        try:
            transport = self._transport_factory(
                self._address,
                self._io_timeout,
                baud_rate=self._baud_rate,
            )
            manufacturer, model, _serial, _firmware = instrument.identify(
                transport
            )
            maker = manufacturer.casefold().replace("-", "").replace(" ", "")
            normalized_model = model.strip().upper()
            if "cryocon" not in maker or normalized_model not in {
                "22C",
                "24C",
            }:
                raise SafetyViolation(
                    f"Unexpected instrument identity: {manufacturer}, {model}",
                    "CRYOCON_IDENTITY_MISMATCH",
                    self._address,
                )
            self._model = normalized_model
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
        self._staged_pid = None
        if transport is not None:
            transport.close()

    def read_status(self) -> dict[str, object]:
        """读取所选主温度，以及 A/B、加热输出和量程附加读数。"""

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
        ramping = status.ramping.strip().upper() in {"ON", "YES"}
        auxiliary = {
            "temp_a": status.temperature_a,
            "temp_b": status.temperature_b,
            "heater_output": status.heater_output_percent,
            "heater_range": status.heater_range.strip().upper(),
        }
        self._status_validated = True
        return {
            "value": status.temperature_a if self._primary_input == "A" else status.temperature_b,
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
        """测量行只查询所选主温度，其余状态留给常规安全轮询。"""

        transport = self._require_transport()
        if not self._status_validated:
            raise InstrumentError(
                "Cryo-con has no validated full status snapshot",
                "CRYOCON_STATUS_UNAVAILABLE",
                self._address,
            )
        try:
            temperature = instrument.read_measurement_temperature(
                transport, self._primary_input
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
        return {"value": temperature}

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> None:
        """先按目标选择并确认 PID，再确认速率，最后写入并确认目标。"""

        del mode, control
        target = self._bounded_target(value)
        rate = self._bounded_rate(rate_per_minute)
        self._apply_pid_and_target(target, rate)

    def hold(self, *, control: str) -> None:
        """以即时主温度读数作为目标；保持输出开启和 Ramp-P 控制。"""

        del control
        transport = self._require_transport()
        try:
            current, unit, alarm = instrument.read_temperature(transport, self._primary_input)
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
        self._validate_channel(self._primary_input, current, unit, alarm)
        target = self._bounded_target(current)
        # Hold 仍使用正速率；设定点等于当前值后仪表会停止继续爬升。
        rate = self._bounded_rate(self._hold_rate)
        self._apply_pid_and_target(target, rate)

    def execute_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """响应 Instrument Setup 按需查询或切换输入曲线与软件侧 PID 表。"""

        if action == "read_setup":
            return self._read_setup()
        if action == "apply_setup":
            return self._apply_setup(dict(payload or {}))
        return super().execute_action(action, payload)

    def _supported_channels(self) -> tuple[str, ...]:
        return ("A", "B", "C", "D") if self._model == "24C" else ("A", "B")

    def _read_channel_sensors(self) -> dict[str, dict[str, int | None]]:
        transport = self._require_transport()
        sensors: dict[str, dict[str, int | None]] = {}
        try:
            for channel in self._supported_channels():
                factory_index, user_index = instrument.read_channel_sensor_indices(
                    transport,
                    channel,
                )
                sensors[channel] = {
                    "factory_index": factory_index,
                    "user_index": user_index,
                }
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con sensor curve query failed: {exc}",
                "CRYOCON_COMMUNICATION_FAILED",
                self._address,
            ) from exc
        return sensors

    def _list_pid_tables(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        candidates: list[Path] = []
        if self._pid_directory.is_dir():
            candidates = sorted(
                self._pid_directory.glob("*.toml"),
                key=lambda item: item.name.casefold(),
            )
        if self._pid_file.is_file() and all(
            item.resolve() != self._pid_file for item in candidates
        ):
            candidates.insert(0, self._pid_file)
        for path in candidates:
            resolved = path.resolve()
            try:
                zones = self._load_pid_zones_from_path(resolved, self._max_value)
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                entries.append(
                    {
                        "name": resolved.name,
                        "path": str(resolved),
                        "valid": False,
                        "zone_count": 0,
                        "max_temperature": None,
                        "error": str(exc),
                    }
                )
            else:
                entries.append(
                    {
                        "name": resolved.name,
                        "path": str(resolved),
                        "valid": True,
                        "zone_count": len(zones),
                        "max_temperature": zones[-1].temperature,
                        "error": "",
                    }
                )
        return entries

    def _read_setup(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "channels": list(self._supported_channels()),
            "channel_sensors": self._read_channel_sensors(),
            "current_pid_file": self._pid_file.name,
            "current_pid_path": str(self._pid_file),
            "pid_directory": str(self._pid_directory),
            "pid_tables": self._list_pid_tables(),
            "factory_index_max": 58,
            "user_index_max": 7,
        }

    def _apply_setup(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        transport = self._require_transport()
        supported = set(self._supported_channels())
        self._staged_pid = None

        raw_channels = payload.get("channels", {})
        if not isinstance(raw_channels, Mapping):
            raise InstrumentError(
                "channels must be a mapping",
                "CRYOCON_INVALID_SETUP",
                self._address,
            )
        channel_updates: list[tuple[str, str, int]] = []
        for raw_channel, raw_spec in raw_channels.items():
            channel = str(raw_channel).strip().upper()
            if channel not in supported:
                raise InstrumentError(
                    f"Channel {channel!r} is not supported on Cryo-con {self._model}",
                    "CRYOCON_INVALID_SETUP",
                    channel,
                )
            if not isinstance(raw_spec, Mapping):
                raise InstrumentError(
                    f"Channel {channel} sensor selection must be a mapping",
                    "CRYOCON_INVALID_SETUP",
                    channel,
                )
            kind = str(raw_spec.get("kind", "")).strip().casefold()
            raw_index = raw_spec.get("index")
            if kind not in {"factory", "user"}:
                raise InstrumentError(
                    f"Channel {channel} curve kind must be 'factory' or 'user'",
                    "CRYOCON_INVALID_SETUP",
                    channel,
                )
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise InstrumentError(
                    f"Channel {channel} curve index must be an integer",
                    "CRYOCON_INVALID_SETUP",
                    channel,
                )
            max_index = 58 if kind == "factory" else 7
            if not 0 <= raw_index <= max_index:
                raise InstrumentError(
                    f"Channel {channel} {kind} index {raw_index} must be between 0 and {max_index}",
                    "CRYOCON_INVALID_SETUP",
                    channel,
                )
            channel_updates.append((channel, kind, raw_index))

        new_pid_file: Path | None = None
        new_pid_zones: tuple[PIDZone, ...] | None = None
        raw_pid_name = payload.get("pid_file")
        if raw_pid_name is not None and str(raw_pid_name).strip():
            requested_name = str(raw_pid_name).strip()
            candidate_path = Path(requested_name)
            if candidate_path.name != requested_name or candidate_path.suffix.casefold() != ".toml":
                raise InstrumentError(
                    f"PID table must be a .toml file name in {self._pid_directory}",
                    "CRYOCON_INVALID_PID_FILE",
                    requested_name,
                )
            resolved_candidate = (self._pid_directory / requested_name).resolve()
            if resolved_candidate.parent != self._pid_directory.resolve() or not resolved_candidate.is_file():
                raise InstrumentError(
                    f"PID table file not found in {self._pid_directory}: {requested_name}",
                    "CRYOCON_INVALID_PID_FILE",
                    requested_name,
                )
            if resolved_candidate != self._pid_file:
                try:
                    new_pid_zones = self._load_pid_zones_from_path(
                        resolved_candidate,
                        self._max_value,
                    )
                except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                    raise InstrumentError(
                        f"Invalid PID table {requested_name}: {exc}",
                        "CRYOCON_INVALID_PID_FILE",
                        requested_name,
                    ) from exc
                new_pid_file = resolved_candidate

        summary_parts: list[str] = []
        for channel, kind, index in channel_updates:
            try:
                instrument.write_channel_sensor_index_and_readback(
                    transport,
                    channel,
                    kind,
                    index,
                )
            except Exception as exc:
                raise InstrumentError(
                    f"Cryo-con input {channel} curve write or readback confirmation failed: {exc}",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    f"{self._address}:{channel}",
                ) from exc
            label = f"USENix {index}" if kind == "user" else f"ISENix {index}"
            summary_parts.append(f"CH {channel} -> {label}")

        config_updates: dict[str, Any] = {}
        if new_pid_file is not None and new_pid_zones is not None:
            # 仅暂存校验好的 PID 表，待核心把 configs/instruments/*.toml 原子落盘后
            # 再通过 apply_config_updates() 生效，避免落盘失败时内存与磁盘配置漂移。
            self._staged_pid = (new_pid_file, new_pid_zones)
            config_updates["pid_file"] = str(new_pid_file)
            summary_parts.append(f"PID table -> {new_pid_file.name}")

        setup = self._read_setup()
        if config_updates:
            setup["config_updates"] = config_updates
        setup["summary"] = (
            f"Cryo-con {self._model} updated: " + ", ".join(summary_parts)
            if summary_parts
            else f"Cryo-con {self._model}: no setup changes"
        )
        return setup

    def apply_config_updates(self, updates: dict[str, Any]) -> dict[str, Any]:
        """在核心将配置原子写回磁盘后激活新的软件侧 PID 表。"""

        raw_pid = updates.get("pid_file")
        if raw_pid is not None and str(raw_pid).strip():
            target = Path(str(raw_pid)).resolve()
            if (
                target.parent != self._pid_directory.resolve()
                or target.suffix.casefold() != ".toml"
                or not target.is_file()
            ):
                raise InstrumentError(
                    f"PID table file not found in {self._pid_directory}: {raw_pid}",
                    "CRYOCON_INVALID_PID_FILE",
                    str(raw_pid),
                )
            if self._staged_pid is not None and self._staged_pid[0] == target:
                zones = self._staged_pid[1]
            else:
                try:
                    zones = self._load_pid_zones_from_path(
                        target,
                        self._max_value,
                    )
                except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                    raise InstrumentError(
                        f"Invalid PID table {target.name}: {exc}",
                        "CRYOCON_INVALID_PID_FILE",
                        target.name,
                    ) from exc
            self._pid_file = target
            self._pid_zones = zones
            self._staged_pid = None
            self.config.extras["pid_file"] = str(target)
        return {
            "current_pid_file": self._pid_file.name,
            "current_pid_path": str(self._pid_file),
        }

    @classmethod
    def _load_pid_zones_from_path(
        cls,
        path: Path,
        configured_maximum: float,
    ) -> tuple[PIDZone, ...]:
        with path.open("rb") as handle:
            pid_config = tomllib.load(handle)
        return cls._parse_pid_zones(
            pid_config.get("zones"),
            configured_maximum,
        )

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
        if status.loop_source.strip().upper() not in {self._primary_input, f"CH{self._primary_input}"}:
            self._configuration_fault(
                f"Loop 1 source is {status.loop_source!r}, expected input {self._primary_input}"
            )
        if status.loop_type.strip().upper() != "RAMPP":
            self._configuration_fault(
                f"Loop 1 type is {status.loop_type!r}, expected RAMPP"
            )
        if status.control.strip().upper() != "ON":
            self._configuration_fault("Controller reports CONTROL OFF")
        if status.ramping.strip().upper() not in {"ON", "OFF", "YES", "NO"}:
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
        if self._primary_input == "A" and target > self._monitor_max:
            raise SafetyViolation(
                f"Target {target:g} K exceeds Temp A Maximum ({self._monitor_max:g} K)",
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
                "cryocon_22c_24c requires a non-empty zones list; "
                "copy the PID values already validated for this cryostat"
            )
        zones: list[PIDZone] = []
        previous_maximum = float("-inf")
        for index, raw_zone in enumerate(raw_zones, start=1):
            if not isinstance(raw_zone, Mapping):
                raise ValueError(f"zones[{index}] must be a table")
            maximum = cls._finite_number(
                raw_zone.get("temperature"),
                f"zones[{index}].temperature",
            )
            p_gain = cls._finite_number(
                raw_zone.get("p_gain"),
                f"zones[{index}].p_gain",
            )
            i_gain = cls._finite_number(
                raw_zone.get("i_gain"),
                f"zones[{index}].i_gain",
            )
            d_gain = cls._finite_number(
                raw_zone.get("d_gain"),
                f"zones[{index}].d_gain",
            )
            if maximum <= previous_maximum:
                raise ValueError("zones temperature values must increase")
            if min(p_gain, i_gain, d_gain) < 0.0 or max(
                p_gain,
                i_gain,
                d_gain,
            ) > 1000.0:
                raise ValueError("PID gains must be within the instrument 0–1000 range")
            raw_range = raw_zone.get("heater_range")
            heater_range = (
                None
                if raw_range is None or not str(raw_range).strip()
                else str(raw_range).strip().upper()
            )
            if heater_range not in {None, "LOW", "MID", "HI"}:
                raise ValueError(
                    f"zones[{index}].heater_range must be LOW, MID, or HI"
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
    def _parse_baud_rate(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("Unsupported Cryo-con serial baud rate")
        if isinstance(value, int):
            baud = value
        elif isinstance(value, str) and value.strip().isdigit():
            baud = int(value.strip())
        else:
            raise ValueError("Unsupported Cryo-con serial baud rate")
        if baud not in (9600, 19200, 38400, 57600, 115200):
            raise ValueError("Unsupported Cryo-con serial baud rate")
        return baud

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
