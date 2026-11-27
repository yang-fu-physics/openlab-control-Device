"""OpenLab Control System Instrument：MultiFields Technologies Kelvinion 控温仪（双回路）。

设计要点：
- 传输层优先支持 USB-B 虚拟串口（CH343, 115200 8N1, 无流控, ``ASRL<n>::INSTR``），
  可选 LAN Socket（``TCPIP0::<ip>::5000::SOCKET``）；当前明确禁用 GPIB。
- 采用类似 Lake Shore 340 的状态汇报与控制思路：不维护软件侧 PID 表，直接复用仪表本机
  PID / 硬件 Zone；``open()`` 仅做只读身份与回路校验，不主动修改输出或设定值。
- 启用独立双回路控制（``output_1`` -> Heater A，``output_2`` -> Heater B），同时兼容仅启用
  单回路及 Kelvinion Mini（2 路输入 A/B + 1 路输出 A）。
- 按当前未验证假设将 ``[READ:SETP:CH]`` 视为爬坡工作设定值（working/ramping setpoint），
  通过内部爬坡跟踪器推算 ``moving`` / ``ready`` 与 ``loop_N_ramping``，所有未实机确认处均以
  ``UNVERIFIED_PROTOCOL (HV-XX)`` 标注。
- 本版本不实现 Filter、Excitation、Reversal 及 Relay/Analog 控制。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import math
import time
from typing import Any

from labcontrol.formatting import require_precision
from labcontrol.instruments.base import (
    InstrumentError,
    SafetyViolation,
    SystemInstrument,
)

from . import kelvinion as protocol


_READING_TO_INPUT: dict[str, str] = {
    f"temp_{ch.lower()}": ch for ch in protocol.INPUT_CHANNELS_STANDARD
}
_INPUT_TO_READING: dict[str, str] = {
    ch: f"temp_{ch.lower()}" for ch in protocol.INPUT_CHANNELS_STANDARD
}
_CONTROL_TO_OUTPUT: dict[str, str] = {
    "output_1": "A",
    "output_2": "B",
}
_OUTPUT_TO_CONTROL: dict[str, str] = {
    "A": "output_1",
    "B": "output_2",
}
_OUTPUT_TO_PREFIX: dict[str, str] = {
    "A": "loop_1",
    "B": "loop_2",
}

# UNVERIFIED_PROTOCOL (HV-03): 爬坡工作设定值到位与外部手动改写判定阈值。
_SETPOINT_REACHED_TOL_K = 0.015
_SETPOINT_STEP_EPS_K = 0.005
_EXTERNAL_OVERRIDE_TOL_K = 0.05
_MIN_RAMP_STALL_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class _ControlBinding:
    """一个已启用控制回路的面板限值与传感器绑定。"""

    control_id: str
    output: str
    input_channel: str
    min_value: float
    max_value: float
    max_rate_per_minute: float
    role: str


@dataclass(slots=True)
class _ActiveRamp:
    """UNVERIFIED_PROTOCOL (HV-03): 跟踪一次命令爬坡的工作设定值进展。"""

    commanded_target: float
    start_setpoint: float
    last_setpoint: float
    rate_k_per_min: float
    last_progress_time: float
    direction: int
    stalled: bool = False


class KelvinionController(SystemInstrument):
    """MultiFields Kelvinion 双回路温度控制器后端（API v4）。"""

    def __init__(
        self,
        config,
        *,
        transport_factory: Callable[..., protocol.KelvinionTransport] = protocol.PyVisaTransport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(config)
        self._transport_factory = transport_factory
        self._clock = clock
        self._transport: protocol.KelvinionTransport | None = None
        self._identity: protocol.KelvinionIdentity | None = None

        raw_address = str(getattr(config, "address", "")).strip()
        try:
            self._address = protocol.validate_visa_address(raw_address)
        except ValueError as exc:
            code = (
                "KELVINION_GPIB_UNSUPPORTED"
                if raw_address.upper().startswith("GPIB")
                else "KELVINION_INVALID_CONFIGURATION"
            )
            raise InstrumentError(str(exc), code, raw_address or config.id) from exc

        extras = dict(getattr(config, "extras", {}) or {})
        self._output_2_enabled = self._parse_bool(
            extras.get("output_2_enabled", True),
            "output_2_enabled",
        )
        self._baud_rate = self._parse_baud_rate(extras.get("baud_rate", 115200))
        self._visa_timeout_ms = self._parse_timeout_ms(
            extras.get("visa_timeout_ms", 1000)
        )
        # UNVERIFIED_PROTOCOL (HV-01): 见 kelvinion.write_command。
        self._write_expects_ack = self._parse_bool(
            extras.get("write_expects_ack", False),
            "write_expects_ack",
        )
        # UNVERIFIED_PROTOCOL (HV-02): 见 kelvinion.format_setpoint_command。
        self._setpoint_unit_suffix = self._parse_bool(
            extras.get("setpoint_unit_suffix", True),
            "setpoint_unit_suffix",
        )

        op_timeout_ms = float(config.operation_timeout_seconds) * 1000.0
        if self._visa_timeout_ms >= op_timeout_ms:
            raise InstrumentError(
                f"Instrument I/O timeout setting is too high: VISA I/O timeout "
                f"{self._visa_timeout_ms:g} ms must be less than "
                f"{config.operation_timeout_seconds:g} s. "
                "Reduce the VISA I/O timeout in Instrument Scanner.",
                "INSTRUMENT_IO_TIMEOUT_TOO_HIGH",
                "visa_timeout_ms",
            )

        self._auxiliary_readings: tuple[str, ...] = tuple(
            getattr(config, "auxiliary_readings", ()) or ()
        )
        self._auxiliary_set = frozenset(self._auxiliary_readings)

        primary_input = _READING_TO_INPUT.get(str(config.main_reading))
        if primary_input is None:
            raise InstrumentError(
                f"Unsupported Kelvinion main_reading: {config.main_reading!r}",
                "KELVINION_INVALID_CONFIGURATION",
                config.id,
            )
        self._primary_input = primary_input

        self._declared_controls, self._control_bindings = self._resolve_controls(config)
        if "output_2" in self._control_bindings and not self._output_2_enabled:
            raise InstrumentError(
                "Enable Output 2 (Heater B) must be true when the Output 2 controller panel is enabled",
                "KELVINION_INVALID_CONFIGURATION",
                config.id,
            )

        self._role_required_inputs: set[str] = {
            binding.input_channel
            for binding in self._control_bindings.values()
            if binding.role in {"sample_temp", "field"}
        }

        self._primary_control: str | None = self._select_primary_control()

        # 仅轮询主读数、已启用控制回路探头及已启用面板请求的温度通道，避免对未接线通道做无效查询。
        polled_inputs: list[str] = [self._primary_input]
        for binding in self._control_bindings.values():
            if binding.input_channel not in polled_inputs:
                polled_inputs.append(binding.input_channel)
        for reading_key in self._auxiliary_readings:
            ch = _READING_TO_INPUT.get(reading_key)
            if ch is not None and ch not in polled_inputs:
                polled_inputs.append(ch)
        self._polled_inputs: tuple[str, ...] = tuple(polled_inputs)

        measurement_inputs: list[str] = [self._primary_input]
        for reading_key in self._auxiliary_readings:
            ch = _READING_TO_INPUT.get(reading_key)
            if ch is not None and ch not in measurement_inputs:
                measurement_inputs.append(ch)
        self._measurement_inputs: tuple[str, ...] = tuple(measurement_inputs)

        self._poll_output_1 = (
            "output_1" in self._control_bindings
            or any(key.startswith("loop_1_") for key in self._auxiliary_readings)
        )
        self._poll_output_2 = self._output_2_enabled and (
            "output_2" in self._control_bindings
            or any(key.startswith("loop_2_") for key in self._auxiliary_readings)
        )

        # UNVERIFIED_PROTOCOL (HV-03): 爬坡状态跟踪器（每次 open/close 重置）。
        self._active_ramps: dict[str, _ActiveRamp] = {}
        self._last_observed_setpoints: dict[str, float] = {}

    def open(self) -> None:
        """建立 USB/LAN 会话并只读验证型号、通道能力与已启用控制回路绑定。"""

        if self._transport is not None:
            return
        transport: protocol.KelvinionTransport | None = None
        try:
            transport = self._transport_factory(
                self._address,
                self._visa_timeout_ms,
                baud_rate=self._baud_rate,
            )
            identity = protocol.identify(transport)
            self._validate_model_capabilities(identity)
            for control_id, binding in self._control_bindings.items():
                actual_input = protocol.read_loop_binding(transport, binding.output)
                if actual_input != binding.input_channel:
                    raise InstrumentError(
                        f"{control_id} (Output {binding.output}) is bound to Input "
                        f"{actual_input!r} on the instrument, expected Input "
                        f"{binding.input_channel!r}",
                        "KELVINION_LOOP_CONFIGURATION_MISMATCH",
                        binding.output,
                    )
            self._transport = transport
            self._identity = identity
            self._active_ramps.clear()
            self._last_observed_setpoints.clear()
        except InstrumentError:
            self._transport = None
            self._identity = None
            if transport is not None:
                transport.close()
            raise
        except protocol.KelvinionProtocolError as exc:
            self._transport = None
            self._identity = None
            if transport is not None:
                transport.close()
            code = (
                "KELVINION_IDENTITY_MISMATCH"
                if "identity" in str(exc).casefold()
                else "KELVINION_INVALID_RESPONSE"
            )
            raise InstrumentError(str(exc), code, self._address) from exc
        except Exception as exc:
            self._transport = None
            self._identity = None
            if transport is not None:
                transport.close()
            raise InstrumentError(
                f"Cannot open Kelvinion at {self._address}: {exc}",
                "KELVINION_IO_ERROR",
                self._address,
            ) from exc

    def close(self) -> None:
        """幂等关闭通讯句柄；绝不关闭加热器、不修改设定值或爬坡率。"""

        transport, self._transport = self._transport, None
        self._identity = None
        self._active_ramps.clear()
        self._last_observed_setpoints.clear()
        if transport is not None:
            transport.close()

    def read_status(self) -> dict[str, object]:
        """读取温度、两路输出状态与独立双回路控制状态（类似 Lake Shore 340）。"""

        transport = self._require_transport()
        # 每次轮询先核对已启用控制端点的实际硬件 LOOP 绑定，防止 Setup 或前面板改绑后继续
        # 把旧探头温度配对为该输出的有效控制状态或就绪判定。
        for control_id, binding in self._control_bindings.items():
            actual_input = self._call_read(
                lambda out=binding.output: protocol.read_loop_binding(transport, out),
                context=f"READ:LOOP:{binding.output}",
            )
            if actual_input != binding.input_channel:
                self._active_ramps.pop(control_id, None)
                self._last_observed_setpoints.pop(control_id, None)
                raise SafetyViolation(
                    f"{control_id} (Output {binding.output}) is bound to Input "
                    f"{actual_input!r} on the instrument, expected Input "
                    f"{binding.input_channel!r}; restore Output {binding.output} Loop "
                    "binding or update Scanner configuration and reconnect",
                    "KELVINION_LOOP_CONFIGURATION_MISMATCH",
                    binding.output,
                )

        temperatures = self._read_temperatures(
            transport,
            channels=self._polled_inputs,
            required_inputs=self._role_required_inputs,
        )

        now = self._clock()
        output_snapshots: dict[str, protocol.OutputReadback] = {}
        motion_by_control: dict[str, tuple[float, float, bool, bool, str]] = {}

        for output, should_poll in (
            ("A", self._poll_output_1),
            ("B", self._poll_output_2),
        ):
            if not should_poll:
                continue
            readback = self._call_read(
                lambda out=output: protocol.read_output_status(transport, out),
                context=f"Output {output}",
            )
            output_snapshots[output] = readback
            control_id = _OUTPUT_TO_CONTROL[output]
            target, moving, ready, ramp_state = self._evaluate_loop_motion(
                control_id,
                readback.working_setpoint_k,
                readback.ramp_rate_k_per_min,
                now,
            )
            motion_by_control[control_id] = (
                target,
                readback.ramp_rate_k_per_min,
                moving,
                ready,
                ramp_state,
            )

        all_values: dict[str, float | int | bool | str | None] = {
            _INPUT_TO_READING[ch]: temperatures[ch] for ch in self._polled_inputs
        }
        for output in ("A", "B"):
            prefix = _OUTPUT_TO_PREFIX[output]
            control_id = _OUTPUT_TO_CONTROL[output]
            snap = output_snapshots.get(output)
            if snap is None:
                all_values[f"{prefix}_output"] = None
                all_values[f"{prefix}_range"] = None
                all_values[f"{prefix}_setpoint"] = None
                all_values[f"{prefix}_ramping"] = None
                all_values[f"{prefix}_mode"] = None
                all_values[f"{prefix}_zone"] = None
            else:
                _target, _rate, _moving, _ready, ramp_state = motion_by_control[control_id]
                all_values[f"{prefix}_output"] = snap.power_percent
                all_values[f"{prefix}_range"] = snap.heater_range
                # UNVERIFIED_PROTOCOL (HV-03): 辅助面板显示仪表原始 [READ:SETP:CH] 读数（爬坡工作设定值）。
                all_values[f"{prefix}_setpoint"] = snap.working_setpoint_k
                all_values[f"{prefix}_ramping"] = ramp_state
                all_values[f"{prefix}_mode"] = "Auto" if snap.mode == "A" else "Manual"
                all_values[f"{prefix}_zone"] = snap.zone_state

        result: dict[str, object] = {
            "value": temperatures[self._primary_input],
            "auxiliary": {
                key: all_values.get(key) for key in self._auxiliary_readings
            },
        }

        if self._control_bindings:
            controls_payload: dict[str, dict[str, object]] = {}
            for control_id in self._control_bindings:
                target, rate, moving, ready, _ramp_state = motion_by_control[control_id]
                controls_payload[control_id] = {
                    "target": target,
                    "rate": rate,
                    "moving": moving,
                    "ready": ready,
                }
            result["controls"] = controls_payload

            if self._primary_control is not None:
                p_target, p_rate, p_moving, p_ready, _p_state = motion_by_control[
                    self._primary_control
                ]
                result["target"] = p_target
                result["rate"] = p_rate
                result["moving"] = p_moving
                result["ready"] = p_ready

        return result

    def read_measurement(self) -> dict[str, object]:
        """实验数据采集快速路径：仅读取主温度与已选辅助温度，跳过加热/Zone 查询。"""

        transport = self._require_transport()
        temperatures = self._read_temperatures(
            transport,
            channels=self._measurement_inputs,
            required_inputs=self._role_required_inputs,
        )
        auxiliary: dict[str, float | None] = {}
        for key in self._auxiliary_readings:
            ch = _READING_TO_INPUT.get(key)
            if ch is not None:
                auxiliary[key] = temperatures[ch]
        return {
            "value": temperatures[self._primary_input],
            "auxiliary": auxiliary,
        }

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> None:
        """对指定控制回路（output_1 / output_2）独立设置爬坡率与目标温度。"""

        del mode
        transport = self._require_transport()
        binding = self._require_control_binding(control)

        target = self._validate_target_value(binding, value)
        rate = self._validate_rate_value(binding, rate_per_minute)

        # 写入前执行全部硬件只读安全检查，避免越界或手动模式下误发写命令。
        self._verify_output_ready_for_write(transport, binding, target)

        before_setpoint = self._call_read(
            lambda: protocol.read_working_setpoint(transport, binding.output),
            context=f"READ:SETP:{binding.output}",
        )

        # 写入失败或回读失败均视为结果不确定，绝不自动重发。
        try:
            protocol.write_ramp_and_verify(
                transport,
                binding.output,
                rate,
                expects_ack=self._write_expects_ack,
            )
        except Exception as exc:
            raise InstrumentError(
                f"Cannot confirm Output {binding.output} ramp rate {rate:.2f} K/min: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                f"SET:RAMP:{binding.output}",
            ) from exc

        try:
            after_setpoint = protocol.write_setpoint_and_readback(
                transport,
                binding.output,
                target,
                expects_ack=self._write_expects_ack,
                unit_suffix=self._setpoint_unit_suffix,
            )
        except Exception as exc:
            raise InstrumentError(
                f"Cannot confirm Output {binding.output} setpoint {target:.2f} K: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                f"SET:SETP:{binding.output}",
            ) from exc

        self._record_commanded_target(
            binding.control_id,
            commanded_target=target,
            before_setpoint=before_setpoint,
            after_setpoint=after_setpoint,
            rate_k_per_min=rate,
        )

    def hold(self, *, control: str) -> None:
        """现读当前回路传感器温度并设为目标，保持加热器开启（不发送 RAMP 0，见 HV-04）。"""

        transport = self._require_transport()
        binding = self._require_control_binding(control)

        try:
            fresh_temp = protocol.read_temperature_k(transport, binding.input_channel)
        except protocol.KelvinionSensorFault as exc:
            raise SafetyViolation(
                str(exc),
                "KELVINION_SENSOR_FAULT",
                binding.input_channel,
            ) from exc
        except protocol.KelvinionProtocolError as exc:
            raise InstrumentError(
                str(exc),
                "KELVINION_INVALID_RESPONSE",
                f"READ:K:{binding.input_channel}",
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Failed to read fresh Temp {binding.input_channel} during hold: {exc}",
                "KELVINION_IO_ERROR",
                f"READ:K:{binding.input_channel}",
            ) from exc

        target = round(fresh_temp, 2)
        self._validate_target_value(binding, target)
        self._verify_output_ready_for_write(transport, binding, target)

        before_setpoint = self._call_read(
            lambda: protocol.read_working_setpoint(transport, binding.output),
            context=f"READ:SETP:{binding.output}",
        )
        current_rate = self._call_read(
            lambda: protocol.read_ramp_rate(transport, binding.output),
            context=f"READ:RAMP:{binding.output}",
        )

        # UNVERIFIED_PROTOCOL (HV-04): 手册 p.40 写爬坡率下限 0.1 K/min，尚不确定固件是否
        # 接受 [SET:RAMP:CH:0]。Hold 仅把目标写为当前新鲜温度即可停止继续爬坡，同时保持输出安全。
        try:
            after_setpoint = protocol.write_setpoint_and_readback(
                transport,
                binding.output,
                target,
                expects_ack=self._write_expects_ack,
                unit_suffix=self._setpoint_unit_suffix,
            )
        except Exception as exc:
            raise InstrumentError(
                f"Cannot confirm Output {binding.output} hold setpoint {target:.2f} K: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                f"SET:SETP:{binding.output}",
            ) from exc

        # 若 [READ:SETP] 仍返回尚未到达的爬坡工作设定值，沿用同一目标与停滞跟踪器，
        # 不得提前清空目标或误报 ready=True。
        self._record_commanded_target(
            binding.control_id,
            commanded_target=target,
            before_setpoint=before_setpoint,
            after_setpoint=after_setpoint,
            rate_k_per_min=current_rate,
        )

    def execute_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """响应 Instrument Setup 按需查询/修改传感器类型曲线、输出回路、量程、Zone 与保存。

        按当前规划，不包含 Filter、Excitation 与 Reversal 参数。
        """

        if action == "read_setup":
            return self._read_setup()
        if action == "apply_setup":
            return self._apply_setup(dict(payload or {}))
        if action == "save_config":
            return self._save_config()
        return super().execute_action(action, payload)

    # ---- Instrument Setup helpers ----

    def _supported_input_channels(self) -> tuple[str, ...]:
        if self._identity is not None and self._identity.is_mini:
            return protocol.INPUT_CHANNELS_MINI
        return protocol.INPUT_CHANNELS_STANDARD

    def _supported_output_channels(self) -> tuple[str, ...]:
        if self._identity is not None and self._identity.is_mini:
            return protocol.OUTPUT_CHANNELS_MINI
        return (
            protocol.OUTPUT_CHANNELS_STANDARD
            if self._output_2_enabled
            else protocol.OUTPUT_CHANNELS_MINI
        )

    def _read_setup(self) -> dict[str, Any]:
        transport = self._require_transport()
        input_channels = self._supported_input_channels()
        output_channels = self._supported_output_channels()

        sensors: dict[str, dict[str, object]] = {}
        for ch in input_channels:
            sensor_type = self._call_read(
                lambda c=ch: protocol.read_sensor_type(transport, c),
                context=f"READ:T:{ch}",
            )
            curve = self._call_read(
                lambda c=ch: protocol.read_curve_number(transport, c),
                context=f"READ:W:{ch}",
            )
            input_limit = self._call_read(
                lambda c=ch: protocol.read_input_limit_k(transport, c),
                context=f"READ:INPUT_LIMIT:{ch}",
            )
            sensors[ch] = {
                "sensor_type": sensor_type,
                "curve": curve,
                "input_limit_k": input_limit,
            }

        outputs: dict[str, dict[str, object]] = {}
        for out in output_channels:
            loop_input = self._call_read(
                lambda o=out: protocol.read_loop_binding(transport, o),
                context=f"READ:LOOP:{out}",
            )
            heater_range = self._call_read(
                lambda o=out: protocol.read_heater_range(transport, o),
                context=f"READ:RANGE:{out}",
            )
            mode = self._call_read(
                lambda o=out: protocol.read_mode(transport, o),
                context=f"READ:MODE:{out}",
            )
            pid_limit = self._call_read(
                lambda o=out: protocol.read_pid_limit_k(transport, o),
                context=f"READ:LIMIT:{out}",
            )
            zone_state = self._call_read(
                lambda o=out: protocol.read_zone_state(transport, o),
                context=f"READ:ZONE:STATE:{out}",
            )
            zones = [
                self._call_read(
                    lambda o=out, z=zone_name: protocol.read_zone_entry(transport, o, z),
                    context=f"READ:ZONE:{zone_name}:{out}",
                ).to_dict()
                for zone_name in protocol.ZONE_NAMES
            ]
            control_id = _OUTPUT_TO_CONTROL[out]
            binding = self._control_bindings.get(control_id)
            outputs[out] = {
                "control_id": control_id,
                "loop_input": loop_input,
                "expected_input": binding.input_channel if binding is not None else "",
                "heater_range": heater_range,
                "mode": "Auto" if mode == "A" else "Manual",
                "pid_limit_k": pid_limit,
                "zone_state": zone_state,
                "zone_enabled": zone_state != "OFF",
                "zones": zones,
            }

        return {
            "model": self._identity.model if self._identity is not None else "Kelvinion",
            "firmware": self._identity.firmware if self._identity is not None else "",
            "is_mini": bool(self._identity.is_mini) if self._identity is not None else False,
            "input_channels": list(input_channels),
            "output_channels": list(output_channels),
            "sensor_types": list(protocol.SENSOR_TYPES),
            "heater_ranges": list(protocol.HEATER_RANGES),
            "curve_min": protocol.MIN_CURVE_NUMBER,
            "curve_max": protocol.MAX_CURVE_NUMBER,
            "sensors": sensors,
            "outputs": outputs,
        }

    def _apply_setup(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        transport = self._require_transport()
        supported_inputs = set(self._supported_input_channels())
        supported_outputs = set(self._supported_output_channels())

        # 先完成全部字段静态校验，任何一项非法则在写任何指令前中止。
        raw_sensors = payload.get("sensors", {})
        if not isinstance(raw_sensors, Mapping):
            raise InstrumentError(
                "sensors must be a mapping",
                "KELVINION_INVALID_SETUP",
                self.config.id,
            )
        sensor_ops: list[tuple[str, str, Any]] = []
        for raw_ch, raw_spec in raw_sensors.items():
            ch = str(raw_ch).strip().upper()
            if ch not in supported_inputs:
                raise InstrumentError(
                    f"Unsupported Kelvinion input channel: {ch!r}",
                    "KELVINION_INVALID_SETUP",
                    ch,
                )
            if not isinstance(raw_spec, Mapping):
                raise InstrumentError(
                    f"Input {ch} setup entry must be a mapping",
                    "KELVINION_INVALID_SETUP",
                    ch,
                )
            unknown_keys = sorted(set(raw_spec) - {"sensor_type", "curve"})
            if unknown_keys:
                raise InstrumentError(
                    f"Input {ch} contains unsupported setup keys: {', '.join(unknown_keys)}",
                    "KELVINION_INVALID_SETUP",
                    ch,
                )
            if "sensor_type" in raw_spec:
                raw_sensor_type = str(raw_spec["sensor_type"]).strip().upper()
                sensor_type = protocol.SENSOR_TYPE_ALIASES.get(raw_sensor_type)
                if sensor_type is None:
                    raise InstrumentError(
                        f"Input {ch} sensor_type must be one of {', '.join(protocol.SENSOR_TYPES)}",
                        "KELVINION_INVALID_SETUP",
                        ch,
                    )
                sensor_ops.append((ch, "sensor_type", sensor_type))
            if "curve" in raw_spec:
                curve = raw_spec["curve"]
                if (
                    isinstance(curve, bool)
                    or not isinstance(curve, int)
                    or not protocol.MIN_CURVE_NUMBER <= curve <= protocol.MAX_CURVE_NUMBER
                ):
                    raise InstrumentError(
                        f"Input {ch} curve must be an integer between "
                        f"{protocol.MIN_CURVE_NUMBER} and {protocol.MAX_CURVE_NUMBER}",
                        "KELVINION_INVALID_SETUP",
                        ch,
                    )
                sensor_ops.append((ch, "curve", curve))

        raw_outputs = payload.get("outputs", {})
        if not isinstance(raw_outputs, Mapping):
            raise InstrumentError(
                "outputs must be a mapping",
                "KELVINION_INVALID_SETUP",
                self.config.id,
            )
        output_ops: list[tuple[str, str, Any]] = []
        zone_ops: list[tuple[str, str, str, Any]] = []
        for raw_out, raw_spec in raw_outputs.items():
            out = str(raw_out).strip().upper()
            if out not in supported_outputs:
                raise InstrumentError(
                    f"Unsupported or disabled Kelvinion output channel: {out!r}",
                    "KELVINION_INVALID_SETUP",
                    out,
                )
            if not isinstance(raw_spec, Mapping):
                raise InstrumentError(
                    f"Output {out} setup entry must be a mapping",
                    "KELVINION_INVALID_SETUP",
                    out,
                )
            unknown_keys = sorted(
                set(raw_spec)
                - {"loop_input", "heater_range", "pid_limit_k", "zone_enabled", "zones"}
            )
            if unknown_keys:
                raise InstrumentError(
                    f"Output {out} contains unsupported setup keys: {', '.join(unknown_keys)}",
                    "KELVINION_INVALID_SETUP",
                    out,
                )
            if "loop_input" in raw_spec:
                loop_in = str(raw_spec["loop_input"]).strip().upper()
                if loop_in == "NULL":
                    loop_in = "OFF"
                if loop_in != "OFF" and loop_in not in supported_inputs:
                    raise InstrumentError(
                        f"Output {out} loop_input {loop_in!r} is not available on this model",
                        "KELVINION_INVALID_SETUP",
                        out,
                    )
                output_ops.append((out, "loop_input", loop_in))
            if "heater_range" in raw_spec:
                hr = str(raw_spec["heater_range"]).strip().upper()
                if hr not in protocol.HEATER_RANGES:
                    raise InstrumentError(
                        f"Output {out} heater_range must be one of {', '.join(protocol.HEATER_RANGES)}",
                        "KELVINION_INVALID_SETUP",
                        out,
                    )
                output_ops.append((out, "heater_range", hr))
            if "pid_limit_k" in raw_spec:
                limit = self._parse_setup_number(
                    raw_spec["pid_limit_k"],
                    f"Output {out} pid_limit_k",
                    0.0,
                    protocol.HARDWARE_MAX_SETPOINT_K,
                )
                output_ops.append((out, "pid_limit_k", limit))
            if "zone_enabled" in raw_spec:
                if not isinstance(raw_spec["zone_enabled"], bool):
                    raise InstrumentError(
                        f"Output {out} zone_enabled must be true or false",
                        "KELVINION_INVALID_SETUP",
                        out,
                    )
                output_ops.append((out, "zone_enabled", raw_spec["zone_enabled"]))
            if "zones" in raw_spec:
                raw_zones = raw_spec["zones"]
                if not isinstance(raw_zones, Mapping):
                    raise InstrumentError(
                        f"Output {out} zones must be a mapping",
                        "KELVINION_INVALID_SETUP",
                        out,
                    )
                for raw_zname, raw_zfields in raw_zones.items():
                    zname = str(raw_zname).strip().upper()
                    if zname not in protocol.ZONE_NAMES:
                        raise InstrumentError(
                            f"Output {out} unknown zone {zname!r}",
                            "KELVINION_INVALID_SETUP",
                            out,
                        )
                    if not isinstance(raw_zfields, Mapping):
                        raise InstrumentError(
                            f"Output {out} {zname} entry must be a mapping",
                            "KELVINION_INVALID_SETUP",
                            out,
                        )
                    unknown_z = sorted(
                        set(raw_zfields)
                        - {"kp", "ki", "kd", "heater_range", "boundary_k"}
                    )
                    if unknown_z:
                        raise InstrumentError(
                            f"Output {out} {zname} contains unsupported keys: {', '.join(unknown_z)}",
                            "KELVINION_INVALID_SETUP",
                            out,
                        )
                    if "kp" in raw_zfields:
                        val = self._parse_setup_number(
                            raw_zfields["kp"], f"{out} {zname} kp", 0.0, 1000.0
                        )
                        zone_ops.append((out, zname, "KP", val))
                    if "ki" in raw_zfields:
                        val = self._parse_setup_number(
                            raw_zfields["ki"], f"{out} {zname} ki", 0.0, 1000.0
                        )
                        zone_ops.append((out, zname, "KI", val))
                    if "kd" in raw_zfields:
                        val = self._parse_setup_number(
                            raw_zfields["kd"], f"{out} {zname} kd", 0.0, 200.0
                        )
                        zone_ops.append((out, zname, "KD", val))
                    if "heater_range" in raw_zfields:
                        hr = str(raw_zfields["heater_range"]).strip().upper()
                        if hr not in protocol.HEATER_RANGES:
                            raise InstrumentError(
                                f"Output {out} {zname} heater_range must be OFF/LOW/MED/HIGH",
                                "KELVINION_INVALID_SETUP",
                                out,
                            )
                        zone_ops.append((out, zname, "RANGE", hr))
                    if "boundary_k" in raw_zfields:
                        val = self._parse_setup_number(
                            raw_zfields["boundary_k"],
                            f"{out} {zname} boundary_k",
                            0.0,
                            protocol.HARDWARE_MAX_SETPOINT_K,
                        )
                        zone_ops.append((out, zname, "BOUNDARY", val))

        summary_parts: list[str] = []
        for ch, field, val in sensor_ops:
            try:
                if field == "sensor_type":
                    protocol.write_sensor_type_and_verify(
                        transport, ch, val, expects_ack=self._write_expects_ack
                    )
                    summary_parts.append(f"CH {ch} Type->{val}")
                else:
                    protocol.write_curve_number_and_verify(
                        transport, ch, val, expects_ack=self._write_expects_ack
                    )
                    summary_parts.append(f"CH {ch} Curve->{val}")
            except Exception as exc:
                raise InstrumentError(
                    f"Input {ch} {field} write or readback confirmation failed: {exc}",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    f"{ch}:{field}",
                ) from exc

        for out, field, val in output_ops:
            try:
                if field == "loop_input":
                    actual_loop = protocol.write_loop_binding_and_verify(
                        transport, out, val, expects_ack=self._write_expects_ack
                    )
                    control_id = _OUTPUT_TO_CONTROL[out]
                    self._active_ramps.pop(control_id, None)
                    self._last_observed_setpoints.pop(control_id, None)
                    binding = self._control_bindings.get(control_id)
                    if binding is not None and actual_loop != binding.input_channel:
                        summary_parts.append(
                            f"Out {out} Loop->{actual_loop} "
                            f"(mismatch with Scanner Input {binding.input_channel}; "
                            "update Scanner and reconnect)"
                        )
                    else:
                        summary_parts.append(f"Out {out} Loop->{actual_loop}")
                elif field == "heater_range":
                    protocol.write_heater_range_and_verify(
                        transport, out, val, expects_ack=self._write_expects_ack
                    )
                    summary_parts.append(f"Out {out} Range->{val}")
                elif field == "pid_limit_k":
                    protocol.write_pid_limit_and_verify(
                        transport, out, val, expects_ack=self._write_expects_ack
                    )
                    summary_parts.append(f"Out {out} Limit->{val:.2f} K")
                elif field == "zone_enabled":
                    state = protocol.write_zone_enabled_and_verify(
                        transport, out, val, expects_ack=self._write_expects_ack
                    )
                    summary_parts.append(f"Out {out} Zone->{state}")
            except Exception as exc:
                raise InstrumentError(
                    f"Output {out} {field} write or readback confirmation failed: {exc}",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    f"{out}:{field}",
                ) from exc

        for out, zname, zfield, val in zone_ops:
            try:
                protocol.write_zone_field_and_verify(
                    transport, out, zname, zfield, val, expects_ack=self._write_expects_ack
                )
                summary_parts.append(f"Out {out} {zname}.{zfield}->{val}")
            except Exception as exc:
                raise InstrumentError(
                    f"Output {out} {zname} {zfield} write or readback confirmation failed: {exc}",
                    "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                    f"{out}:{zname}:{zfield}",
                ) from exc

        setup = self._read_setup()
        setup["summary"] = (
            "Kelvinion updated: " + ", ".join(summary_parts)
            if summary_parts
            else "Kelvinion: no setup changes"
        )
        return setup

    def _save_config(self) -> dict[str, Any]:
        transport = self._require_transport()
        try:
            protocol.save_configuration(
                transport,
                expects_ack=self._write_expects_ack,
            )
            # 确认保存后串口链路仍响应正常。
            protocol.identify(transport)
        except Exception as exc:
            raise InstrumentError(
                f"Failed to confirm Kelvinion [SET:SAVE]: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                "SET:SAVE",
            ) from exc
        return {
            "summary": "Saved Kelvinion settings to non-volatile memory ([SET:SAVE]).",
        }

    # ---- Internal helpers ----

    def _resolve_controls(
        self,
        config,
    ) -> tuple[frozenset[str], dict[str, _ControlBinding]]:
        panels = tuple(getattr(config, "panels", ()) or ())
        declared: set[str] = set()
        bindings: dict[str, _ControlBinding] = {}
        for panel in panels:
            if getattr(panel, "template", "") != "controller":
                continue
            control_id = str(getattr(panel, "control_id", "") or "output_1").strip()
            if control_id not in _CONTROL_TO_OUTPUT:
                raise InstrumentError(
                    f"Unsupported Kelvinion control endpoint: {control_id!r}",
                    "KELVINION_INVALID_CONFIGURATION",
                    control_id,
                )
            declared.add(control_id)
            if not getattr(panel, "enabled", True):
                continue
            reading_key = str(getattr(panel, "reading", "") or config.main_reading)
            input_ch = _READING_TO_INPUT.get(reading_key)
            if input_ch is None:
                raise InstrumentError(
                    f"Controller panel {control_id} references unsupported reading {reading_key!r}",
                    "KELVINION_INVALID_CONFIGURATION",
                    control_id,
                )
            min_val = float(getattr(panel, "min_value", config.min_value))
            max_val = float(getattr(panel, "max_value", config.max_value))
            max_rate = float(
                getattr(panel, "max_rate_per_minute", config.max_rate_per_minute)
            )
            role = str(getattr(panel, "role", "none") or "none")
            bindings[control_id] = _ControlBinding(
                control_id=control_id,
                output=_CONTROL_TO_OUTPUT[control_id],
                input_channel=input_ch,
                min_value=min_val,
                max_value=max_val,
                max_rate_per_minute=max_rate,
                role=role,
            )
        if not panels and getattr(config, "control_enabled", True):
            # 兼容最小单元测试配置（未提供 panels 元组时默认单回路 output_1）。
            declared.add("output_1")
            bindings["output_1"] = _ControlBinding(
                control_id="output_1",
                output="A",
                input_channel=self._primary_input,
                min_value=float(config.min_value),
                max_value=float(config.max_value),
                max_rate_per_minute=float(config.max_rate_per_minute),
                role="sample_temp",
            )
        return frozenset(declared), bindings

    def _select_primary_control(self) -> str | None:
        for binding in self._control_bindings.values():
            if binding.role in {"sample_temp", "field"}:
                return binding.control_id
        if "output_1" in self._control_bindings:
            return "output_1"
        if self._control_bindings:
            return next(iter(self._control_bindings))
        return None

    def _validate_model_capabilities(self, identity: protocol.KelvinionIdentity) -> None:
        if not identity.is_mini:
            return
        if self._output_2_enabled or "output_2" in self._control_bindings:
            raise InstrumentError(
                "Connected device is Kelvinion Mini (1 output, Inputs A-B only), "
                "but Output 2 (Heater B) is enabled in configuration",
                "KELVINION_MODEL_CAPABILITY_MISMATCH",
                self.config.id,
            )
        unsupported_inputs = [
            ch for ch in self._polled_inputs if ch not in protocol.INPUT_CHANNELS_MINI
        ]
        if unsupported_inputs:
            raise InstrumentError(
                "Connected device is Kelvinion Mini (Inputs A-B only), but configuration "
                f"requests Input(s) {', '.join(unsupported_inputs)}",
                "KELVINION_MODEL_CAPABILITY_MISMATCH",
                self.config.id,
            )

    def _read_temperatures(
        self,
        transport: protocol.KelvinionTransport,
        *,
        channels: tuple[str, ...],
        required_inputs: set[str],
    ) -> dict[str, float | None]:
        temperatures: dict[str, float | None] = {}
        for ch in channels:
            try:
                temperatures[ch] = protocol.read_temperature_k(transport, ch)
            except protocol.KelvinionSensorFault as exc:
                if ch in required_inputs:
                    raise SafetyViolation(
                        str(exc),
                        "KELVINION_SENSOR_FAULT",
                        ch,
                    ) from exc
                temperatures[ch] = None
            except protocol.KelvinionProtocolError as exc:
                raise InstrumentError(
                    str(exc),
                    "KELVINION_INVALID_RESPONSE",
                    f"READ:K:{ch}",
                ) from exc
            except Exception as exc:
                raise InstrumentError(
                    f"Failed reading Temp {ch}: {exc}",
                    "KELVINION_IO_ERROR",
                    f"READ:K:{ch}",
                ) from exc
        return temperatures

    def _verify_output_ready_for_write(
        self,
        transport: protocol.KelvinionTransport,
        binding: _ControlBinding,
        target_k: float,
    ) -> None:
        actual_input = self._call_read(
            lambda: protocol.read_loop_binding(transport, binding.output),
            context=f"READ:LOOP:{binding.output}",
        )
        if actual_input != binding.input_channel:
            raise SafetyViolation(
                f"Output {binding.output} is bound to Input {actual_input!r}, "
                f"expected Input {binding.input_channel!r}",
                "KELVINION_LOOP_CONFIGURATION_MISMATCH",
                binding.output,
            )
        mode = self._call_read(
            lambda: protocol.read_mode(transport, binding.output),
            context=f"READ:MODE:{binding.output}",
        )
        if mode != "A":
            raise SafetyViolation(
                f"Output {binding.output} is in Manual mode (M); switch to Auto PID (A) before commanding a temperature target",
                "KELVINION_MODE_MISMATCH",
                binding.output,
            )
        pid_limit = self._call_read(
            lambda: protocol.read_pid_limit_k(transport, binding.output),
            context=f"READ:LIMIT:{binding.output}",
        )
        curve_limit = self._call_read(
            lambda: protocol.read_input_limit_k(transport, binding.input_channel),
            context=f"READ:INPUT_LIMIT:{binding.input_channel}",
        )
        hardware_limit = min(pid_limit, curve_limit)
        if hardware_limit <= 0.0 or target_k > hardware_limit:
            raise SafetyViolation(
                f"Output {binding.output} target {target_k:.2f} K exceeds hardware limit "
                f"{hardware_limit:.2f} K (PID limit {pid_limit:.2f} K, Input {binding.input_channel} curve limit {curve_limit:.2f} K)",
                "KELVINION_HARDWARE_TARGET_LIMIT",
                binding.output,
            )

    def _validate_target_value(self, binding: _ControlBinding, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SafetyViolation(
                f"{binding.control_id} target must be a finite number",
                "LOCAL_TARGET_LIMIT",
                binding.control_id,
            )
        low = max(protocol.HARDWARE_MIN_SETPOINT_K, binding.min_value)
        high = min(protocol.HARDWARE_MAX_SETPOINT_K, binding.max_value)
        if not low <= float(value) <= high:
            raise SafetyViolation(
                f"{binding.control_id} target {value:g} K is outside the allowed range [{low:g}, {high:g}] K",
                "LOCAL_TARGET_LIMIT",
                binding.control_id,
            )
        try:
            return require_precision(float(value), 2, "Setpoint")
        except ValueError as exc:
            raise SafetyViolation(str(exc), "SETPOINT_PRECISION", binding.control_id) from exc

    def _validate_rate_value(self, binding: _ControlBinding, rate_per_minute: float) -> float:
        if (
            isinstance(rate_per_minute, bool)
            or not isinstance(rate_per_minute, (int, float))
            or not math.isfinite(rate_per_minute)
        ):
            raise SafetyViolation(
                f"{binding.control_id} ramp rate must be a finite number",
                "LOCAL_RATE_LIMIT",
                binding.control_id,
            )
        max_rate = min(protocol.HARDWARE_MAX_RAMP_K_PER_MIN, binding.max_rate_per_minute)
        if not protocol.HARDWARE_MIN_RAMP_K_PER_MIN <= float(rate_per_minute) <= max_rate:
            raise SafetyViolation(
                f"{binding.control_id} ramp rate {rate_per_minute:g} K/min is outside the allowed "
                f"range [{protocol.HARDWARE_MIN_RAMP_K_PER_MIN:g}, {max_rate:g}] K/min",
                "LOCAL_RATE_LIMIT",
                binding.control_id,
            )
        try:
            return require_precision(float(rate_per_minute), 2, "Ramp rate")
        except ValueError as exc:
            raise SafetyViolation(str(exc), "SETPOINT_PRECISION", binding.control_id) from exc

    def _record_commanded_target(
        self,
        control_id: str,
        *,
        commanded_target: float,
        before_setpoint: float,
        after_setpoint: float,
        rate_k_per_min: float,
    ) -> None:
        now = self._clock()
        self._last_observed_setpoints[control_id] = after_setpoint
        if abs(after_setpoint - commanded_target) <= _SETPOINT_REACHED_TOL_K:
            self._active_ramps.pop(control_id, None)
            return
        delta = commanded_target - before_setpoint
        direction = 1 if delta > 0 else (-1 if delta < 0 else (1 if commanded_target > after_setpoint else -1))
        self._active_ramps[control_id] = _ActiveRamp(
            commanded_target=commanded_target,
            start_setpoint=before_setpoint,
            last_setpoint=after_setpoint,
            rate_k_per_min=rate_k_per_min,
            last_progress_time=now,
            direction=direction,
        )

    def _evaluate_loop_motion(
        self,
        control_id: str,
        working_setpoint: float,
        ramp_rate_k_per_min: float,
        now: float,
    ) -> tuple[float, bool, bool, str]:
        """UNVERIFIED_PROTOCOL (HV-03): 根据爬坡工作设定值推算 (target, moving, ready, ramp_state)。

        若爬坡停滞或工作设定值反向偏离原目标，严禁丢弃原目标或误报 ``ready=True``：必须保留
        ``commanded_target``，报告 ``moving=False``、``ready=False``、``ramp_state='Stalled'``，
        使核心稳态判定启动超时计时且绝不会在错误温度误判稳定。
        """

        ramp = self._active_ramps.get(control_id)
        if ramp is not None:
            if abs(working_setpoint - ramp.commanded_target) <= _SETPOINT_REACHED_TOL_K:
                self._active_ramps.pop(control_id, None)
                self._last_observed_setpoints[control_id] = working_setpoint
                return working_setpoint, False, True, "Idle"

            # 检测前面板人工反向改写或越过目标：保留原目标并标记停滞/未就绪，避免在错误温度判稳。
            overshot = (
                (ramp.direction > 0 and working_setpoint > ramp.commanded_target + _EXTERNAL_OVERRIDE_TOL_K)
                or (ramp.direction < 0 and working_setpoint < ramp.commanded_target - _EXTERNAL_OVERRIDE_TOL_K)
            )
            reversed_move = (
                (working_setpoint - ramp.last_setpoint) * ramp.direction
                < -_EXTERNAL_OVERRIDE_TOL_K
            )
            if overshot or reversed_move:
                ramp.stalled = True
                ramp.last_setpoint = working_setpoint
                self._last_observed_setpoints[control_id] = working_setpoint
                return ramp.commanded_target, False, False, "Stalled"

            prev_distance = abs(ramp.last_setpoint - ramp.commanded_target)
            curr_distance = abs(working_setpoint - ramp.commanded_target)
            progressed = (prev_distance - curr_distance) >= _SETPOINT_STEP_EPS_K
            if progressed:
                ramp.last_setpoint = working_setpoint
                ramp.last_progress_time = now
                ramp.stalled = False
                self._last_observed_setpoints[control_id] = working_setpoint
                return ramp.commanded_target, True, False, "Ramping"

            if ramp.stalled:
                self._last_observed_setpoints[control_id] = working_setpoint
                return ramp.commanded_target, False, False, "Stalled"

            eff_rate = max(
                0.05,
                ramp_rate_k_per_min if ramp_rate_k_per_min > 0.0 else ramp.rate_k_per_min,
            )
            step_seconds = 0.6 / eff_rate  # 0.01 K / (eff_rate / 60 s)
            stall_timeout = max(_MIN_RAMP_STALL_TIMEOUT_SECONDS, 4.0 * step_seconds)
            if now - ramp.last_progress_time >= stall_timeout:
                ramp.stalled = True
                self._last_observed_setpoints[control_id] = working_setpoint
                return ramp.commanded_target, False, False, "Stalled"

            self._last_observed_setpoints[control_id] = working_setpoint
            return ramp.commanded_target, True, False, "Ramping"

        previous = self._last_observed_setpoints.get(control_id)
        self._last_observed_setpoints[control_id] = working_setpoint
        if previous is not None and abs(working_setpoint - previous) >= _SETPOINT_STEP_EPS_K:
            return working_setpoint, True, False, "Ramping"
        return working_setpoint, False, True, "Idle"

    def _require_control_binding(self, control: str) -> _ControlBinding:
        cid = str(control).strip()
        binding = self._control_bindings.get(cid)
        if binding is None:
            raise InstrumentError(
                f"Kelvinion control {cid!r} is not enabled",
                "KELVINION_UNKNOWN_CONTROL",
                cid,
            )
        return binding

    def _require_transport(self) -> protocol.KelvinionTransport:
        if self._transport is None:
            raise InstrumentError(
                "Kelvinion is not connected",
                "NOT_CONNECTED",
                self._address,
            )
        return self._transport

    def _call_read(self, fn: Callable[[], Any], *, context: str) -> Any:
        try:
            return fn()
        except protocol.KelvinionProtocolError as exc:
            raise InstrumentError(
                str(exc),
                "KELVINION_INVALID_RESPONSE",
                context,
            ) from exc
        except Exception as exc:
            raise InstrumentError(
                f"Kelvinion communication failed during {context}: {exc}",
                "KELVINION_IO_ERROR",
                context,
            ) from exc

    def _parse_bool(self, value: Any, name: str) -> bool:
        if not isinstance(value, bool):
            raise InstrumentError(
                f"{name} must be true or false",
                "KELVINION_INVALID_CONFIGURATION",
                self.config.id,
            )
        return value

    def _parse_baud_rate(self, value: Any) -> int:
        if isinstance(value, bool):
            raise InstrumentError(
                "baud_rate must be a supported serial baud rate",
                "KELVINION_INVALID_CONFIGURATION",
                self.config.id,
            )
        if isinstance(value, int):
            baud = value
        elif isinstance(value, str) and value.strip().isdigit():
            baud = int(value.strip())
        else:
            raise InstrumentError(
                f"Invalid baud_rate: {value!r}",
                "KELVINION_INVALID_CONFIGURATION",
                self.config.id,
            )
        if baud not in protocol.ALLOWED_BAUD_RATES:
            raise InstrumentError(
                f"Unsupported baud_rate {baud}; allowed: {', '.join(str(b) for b in protocol.ALLOWED_BAUD_RATES)}",
                "KELVINION_INVALID_CONFIGURATION",
                self.config.id,
            )
        return baud

    def _parse_timeout_ms(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InstrumentError(
                "visa_timeout_ms must be a positive integer",
                "KELVINION_INVALID_CONFIGURATION",
                self.config.id,
            )
        return value

    def _parse_setup_number(
        self,
        value: Any,
        label: str,
        minimum: float,
        maximum: float,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InstrumentError(
                f"{label} must be a finite number",
                "KELVINION_INVALID_SETUP",
                label,
            )
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise InstrumentError(
                f"{label} must be between {minimum:g} and {maximum:g}",
                "KELVINION_INVALID_SETUP",
                label,
            )
        return number
