"""MultiFields Kelvinion USB-B (VISA serial), manual v3.1.1."""

from __future__ import annotations

import ast
import math
import operator
import re

import pyvisa
from pyvisa.constants import ControlFlow, Parity, StopBits

from labcontrol.instruments.base import InstrumentError, SafetyViolation, SystemInstrument


_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_IDENTITY = re.compile(r"^MultiFields Technologies\s*,\s*Kelvinion\s*,", re.I)
_OUTPUT_READS = {"target": "SETP", "rate": "RAMP", "power": "POWER", "range": "RANGE"}


def _formula_value(node: ast.AST, x: float) -> float:
    if isinstance(node, ast.Name) and node.id == "x":
        return x
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        return _BINARY[type(node.op)](_formula_value(node.left, x), _formula_value(node.right, x))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_formula_value(node.operand, x))
    raise ValueError("Use only x, numbers, parentheses and + - * /")


class Kelvinion(SystemInstrument):
    def __init__(self, config) -> None:
        super().__init__(config)
        if not re.fullmatch(r"ASRL[0-9]+::INSTR", config.address, re.I):
            raise InstrumentError("Select the Kelvinion USB serial VISA address (ASRL…::INSTR)",
                                  "KELVINION_INVALID_CONFIGURATION", config.address)
        self._timeout = config.extras["visa_timeout_ms"]
        if self._timeout >= config.operation_timeout_seconds * 1000:
            raise InstrumentError("VISA I/O timeout must be below the framework operation timeout",
                                  "INSTRUMENT_IO_TIMEOUT_TOO_HIGH", "visa_timeout_ms")
        self._inputs = {"A": config.main_reading[-1].upper()}
        self._formula = None
        if config.extras["loop2_enabled"]:
            self._inputs["B"] = config.extras["loop2_input"]
            formula = config.extras["loop2_setpoint"].strip()
            try:
                if len(formula) > 128:
                    raise ValueError("Formula must be at most 128 characters")
                self._formula = ast.parse(formula, mode="eval").body
                # 检查语法，不代入示例温度来决定公式在真实目标处是否有效。
                for node in ast.walk(self._formula):
                    if (type(node) not in (ast.Name, ast.Load, ast.Constant, ast.BinOp, ast.UnaryOp,
                                           *_BINARY, *_UNARY)
                        or isinstance(node, ast.Name) and node.id != "x"
                        or isinstance(node, ast.Constant) and type(node.value) not in (int, float)):
                        raise ValueError("Use only x, numbers, parentheses and + - * /")
            except (SyntaxError, ValueError) as exc:
                raise InstrumentError(f"Invalid Output 2 target formula: {exc}",
                                      "KELVINION_INVALID_CONFIGURATION", "loop2_setpoint") from exc
        self._manager = None
        self._session = None

    def open(self) -> None:
        try:
            self._manager = pyvisa.ResourceManager()
            self._session = self._manager.open_resource(
                self.config.address, open_timeout=self._timeout, timeout=self._timeout,
                baud_rate=115200, data_bits=8, parity=Parity.none, stop_bits=StopBits.one,
                flow_control=ControlFlow.none, read_termination="\r\n", write_termination=None,
            )
            identity = self._query("*IDN?")
            if not _IDENTITY.match(identity):
                raise InstrumentError(f"Unexpected instrument identity: {identity}",
                                      "KELVINION_IDENTITY_MISMATCH", self.config.address)
            self._check_loops()
        except InstrumentError:
            self.close()
            raise
        except (ValueError, OSError, pyvisa.errors.VisaIOError) as exc:
            self.close()
            raise InstrumentError(f"Cannot open Kelvinion: {exc}",
                                  "KELVINION_IO_ERROR", self.config.address) from exc

    def close(self) -> None:
        session, self._session = self._session, None
        manager, self._manager = self._manager, None
        try:
            if session is not None:
                session.close()
        finally:
            if manager is not None:
                manager.close()

    def _query(self, command: str) -> str:
        try:
            response = self._session.query(f"[{command}]").strip()
        except pyvisa.errors.VisaIOError as exc:
            raise InstrumentError(f"Kelvinion query failed: {exc}",
                                  "KELVINION_IO_ERROR", command) from exc
        if not re.fullmatch(r"\[[^\[\]\r\n]+\]", response):
            raise InstrumentError(f"Invalid Kelvinion response: {response!r}",
                                  "KELVINION_INVALID_RESPONSE", command)
        return response[1:-1].strip()

    def _number(self, command: str) -> float:
        response = self._query(command)
        try:
            value = float(response)
        except ValueError as exc:
            raise InstrumentError(f"Invalid numeric response: {response!r}",
                                  "KELVINION_INVALID_RESPONSE", command) from exc
        if not math.isfinite(value):
            raise InstrumentError(f"Invalid numeric response: {response!r}",
                                  "KELVINION_INVALID_RESPONSE", command)
        return value

    def _temperature(self, channel: str) -> float:
        command = f"READ:K:{channel}"
        value = self._number(command)
        if not 0 < value <= 999:
            raise InstrumentError(f"Invalid Temp {channel}: {value:g} K",
                                  "KELVINION_INPUT_STATUS", command)
        return value

    def _check_loops(self) -> None:
        for output, channel in self._inputs.items():
            actual = self._query(f"READ:LOOP:{output}")
            if actual != channel:
                raise SafetyViolation(f"Output {output} uses Input {actual}; expected {channel}",
                                      "KELVINION_LOOP_CONFIGURATION_MISMATCH", output)

    def read_measurement(self) -> dict[str, object]:
        return {"value": self._temperature(self._inputs["A"])}

    def read_status(self) -> dict[str, object]:
        current = self._temperature(self._inputs["A"])
        temperatures = {self._inputs["A"]: current}
        if "B" in self._inputs and self._inputs["B"] not in temperatures:
            temperatures[self._inputs["B"]] = self._temperature(self._inputs["B"])
        target = self._number("READ:SETP:A")
        rate = self._number("READ:RAMP:A")
        auxiliary = {}
        for key in self.config.auxiliary_readings:
            if key.startswith("temp_"):
                channel = key[-1].upper()
                # 非控制探头读数无效时不伪装为零温度；控制探头则报错。
                if channel in temperatures:
                    auxiliary[key] = temperatures[channel]
                else:
                    value = self._number(f"READ:K:{channel}")
                    auxiliary[key] = value if 0 < value <= 999 else None
            elif key == "loop_1_target":
                auxiliary[key] = target
            elif key == "loop_1_rate":
                auxiliary[key] = rate
            else:
                _, number, metric = key.split("_")
                output = "A" if number == "1" else "B"
                command = f"READ:{_OUTPUT_READS[metric]}:{output}"
                auxiliary[key] = self._query(command) if metric == "range" else self._number(command)
        return {"value": current, "target": target, "rate": rate, "auxiliary": auxiliary}

    def _check_targets(self, targets: dict[str, float]) -> None:
        # 在发送任何 SET 前检查所有参与回路，避免明知第二路越界仍先改第一路。
        for output, value in targets.items():
            if not (max(0.0, self.config.min_value) <= value <= min(999.0, self.config.max_value)):
                raise SafetyViolation(f"Output {output} target {value:g} K exceeds configured limits",
                                      "LOCAL_TARGET_LIMIT", output)
        self._check_loops()
        for output, value in targets.items():
            if self._query(f"READ:MODE:{output}") != "A":
                raise SafetyViolation(f"Output {output} must be in Auto mode",
                                      "KELVINION_MODE_MISMATCH", output)
            pid_limit = self._number(f"READ:LIMIT:{output}")
            curve_limit = self._number(f"READ:INPUT_LIMIT:{self._inputs[output]}")
            if value > min(pid_limit, curve_limit) or min(pid_limit, curve_limit) <= 0:
                raise SafetyViolation(f"Output {output} target exceeds its PID or sensor curve limit",
                                      "KELVINION_HARDWARE_TARGET_LIMIT", output)

    def _write_verified(self, setting: str, output: str, value: float) -> None:
        unit = "K" if setting == "SETP" else ""
        command = f"SET:{setting}:{output}:{value:.2f}{unit}"
        try:
            self._session.write(f"[{command}]")
            actual = self._number(f"READ:{setting}:{output}")
            if not math.isclose(actual, value, rel_tol=0, abs_tol=1e-6):
                raise InstrumentError(f"Expected {value:.2f}, read back {actual:g}")
        except (pyvisa.errors.VisaIOError, InstrumentError) as exc:
            raise InstrumentError(
                f"Cannot confirm {command}: {exc}. Earlier settings may have applied.",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN", command,
            ) from exc

    def set_target(self, value: float, rate_per_minute: float, mode: str = "Settle", *, control: str) -> None:
        if not 0.1 <= rate_per_minute <= min(10.0, self.config.max_rate_per_minute):
            raise SafetyViolation("Ramp rate outside the configured or Kelvinion limits",
                                  "LOCAL_RATE_LIMIT", self.config.id)
        if not math.isfinite(value) or any(
            not math.isclose(item, round(item, 2), rel_tol=0, abs_tol=1e-9)
            for item in (value, rate_per_minute)
        ):
            raise SafetyViolation("Kelvinion target and rate support two decimal places",
                                  "KELVINION_PARAMETER_PRECISION", self.config.id)
        targets = {"A": value}
        if self._formula is not None:
            try:
                linked = _formula_value(self._formula, value)
                if not math.isfinite(linked):
                    raise ValueError("Nonfinite target")
            except (ArithmeticError, ValueError) as exc:
                raise SafetyViolation(f"Cannot calculate Output 2 target: {exc}",
                                      "KELVINION_FORMULA_ERROR", "loop2_setpoint") from exc
            if not max(0.0, self.config.min_value) <= linked <= min(999.0, self.config.max_value):
                raise SafetyViolation(f"Output B formula target {linked:g} K exceeds configured limits",
                                      "LOCAL_TARGET_LIMIT", "B")
            targets["B"] = round(linked, 2)
        self._check_targets(targets)
        for output in targets:
            self._write_verified("RAMP", output, rate_per_minute)
        for output, target in targets.items():
            self._write_verified("SETP", output, target)

    def hold(self, *, control: str) -> None:
        # Hold 使用各自现场读数，不套联动公式，也不关闭升降温、量程或加热。
        targets = {output: round(self._temperature(channel), 2)
                   for output, channel in self._inputs.items()}
        self._check_targets(targets)
        for output, target in targets.items():
            self._write_verified("SETP", output, target)
