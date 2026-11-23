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
        self.channel_sensors: dict[str, tuple[int | None, int | None]] = {
            "A": (1, None),
            "B": (0, 2),
            "C": (20, None),
            "D": (0, 5),
        }

    def query(self, command: str) -> str:
        self.commands.append(command)
        if command == "*IDN?":
            return f"Cryo-con,{self.model},204683,1.01A"
        if command == STATUS_QUERY:
            return self.status
        if command == MEASUREMENT_TEMPERATURE_QUERY:
            return self.measurement_temperature
        if command == "INPUT? B;:INPUT B:UNITS?;ALARM?":
            return "5.5;K;--"
        if command == "INPUT? A":
            return self.status.split(';')[0]
        if command == "INPUT? A;:INPUT A:UNITS?;ALARM?":
            return ';'.join(self.status.split(';')[:3])
        if command.startswith("INPUT ") and command.endswith(":ISENIX?;USENIX?"):
            channel = command[len("INPUT ") : command.index(":")]
            factory_ix, user_ix = self.channel_sensors[channel]
            f_text = "NONE" if factory_ix is None else str(factory_ix)
            u_text = "NONE" if user_ix is None else str(user_ix)
            return f"{f_text};{u_text}"
        if command.startswith("INPUT ") and (
            command.endswith(";ISENIX?") or command.endswith(";USENIX?")
        ):
            head, rest = command[len("INPUT ") :].split(":", 1)
            key, tail = rest.split(" ", 1)
            requested = int(tail.split(";", 1)[0])
            if key == self.mismatch_parameter:
                return str(requested + 1)
            factory_ix, user_ix = self.channel_sensors[head]
            if key == "ISENIX":
                self.channel_sensors[head] = (requested, None)
            else:
                self.channel_sensors[head] = (None, requested)
            return str(requested)
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
    def test_a_controls_loop1_b_is_auxiliary_and_firmware_no_is_ready(self):
        transport = FakeTransport(status=_status(temperature_a="300", temperature_b="297.25", setpoint="300", source="CHA", ramping="NO"))
        config = _config(auxiliary_readings=["temp_b", "heater_output", "heater_range"])
        config.main_reading = "temp_a"
        config.address = "ASRL4::INSTR"
        config.extras['baud_rate'] = '9600'
        controller = self._controller(transport, config=config)
        controller.open()
        status = controller.read_status()
        self.assertEqual(status['value'], 300)
        self.assertEqual(status['auxiliary']['temp_b'], 297.25)
        self.assertTrue(status['ready'])
        transport.commands.clear()
        self.assertEqual(controller.read_measurement()['value'], 300)
        self.assertEqual(transport.commands, ['INPUT? A'])
        controller.hold(control='loop_1')
        self.assertIn('INPUT? A;:INPUT A:UNITS?;ALARM?', transport.commands)
        self.assertEqual(transport.commands[-1], 'LOOP 1:SETPT 300;SETPT?')
        self.assertFalse(any('LOOP 2' in command for command in transport.commands))
        controller.close()

    def test_site_pid_derivative_is_preserved_without_ratio_clamping(self):
        zones = Cryocon22C24CController._parse_pid_zones([{'temperature': 500, 'p_gain': 40, 'i_gain': 30, 'd_gain': 30}], 400)
        self.assertEqual(zones[0].i_gain, 30)
        self.assertEqual(zones[0].d_gain, 30)

    def _controller(
        self,
        transport: FakeTransport,
        *,
        config=None,
    ) -> Cryocon22C24CController:
        return Cryocon22C24CController(
            _config() if config is None else config,
            transport_factory=lambda _address, _timeout, **_options: transport,
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
            "INPUT? B;:INPUT B:UNITS?;ALARM?",
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


    def test_accepts_non_usb_visa_addresses_and_trailing_semicolons(self) -> None:
        config = _config()
        config.address = "ASRL3::INSTR"
        transport = FakeTransport(
            status=_status() + ";",
            measurement_temperature="6.875 K;",
        )
        controller = self._controller(transport, config=config)
        controller.open()
        reading = controller.read_status()
        self.assertEqual(reading["value"], 4.2)
        self.assertEqual(controller.read_measurement(), {"value": 6.875})

    def test_hold_rate_above_max_rate_is_rejected_at_init(self) -> None:
        config = _config()
        config.extras["hold_rate_per_minute"] = 12.0
        with self.assertRaises(SafetyViolation) as caught:
            self._controller(FakeTransport(), config=config)
        self.assertEqual(caught.exception.code, "CRYOCON_RATE_LIMIT")

    def test_monitor_panel_keeps_both_channels_for_a_or_b_primary(self) -> None:
        manifest_path = ROOT / "instruments" / "cryocon_22c_24c" / "instrument.toml"
        with manifest_path.open("rb") as handle:
            manifest = tomllib.load(handle)
        monitor = next(panel for panel in manifest["panels"] if panel["id"] == "monitor")
        self.assertEqual(
            monitor["readings"],
            ["temp_a", "temp_b", "heater_output", "heater_range"],
        )

    def test_primary_a_target_above_monitor_a_max_is_rejected_before_write(self) -> None:
        transport = FakeTransport(
            status=_status(temperature_a="300", temperature_b="297.25", setpoint="300", source="CHA", ramping="NO")
        )
        config = _config(auxiliary_readings=["temp_b", "heater_output", "heater_range"])
        config.main_reading = "temp_a"
        controller = self._controller(transport, config=config)
        controller.open()
        transport.commands.clear()
        with self.assertRaises(SafetyViolation) as caught:
            controller.set_target(350.0, 1.0, control="loop_1")
        self.assertEqual(caught.exception.code, "CRYOCON_TARGET_LIMIT")
        self.assertEqual(transport.commands, [])

        # Hold must also reject a fresh Temp A reading above Temp A Maximum without writing SETPT.
        transport.status = _status(
            temperature_a="321", temperature_b="297.25", setpoint="300", source="CHA", ramping="NO"
        )
        transport.commands.clear()
        with self.assertRaises(SafetyViolation) as hold_caught:
            controller.hold(control="loop_1")
        self.assertEqual(hold_caught.exception.code, "CRYOCON_TARGET_LIMIT")
        self.assertEqual(transport.commands, ["INPUT? A;:INPUT A:UNITS?;ALARM?"])

        # Status validation rejects a controller setpoint above Temp A Maximum when CH A is primary.
        bad_setpoint_transport = FakeTransport(
            status=_status(temperature_a="300", temperature_b="297.25", setpoint="350", source="CHA", ramping="NO")
        )
        bad_controller = self._controller(bad_setpoint_transport, config=config)
        with self.assertRaises(SafetyViolation) as status_caught:
            bad_controller.open()
        self.assertEqual(status_caught.exception.code, "CRYOCON_TARGET_LIMIT")

        # Primary B is independent of Temp A max so 350 K target remains allowed.
        b_transport = FakeTransport()
        b_controller = self._controller(b_transport)
        b_controller.open()
        b_transport.commands.clear()
        b_controller.set_target(350.0, 1.0, control="loop_1")
        self.assertEqual(b_transport.commands[-1], "LOOP 1:SETPT 350;SETPT?")

    def test_invalid_baud_rate_or_main_reading_raises_value_error(self) -> None:
        for bad_baud in (9600.5, True, "4800", "abc"):
            with self.subTest(baud_rate=bad_baud):
                config = _config()
                config.extras["baud_rate"] = bad_baud
                with self.assertRaisesRegex(ValueError, "baud rate"):
                    self._controller(FakeTransport(), config=config)

        bad_reading = _config()
        bad_reading.main_reading = "temp_c"
        with self.assertRaisesRegex(ValueError, "main_reading"):
            self._controller(FakeTransport(), config=bad_reading)

    def test_setup_switches_curves_on_24c_and_22c_and_software_pid_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pid_dir = Path(temp)
            pid_a = pid_dir / "rod_a.toml"
            pid_b = pid_dir / "rod_b.toml"
            pid_bad = pid_dir / "empty.toml"
            pid_a.write_text(
                "[[zones]]\ntemperature = 400.0\np_gain = 10.0\ni_gain = 20.0\nd_gain = 1.0\nheater_range = 'LOW'\n",
                encoding="utf-8",
            )
            pid_b.write_text(
                "[[zones]]\ntemperature = 400.0\np_gain = 55.0\ni_gain = 12.0\nd_gain = 2.0\nheater_range = 'MID'\n",
                encoding="utf-8",
            )
            pid_bad.write_text("zones = []\n", encoding="utf-8")

            # Model 24C exposes A, B, C, D and switches both curves and software PID table.
            transport_24c = FakeTransport(model="24C")
            controller_24c = self._controller(transport_24c, config=_config(pid_file=pid_a))
            controller_24c.open()

            setup = controller_24c.execute_action("read_setup")
            self.assertEqual(setup["model"], "24C")
            self.assertEqual(setup["channels"], ["A", "B", "C", "D"])
            self.assertEqual(setup["channel_sensors"]["C"]["factory_index"], 20)
            self.assertEqual(setup["channel_sensors"]["D"]["user_index"], 5)
            self.assertEqual(setup["current_pid_file"], "rod_a.toml")
            by_name = {item["name"]: item for item in setup["pid_tables"]}
            self.assertTrue(by_name["rod_a.toml"]["valid"])
            self.assertTrue(by_name["rod_b.toml"]["valid"])
            self.assertFalse(by_name["empty.toml"]["valid"])

            applied = controller_24c.execute_action(
                "apply_setup",
                {
                    "channels": {
                        "A": {"kind": "factory", "index": 12},
                        "C": {"kind": "user", "index": 3},
                        "D": {"kind": "factory", "index": 0},
                    },
                    "pid_file": "rod_b.toml",
                },
            )
            self.assertEqual(applied["channel_sensors"]["A"]["factory_index"], 12)
            self.assertEqual(applied["channel_sensors"]["C"]["user_index"], 3)
            self.assertEqual(applied["channel_sensors"]["D"]["factory_index"], 0)
            self.assertEqual(applied["config_updates"]["pid_file"], str(pid_b.resolve()))
            self.assertIn("INPUT C:USENIX 3;USENIX?", transport_24c.commands)

            # Before core confirms disk persistence via apply_config_updates(), the active
            # PID table must remain rod_a.toml (zero drift if disk save fails), and retrying
            # Apply must still emit config_updates instead of reporting "no setup changes".
            self.assertEqual(applied["current_pid_file"], "rod_a.toml")
            self.assertEqual(
                controller_24c.execute_action("read_setup")["current_pid_file"],
                "rod_a.toml",
            )
            transport_24c.commands.clear()
            controller_24c.set_target(100.0, 2.0, control="loop_1")
            self.assertIn("LOOP 1:PGAIN 10;PGAIN?", transport_24c.commands)
            self.assertIn("LOOP 1:RANGE LOW;RANGE?", transport_24c.commands)

            retried = controller_24c.execute_action("apply_setup", {"pid_file": "rod_b.toml"})
            self.assertEqual(retried["config_updates"]["pid_file"], str(pid_b.resolve()))
            self.assertIn("PID table -> rod_b.toml", retried["summary"])

            # Once core persists configs/instruments/*.toml and invokes apply_config_updates(),
            # the new PID table activates and takes effect on the next set_target().
            committed = controller_24c.apply_config_updates(retried["config_updates"])
            self.assertEqual(committed["current_pid_file"], "rod_b.toml")
            self.assertEqual(
                controller_24c.execute_action("read_setup")["current_pid_file"],
                "rod_b.toml",
            )
            transport_24c.commands.clear()
            controller_24c.set_target(100.0, 2.0, control="loop_1")
            self.assertIn("LOOP 1:PGAIN 55;PGAIN?", transport_24c.commands)
            self.assertIn("LOOP 1:RANGE MID;RANGE?", transport_24c.commands)

            # Reject invalid/empty PID table and path traversal without mutating active table.
            with self.assertRaises(InstrumentError) as bad_pid:
                controller_24c.execute_action("apply_setup", {"pid_file": "empty.toml"})
            self.assertEqual(bad_pid.exception.code, "CRYOCON_INVALID_PID_FILE")
            with self.assertRaises(InstrumentError) as escape_pid:
                controller_24c.execute_action("apply_setup", {"pid_file": "../rod_a.toml"})
            self.assertEqual(escape_pid.exception.code, "CRYOCON_INVALID_PID_FILE")

            # Readback mismatch raises INSTRUMENT_WRITE_RESULT_UNKNOWN.
            mismatch_transport = FakeTransport(model="24C", mismatch_parameter="USENIX")
            mismatch_controller = self._controller(mismatch_transport, config=_config(pid_file=pid_a))
            mismatch_controller.open()
            with self.assertRaises(InstrumentError) as mismatch:
                mismatch_controller.execute_action(
                    "apply_setup",
                    {"channels": {"C": {"kind": "user", "index": 2}}},
                )
            self.assertEqual(mismatch.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")

            # Model 22C exposes only A and B and rejects C/D.
            transport_22c = FakeTransport(model="22C")
            controller_22c = self._controller(transport_22c, config=_config(pid_file=pid_a))
            controller_22c.open()
            setup_22c = controller_22c.execute_action("read_setup")
            self.assertEqual(setup_22c["channels"], ["A", "B"])
            with self.assertRaises(InstrumentError) as bad_ch:
                controller_22c.execute_action(
                    "apply_setup",
                    {"channels": {"C": {"kind": "user", "index": 1}}},
                )
            self.assertEqual(bad_ch.exception.code, "CRYOCON_INVALID_SETUP")


if __name__ == "__main__":
    unittest.main()
