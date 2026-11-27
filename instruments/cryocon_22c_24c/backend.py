"""OpenLab Control System Instrument：Cryo-con 22C/24C 温度控制器。

通过单一 ``model`` 开关选择机型（``22C`` / ``24C``）：
1. **``22C``（默认，单回路）**：仅有 Loop 1 单回路配置与 ``[[zones]]``，不查询也不写入 Loop 2。
2. **``24C``（双回路分段公式联控）**：
   - 默认主回路为 Loop 2（样品，绑定所选 ``main_reading``，如 Input B），从回路为 Loop 1
     （冷台/热沉，绑定另一路输入，如 Input A）；亦可在 24C PID 文件顶层可选写 ``primary_loop = 1``；
   - 两路分别按各自目标温度下发独立 PID 表（``[[zones]]`` 与 ``[[linked_pid_zones]]``）；
   - 从回路目标由 ``[[linked_target_zones]]`` 的分段公式按主目标 ``x``（``<=`` 上限）计算；
   - ``moving`` 在任一回路处于斜坡（ramping）时为 ``True``，两路均停爬才报 ``ready = True``；
   - ``hold()`` 现读两路即时传感器温度各自保持，不套联动公式；
   - ``open()`` 严格核对 ``*IDN?`` 型号与配置的 ``model`` 一致（防止 22C/24C 错配）。
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import operator
from pathlib import Path
import tomllib
from typing import Any

from labcontrol.instruments.base import InstrumentError, SafetyViolation, SystemInstrument

from . import cryocon_22c_24c as instrument


_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_SAFE_FUNCTIONS: dict[str, Callable[..., float]] = {
    "min": min,
    "max": max,
}
_MAX_FORMULA_LENGTH = 128


@dataclass(frozen=True, slots=True)
class PIDZone:
    """PID 表的一行；``temperature`` 是包含等于（``<=``）的温区上限。"""

    temperature: float
    p_gain: float
    i_gain: float
    d_gain: float
    heater_range: str | None = None


@dataclass(frozen=True, slots=True)
class LinkedTargetZone:
    """从回路分段目标公式的一行；``temperature`` 是主目标 ``x`` 的包含等于上限（``<=``）。"""

    temperature: float
    formula: str
    compiled: ast.AST


@dataclass(frozen=True, slots=True)
class PIDProfile:
    """一份 TOML 配置文件解析出的主回路 PID、从回路 PID 与分段联动公式。"""

    primary_zones: tuple[PIDZone, ...]
    linked_pid_zones: tuple[PIDZone, ...] = ()
    linked_target_zones: tuple[LinkedTargetZone, ...] = ()
    primary_loop: int = 2


def _compile_linked_formula(formula: str) -> ast.AST:
    """用严格 AST 白名单校验并编译单段联动公式，禁止任意代码执行。"""

    text = str(formula).strip()
    if not text:
        raise ValueError("Linked target formula must not be empty")
    if len(text) > _MAX_FORMULA_LENGTH:
        raise ValueError(
            f"Linked target formula must be at most {_MAX_FORMULA_LENGTH} characters"
        )
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise ValueError(f"Invalid linked target formula {text!r}: {exc.msg}") from exc
    _validate_formula_ast(node)
    return node


def _validate_formula_ast(node: ast.AST) -> None:
    if isinstance(node, ast.Name):
        if node.id != "x":
            raise ValueError(
                f"Unsupported variable {node.id!r} in linked formula; only 'x' is allowed"
            )
        return
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(
                f"Unsupported constant {node.value!r} in linked formula; use finite numbers only"
            )
        if not math.isfinite(float(node.value)):
            raise ValueError("Linked formula constants must be finite numbers")
        return
    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BINARY_OPS:
            raise ValueError(
                "Linked formula supports only +, -, *, /, parentheses, min(), and max()"
            )
        _validate_formula_ast(node.left)
        _validate_formula_ast(node.right)
        return
    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ValueError(
                "Linked formula supports only unary + and -"
            )
        _validate_formula_ast(node.operand)
        return
    if isinstance(node, ast.Call):
        if (
            not isinstance(node.func, ast.Name)
            or node.func.id not in _SAFE_FUNCTIONS
            or node.keywords
            or len(node.args) < 2
        ):
            raise ValueError(
                "Linked formula calls support only min(a, b, ...) and max(a, b, ...)"
            )
        for arg in node.args:
            _validate_formula_ast(arg)
        return
    raise ValueError(
        "Linked formula supports only x, finite numbers, parentheses, + - * /, min(), and max()"
    )


def _eval_formula_ast(node: ast.AST, x: float) -> float:
    if isinstance(node, ast.Name):
        return float(x)
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        left = _eval_formula_ast(node.left, x)
        right = _eval_formula_ast(node.right, x)
        result = _BINARY_OPS[type(node.op)](left, right)
        if not math.isfinite(result):
            raise ValueError("Linked formula produced a non-finite value")
        return result
    if isinstance(node, ast.UnaryOp):
        result = _UNARY_OPS[type(node.op)](_eval_formula_ast(node.operand, x))
        if not math.isfinite(result):
            raise ValueError("Linked formula produced a non-finite value")
        return result
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        values = [_eval_formula_ast(arg, x) for arg in node.args]
        return float(_SAFE_FUNCTIONS[node.func.id](*values))
    raise ValueError("Unsupported AST node in linked formula")


class Cryocon22C24CController(SystemInstrument):
    """支持 22C 单回路与 24C 双回路分段公式联控的温度控制器后端。"""

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
        self._configured_model = self._parse_model(config.extras.get("model", "22C"))
        self._model = self._configured_model
        self._address = config.address

        primary_input = {"temp_a": "A", "temp_b": "B"}.get(config.main_reading)
        if primary_input is None:
            raise ValueError(
                f"Unsupported Cryo-con main_reading {config.main_reading!r}"
            )
        self._primary_input = primary_input
        self._linked_loop_enabled = self._configured_model == "24C"
        self._primary_loop = 2 if self._linked_loop_enabled else 1
        self._linked_loop: int | None = (
            1 if self._linked_loop_enabled else None
        )
        self._linked_input: str | None = (
            ("B" if self._primary_input == "A" else "A")
            if self._linked_loop_enabled
            else None
        )

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
            raise ValueError("cryocon_22c_24c.address must not be empty")
        if self._monitor_max <= 0:
            raise ValueError("monitor_a_max_temperature must be positive")
        self._hold_rate = self._bounded_rate(
            config.extras["hold_rate_per_minute"]
        )
        operation_timeout = self._positive_number(
            config.operation_timeout_seconds,
            "operation_timeout_seconds",
        )
        if self._linked_loop_enabled:
            minimum_operation_timeout = self._io_timeout * 13.0 + 0.5
            timeout_label = "thirteen VISA I/O timeouts"
        else:
            minimum_operation_timeout = self._io_timeout * 7.0 + 0.5
            timeout_label = "seven VISA I/O timeouts"
        if operation_timeout <= minimum_operation_timeout:
            raise InstrumentError(
                f"Instrument I/O timeout setting is too high: "
                f"{timeout_label} of {self._io_timeout:g} s plus 0.5 s "
                f"need {minimum_operation_timeout:g} s for Hold; "
                f"must be less than {operation_timeout:g} s. "
                "Reduce the I/O timeout in Instrument Scanner.",
                "INSTRUMENT_IO_TIMEOUT_TOO_HIGH",
                "io_timeout_seconds",
            )
        self._min_value = float(config.min_value)
        self._max_value = float(config.max_value)
        self._pid_file = Path(config.extras["pid_file"]).resolve()
        self._pid_directory = self._pid_file.parent
        profile = self._load_profile_from_path(self._pid_file)
        self._apply_profile(profile)
        self._staged_pid: tuple[Path, PIDProfile] | None = None

    def _apply_profile(self, profile: PIDProfile) -> None:
        self._pid_zones = profile.primary_zones
        self._linked_pid_zones = profile.linked_pid_zones
        self._linked_target_zones = profile.linked_target_zones
        if self._linked_loop_enabled:
            self._primary_loop = profile.primary_loop
            self._linked_loop = 1 if self._primary_loop == 2 else 2
        else:
            self._primary_loop = 1
            self._linked_loop = None

    def open(self) -> None:
        """建立 VISA 会话并只读验证型号与配置一致、传感器与活动回路状态。"""

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
            if normalized_model == "22C" and self._configured_model == "24C":
                raise SafetyViolation(
                    "Connected Cryo-con 22C supports only single-loop Loop 1 control; "
                    "switch Controller Model to 22C in Instrument Scanner",
                    "CRYOCON_22C_SINGLE_LOOP_ONLY",
                    self._address,
                )
            if normalized_model != self._configured_model:
                raise SafetyViolation(
                    f"Connected Cryo-con {normalized_model} does not match configured "
                    f"model {self._configured_model}; update Controller Model in Instrument Scanner",
                    "CRYOCON_MODEL_MISMATCH",
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
        """读取所选主温度、辅助温度与活动回路状态；24C 联控时两路均停爬才 ready。"""

        transport = self._require_transport()
        self._status_validated = False
        if not self._linked_loop_enabled:
            try:
                status = instrument.read_status(
                    transport,
                    loop=self._primary_loop,
                )
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
            auxiliary: dict[str, object] = {
                "temp_a": status.temperature_a,
                "temp_b": status.temperature_b,
                "heater_output": status.heater_output_percent,
                "heater_range": _normalize_range_label(status.heater_range),
                "linked_setpoint": None,
                "linked_heater_output": None,
                "linked_heater_range": None,
                "linked_ramping": None,
            }
            self._status_validated = True
            return {
                "value": (
                    status.temperature_a
                    if self._primary_input == "A"
                    else status.temperature_b
                ),
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

        try:
            dual = instrument.read_dual_loop_status(transport)
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con dual-loop communication failed: {exc}",
                "CRYOCON_COMMUNICATION_FAILED",
                self._address,
            ) from exc
        self._validate_dual_status(dual)
        assert self._linked_loop is not None
        primary_state = dual.loop(self._primary_loop)
        linked_state = dual.loop(self._linked_loop)
        primary_ramping = primary_state.ramping.strip().upper() in {"ON", "YES"}
        linked_ramping = linked_state.ramping.strip().upper() in {"ON", "YES"}
        # 两路均不在爬坡时才进入 ready / 稳态计时。
        moving = primary_ramping or linked_ramping
        auxiliary = {
            "temp_a": dual.temperature_a,
            "temp_b": dual.temperature_b,
            "heater_output": primary_state.heater_output_percent,
            "heater_range": _normalize_range_label(primary_state.heater_range),
            "linked_setpoint": linked_state.setpoint,
            "linked_heater_output": linked_state.heater_output_percent,
            "linked_heater_range": _normalize_range_label(linked_state.heater_range),
            "linked_ramping": linked_ramping,
        }
        self._status_validated = True
        return {
            "value": (
                dual.temperature_a
                if self._primary_input == "A"
                else dual.temperature_b
            ),
            "target": primary_state.setpoint,
            "rate": primary_state.rate,
            "moving": moving,
            "ready": not moving,
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
        """先校验并计算单/双回路目标与各自 PID，再统一下发并逐项回读确认。"""

        del mode, control
        primary_target = self._bounded_target(value)
        rate = self._bounded_rate(rate_per_minute)
        self._require_transport()
        self._require_validated_status()
        if not self._linked_loop_enabled:
            self._apply_single_loop_pid_and_target(
                self._primary_loop,
                self._pid_zones,
                primary_target,
                rate,
            )
            return

        linked_target = self._evaluate_linked_target(primary_target)
        self._apply_dual_loop_pid_and_targets(
            primary_target=primary_target,
            linked_target=linked_target,
            rate=rate,
        )

    def hold(self, *, control: str) -> None:
        """以即时传感器温度作为各自目标（联控时不套联动公式），保持输出与 Ramp-P 控制。"""

        del control
        transport = self._require_transport()
        self._require_validated_status()
        rate = self._bounded_rate(self._hold_rate)

        if not self._linked_loop_enabled:
            try:
                current, unit, alarm = instrument.read_temperature(
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
                    f"Cryo-con communication failed during hold: {exc}",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    self._address,
                ) from exc
            self._validate_channel(self._primary_input, current, unit, alarm)
            target = self._bounded_target(current)
            self._apply_single_loop_pid_and_target(
                self._primary_loop,
                self._pid_zones,
                target,
                rate,
            )
            return

        assert self._linked_input is not None
        try:
            readings = instrument.read_temperatures_ab(transport)
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con communication failed during dual-loop hold: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                self._address,
            ) from exc

        for ch in ("A", "B"):
            temp, unit, alarm = readings[ch]
            self._validate_channel(ch, temp, unit, alarm)
        temp_a = readings["A"][0]
        if temp_a > self._monitor_max:
            raise SafetyViolation(
                f"Input A is {temp_a:g} K, above the software limit {self._monitor_max:g} K",
                "CRYOCON_INPUT_A_HIGH_LIMIT",
                "A",
            )
        primary_now = self._bounded_target(readings[self._primary_input][0])
        linked_now = self._bounded_linked_target(readings[self._linked_input][0])
        self._apply_dual_loop_pid_and_targets(
            primary_target=primary_now,
            linked_target=linked_now,
            rate=rate,
        )

    def execute_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """响应 Instrument Setup 按需查询或切换输入曲线与软件侧 PID/联动配置表。"""

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
                profile = self._load_profile_from_path(resolved)
            except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                entries.append(
                    {
                        "name": resolved.name,
                        "path": str(resolved),
                        "valid": False,
                        "zone_count": 0,
                        "linked_pid_zone_count": 0,
                        "linked_target_zone_count": 0,
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
                        "zone_count": len(profile.primary_zones),
                        "linked_pid_zone_count": len(profile.linked_pid_zones),
                        "linked_target_zone_count": len(profile.linked_target_zones),
                        "max_temperature": profile.primary_zones[-1].temperature,
                        "error": "",
                    }
                )
        return entries

    def _linked_formula_preview(self) -> list[dict[str, float]]:
        if not self._linked_loop_enabled or not self._linked_target_zones:
            return []
        candidates: list[float] = [self._min_value]
        for sample in (4.0, 10.0, 100.0, 300.0):
            if self._min_value < sample < self._max_value:
                candidates.append(sample)
        for zone in self._linked_target_zones:
            if self._min_value <= zone.temperature <= self._max_value:
                candidates.append(zone.temperature)
                above = round(zone.temperature + 0.1, 4)
                if above <= self._max_value:
                    candidates.append(above)
        candidates.append(self._max_value)
        unique_points = sorted({round(val, 4) for val in candidates})
        preview: list[dict[str, float]] = []
        for point in unique_points:
            linked = self._evaluate_linked_target_from_zones(
                self._linked_target_zones,
                point,
            )
            preview.append({"primary_k": point, "linked_k": linked})
        return preview

    def _read_setup(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "channels": list(self._supported_channels()),
            "channel_sensors": self._read_channel_sensors(),
            "primary_loop": self._primary_loop,
            "primary_input": self._primary_input,
            "linked_loop_enabled": self._linked_loop_enabled,
            "linked_loop": self._linked_loop,
            "linked_input": self._linked_input,
            "linked_target_zones": [
                {"temperature": z.temperature, "formula": z.formula}
                for z in self._linked_target_zones
            ],
            "linked_preview": self._linked_formula_preview(),
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
        new_profile: PIDProfile | None = None
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
                    new_profile = self._load_profile_from_path(resolved_candidate)
                except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                    raise InstrumentError(
                        f"Invalid PID table {requested_name}: {exc}",
                        "CRYOCON_INVALID_PID_FILE",
                        requested_name,
                    ) from exc
                # 若新 PID 表改变了 24C 主从回路编号，在暂存或写入任何参数前先实测核对硬件 SOURCE。
                if self._linked_loop_enabled and new_profile.primary_loop != self._primary_loop:
                    self._verify_hardware_loop_sources(
                        transport,
                        primary_loop=new_profile.primary_loop,
                    )
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
        if new_pid_file is not None and new_profile is not None:
            # 仅暂存校验好的配置表，待核心把 configs/instruments/*.toml 原子落盘后
            # 再通过 apply_config_updates() 生效，避免落盘失败时内存与磁盘配置漂移。
            self._staged_pid = (new_pid_file, new_profile)
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
        """在核心将配置原子写回磁盘后激活新的软件侧 PID 与联动公式表。"""

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
                profile = self._staged_pid[1]
            else:
                try:
                    profile = self._load_profile_from_path(target)
                except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
                    raise InstrumentError(
                        f"Invalid PID table {target.name}: {exc}",
                        "CRYOCON_INVALID_PID_FILE",
                        target.name,
                    ) from exc
            if self._linked_loop_enabled and profile.primary_loop != self._primary_loop:
                if self._transport is not None:
                    try:
                        self._verify_hardware_loop_sources(
                            self._transport,
                            primary_loop=profile.primary_loop,
                        )
                    except Exception:
                        self._staged_pid = None
                        raise
                else:
                    self._status_validated = False
            self._pid_file = target
            self._apply_profile(profile)
            self._staged_pid = None
            self.config.extras["pid_file"] = str(target)
        return {
            "current_pid_file": self._pid_file.name,
            "current_pid_path": str(self._pid_file),
            "primary_loop": self._primary_loop,
            "linked_loop": self._linked_loop,
            "linked_target_zones": [
                {"temperature": z.temperature, "formula": z.formula}
                for z in self._linked_target_zones
            ],
            "linked_preview": self._linked_formula_preview(),
        }

    def _load_profile_from_path(self, path: Path) -> PIDProfile:
        with path.open("rb") as handle:
            pid_config = tomllib.load(handle)
        unknown_top = sorted(
            set(pid_config)
            - {"zones", "linked_pid_zones", "linked_target_zones", "primary_loop"}
        )
        if unknown_top:
            raise ValueError(
                f"PID file contains unsupported top-level key(s): {', '.join(unknown_top)}"
            )
        primary_zones = self._parse_pid_zones(
            pid_config.get("zones"),
            self._max_value,
        )
        raw_linked_pid = pid_config.get("linked_pid_zones")
        raw_linked_targets = pid_config.get("linked_target_zones")
        raw_primary_loop = pid_config.get("primary_loop")

        if not self._linked_loop_enabled:
            if raw_primary_loop is not None:
                parsed_loop = self._parse_loop_number(raw_primary_loop)
                if parsed_loop != 1:
                    raise ValueError(
                        "Cryo-con 22C supports only Loop 1; primary_loop cannot be 2"
                    )
            linked_pid = (
                self._parse_pid_zones(
                    raw_linked_pid,
                    configured_maximum=None,
                    label="linked_pid_zones",
                )
                if raw_linked_pid
                else ()
            )
            linked_targets = (
                self._parse_linked_target_zones(
                    raw_linked_targets,
                    self._max_value,
                )
                if raw_linked_targets
                else ()
            )
            return PIDProfile(
                primary_zones=primary_zones,
                linked_pid_zones=linked_pid,
                linked_target_zones=linked_targets,
                primary_loop=1,
            )

        primary_loop = (
            2
            if raw_primary_loop is None
            else self._parse_loop_number(raw_primary_loop)
        )
        linked_pid = self._parse_pid_zones(
            raw_linked_pid,
            configured_maximum=None,
            label="linked_pid_zones",
        )
        linked_targets = self._parse_linked_target_zones(
            raw_linked_targets,
            self._max_value,
        )
        self._validate_linked_profile_samples(linked_targets, linked_pid)
        return PIDProfile(
            primary_zones=primary_zones,
            linked_pid_zones=linked_pid,
            linked_target_zones=linked_targets,
            primary_loop=primary_loop,
        )

    def _validate_linked_profile_samples(
        self,
        target_zones: tuple[LinkedTargetZone, ...],
        linked_pid_zones: tuple[PIDZone, ...],
    ) -> None:
        """在加载配置阶段抽检所有分段边界与端点的公式结果，提前拦截越界或 PID 覆盖不足。"""

        probes: list[float] = [self._min_value, self._max_value]
        for zone in target_zones:
            if self._min_value <= zone.temperature <= self._max_value:
                probes.append(zone.temperature)
                above = zone.temperature + 1e-6
                if above <= self._max_value:
                    probes.append(above)
        pid_ceiling = linked_pid_zones[-1].temperature
        for x in probes:
            linked_val = self._evaluate_linked_target_from_zones(target_zones, x)
            if linked_val <= 0.0:
                raise ValueError(
                    f"linked_target_zones produces non-positive linked target {linked_val:g} K at x={x:g} K"
                )
            if linked_val > self._max_value:
                raise ValueError(
                    f"linked_target_zones produces linked target {linked_val:g} K at x={x:g} K, "
                    f"exceeding configured maximum {self._max_value:g} K"
                )
            if self._linked_input == "A" and linked_val > self._monitor_max:
                raise ValueError(
                    f"linked_target_zones produces Input A target {linked_val:g} K at x={x:g} K, "
                    f"exceeding Temp A Maximum ({self._monitor_max:g} K)"
                )
            if linked_val > pid_ceiling:
                raise ValueError(
                    f"linked_pid_zones maximum ({pid_ceiling:g} K) does not cover "
                    f"linked target {linked_val:g} K at x={x:g} K"
                )

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

    @staticmethod
    def _select_pid_zone(zones: tuple[PIDZone, ...], target: float) -> PIDZone:
        for zone in zones:
            if target <= zone.temperature:
                return zone
        raise SafetyViolation(
            f"Target {target:g} K exceeds PID table maximum ({zones[-1].temperature:g} K)",
            "CRYOCON_TARGET_LIMIT",
            "pid_zones",
        )

    @staticmethod
    def _evaluate_linked_target_from_zones(
        zones: tuple[LinkedTargetZone, ...],
        primary_target: float,
    ) -> float:
        selected = next(
            (zone for zone in zones if primary_target <= zone.temperature),
            zones[-1],
        )
        try:
            raw = _eval_formula_ast(selected.compiled, primary_target)
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(
                f"Cannot evaluate linked formula {selected.formula!r} at x={primary_target:g} K: {exc}"
            ) from exc
        if not math.isfinite(raw):
            raise ValueError(
                f"Linked formula {selected.formula!r} produced a non-finite target at x={primary_target:g} K"
            )
        return round(raw, 4)

    def _evaluate_linked_target(self, primary_target: float) -> float:
        try:
            computed = self._evaluate_linked_target_from_zones(
                self._linked_target_zones,
                primary_target,
            )
        except ValueError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_FORMULA_ERROR",
                self.config.id,
            ) from exc
        return self._bounded_linked_target(computed)

    def _apply_single_loop_pid_and_target(
        self,
        loop: int,
        zones: tuple[PIDZone, ...],
        target: float,
        rate: float,
    ) -> None:
        transport = self._require_transport()
        zone = self._select_pid_zone(zones, target)
        operations: list[tuple[str, float | str]] = [
            ("PGAIN", zone.p_gain),
            ("IGAIN", zone.i_gain),
            ("DGAIN", zone.d_gain),
        ]
        if zone.heater_range is not None:
            operations.append(("RANGE", zone.heater_range))
        operations.extend((("RATE", rate), ("SETPT", target)))
        self._execute_loop_operations(
            transport,
            [(loop, parameter, requested) for parameter, requested in operations],
        )

    def _apply_dual_loop_pid_and_targets(
        self,
        *,
        primary_target: float,
        linked_target: float,
        rate: float,
    ) -> None:
        assert self._linked_loop is not None
        transport = self._require_transport()
        # 在发送任何一条写命令前，先为两路各自选定并核对 PID 段。
        primary_zone = self._select_pid_zone(self._pid_zones, primary_target)
        linked_zone = self._select_pid_zone(self._linked_pid_zones, linked_target)

        # 先下发并回读确认两路的 PID、可选量程和速率，最后再下发两路的 SETPT。
        operations: list[tuple[int, str, float | str]] = []
        for loop, zone in (
            (self._primary_loop, primary_zone),
            (self._linked_loop, linked_zone),
        ):
            operations.extend(
                (
                    (loop, "PGAIN", zone.p_gain),
                    (loop, "IGAIN", zone.i_gain),
                    (loop, "DGAIN", zone.d_gain),
                )
            )
            if zone.heater_range is not None:
                operations.append((loop, "RANGE", zone.heater_range))
            operations.append((loop, "RATE", rate))
        operations.append((self._linked_loop, "SETPT", linked_target))
        operations.append((self._primary_loop, "SETPT", primary_target))
        self._execute_loop_operations(transport, operations)

    def _execute_loop_operations(
        self,
        transport: instrument.QueryTransport,
        operations: list[tuple[int, str, float | str]],
    ) -> None:
        last_context = self._address
        try:
            for loop, parameter, requested in operations:
                last_context = f"{self._address}:LOOP{loop}:{parameter}"
                if isinstance(requested, str):
                    instrument.write_enum_and_readback(
                        transport,
                        parameter,
                        requested,
                        loop=loop,
                    )
                else:
                    instrument.write_number_and_readback(
                        transport,
                        parameter,
                        requested,
                        loop=loop,
                    )
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con write or readback confirmation failed: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                last_context,
            ) from exc

    def _validate_status(
        self,
        status: instrument.ControllerReadback,
    ) -> None:
        self._validate_common_channels_and_control(
            status.temperature_a,
            status.unit_a,
            status.alarm_a,
            status.temperature_b,
            status.unit_b,
            status.alarm_b,
            status.control,
        )
        self._validate_loop(
            instrument.LoopReadback(
                loop=self._primary_loop,
                setpoint=status.setpoint,
                rate=status.rate,
                ramping=status.ramping,
                heater_range=status.heater_range,
                heater_output_percent=status.heater_output_percent,
                loop_error=status.loop_error,
                loop_type=status.loop_type,
                loop_source=status.loop_source,
            ),
            expected_input=self._primary_input,
            is_primary=True,
        )

    def _validate_dual_status(self, dual: instrument.DualLoopReadback) -> None:
        assert self._linked_loop is not None
        assert self._linked_input is not None
        self._validate_common_channels_and_control(
            dual.temperature_a,
            dual.unit_a,
            dual.alarm_a,
            dual.temperature_b,
            dual.unit_b,
            dual.alarm_b,
            dual.control,
        )
        self._validate_loop(
            dual.loop(self._primary_loop),
            expected_input=self._primary_input,
            is_primary=True,
        )
        self._validate_loop(
            dual.loop(self._linked_loop),
            expected_input=self._linked_input,
            is_primary=False,
        )

    def _validate_common_channels_and_control(
        self,
        temperature_a: float,
        unit_a: str,
        alarm_a: str,
        temperature_b: float,
        unit_b: str,
        alarm_b: str,
        control: str,
    ) -> None:
        self._validate_channel("A", temperature_a, unit_a, alarm_a)
        self._validate_channel("B", temperature_b, unit_b, alarm_b)
        if temperature_a > self._monitor_max:
            raise SafetyViolation(
                f"Input A is {temperature_a:g} K, above the "
                f"software limit {self._monitor_max:g} K",
                "CRYOCON_INPUT_A_HIGH_LIMIT",
                "A",
            )
        if control.strip().upper() != "ON":
            self._configuration_fault("Controller reports CONTROL OFF")

    def _validate_loop_source(
        self,
        loop_state: instrument.LoopReadback,
        *,
        expected_input: str,
    ) -> None:
        if loop_state.loop_source.strip().upper() not in {
            expected_input,
            f"CH{expected_input}",
        }:
            self._configuration_fault(
                f"Loop {loop_state.loop} source is {loop_state.loop_source!r}, expected input {expected_input}"
            )

    def _verify_hardware_loop_sources(
        self,
        transport: instrument.QueryTransport,
        *,
        primary_loop: int,
    ) -> None:
        """热切 24C 主从回路编号前，即时查询硬件两路 SOURCE 并确认与配置输入完全一致。"""

        if not self._linked_loop_enabled:
            return
        assert self._linked_input is not None
        linked_loop = 1 if primary_loop == 2 else 2
        try:
            dual = instrument.read_dual_loop_status(transport)
        except instrument.CryoconProtocolError as exc:
            raise SafetyViolation(
                str(exc),
                "CRYOCON_INVALID_READBACK",
                self._address,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Cryo-con dual-loop source verification failed: {exc}",
                "CRYOCON_COMMUNICATION_FAILED",
                self._address,
            ) from exc
        self._validate_loop_source(
            dual.loop(primary_loop),
            expected_input=self._primary_input,
        )
        self._validate_loop_source(
            dual.loop(linked_loop),
            expected_input=self._linked_input,
        )

    def _validate_loop(
        self,
        loop_state: instrument.LoopReadback,
        *,
        expected_input: str,
        is_primary: bool,
    ) -> None:
        label = f"Loop {loop_state.loop}"
        self._validate_loop_source(loop_state, expected_input=expected_input)
        if loop_state.loop_type.strip().upper() != "RAMPP":
            self._configuration_fault(
                f"{label} type is {loop_state.loop_type!r}, expected RAMPP"
            )
        if loop_state.ramping.strip().upper() not in {"ON", "OFF", "YES", "NO"}:
            self._configuration_fault(
                f"Invalid {label} ramp state {loop_state.ramping!r}"
            )
        heater_range = _normalize_range_label(loop_state.heater_range)
        if heater_range not in {"LOW", "MID", "HI"}:
            self._configuration_fault(
                f"Invalid {label} heater range {loop_state.heater_range!r}"
            )
        if not 0.0 <= loop_state.heater_output_percent <= 100.0:
            self._configuration_fault(
                f"Invalid {label} heater output {loop_state.heater_output_percent!r}%"
            )
        normalized_error = "".join(loop_state.loop_error.casefold().split())
        if "htrok" not in normalized_error:
            raise SafetyViolation(
                f"{label} reports {loop_state.loop_error!r}",
                "CRYOCON_HEATER_FAULT",
                f"loop{loop_state.loop}",
            )
        if is_primary:
            self._bounded_target(loop_state.setpoint)
        else:
            self._bounded_linked_target(loop_state.setpoint)
        self._bounded_rate(loop_state.rate)

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
        if not self._min_value <= target <= self._max_value:
            raise SafetyViolation(
                f"Target {target:g} K is outside the configured "
                f"{self._min_value:g}–{self._max_value:g} K range",
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

    def _bounded_linked_target(self, value: Any) -> float:
        target = self._finite_number(value, "linked target")
        if target <= 0.0 or target > self._max_value:
            raise SafetyViolation(
                f"Linked Loop {self._linked_loop} target {target:g} K is outside "
                f"(0, {self._max_value:g}] K",
                "CRYOCON_TARGET_LIMIT",
                f"loop{self._linked_loop}",
            )
        if self._linked_input == "A" and target > self._monitor_max:
            raise SafetyViolation(
                f"Linked Loop {self._linked_loop} target {target:g} K exceeds "
                f"Temp A Maximum ({self._monitor_max:g} K)",
                "CRYOCON_TARGET_LIMIT",
                f"loop{self._linked_loop}",
            )
        if self._linked_pid_zones and target > self._linked_pid_zones[-1].temperature:
            raise SafetyViolation(
                f"Linked Loop {self._linked_loop} target {target:g} K exceeds "
                f"linked_pid_zones maximum ({self._linked_pid_zones[-1].temperature:g} K)",
                "CRYOCON_TARGET_LIMIT",
                f"loop{self._linked_loop}",
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

    def _require_validated_status(self) -> None:
        if not self._status_validated:
            raise InstrumentError(
                "Cryo-con has no validated full status snapshot",
                "CRYOCON_STATUS_UNAVAILABLE",
                self._address,
            )

    @classmethod
    def _parse_pid_zones(
        cls,
        raw_zones: Any,
        configured_maximum: float | None,
        *,
        label: str = "zones",
    ) -> tuple[PIDZone, ...]:
        if not isinstance(raw_zones, list) or not raw_zones:
            raise ValueError(
                f"cryocon_22c_24c requires a non-empty {label} list; "
                "copy the PID values already validated for this cryostat"
            )
        zones: list[PIDZone] = []
        previous_maximum = float("-inf")
        allowed_keys = frozenset({"temperature", "p_gain", "i_gain", "d_gain", "heater_range"})
        for index, raw_zone in enumerate(raw_zones, start=1):
            if not isinstance(raw_zone, Mapping):
                raise ValueError(f"{label}[{index}] must be a table")
            unknown_keys = sorted(str(key) for key in raw_zone if key not in allowed_keys)
            if unknown_keys:
                raise ValueError(
                    f"{label}[{index}] contains unsupported key(s): {', '.join(unknown_keys)}"
                )
            maximum = cls._positive_number(
                raw_zone.get("temperature"),
                f"{label}[{index}].temperature",
            )
            p_gain = cls._finite_number(
                raw_zone.get("p_gain"),
                f"{label}[{index}].p_gain",
            )
            i_gain = cls._finite_number(
                raw_zone.get("i_gain"),
                f"{label}[{index}].i_gain",
            )
            d_gain = cls._finite_number(
                raw_zone.get("d_gain"),
                f"{label}[{index}].d_gain",
            )
            if maximum <= previous_maximum:
                raise ValueError(f"{label} temperature values must increase")
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
                else _normalize_range_label(str(raw_range))
            )
            if heater_range not in {None, "LOW", "MID", "HI"}:
                raise ValueError(
                    f"{label}[{index}].heater_range must be LOW, MID, or HI"
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
        if configured_maximum is not None and zones[-1].temperature < configured_maximum:
            raise ValueError(
                f"The final {label} temperature must cover the configured maximum temperature"
            )
        return tuple(zones)

    @classmethod
    def _parse_linked_target_zones(
        cls,
        raw_zones: Any,
        configured_maximum: float,
    ) -> tuple[LinkedTargetZone, ...]:
        if not isinstance(raw_zones, list) or not raw_zones:
            raise ValueError(
                "cryocon_22c_24c dual-loop linked mode requires a non-empty "
                "linked_target_zones list"
            )
        zones: list[LinkedTargetZone] = []
        previous_maximum = float("-inf")
        allowed_keys = frozenset({"temperature", "formula"})
        for index, raw_zone in enumerate(raw_zones, start=1):
            if not isinstance(raw_zone, Mapping):
                raise ValueError(f"linked_target_zones[{index}] must be a table")
            unknown_keys = sorted(str(key) for key in raw_zone if key not in allowed_keys)
            if unknown_keys:
                raise ValueError(
                    f"linked_target_zones[{index}] contains unsupported key(s): "
                    + ", ".join(unknown_keys)
                )
            maximum = cls._positive_number(
                raw_zone.get("temperature"),
                f"linked_target_zones[{index}].temperature",
            )
            if maximum <= previous_maximum:
                raise ValueError("linked_target_zones temperature values must increase")
            raw_formula = raw_zone.get("formula")
            if not isinstance(raw_formula, str):
                raise ValueError(
                    f"linked_target_zones[{index}].formula must be a string"
                )
            compiled = _compile_linked_formula(raw_formula)
            zones.append(
                LinkedTargetZone(
                    temperature=maximum,
                    formula=raw_formula.strip(),
                    compiled=compiled,
                )
            )
            previous_maximum = maximum
        if zones[-1].temperature < configured_maximum:
            raise ValueError(
                "The final linked_target_zones temperature must cover the configured maximum temperature"
            )
        return tuple(zones)

    @staticmethod
    def _parse_model(value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("Controller model must be '22C' or '24C'")
        normalized = value.strip().upper()
        if normalized not in {"22C", "24C"}:
            raise ValueError("Controller model must be '22C' or '24C'")
        return normalized

    @staticmethod
    def _parse_loop_number(value: Any) -> int:
        if isinstance(value, bool):
            raise ValueError("primary_loop must be 1 or 2")
        if isinstance(value, int):
            loop = value
        elif isinstance(value, str) and value.strip().isdigit():
            loop = int(value.strip())
        else:
            raise ValueError("primary_loop must be 1 or 2")
        if loop not in (1, 2):
            raise ValueError("primary_loop must be 1 or 2")
        return loop

    @staticmethod
    def _parse_bool(value: Any, label: str) -> bool:
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be true or false")
        return value

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
        if isinstance(value, bool):
            raise ValueError(f"{label} must be a finite number")
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


def _normalize_range_label(raw: str) -> str:
    upper = str(raw).strip().upper()
    return "HI" if upper == "HIGH" else upper
