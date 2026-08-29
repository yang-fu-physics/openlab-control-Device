from __future__ import annotations

import math

import pyvisa

from labcontrol.instruments.base import (
    InstrumentError,
    SafetyViolation,
    SystemInstrument,
)


_INPUT_STATUS_FLAGS = (
    (1, "invalid reading"),
    (2, "old reading"),
    (16, "temperature underrange"),
    (32, "temperature overrange"),
    (64, "units zero"),
    (128, "units overrange"),
)


class LakeShore340(SystemInstrument):
    """GPIB backend for a Lake Shore Model 340 temperature controller."""

    def __init__(self, config) -> None:
        super().__init__(config)
        extras = config.extras
        self._primary_input = {
            "temp_a": "A",
            "temp_b": "B",
        }[config.main_reading]
        self._loop_1_input = extras["loop1_input"]
        self._loop_2_input = extras["loop2_input"]
        self._loop_2_enabled = extras["loop2_enabled"]
        self._visa_timeout_ms = extras["visa_timeout_ms"]

        if self._loop_1_input not in ("A", "B"):
            raise InstrumentError(
                "loop1_input must be A or B",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.id,
            )
        if self._loop_2_input not in ("A", "B"):
            raise InstrumentError(
                "loop2_input must be A or B",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.id,
            )
        if not isinstance(self._loop_2_enabled, bool):
            raise InstrumentError(
                "loop2_enabled must be true or false",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.id,
            )
        if (
            isinstance(self._visa_timeout_ms, bool)
            or not isinstance(self._visa_timeout_ms, int)
            or self._visa_timeout_ms <= 0
            or self._visa_timeout_ms
            >= config.operation_timeout_seconds * 1000
        ):
            raise InstrumentError(
                "visa_timeout_ms must be a positive integer shorter than operation_timeout_seconds",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.id,
            )
        if not config.address.upper().startswith("GPIB") or not (
            config.address.upper().endswith("::INSTR")
        ):
            raise InstrumentError(
                "Lake Shore 340 requires a GPIB VISA resource address",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.address,
            )

        self._loop_inputs = {
            1: self._loop_1_input,
            2: self._loop_2_input,
        }
        self._active_loops = (1, 2) if self._loop_2_enabled else (1,)
        matching_loops = tuple(
            loop
            for loop in self._active_loops
            if self._loop_inputs[loop] == self._primary_input
        )
        if len(matching_loops) != 1:
            raise InstrumentError(
                "main_reading must belong to exactly one enabled control loop",
                "LAKESHORE340_INVALID_CONFIGURATION",
                config.id,
            )
        self._primary_loop = matching_loops[0]
        self._auxiliary_input = (
            "B" if self._primary_input == "A" else "A"
        )
        self._role_required_inputs = {
            {
                "temp_a": "A",
                "temp_b": "B",
            }[panel.reading]
            for panel in config.panels
            if panel.enabled
            and panel.template == "controller"
            and panel.role in {"sample_temp", "field"}
        }
        self._resource_manager = None
        self._resource = None

    def open(self) -> None:
        try:
            manager = pyvisa.ResourceManager()
        except pyvisa.errors.VisaIOError as exc:
            raise InstrumentError(
                f"Cannot open {self.config.address}: {exc}",
                "LAKESHORE340_IO_ERROR",
                self.config.address,
            ) from exc
        try:
            resource = manager.open_resource(self.config.address)
        except pyvisa.errors.VisaIOError as exc:
            manager.close()
            raise InstrumentError(
                f"Cannot open {self.config.address}: {exc}",
                "LAKESHORE340_IO_ERROR",
                self.config.address,
            ) from exc

        resource.timeout = self._visa_timeout_ms
        resource.read_termination = "\r\n"
        resource.write_termination = "\r\n"
        self._resource_manager = manager
        self._resource = resource
        try:
            identity = self._query("*IDN?")
            identity_fields = [
                field.strip().upper()
                for field in identity.split(",")
            ]
            if identity_fields[:2] != ["LSCI", "MODEL340"]:
                raise InstrumentError(
                    f"Unexpected instrument identity: {identity}",
                    "LAKESHORE340_IDENTITY_MISMATCH",
                    self.config.address,
                )
            for loop in self._active_loops:
                fields = [
                    field.strip()
                    for field in self._query(f"CSET? {loop}").split(",")
                ]
                if len(fields) != 4:
                    raise InstrumentError(
                        f"Invalid CSET? {loop} response",
                        "LAKESHORE340_INVALID_RESPONSE",
                        f"CSET? {loop}",
                    )
                if (
                    fields[0].upper() != self._loop_inputs[loop]
                    or fields[1] != "1"
                    or fields[2] != "1"
                ):
                    raise InstrumentError(
                        f"Loop {loop} must be enabled on Temp {self._loop_inputs[loop]} in kelvin",
                        "LAKESHORE340_LOOP_CONFIGURATION_MISMATCH",
                        str(loop),
                    )
        except InstrumentError:
            self.close()
            raise

    def close(self) -> None:
        resource, self._resource = self._resource, None
        manager, self._resource_manager = self._resource_manager, None
        try:
            if resource is not None:
                resource.close()
        finally:
            if manager is not None:
                manager.close()

    def read_status(self) -> dict[str, object]:
        temperatures = self._read_temperatures(
            required_inputs=self._role_required_inputs
        )
        loop_1_output = self._float_query("HTR?", percent=True)
        loop_1_range = self._integer_query("RANGE?")
        if not 0 <= loop_1_range <= 5:
            raise InstrumentError(
                f"Invalid heater range: {loop_1_range}",
                "LAKESHORE340_INVALID_RESPONSE",
                "RANGE?",
            )
        loop_1_setpoint = self._float_query("SETP? 1")
        loop_1_ramping = self._boolean_query("RAMPST? 1")

        loop_2_output: float | None = None
        loop_2_setpoint: float | None = None
        loop_2_ramping: bool | None = None
        if self._loop_2_enabled:
            loop_2_output = self._float_query("AOUT? 2", percent=True)
            loop_2_setpoint = self._float_query("SETP? 2")
            loop_2_ramping = self._boolean_query("RAMPST? 2")

        values: dict[str, float | int | bool | None] = {
            f"temp_{self._auxiliary_input.casefold()}": temperatures[
                self._auxiliary_input
            ],
            "loop_1_output": loop_1_output,
            "loop_1_range": loop_1_range,
            "loop_1_setpoint": loop_1_setpoint,
            "loop_1_ramping": loop_1_ramping,
            "loop_2_output": loop_2_output,
            "loop_2_setpoint": loop_2_setpoint,
            "loop_2_ramping": loop_2_ramping,
        }
        primary_ramping = (
            loop_1_ramping
            if self._primary_loop == 1
            else bool(loop_2_ramping)
        )
        primary_setpoint = (
            loop_1_setpoint
            if self._primary_loop == 1
            else loop_2_setpoint
        )
        return {
            "value": temperatures[self._primary_input],
            "target": primary_setpoint,
            "moving": primary_ramping,
            "ready": not primary_ramping,
            "auxiliary": {
                key: values[key]
                for key in self.config.auxiliary_readings
            },
        }

    def read_measurement(self) -> dict[str, object]:
        temperatures = self._read_temperatures(
            required_inputs=self._role_required_inputs
        )
        auxiliary = {}
        auxiliary_key = f"temp_{self._auxiliary_input.casefold()}"
        if auxiliary_key in self.config.auxiliary_readings:
            auxiliary[auxiliary_key] = temperatures[self._auxiliary_input]
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
        del mode, control
        if not math.isfinite(value) or not (
            self.config.min_value <= value <= self.config.max_value
        ):
            raise SafetyViolation(
                "Target exceeds the configured safety envelope",
                "LOCAL_TARGET_LIMIT",
                self.config.id,
            )
        if (
            not math.isfinite(rate_per_minute)
            or not 0.1 <= rate_per_minute <= 100.0
            or rate_per_minute > self.config.max_rate_per_minute
        ):
            raise SafetyViolation(
                "Ramp rate exceeds the configured or Model 340 limit",
                "LOCAL_RATE_LIMIT",
                self.config.id,
            )

        rate_text = f"{rate_per_minute:.9g}"
        value_text = f"{value:.9g}"
        for loop in self._active_loops:
            self._write(f"RAMP {loop},1,{rate_text}")
        for loop in self._active_loops:
            self._write(f"SETP {loop},{value_text}")

    def hold(self, *, control: str) -> None:
        del control
        temperatures = self._read_temperatures(
            required_inputs={
                self._loop_inputs[loop]
                for loop in self._active_loops
            }
        )
        for loop in self._active_loops:
            value = float(temperatures[self._loop_inputs[loop]])
            self._write(f"SETP {loop},{value:.9g}")
        for loop in self._active_loops:
            self._write(f"RAMP {loop},0")

    def _read_temperatures(
        self,
        *,
        required_inputs: set[str],
    ) -> dict[str, float | None]:
        temperatures: dict[str, float | None] = {}
        for input_name in ("A", "B"):
            temperatures[input_name] = self._float_query(
                f"KRDG? {input_name}"
            )
            status = self._integer_query(f"RDGST? {input_name}")
            if status:
                if input_name not in required_inputs:
                    temperatures[input_name] = None
                    continue
                active = ", ".join(
                    label
                    for bit, label in _INPUT_STATUS_FLAGS
                    if status & bit
                )
                raise InstrumentError(
                    f"Temp {input_name} reading status {status}: {active}",
                    "LAKESHORE340_INPUT_STATUS",
                    input_name,
                )
        return temperatures

    def _query(self, command: str) -> str:
        if self._resource is None:
            raise InstrumentError(
                "Lake Shore 340 is not connected",
                "NOT_CONNECTED",
                self.config.address,
            )
        try:
            response = str(self._resource.query(command)).strip()
        except pyvisa.errors.VisaIOError as exc:
            raise InstrumentError(
                f"GPIB query failed for {command}: {exc}",
                "LAKESHORE340_IO_ERROR",
                command,
            ) from exc
        if not response:
            raise InstrumentError(
                f"Empty response to {command}",
                "LAKESHORE340_INVALID_RESPONSE",
                command,
            )
        return response

    def _write(self, command: str) -> None:
        if self._resource is None:
            raise InstrumentError(
                "Lake Shore 340 is not connected",
                "NOT_CONNECTED",
                self.config.address,
            )
        try:
            self._resource.write(command)
        except pyvisa.errors.VisaIOError as exc:
            raise InstrumentError(
                f"Result is unknown after GPIB write failed for {command}: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                command,
            ) from exc

    def _float_query(self, command: str, *, percent: bool = False) -> float:
        response = self._query(command)
        if percent:
            response = response.removesuffix("%").strip()
        try:
            value = float(response)
        except ValueError as exc:
            raise InstrumentError(
                f"Invalid numeric response to {command}: {response}",
                "LAKESHORE340_INVALID_RESPONSE",
                command,
            ) from exc
        if not math.isfinite(value):
            raise InstrumentError(
                f"Non-finite response to {command}: {response}",
                "LAKESHORE340_INVALID_RESPONSE",
                command,
            )
        return value

    def _integer_query(self, command: str) -> int:
        response = self._query(command)
        try:
            return int(response)
        except ValueError as exc:
            raise InstrumentError(
                f"Invalid integer response to {command}: {response}",
                "LAKESHORE340_INVALID_RESPONSE",
                command,
            ) from exc

    def _boolean_query(self, command: str) -> bool:
        value = self._integer_query(command)
        if value not in (0, 1):
            raise InstrumentError(
                f"Invalid boolean response to {command}: {value}",
                "LAKESHORE340_INVALID_RESPONSE",
                command,
            )
        return bool(value)
