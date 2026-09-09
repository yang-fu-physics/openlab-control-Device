from __future__ import annotations

import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT.parent / "OpenLabControl" / "src"
sys.path.insert(0, str(CORE_SRC))

from labcontrol.instruments.base import InstrumentError, InstrumentWarning  # noqa: E402
from labcontrol.package_support.loading import load_source_object  # noqa: E402


EXAMPLE = ROOT / "examples" / "labview_dll"
LabVIEWDLL = load_source_object(
    EXAMPLE,
    "labview_dll:LabVIEWDLL",
    "test_system_labview_dll_loader",
)
LabVIEWDLLFailure = sys.modules[LabVIEWDLL.__module__].LabVIEWDLLFailure
LabVIEWSystemInstrument = load_source_object(
    EXAMPLE,
    "backend:LabVIEWSystemInstrument",
    "test_system_labview_dll_backend",
)
BackendLabVIEWDLLFailure = sys.modules[
    LabVIEWSystemInstrument.__module__
].LabVIEWDLLFailure


def _envelope(result=None, *, severity="ok", code="", message="", context=""):
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "context": context,
        "result": {} if result is None else result,
    }


class _Function:
    def __init__(self, name, responder) -> None:
        self.name = name
        self.responder = responder
        self.argtypes = None
        self.restype = None

    def __call__(
        self,
        request_pointer,
        request_length,
        response_pointer,
        response_capacity,
        response_length_pointer,
    ):
        request = json.loads(
            ctypes.string_at(request_pointer, request_length).decode("utf-8")
        )
        response = json.dumps(
            self.responder(self.name, request),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(response) > response_capacity:
            return 2
        ctypes.memmove(response_pointer, response, len(response))
        ctypes.cast(
            response_length_pointer,
            ctypes.POINTER(ctypes.c_int32),
        )[0] = len(response)
        return 0


class _Library:
    def __init__(self, responder) -> None:
        for name in ("OLC_Describe", "OLC_Open", "OLC_Invoke", "OLC_Close"):
            setattr(self, name, _Function(name, responder))


class _BackendDriver:
    def __init__(self) -> None:
        self.description = {
            "abi_version": 1,
            "kind": "system_instrument",
            "manual_functions": [
                {
                    "id": "diagnostic_read",
                    "label": "Diagnostic Read",
                    "inputs": [
                        {
                            "name": "mode",
                            "label": "Mode",
                            "type": "choice",
                            "default": "Current",
                            "choices": ["Current", "Voltage"],
                        }
                    ],
                    "outputs": [
                        {
                            "name": "value",
                            "label": "Value",
                            "type": "float",
                            "unit": "V",
                            "decimals": 9,
                        }
                    ],
                }
            ],
        }
        self.calls = []
        self.warning = False

    def open(self, payload):
        self.calls.append(("open", payload))
        return {}

    def invoke(self, operation, payload):
        self.calls.append((operation, payload))
        if self.warning:
            raise BackendLabVIEWDLLFailure(
                "reading is stale",
                "DLL_STALE_READING",
                "sensor_a",
                "warning",
            )
        if operation == "read_status":
            return {"value": 4.2, "ready": True}
        if operation == "manual_function":
            return {"value": 1.25e-6}
        return {}

    def close(self):
        self.calls.append(("close", {}))
        return {}


class LabVIEWDLLTemplateTests(unittest.TestCase):
    def test_ctypes_loader_round_trips_utf8_json(self) -> None:
        calls = []

        def respond(name, request):
            calls.append((name, request))
            if name == "OLC_Describe":
                return _envelope(
                    {"abi_version": 1, "kind": "system_instrument"}
                )
            return _envelope({"echo": request})

        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "driver.dll"
            dll_path.touch()
            loader_module = sys.modules[LabVIEWDLL.__module__]
            with patch.object(loader_module.ctypes, "CDLL", return_value=_Library(respond)):
                driver = LabVIEWDLL(dll_path, expected_kind="system_instrument")
                result = driver.invoke("read_status", {"label": "温度"})

        self.assertEqual(result["echo"]["parameters"]["label"], "温度")
        self.assertEqual(calls[0], ("OLC_Describe", {}))
        self.assertEqual(calls[1][1]["operation"], "read_status")

    def test_loader_preserves_structured_warning(self) -> None:
        def respond(name, _request):
            if name == "OLC_Describe":
                return _envelope(
                    {"abi_version": 1, "kind": "system_instrument"}
                )
            return _envelope(
                severity="warning",
                code="DLL_RANGE_WARNING",
                message="range warning",
                context="channel_a",
            )

        with tempfile.TemporaryDirectory() as directory:
            dll_path = Path(directory) / "driver.dll"
            dll_path.touch()
            loader_module = sys.modules[LabVIEWDLL.__module__]
            with patch.object(loader_module.ctypes, "CDLL", return_value=_Library(respond)):
                driver = LabVIEWDLL(dll_path, expected_kind="system_instrument")
                with self.assertRaises(LabVIEWDLLFailure) as caught:
                    driver.invoke("read_status", {})

        self.assertEqual(caught.exception.severity, "warning")
        self.assertEqual(caught.exception.code, "DLL_RANGE_WARNING")
        self.assertEqual(caught.exception.context, "channel_a")

    def test_backend_maps_lifecycle_safety_and_warning(self) -> None:
        backend_module = sys.modules[LabVIEWSystemInstrument.__module__]
        driver = _BackendDriver()
        panel = SimpleNamespace(
            enabled=True,
            template="controller",
            control_id="main",
            min_value=2.0,
            max_value=400.0,
            max_rate_per_minute=10.0,
        )
        config = SimpleNamespace(
            id="temperature",
            address="GPIB0::12::INSTR",
            extras={"visa_timeout_ms": 1000},
            operation_timeout_seconds=10.0,
            panels=(panel,),
        )
        with patch.object(backend_module, "LabVIEWDLL", return_value=driver):
            backend = LabVIEWSystemInstrument(config)

        backend.open()
        self.assertEqual(backend.read_status(), {"value": 4.2, "ready": True})
        backend.set_target(10.0, 1.5, control="main")
        backend.hold(control="main")
        result = backend.execute_dll_function(
            "diagnostic_read",
            {"mode": "Current"},
        )
        self.assertEqual(result, {"value": 1.25e-6})
        self.assertEqual(
            backend.dll_functions[0]["id"],
            "diagnostic_read",
        )
        calls_before_rejection = len(driver.calls)
        with self.assertRaises(InstrumentError):
            backend.set_target(401.0, 1.5, control="main")
        with self.assertRaises(InstrumentError):
            backend.set_target(10.0, 10.1, control="main")
        self.assertEqual(len(driver.calls), calls_before_rejection)
        backend.close()
        self.assertEqual(driver.calls[0][0], "open")
        self.assertEqual(driver.calls[2][1]["value"], 10.0)
        self.assertEqual(
            driver.calls[4][1]["function_id"],
            "diagnostic_read",
        )
        self.assertEqual(driver.calls[-1][0], "close")

        driver.warning = True
        with self.assertRaises(InstrumentWarning) as caught:
            backend.read_status()
        self.assertEqual(caught.exception.code, "DLL_STALE_READING")
        self.assertEqual(caught.exception.context, "sensor_a")


if __name__ == "__main__":
    unittest.main()
