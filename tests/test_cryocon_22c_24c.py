from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
PID_FIXTURE = ROOT / "tests" / "fixtures" / "cryocon_pid.toml"
CORE_SRC = ROOT.parent / "OpenLabControl" / "src"
if CORE_SRC.is_dir():
    sys.path.insert(0, str(CORE_SRC))
sys.path.insert(0, str(ROOT / "instruments"))

from labcontrol.instruments.base import (  # noqa: E402
    InstrumentError,
    SystemInstrument,
    SafetyViolation,
)
from labcontrol.package_support.loading import load_source_object  # noqa: E402
from labcontrol.models import InstrumentKind  # noqa: E402
from cryocon_22c_24c.backend import Cryocon22C24CController  # noqa: E402
from cryocon_22c_24c.cryocon_22c_24c import (  # noqa: E402
    MEASUREMENT_TEMPERATURE_QUERY,
    STATUS_QUERY,
)


def _status(
    *,
    temperature_a: str = "20.0",
    alarm_a: str = "--",
    temperature_b: str = "4.2",
    alarm_b: str = "--",
    setpoint: str = "4.0",
    rate: str = "1.0",
    ramping: str = "OFF",
    heater_range: str = "LOW",
    heater_output: str = "12.5",
    loop_error: str = "--Htr OK--",
    loop_type: str = "RAMPP",
    source: str = "B",
    control: str = "ON",
) -> str:
    return ";".join(
        (
            temperature_a,
            "K",
            alarm_a,
            temperature_b,
            "K",
            alarm_b,
            setpoint,
            rate,
            ramping,
            heater_range,
            heater_output,
            loop_error,
            loop_type,
            source,
            control,
        )
    )


class FakeTransport:
    def __init__(
        self,
        *,
        model: str = "24C",
        status: str | None = None,
        measurement_temperature: str = "6.125",
        mismatch_parameter: str = "",
    ) -> None:
        self.model = model
        self.status = _status() if status is None else status
        self.measurement_temperature = measurement_temperature
        self.mismatch_parameter = mismatch_parameter
        self.commands: list[str] = []
        self.closed = False

    def query(self, command: str) -> str:
        self.commands.append(command)
        if command == "*IDN?":
            return f"Cryo-con,{self.model},204683,1.01A"
        if command == STATUS_QUERY:
            return self.status
        if command == MEASUREMENT_TEMPERATURE_QUERY:
            return self.measurement_temperature
        if command == "INPUT B:TEMPERATURE?;UNITS?;ALARM?":
            return "5.5;K;--"
        operation = command.removeprefix("LOOP 1:")
        parameter = operation.split(" ", 1)[0]
        if ";" in command and command.endswith(f";{parameter}?"):
            requested = operation.split(" ", 1)[1].split(";", 1)[0]
            if parameter == self.mismatch_parameter:
                return "999"
            return requested
        raise AssertionError(f"Unexpected command: {command}")

    def close(self) -> None:
        self.closed = True


def _config(*, pid_file: Path = PID_FIXTURE, auxiliary_readings=None):
    return SimpleNamespace(
        id="temperature",
        display_name="Temperature",
        kind=InstrumentKind.TEMPERATURE,
        unit="K",
        main_reading="temp_b",
        auxiliary_readings=tuple(
            ["temp_a", "heater_output", "heater_range"]
            if auxiliary_readings is None
            else auxiliary_readings
        ),
        min_value=2.0,
        max_value=400.0,
        max_rate_per_minute=10.0,
        operation_timeout_seconds=10.0,
        address="USB0::0x1234::0x5678::SERIAL::INSTR",
        extras={
            "io_timeout_seconds": 1.0,
            "monitor_a_max_temperature": 320.0,
            "hold_rate_per_minute": 1.0,
            "pid_file": str(pid_file),
        },
    )


class CryoconControllerTests(unittest.TestCase):
    def _controller(
        self,
        transport: FakeTransport,
        *,
        config=None,
    ) -> Cryocon22C24CController:
        return Cryocon22C24CController(
            _config() if config is None else config,
            transport_factory=lambda _address, _timeout: transport,
        )

    def test_connect_is_read_only_and_accepts_22c_or_24c(self) -> None:
        for model in ("22C", "24C"):
            with self.subTest(model=model):
                transport = FakeTransport(model=model)
                controller = self._controller(transport)
                controller.open()
                self.assertEqual(transport.commands, ["*IDN?", STATUS_QUERY])
                controller.close()
                self.assertTrue(transport.closed)
                self.assertFalse(
                    any("CONTROL " in item or "STOP" in item for item in transport.commands)
                )

    def test_core_loader_can_import_backend_with_relative_instrument_module(
        self,
    ) -> None:
        backend = load_source_object(
            ROOT / "instruments" / "cryocon_22c_24c",
            "backend:Cryocon22C24CController",
            "cryocon_instrument_repository_test",
        )
        self.assertTrue(issubclass(backend, SystemInstrument))

    def test_status_returns_main_and_auxiliary_values(self) -> None:
        transport = FakeTransport(
            status=_status(
                temperature_a="25.125",
                temperature_b="6.75",
                setpoint="7.0",
                rate="2.5",
                ramping="ON",
                heater_range="MID",
                heater_output="17.25",
            )
        )
        controller = self._controller(transport)
        controller.open()
        reading = controller.read_status()

        self.assertEqual(reading["value"], 6.75)
        self.assertEqual(reading["target"], 7.0)
        self.assertEqual(reading["rate"], 2.5)
        self.assertFalse(reading["ready"])
        self.assertEqual(
            reading["auxiliary"],
            {
                "temp_a": 25.125,
                "heater_output": 17.25,
                "heater_range": "MID",
            },
        )
    def test_auxiliary_selection_controls_returned_values(self) -> None:
        transport = FakeTransport()
        controller = self._controller(
            transport,
            config=_config(auxiliary_readings=["heater_output"]),
        )
        controller.open()
        reading = controller.read_status()
        self.assertEqual(tuple(reading["auxiliary"]), ("heater_output",))

    def test_measurement_poll_queries_only_temp_b(self) -> None:
        transport = FakeTransport(measurement_temperature="6.875 K")
        controller = self._controller(transport)
        controller.open()
        transport.commands.clear()

        reading = controller.read_measurement()

        self.assertEqual(transport.commands, [MEASUREMENT_TEMPERATURE_QUERY])
        self.assertEqual(reading, {"value": 6.875})

    def test_set_target_selects_pid_zone_and_confirms_every_write(self) -> None:
        transport = FakeTransport()
        controller = self._controller(transport)
        controller.open()
        transport.commands.clear()

        controller.set_target(50.0, 3.0, control="loop_1")

        self.assertEqual(
            transport.commands,
            [
                "LOOP 1:PGAIN 2;PGAIN?",
                "LOOP 1:IGAIN 8;IGAIN?",
                "LOOP 1:DGAIN 2;DGAIN?",
                "LOOP 1:RANGE MID;RANGE?",
                "LOOP 1:RATE 3;RATE?",
                "LOOP 1:SETPT 50;SETPT?",
            ],
        )
        self.assertTrue(all(";" in command for command in transport.commands))

    def test_pid_temperature_is_a_strict_upper_boundary(self) -> None:
        transport = FakeTransport()
        controller = self._controller(transport)
        controller.open()

        transport.commands.clear()
        controller.set_target(9.999, 1.0, control="loop_1")
        self.assertEqual(transport.commands[0], "LOOP 1:PGAIN 1;PGAIN?")

        transport.commands.clear()
        controller.set_target(10.0, 1.0, control="loop_1")
        self.assertEqual(transport.commands[0], "LOOP 1:PGAIN 2;PGAIN?")

        transport.commands.clear()
        controller.set_target(400.0, 1.0, control="loop_1")
        self.assertEqual(transport.commands[0], "LOOP 1:PGAIN 2;PGAIN?")

    def test_hold_uses_fresh_b_reading_and_never_turns_output_off(self) -> None:
        transport = FakeTransport()
        controller = self._controller(transport)
        controller.open()
        transport.commands.clear()

        controller.hold(control="loop_1")

        self.assertEqual(
            transport.commands[0],
            "INPUT B:TEMPERATURE?;UNITS?;ALARM?",
        )
        self.assertEqual(transport.commands[-1], "LOOP 1:SETPT 5.5;SETPT?")
        self.assertFalse(
            any("CONTROL OFF" in item or "STOP" in item for item in transport.commands)
        )

    def test_sensor_alarm_or_input_a_high_limit_is_immediate_safety_error(self) -> None:
        for response, code in (
            (_status(alarm_b="SF"), "CRYOCON_SENSOR_ALARM"),
            (_status(temperature_a="321"), "CRYOCON_INPUT_A_HIGH_LIMIT"),
            (_status(loop_error="-Overtemp-"), "CRYOCON_HEATER_FAULT"),
        ):
            with self.subTest(code=code):
                controller = self._controller(FakeTransport(status=response))
                with self.assertRaises(SafetyViolation) as captured:
                    controller.open()
                self.assertEqual(captured.exception.code, code)

    def test_write_readback_mismatch_has_uncertain_result_and_is_not_replayed(self) -> None:
        transport = FakeTransport(mismatch_parameter="IGAIN")
        controller = self._controller(transport)
        controller.open()
        transport.commands.clear()

        with self.assertRaises(InstrumentError) as captured:
            controller.set_target(5.0, 1.0, control="loop_1")

        self.assertEqual(captured.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")
        self.assertEqual(
            transport.commands,
            [
                "LOOP 1:PGAIN 1;PGAIN?",
                "LOOP 1:IGAIN 4;IGAIN?",
            ],
        )

    def test_local_limits_and_pid_table_are_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "zones"):
            self._controller(
                FakeTransport(),
                config=_config(
                    pid_file=(
                        ROOT
                        / "instruments"
                        / "cryocon_22c_24c"
                        / "pid.example.toml"
                    )
                ),
            )

        controller = self._controller(FakeTransport())
        controller.open()
        with self.assertRaises(SafetyViolation):
            controller.set_target(401.0, 1.0, control="loop_1")
        with self.assertRaises(SafetyViolation):
            controller.set_target(4.0, 10.1, control="loop_1")

        short_budget = _config()
        short_budget.operation_timeout_seconds = 7.5
        with self.assertRaisesRegex(InstrumentError, "seven VISA I/O timeouts") as error:
            self._controller(FakeTransport(), config=short_budget)
        self.assertEqual(error.exception.code, "INSTRUMENT_IO_TIMEOUT_TOO_HIGH")

    def test_pid_file_missing_or_invalid_fails_before_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "missing.toml"
            with self.assertRaises(FileNotFoundError):
                self._controller(
                    FakeTransport(),
                    config=_config(pid_file=pid_file),
                )

            pid_file.write_text("[[zones]\n", encoding="utf-8")
            with self.assertRaises(tomllib.TOMLDecodeError):
                self._controller(
                    FakeTransport(),
                    config=_config(pid_file=pid_file),
                )

    def test_pid_gains_accept_instrument_zero_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "pid.toml"
            pid_file.write_text(
                "[[zones]]\n"
                "temperature = 400.0\n"
                "p_gain = 0.0\n"
                "i_gain = 0.0\n"
                "d_gain = 0.0\n",
                encoding="utf-8",
            )
            controller = self._controller(
                FakeTransport(),
                config=_config(pid_file=pid_file),
            )
            self.assertEqual(controller._pid_zones[0].p_gain, 0.0)

            pid_file.write_text(
                "[[zones]]\n"
                "temperature = 400.0\n"
                "p_gain = -0.1\n"
                "i_gain = 0.0\n"
                "d_gain = 0.0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "0–1000"):
                self._controller(
                    FakeTransport(),
                    config=_config(pid_file=pid_file),
                )


if __name__ == "__main__":
    unittest.main()
