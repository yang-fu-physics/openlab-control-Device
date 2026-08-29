from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT.parent / "OpenLabControl"
sys.path.insert(0, str(CORE_ROOT / "src"))

from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    load_instrument_manifest,
)


INSTRUMENT = (
    ROOT
    / "instruments"
    / "lakeshore340"
)
AUXILIARY_READINGS = (
    "temp_b",
    "loop_1_output",
    "loop_1_range",
    "loop_1_setpoint",
    "loop_1_ramping",
    "loop_2_output",
    "loop_2_setpoint",
    "loop_2_ramping",
)


class FakeVisaIOError(Exception):
    pass


class FakeResource:
    def __init__(self) -> None:
        self.responses = {
            "*IDN?": "LSCI,MODEL340,340001,02032001",
            "CSET? 1": "A,1,1,1",
            "CSET? 2": "B,1,1,1",
            "KRDG? A": "+4.200000E+0",
            "RDGST? A": "0",
            "KRDG? B": "+20.12500E+0",
            "RDGST? B": "0",
            "HTR?": "+050.0%",
            "RANGE?": "4",
            "SETP? 1": "+4.000000E+0",
            "RAMPST? 1": "0",
            "AOUT? 2": "-012.5",
            "SETP? 2": "+4.000000E+0",
            "RAMPST? 2": "1",
        }
        self.queries: list[str] = []
        self.writes: list[str] = []
        self.write_error = False
        self.timeout = None
        self.read_termination = None
        self.write_termination = None
        self.closed = False

    def query(self, command: str) -> str:
        self.queries.append(command)
        return self.responses[command]

    def write(self, command: str) -> None:
        self.writes.append(command)
        if self.write_error:
            raise FakeVisaIOError("write timeout")

    def close(self) -> None:
        self.closed = True


class FakeResourceManager:
    def __init__(self, resource: FakeResource) -> None:
        self.resource = resource
        self.addresses: list[str] = []
        self.closed = False

    def open_resource(self, address: str) -> FakeResource:
        self.addresses.append(address)
        return self.resource

    def close(self) -> None:
        self.closed = True


def _backend_class(manager: FakeResourceManager):
    pyvisa = types.ModuleType("pyvisa")
    pyvisa.ResourceManager = lambda: manager
    pyvisa.errors = types.SimpleNamespace(VisaIOError=FakeVisaIOError)
    spec = importlib.util.spec_from_file_location(
        "test_lakeshore340_backend",
        INSTRUMENT / "backend.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load Lake Shore 340 backend")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"pyvisa": pyvisa}):
        spec.loader.exec_module(module)
    return module.LakeShore340


def _config(
    *,
    loop2_enabled: bool = True,
    main_reading: str = "temp_a",
    loop1_input: str = "A",
    loop2_input: str = "B",
    role: str = "sample_temp",
):
    return SimpleNamespace(
        id="temperature",
        address="GPIB0::12::INSTR",
        main_reading=main_reading,
        auxiliary_readings=(
            "temp_b" if main_reading == "temp_a" else "temp_a",
            *AUXILIARY_READINGS[1:],
        ),
        panels=(
            SimpleNamespace(
                enabled=True,
                template="controller",
                role=role,
                reading=main_reading,
            ),
        ),
        min_value=1.8,
        max_value=400.0,
        max_rate_per_minute=30.0,
        operation_timeout_seconds=5.0,
        extras={
            "loop1_input": loop1_input,
            "loop2_input": loop2_input,
            "loop2_enabled": loop2_enabled,
            "visa_timeout_ms": 1000,
        },
    )


class LakeShore340Tests(unittest.TestCase):
    def test_status_and_measurement_use_expected_queries(self) -> None:
        resource = FakeResource()
        manager = FakeResourceManager(resource)
        backend = _backend_class(manager)(_config())
        backend.open()

        self.assertEqual(manager.addresses, ["GPIB0::12::INSTR"])
        self.assertEqual(resource.timeout, 1000)
        self.assertEqual(resource.read_termination, "\r\n")
        self.assertEqual(resource.write_termination, "\r\n")
        resource.queries.clear()

        status = backend.read_status()
        self.assertEqual(status["value"], 4.2)
        self.assertEqual(status["target"], 4.0)
        self.assertFalse(status["moving"])
        self.assertTrue(status["ready"])
        self.assertNotIn("rate", status)
        self.assertEqual(
            status["auxiliary"],
            {
                "temp_b": 20.125,
                "loop_1_output": 50.0,
                "loop_1_range": 4,
                "loop_1_setpoint": 4.0,
                "loop_1_ramping": False,
                "loop_2_output": -12.5,
                "loop_2_setpoint": 4.0,
                "loop_2_ramping": True,
            },
        )
        self.assertNotIn("RAMP? 1", resource.queries)
        self.assertNotIn("RAMP? 2", resource.queries)
        self.assertNotIn("HTRST?", resource.queries)

        resource.queries.clear()
        measurement = backend.read_measurement()
        self.assertEqual(
            measurement,
            {
                "value": 4.2,
                "auxiliary": {"temp_b": 20.125},
            },
        )
        self.assertEqual(
            resource.queries,
            ["KRDG? A", "RDGST? A", "KRDG? B", "RDGST? B"],
        )

        backend.close()
        self.assertTrue(resource.closed)
        self.assertTrue(manager.closed)

    def test_set_target_and_hold_operate_both_enabled_loops(self) -> None:
        resource = FakeResource()
        backend = _backend_class(FakeResourceManager(resource))(_config())
        backend.open()
        backend.set_target(10.0, 2.5, control="main")
        self.assertEqual(
            resource.writes,
            [
                "RAMP 1,1,2.5",
                "RAMP 2,1,2.5",
                "SETP 1,10",
                "SETP 2,10",
            ],
        )

        resource.writes.clear()
        backend.hold(control="main")
        self.assertEqual(
            resource.writes,
            [
                "SETP 1,4.2",
                "SETP 2,20.125",
                "RAMP 1,0",
                "RAMP 2,0",
            ],
        )

    def test_disabled_loop_2_is_neither_queried_nor_written(self) -> None:
        resource = FakeResource()
        backend = _backend_class(FakeResourceManager(resource))(
            _config(loop2_enabled=False)
        )
        backend.open()
        self.assertNotIn("CSET? 2", resource.queries)

        resource.queries.clear()
        status = backend.read_status()
        for command in ("AOUT? 2", "SETP? 2", "RAMPST? 2"):
            self.assertNotIn(command, resource.queries)
        self.assertIsNone(status["auxiliary"]["loop_2_output"])
        self.assertIsNone(status["auxiliary"]["loop_2_setpoint"])
        self.assertIsNone(status["auxiliary"]["loop_2_ramping"])

        backend.set_target(10.0, 2.5, control="main")
        self.assertEqual(
            resource.writes,
            ["RAMP 1,1,2.5", "SETP 1,10"],
        )

    def test_write_timeout_reports_unknown_instrument_result(self) -> None:
        resource = FakeResource()
        backend = _backend_class(FakeResourceManager(resource))(_config())
        backend.open()
        resource.write_error = True

        with self.assertRaises(InstrumentError) as caught:
            backend.set_target(10.0, 2.5, control="main")

        self.assertEqual(
            caught.exception.code,
            "INSTRUMENT_WRITE_RESULT_UNKNOWN",
        )
        self.assertEqual(resource.writes, ["RAMP 1,1,2.5"])

    def test_non_role_input_status_is_returned_as_missing(self) -> None:
        resource = FakeResource()
        resource.responses["CSET? 1"] = "B,1,1,1"
        resource.responses["RDGST? A"] = "32"
        backend = _backend_class(FakeResourceManager(resource))(
            _config(
                loop2_enabled=False,
                main_reading="temp_b",
                loop1_input="B",
            )
        )
        backend.open()

        status = backend.read_status()
        self.assertEqual(status["value"], 20.125)
        self.assertIsNone(status["auxiliary"]["temp_a"])
        self.assertEqual(
            backend.read_measurement(),
            {"value": 20.125, "auxiliary": {"temp_a": None}},
        )

    def test_role_none_primary_status_is_returned_as_missing(self) -> None:
        resource = FakeResource()
        resource.responses["RDGST? A"] = "32"
        backend = _backend_class(FakeResourceManager(resource))(
            _config(role="none")
        )
        backend.open()

        status = backend.read_status()
        self.assertIsNone(status["value"])
        self.assertEqual(status["auxiliary"]["temp_b"], 20.125)

    def test_required_input_status_and_disabled_primary_loop_fail_fast(self) -> None:
        resource = FakeResource()
        resource.responses["RDGST? A"] = "32"
        backend = _backend_class(FakeResourceManager(resource))(_config())
        backend.open()
        with self.assertRaises(InstrumentError) as caught:
            backend.read_measurement()
        self.assertEqual(caught.exception.code, "LAKESHORE340_INPUT_STATUS")
        self.assertEqual(caught.exception.context, "A")

        with self.assertRaises(InstrumentError) as caught:
            _backend_class(FakeResourceManager(FakeResource()))(
                _config(loop2_enabled=False, main_reading="temp_b")
            )
        self.assertEqual(
            caught.exception.code,
            "LAKESHORE340_INVALID_CONFIGURATION",
        )

    def test_hold_requires_the_active_loop_input_even_without_a_role(self) -> None:
        resource = FakeResource()
        resource.responses["RDGST? A"] = "32"
        backend = _backend_class(FakeResourceManager(resource))(
            _config(role="none")
        )
        backend.open()

        with self.assertRaises(InstrumentError) as caught:
            backend.hold(control="main")
        self.assertEqual(caught.exception.code, "LAKESHORE340_INPUT_STATUS")
        self.assertEqual(resource.writes, [])

    def test_manifest_declares_named_config_and_panel_templates(self) -> None:
        descriptor = load_instrument_manifest(INSTRUMENT)
        self.assertTrue(descriptor.valid, descriptor.error)

        with (INSTRUMENT / "instrument.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        self.assertEqual(
            [field["id"] for field in manifest["config_fields"]],
            ["loop1_input", "loop2_input", "loop2_enabled", "visa_timeout_ms"],
        )
        self.assertEqual([control["id"] for control in manifest["controls"]], ["main"])
        self.assertEqual(
            [panel["id"] for panel in manifest["panels"]],
            ["control", "temperatures", "loop_1", "loop_2"],
        )
        controller = manifest["panels"][0]
        self.assertEqual(controller["control"], "main")
        self.assertEqual(controller["reading_options"], ["temp_a", "temp_b"])
        self.assertEqual(controller["default_reading"], "temp_a")


if __name__ == "__main__":
    unittest.main()
