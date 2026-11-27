from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
for candidate in (
    ROOT.parent / "OpenLabControl",
    ROOT.parent / "openlab-control",
):
    if (candidate / "src").is_dir():
        if str(candidate / "src") not in sys.path:
            sys.path.insert(0, str(candidate / "src"))
        if (candidate / "tools").is_dir() and str(candidate / "tools") not in sys.path:
            sys.path.insert(0, str(candidate / "tools"))
        break

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from labcontrol.config import StabilityConfig, load_config  # noqa: E402
from labcontrol.events import EventManager  # noqa: E402
from labcontrol.instrument_manager import InstrumentManager  # noqa: E402
from labcontrol.instruments.base import InstrumentError, SafetyViolation  # noqa: E402
from labcontrol.instruments.manifest import load_instrument_manifest  # noqa: E402
from labcontrol.models import InstrumentActivity, InstrumentKind, StabilityState  # noqa: E402
from labcontrol.stability import StabilityEvaluator  # noqa: E402
from instrument_scanner import (  # noqa: E402
    InstrumentScannerWindow,
    VisaScanResult,
    execute_save_plan,
)


INSTRUMENT = ROOT / "instruments" / "kelvinion"


def _load_kelvinion_package():
    """以独立包形式加载 instruments/kelvinion，支持相对导入。"""

    pkg_name = "test_kelvinion_pkg"
    if pkg_name in sys.modules:
        return (
            sys.modules[f"{pkg_name}.kelvinion"],
            sys.modules[f"{pkg_name}.backend"],
            sys.modules[f"{pkg_name}.frontend"],
        )
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(INSTRUMENT)]
    sys.modules[pkg_name] = pkg

    loaded = []
    for mod_name in ("kelvinion", "backend", "frontend"):
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{mod_name}",
            INSTRUMENT / f"{mod_name}.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {mod_name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded.append(module)
    return tuple(loaded)


kelvinion_proto, kelvinion_backend, kelvinion_frontend = _load_kelvinion_package()
KelvinionController = kelvinion_backend.KelvinionController
SetupWidget = kelvinion_frontend.SetupWidget


class FakeKelvinionTransport:
    """模拟 Kelvinion 方括号协议的伪仪表，支持爬坡工作设定值与 ACK/后缀开关。"""

    def __init__(
        self,
        address: str = "ASRL3::INSTR",
        timeout_ms: int = 1000,
        *,
        baud_rate: int = 115200,
        is_mini: bool = False,
        ramping_working_setpoint: bool = True,
        reply_ack: bool = False,
        require_setp_suffix: bool = True,
    ) -> None:
        self.address = address
        self.timeout_ms = timeout_ms
        self.baud_rate = baud_rate
        self.ramping_working_setpoint = ramping_working_setpoint
        self.reply_ack = reply_ack
        self.require_setp_suffix = require_setp_suffix
        self.closed = False
        self.write_error = False
        self.ramp_mismatch = False
        self.setup_mismatch = False
        self.queries: list[str] = []
        self.writes: list[str] = []
        self.pending_targets: dict[str, float] = {}

        idn = (
            "[MultiFields Technologies,Kelvinion Mini,1005,V2.0.6]\r\n"
            if is_mini
            else "[MultiFields Technologies,Kelvinion,1005,V2.1.16]\r\n"
        )
        self.responses: dict[str, str] = {
            "[*IDN?]": idn,
            "[READ:K:A]": "[10.000]\r\n",
            "[READ:K:B]": "[20.500]\r\n",
            "[READ:K:C]": "[50.000]\r\n",
            "[READ:K:D]": "[77.250]\r\n",
            "[READ:K:E]": "[0.000]\r\n",
            "[READ:K:F]": "[S/C]\r\n",
            "[READ:K:G]": "[0.000]\r\n",
            "[READ:K:H]": "[0.000]\r\n",
            "[READ:LOOP:A]": "[A]\r\n",
            "[READ:LOOP:B]": "[B]\r\n",
            "[READ:MODE:A]": "[A]\r\n",
            "[READ:MODE:B]": "[A]\r\n",
            "[READ:RANGE:A]": "[HIGH]\r\n",
            "[READ:RANGE:B]": "[MED]\r\n",
            "[READ:POWER:A]": "[25.500]\r\n",
            "[READ:POWER:B]": "[12.000]\r\n",
            "[READ:SETP:A]": "[10.000]\r\n",
            "[READ:SETP:B]": "[20.000]\r\n",
            "[READ:RAMP:A]": "[1.000]\r\n",
            "[READ:RAMP:B]": "[2.000]\r\n",
            "[READ:LIMIT:A]": "[350.000]\r\n",
            "[READ:LIMIT:B]": "[350.000]\r\n",
            "[READ:ZONE:STATE:A]": "[OFF]\r\n",
            "[READ:ZONE:STATE:B]": "[2]\r\n",
        }
        for ch in ("A", "B", "C", "D", "E", "F", "G", "H"):
            self.responses[f"[READ:T:{ch}]"] = "[CERNOX]\r\n"
            self.responses[f"[READ:W:{ch}]"] = "[0]\r\n"
            self.responses[f"[READ:INPUT_LIMIT:{ch}]"] = "[350.000]\r\n"
        for out in ("A", "B"):
            for idx in range(1, 6):
                self.responses[f"[READ:ZONE:ZONE{idx}:{out}]"] = (
                    f"[10.00,5.00,1.0,{idx % 4},{idx * 60:.2f}]\r\n"
                )

    def _apply_set(self, frame: str) -> str:
        if self.write_error:
            raise OSError("serial write timeout")
        inner = frame[1:-1]
        if inner == "SET:SAVE":
            return "[OK]\r\n"
        parts = inner.split(":")
        if parts[0] != "SET":
            raise AssertionError(f"Expected SET frame, got {frame!r}")
        cmd = parts[1]
        if cmd == "RAMP":
            out, val = parts[2], float(parts[3])
            if not self.ramp_mismatch:
                self.responses[f"[READ:RAMP:{out}]"] = f"[{val:.3f}]\r\n"
        elif cmd == "SETP":
            out, raw_val = parts[2], parts[3]
            if self.require_setp_suffix and not raw_val.endswith("K"):
                raise OSError("firmware rejected missing K suffix")
            if not self.require_setp_suffix and raw_val.endswith("K"):
                raise OSError("firmware rejected K suffix")
            val = float(raw_val.removesuffix("K"))
            self.pending_targets[out] = val
            if not self.ramping_working_setpoint:
                self.responses[f"[READ:SETP:{out}]"] = f"[{val:.3f}]\r\n"
        elif cmd == "T":
            ch, sensor = parts[2], parts[3]
            if not self.setup_mismatch:
                self.responses[f"[READ:T:{ch}]"] = f"[{sensor}]\r\n"
        elif cmd == "W":
            ch, curve = parts[2], parts[3]
            if not self.setup_mismatch:
                self.responses[f"[READ:W:{ch}]"] = f"[{curve}]\r\n"
        elif cmd == "LOOP":
            out, ch = parts[2], parts[3]
            if not self.setup_mismatch:
                self.responses[f"[READ:LOOP:{out}]"] = (
                    "[NULL]\r\n" if ch == "OFF" else f"[{ch}]\r\n"
                )
        elif cmd == "RANGE":
            out, rng = parts[2], parts[3]
            if not self.setup_mismatch:
                self.responses[f"[READ:RANGE:{out}]"] = f"[{rng}]\r\n"
        elif cmd == "LIMIT":
            out, lim = parts[2], float(parts[3])
            if not self.setup_mismatch:
                self.responses[f"[READ:LIMIT:{out}]"] = f"[{lim:.3f}]\r\n"
        elif cmd == "ZONE":
            out = parts[2]
            if len(parts) == 4:
                state = parts[3]
                if not self.setup_mismatch:
                    self.responses[f"[READ:ZONE:STATE:{out}]"] = (
                        "[1]\r\n" if state == "ON" else "[OFF]\r\n"
                    )
            else:
                zone, field, raw_val = parts[3], parts[4], parts[5]
                if not self.setup_mismatch:
                    cur = self.responses[f"[READ:ZONE:{zone}:{out}]"].strip()[1:-1].split(",")
                    idx = {"KP": 0, "KI": 1, "KD": 2, "RANGE": 3, "BOUNDARY": 4}[field]
                    if field == "RANGE":
                        cur[idx] = str(kelvinion_proto.ZONE_RANGE_TO_CODE[raw_val])
                    else:
                        cur[idx] = raw_val
                    self.responses[f"[READ:ZONE:{zone}:{out}]"] = (
                        "[" + ",".join(cur) + "]\r\n"
                    )
        return "[OK]\r\n"

    def query(self, frame: str) -> str:
        self.queries.append(frame)
        if frame.startswith("[SET:"):
            self.writes.append(frame)
            ack = self._apply_set(frame)
            if not self.reply_ack:
                raise OSError("unexpected ACK timeout")
            return ack
        return self.responses[frame]

    def write(self, frame: str) -> None:
        self.writes.append(frame)
        self._apply_set(frame)

    def read(self) -> str:
        return "[OK]\r\n"

    def close(self) -> None:
        self.closed = True


def _panel(
    panel_id: str,
    control_id: str,
    reading: str,
    *,
    enabled: bool = True,
    role: str = "none",
    min_value: float = 1.8,
    max_value: float = 400.0,
    max_rate: float = 10.0,
):
    return SimpleNamespace(
        id=panel_id,
        enabled=enabled,
        template="controller",
        control_id=control_id,
        reading=reading,
        role=role,
        min_value=min_value,
        max_value=max_value,
        max_rate_per_minute=max_rate,
    )


def _config(
    *,
    address: str = "ASRL3::INSTR",
    output_2_enabled: bool = True,
    control_2_enabled: bool = True,
    main_reading: str = "temp_a",
    control_1_reading: str = "temp_a",
    control_2_reading: str = "temp_b",
    auxiliary_readings: tuple[str, ...] = (
        "temp_b",
        "temp_c",
        "temp_d",
        "loop_1_output",
        "loop_1_range",
        "loop_1_setpoint",
        "loop_1_ramping",
        "loop_1_mode",
        "loop_1_zone",
        "loop_2_output",
        "loop_2_range",
        "loop_2_setpoint",
        "loop_2_ramping",
        "loop_2_mode",
        "loop_2_zone",
    ),
    visa_timeout_ms: int = 1000,
    baud_rate: str = "115200",
    write_expects_ack: bool = False,
    setpoint_unit_suffix: bool = True,
    role_1: str = "sample_temp",
    role_2: str = "none",
    min_value: float = 1.8,
    max_value: float = 400.0,
    max_rate: float = 10.0,
):
    return SimpleNamespace(
        id="kelvinion",
        address=address,
        main_reading=main_reading,
        auxiliary_readings=auxiliary_readings,
        panels=(
            _panel(
                "control_1",
                "output_1",
                control_1_reading,
                enabled=True,
                role=role_1,
                min_value=min_value,
                max_value=max_value,
                max_rate=max_rate,
            ),
            _panel(
                "control_2",
                "output_2",
                control_2_reading,
                enabled=control_2_enabled,
                role=role_2,
                min_value=min_value,
                max_value=max_value,
                max_rate=max_rate,
            ),
        ),
        min_value=min_value,
        max_value=max_value,
        max_rate_per_minute=max_rate,
        operation_timeout_seconds=10.0,
        extras={
            "output_2_enabled": output_2_enabled,
            "baud_rate": baud_rate,
            "visa_timeout_ms": visa_timeout_ms,
            "write_expects_ack": write_expects_ack,
            "setpoint_unit_suffix": setpoint_unit_suffix,
        },
    )


class KelvinionDriverTests(unittest.TestCase):
    def test_manifest_and_identity_pattern(self) -> None:
        descriptor = load_instrument_manifest(INSTRUMENT)
        self.assertTrue(descriptor.valid, descriptor.error)
        self.assertEqual(descriptor.id, "kelvinion")
        self.assertEqual(descriptor.api_version, "4")
        self.assertEqual(descriptor.setup_widget, "frontend:SetupWidget")
        self.assertEqual(
            [c.id for c in descriptor.controls],
            ["output_1", "output_2"],
        )
        self.assertEqual(
            [p.id for p in descriptor.panels],
            [
                "control_1",
                "control_2",
                "temperatures_ad",
                "temperatures_eh",
                "loop_1",
                "loop_2",
                "output_modes",
            ],
        )
        pattern = re.compile(descriptor.identity_pattern)
        self.assertRegex("[MultiFields Technologies,Kelvinion,1005,V2.1.16]", pattern)
        self.assertRegex("MultiFields Technologies,Kelvinion Mini,1005,V2.0.6", pattern)
        self.assertNotRegex("LSCI,MODEL340,340001,02032001", pattern)

        # Verify OpenLab Control core package loader imports backend & frontend cleanly.
        from labcontrol.package_support.loading import load_source_object

        loaded_backend = load_source_object(
            INSTRUMENT, descriptor.backend, "kelvinion_loader_backend_test"
        )
        loaded_widget = load_source_object(
            INSTRUMENT, descriptor.setup_widget, "kelvinion_loader_widget_test"
        )
        self.assertEqual(loaded_backend.__name__, "KelvinionController")
        self.assertEqual(loaded_widget.__name__, "SetupWidget")

    def test_gpib_rejected_and_usb_serial_options_configured(self) -> None:
        with self.assertRaises(InstrumentError) as err:
            KelvinionController(_config(address="GPIB0::13::INSTR"))
        self.assertEqual(err.exception.code, "KELVINION_GPIB_UNSUPPORTED")

        with self.assertRaises(InstrumentError) as bad_addr:
            KelvinionController(_config(address="USB0::0x1234::INSTR"))
        self.assertEqual(bad_addr.exception.code, "KELVINION_INVALID_CONFIGURATION")

        # LAN TCP socket is accepted cleanly.
        lan_ctrl = KelvinionController(
            _config(address="TCPIP0::192.168.1.199::5000::SOCKET")
        )
        self.assertEqual(lan_ctrl._address, "TCPIP0::192.168.1.199::5000::SOCKET")

        # Excessive VISA timeout rejected.
        with self.assertRaises(InstrumentError) as timeout_err:
            KelvinionController(_config(visa_timeout_ms=10000))
        self.assertEqual(timeout_err.exception.code, "INSTRUMENT_IO_TIMEOUT_TOO_HIGH")

        # Contradictory config: control_2 enabled while output_2_enabled=False.
        with self.assertRaises(InstrumentError) as conflict:
            KelvinionController(
                _config(output_2_enabled=False, control_2_enabled=True)
            )
        self.assertEqual(conflict.exception.code, "KELVINION_INVALID_CONFIGURATION")

        # Verify PyVisaTransport sets CH343 115200 8N1 no flow control & \r\n.
        fake_res = Mock()
        fake_rm = Mock()
        fake_rm.open_resource.return_value = fake_res
        import pyvisa
        from pyvisa.constants import ControlFlow, Parity, StopBits

        with patch.object(pyvisa, "ResourceManager", return_value=fake_rm):
            transport = kelvinion_proto.PyVisaTransport("ASRL4::INSTR", 1200, baud_rate=115200)
            self.assertEqual(fake_res.timeout, 1200)
            self.assertEqual(fake_res.baud_rate, 115200)
            self.assertEqual(fake_res.data_bits, 8)
            self.assertEqual(fake_res.parity, Parity.none)
            self.assertEqual(fake_res.stop_bits, StopBits.one)
            self.assertEqual(fake_res.flow_control, ControlFlow.none)
            self.assertEqual(fake_res.read_termination, "\r\n")
            self.assertEqual(fake_res.write_termination, "\r\n")
            transport.close()
            fake_res.close.assert_called_once()
            fake_rm.close.assert_called_once()

    def test_identity_mismatch_and_mini_capability_enforcement(self) -> None:
        bad_transport = FakeKelvinionTransport()
        bad_transport.responses["[*IDN?]"] = "[OtherVendor,OtherModel,1,1]\r\n"
        ctrl = KelvinionController(_config(), transport_factory=lambda *a, **k: bad_transport)
        with self.assertRaises(InstrumentError) as err:
            ctrl.open()
        self.assertEqual(err.exception.code, "KELVINION_IDENTITY_MISMATCH")
        self.assertTrue(bad_transport.closed)
        self.assertEqual(bad_transport.writes, [])

        # Kelvinion Mini refuses Output 2 enabled.
        mini_transport = FakeKelvinionTransport(is_mini=True)
        ctrl_dual = KelvinionController(
            _config(output_2_enabled=True, control_2_enabled=True),
            transport_factory=lambda *a, **k: mini_transport,
        )
        with self.assertRaises(InstrumentError) as mini_err:
            ctrl_dual.open()
        self.assertEqual(mini_err.exception.code, "KELVINION_MODEL_CAPABILITY_MISMATCH")
        self.assertTrue(mini_transport.closed)

        # Kelvinion Mini succeeds when Output 2 disabled and only Inputs A/B requested.
        mini_ok = FakeKelvinionTransport(is_mini=True)
        ctrl_mini = KelvinionController(
            _config(
                output_2_enabled=False,
                control_2_enabled=False,
                auxiliary_readings=("temp_b", "loop_1_output", "loop_1_range", "loop_1_setpoint", "loop_1_ramping"),
            ),
            transport_factory=lambda *a, **k: mini_ok,
        )
        ctrl_mini.open()
        status = ctrl_mini.read_status()
        self.assertEqual(status["value"], 10.0)
        self.assertEqual(set(status["controls"]), {"output_1"})
        self.assertNotIn("[READ:LOOP:B]", mini_ok.queries)
        ctrl_mini.close()

    def test_dual_loop_status_measurement_and_selective_polling(self) -> None:
        transport = FakeKelvinionTransport()
        ctrl = KelvinionController(_config(), transport_factory=lambda *a, **k: transport)
        ctrl.open()
        self.assertIn("[READ:LOOP:A]", transport.queries)
        self.assertIn("[READ:LOOP:B]", transport.queries)
        self.assertEqual(transport.writes, [])

        transport.queries.clear()
        status = ctrl.read_status()
        self.assertEqual(status["value"], 10.0)
        self.assertEqual(status["target"], 10.0)
        self.assertEqual(status["rate"], 1.0)
        self.assertFalse(status["moving"])
        self.assertTrue(status["ready"])
        self.assertEqual(
            status["controls"],
            {
                "output_1": {"target": 10.0, "rate": 1.0, "moving": False, "ready": True},
                "output_2": {"target": 20.0, "rate": 2.0, "moving": False, "ready": True},
            },
        )
        self.assertEqual(status["auxiliary"]["temp_b"], 20.5)
        self.assertEqual(status["auxiliary"]["temp_c"], 50.0)
        self.assertEqual(status["auxiliary"]["loop_1_output"], 25.5)
        self.assertEqual(status["auxiliary"]["loop_1_range"], "HIGH")
        self.assertEqual(status["auxiliary"]["loop_1_setpoint"], 10.0)
        self.assertEqual(status["auxiliary"]["loop_1_ramping"], "Idle")
        self.assertEqual(status["auxiliary"]["loop_1_mode"], "Auto")
        self.assertEqual(status["auxiliary"]["loop_1_zone"], "OFF")
        self.assertEqual(status["auxiliary"]["loop_2_output"], 12.0)
        self.assertEqual(status["auxiliary"]["loop_2_range"], "MED")
        self.assertEqual(status["auxiliary"]["loop_2_zone"], "Zone 2")
        # Unselected channels E–H are NOT queried!
        for ch in ("E", "F", "G", "H"):
            self.assertNotIn(f"[READ:K:{ch}]", transport.queries)

        # Fast measurement path only queries temperature inputs.
        transport.queries.clear()
        meas = ctrl.read_measurement()
        self.assertEqual(
            meas,
            {
                "value": 10.0,
                "auxiliary": {"temp_b": 20.5, "temp_c": 50.0, "temp_d": 77.25},
            },
        )
        self.assertEqual(
            transport.queries,
            ["[READ:K:A]", "[READ:K:B]", "[READ:K:C]", "[READ:K:D]"],
        )

        # Idempotent close never writes.
        ctrl.close()
        ctrl.close()
        self.assertEqual(transport.writes, [])
        self.assertTrue(transport.closed)

    def test_sensor_fault_required_vs_monitor_channel(self) -> None:
        transport = FakeKelvinionTransport()
        # Monitor channel C unplugged (0.000 K) and D short circuit (S/C) -> None.
        transport.responses["[READ:K:C]"] = "[0.000]\r\n"
        transport.responses["[READ:K:D]"] = "[S/C]\r\n"
        ctrl = KelvinionController(_config(), transport_factory=lambda *a, **k: transport)
        ctrl.open()
        status = ctrl.read_status()
        self.assertEqual(status["value"], 10.0)
        self.assertIsNone(status["auxiliary"]["temp_c"])
        self.assertIsNone(status["auxiliary"]["temp_d"])

        # Role-required sample_temp channel A fault raises SafetyViolation!
        transport.responses["[READ:K:A]"] = "[S/C]\r\n"
        with self.assertRaises(SafetyViolation) as err:
            ctrl.read_status()
        self.assertEqual(err.exception.code, "KELVINION_SENSOR_FAULT")
        self.assertEqual(err.exception.context, "A")
        ctrl.close()

    def test_ramping_working_setpoint_state_machine_and_overrides_hv03(self) -> None:
        now = [100.0]
        transport = FakeKelvinionTransport(ramping_working_setpoint=True)
        ctrl = KelvinionController(
            _config(),
            transport_factory=lambda *a, **k: transport,
            clock=lambda: now[0],
        )
        ctrl.open()
        ctrl.read_status()

        # Command independent ramps on output_1 (10 -> 50 K) and output_2 (20 -> 15 K).
        ctrl.set_target(50.0, 2.5, control="output_1")
        ctrl.set_target(15.0, 1.5, control="output_2")
        self.assertEqual(
            transport.writes,
            [
                "[SET:RAMP:A:2.50]",
                "[SET:SETP:A:50.00K]",
                "[SET:RAMP:B:1.50]",
                "[SET:SETP:B:15.00K]",
            ],
        )

        # Working setpoint starts ramping: moving=True, ready=False, target=final target!
        now[0] += 2.0
        transport.responses["[READ:SETP:A]"] = "[15.000]\r\n"
        transport.responses["[READ:SETP:B]"] = "[18.500]\r\n"
        st1 = ctrl.read_status()
        self.assertEqual(st1["controls"]["output_1"], {"target": 50.0, "rate": 2.5, "moving": True, "ready": False})
        self.assertEqual(st1["controls"]["output_2"], {"target": 15.0, "rate": 1.5, "moving": True, "ready": False})
        self.assertEqual(st1["auxiliary"]["loop_1_setpoint"], 15.0)
        self.assertEqual(st1["auxiliary"]["loop_1_ramping"], "Ramping")
        self.assertEqual(st1["auxiliary"]["loop_2_setpoint"], 18.5)
        self.assertEqual(st1["auxiliary"]["loop_2_ramping"], "Ramping")

        # Output 2 arrives at 15.00 K while Output 1 is still ramping.
        now[0] += 5.0
        transport.responses["[READ:SETP:A]"] = "[30.000]\r\n"
        transport.responses["[READ:SETP:B]"] = "[15.000]\r\n"
        st2 = ctrl.read_status()
        self.assertTrue(st2["controls"]["output_1"]["moving"])
        self.assertFalse(st2["controls"]["output_1"]["ready"])
        self.assertFalse(st2["controls"]["output_2"]["moving"])
        self.assertTrue(st2["controls"]["output_2"]["ready"])
        self.assertEqual(st2["controls"]["output_2"]["target"], 15.0)
        self.assertEqual(st2["auxiliary"]["loop_2_ramping"], "Idle")

        # Output 1 arrives at 50.00 K.
        now[0] += 5.0
        transport.responses["[READ:SETP:A]"] = "[50.000]\r\n"
        st3 = ctrl.read_status()
        self.assertFalse(st3["controls"]["output_1"]["moving"])
        self.assertTrue(st3["controls"]["output_1"]["ready"])
        self.assertEqual(st3["auxiliary"]["loop_1_ramping"], "Idle")

        # Front-panel reversal keeps original commanded target, reports Stalled and ready=False.
        ctrl.set_target(80.0, 2.0, control="output_1")
        now[0] += 2.0
        transport.responses["[READ:SETP:A]"] = "[55.000]\r\n"
        self.assertTrue(ctrl.read_status()["controls"]["output_1"]["moving"])
        now[0] += 2.0
        transport.responses["[READ:SETP:A]"] = "[40.000]\r\n"  # reversed!
        st_override = ctrl.read_status()
        self.assertFalse(st_override["controls"]["output_1"]["moving"])
        self.assertFalse(st_override["controls"]["output_1"]["ready"])
        self.assertEqual(st_override["controls"]["output_1"]["target"], 80.0)
        self.assertEqual(st_override["auxiliary"]["loop_1_setpoint"], 40.0)
        self.assertEqual(st_override["auxiliary"]["loop_1_ramping"], "Stalled")

        # Subsequent poll still stuck at 40 K remains Stalled / ready=False / target=80 K.
        now[0] += 2.0
        st_override2 = ctrl.read_status()
        self.assertFalse(st_override2["controls"]["output_1"]["moving"])
        self.assertFalse(st_override2["controls"]["output_1"]["ready"])
        self.assertEqual(st_override2["controls"]["output_1"]["target"], 80.0)
        self.assertEqual(st_override2["auxiliary"]["loop_1_ramping"], "Stalled")

        # If setpoint later resumes moving toward 80 K and arrives, it cleanly recovers.
        now[0] += 2.0
        transport.responses["[READ:SETP:A]"] = "[60.000]\r\n"
        st_resume = ctrl.read_status()
        self.assertTrue(st_resume["controls"]["output_1"]["moving"])
        self.assertFalse(st_resume["controls"]["output_1"]["ready"])
        self.assertEqual(st_resume["auxiliary"]["loop_1_ramping"], "Ramping")
        now[0] += 2.0
        transport.responses["[READ:SETP:A]"] = "[80.000]\r\n"
        st_arrived = ctrl.read_status()
        self.assertFalse(st_arrived["controls"]["output_1"]["moving"])
        self.assertTrue(st_arrived["controls"]["output_1"]["ready"])
        self.assertEqual(st_arrived["auxiliary"]["loop_1_ramping"], "Idle")

        # Passive front-panel ramp detected between polls when idle.
        now[0] += 1.0
        transport.responses["[READ:SETP:A]"] = "[81.000]\r\n"
        st_passive = ctrl.read_status()
        self.assertTrue(st_passive["controls"]["output_1"]["moving"])
        self.assertEqual(st_passive["auxiliary"]["loop_1_ramping"], "Ramping")
        now[0] += 1.0
        st_passive_done = ctrl.read_status()
        self.assertFalse(st_passive_done["controls"]["output_1"]["moving"])
        self.assertEqual(st_passive_done["auxiliary"]["loop_1_ramping"], "Idle")
        ctrl.close()

    def test_stalled_ramp_preserves_target_stays_not_ready_and_prevents_false_stability(self) -> None:
        """P1 regression: commanded 50 K while working setpoint & Temp A remain stuck at 10 K."""

        now = [100.0]
        transport = FakeKelvinionTransport(ramping_working_setpoint=True)
        ctrl = KelvinionController(
            _config(),
            transport_factory=lambda *a, **k: transport,
            clock=lambda: now[0],
        )
        ctrl.open()
        ctrl.read_status()

        # Command 50 K; instrument working setpoint and actual Temp A stay stuck at 10 K.
        ctrl.set_target(50.0, 2.0, control="output_1")
        evaluator = StabilityEvaluator(
            StabilityConfig(
                tolerance=0.05,
                max_slope_per_minute=0.05,
                dwell_seconds=5.0,
                timeout_seconds=10.0,
                window_seconds=2.0,
            )
        )
        evaluator.reset(50.0, now[0])

        # Within stall window: still ramping, target=50 K, stability timer paused.
        now[0] += 5.0
        st_wait = ctrl.read_status()
        self.assertEqual(st_wait["controls"]["output_1"], {"target": 50.0, "rate": 2.0, "moving": True, "ready": False})
        self.assertEqual(st_wait["auxiliary"]["loop_1_ramping"], "Ramping")
        res_wait = evaluator.update(
            float(st_wait["value"]),
            float(st_wait["target"]),
            now[0],
            ready=bool(st_wait["ready"]),
            moving=bool(st_wait["moving"]),
        )
        self.assertEqual(res_wait.state, StabilityState.MOVING)

        # Past stall timeout (25 s > 20 s): MUST keep target=50 K, moving=False, ready=False, Stalled!
        now[0] += 25.0
        st_stall = ctrl.read_status()
        self.assertEqual(st_stall["target"], 50.0)
        self.assertEqual(st_stall["controls"]["output_1"], {"target": 50.0, "rate": 2.0, "moving": False, "ready": False})
        self.assertEqual(st_stall["auxiliary"]["loop_1_setpoint"], 10.0)
        self.assertEqual(st_stall["auxiliary"]["loop_1_ramping"], "Stalled")

        # Repeated polls stuck at 10 K must NEVER stabilize at 10 K and must time out cleanly.
        last_res = None
        for _ in range(8):
            now[0] += 2.0
            st = ctrl.read_status()
            self.assertEqual(st["target"], 50.0)
            self.assertFalse(st["moving"])
            self.assertFalse(st["ready"])
            self.assertEqual(st["auxiliary"]["loop_1_ramping"], "Stalled")
            last_res = evaluator.update(
                float(st["value"]),
                float(st["target"]),
                now[0],
                ready=bool(st["ready"]),
                moving=bool(st["moving"]),
            )
            self.assertNotEqual(last_res.state, StabilityState.STABLE)
        assert last_res is not None
        self.assertEqual(last_res.state, StabilityState.TIMED_OUT)

        # Even if Temp A drifts to 50.0 K while working setpoint is still stalled at 10 K,
        # ready=False prevents false STABLE until setpoint arrives or operator commands hold().
        transport.responses["[READ:K:A]"] = "[50.000]\r\n"
        evaluator2 = StabilityEvaluator(
            StabilityConfig(
                tolerance=0.05,
                max_slope_per_minute=0.05,
                dwell_seconds=2.0,
                timeout_seconds=100.0,
                window_seconds=2.0,
            )
        )
        evaluator2.reset(50.0, now[0])
        for _ in range(5):
            now[0] += 1.0
            st = ctrl.read_status()
            res2 = evaluator2.update(
                float(st["value"]),
                float(st["target"]),
                now[0],
                ready=bool(st["ready"]),
                moving=bool(st["moving"]),
            )
            self.assertNotEqual(res2.state, StabilityState.STABLE)

        # Explicit hold() clears the stalled ramp cleanly.
        transport.responses["[READ:K:A]"] = "[10.000]\r\n"
        ctrl.hold(control="output_1")
        now[0] += 1.0
        st_held = ctrl.read_status()
        self.assertEqual(st_held["target"], 10.0)
        self.assertFalse(st_held["moving"])
        self.assertTrue(st_held["ready"])
        self.assertEqual(st_held["auxiliary"]["loop_1_ramping"], "Idle")
        ctrl.close()

    def test_hv01_ack_and_hv02_unit_suffix_toggles(self) -> None:
        transport = FakeKelvinionTransport(
            ramping_working_setpoint=False,
            reply_ack=True,
            require_setp_suffix=False,
        )
        ctrl = KelvinionController(
            _config(write_expects_ack=True, setpoint_unit_suffix=False),
            transport_factory=lambda *a, **k: transport,
        )
        ctrl.open()
        ctrl.set_target(25.25, 1.5, control="output_1")
        self.assertEqual(
            transport.writes,
            ["[SET:RAMP:A:1.50]", "[SET:SETP:A:25.25]"],
        )
        st = ctrl.read_status()
        self.assertFalse(st["controls"]["output_1"]["moving"])
        self.assertEqual(st["controls"]["output_1"]["target"], 25.25)
        ctrl.close()

    def test_safety_checks_hold_and_uncertain_write(self) -> None:
        transport = FakeKelvinionTransport()
        ctrl = KelvinionController(_config(), transport_factory=lambda *a, **k: transport)
        ctrl.open()

        # Precision & limit violations reject before any write.
        for target, rate in ((25.001, 1.0), (25.0, 1.001), (25.0, 0.05), (25.0, 10.5), (500.0, 1.0)):
            with self.assertRaises(SafetyViolation):
                ctrl.set_target(target, rate, control="output_1")
        self.assertEqual(transport.writes, [])

        # Manual mode blocks writes.
        transport.responses["[READ:MODE:A]"] = "[M]\r\n"
        with self.assertRaises(SafetyViolation) as mode_err:
            ctrl.set_target(25.0, 1.0, control="output_1")
        self.assertEqual(mode_err.exception.code, "KELVINION_MODE_MISMATCH")
        transport.responses["[READ:MODE:A]"] = "[A]\r\n"

        # Hardware PID limit and curve limit block writes.
        transport.responses["[READ:LIMIT:A]"] = "[20.000]\r\n"
        with self.assertRaises(SafetyViolation) as lim_err:
            ctrl.set_target(25.0, 1.0, control="output_1")
        self.assertEqual(lim_err.exception.code, "KELVINION_HARDWARE_TARGET_LIMIT")
        transport.responses["[READ:LIMIT:A]"] = "[350.000]\r\n"
        transport.responses["[READ:INPUT_LIMIT:A]"] = "[22.000]\r\n"
        with self.assertRaises(SafetyViolation) as curve_err:
            ctrl.set_target(25.0, 1.0, control="output_1")
        self.assertEqual(curve_err.exception.code, "KELVINION_HARDWARE_TARGET_LIMIT")
        transport.responses["[READ:INPUT_LIMIT:A]"] = "[350.000]\r\n"
        self.assertEqual(transport.writes, [])

        # Independent hold on output_2 reads fresh Temp B (20.500 K) and does NOT send RAMP 0.
        transport.responses["[READ:K:B]"] = "[20.567]\r\n"
        ctrl.hold(control="output_2")
        self.assertEqual(transport.writes, ["[SET:SETP:B:20.57K]"])

        # Hold with broken sensor fails without writing.
        transport.writes.clear()
        transport.responses["[READ:K:B]"] = "[0.000]\r\n"
        with self.assertRaises(SafetyViolation) as hold_err:
            ctrl.hold(control="output_2")
        self.assertEqual(hold_err.exception.code, "KELVINION_SENSOR_FAULT")
        self.assertEqual(transport.writes, [])

        # Write timeout / ramp readback mismatch reports INSTRUMENT_WRITE_RESULT_UNKNOWN without retry.
        transport.ramp_mismatch = True
        with self.assertRaises(InstrumentError) as unknown_err:
            ctrl.set_target(30.0, 2.0, control="output_1")
        self.assertEqual(unknown_err.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")
        self.assertEqual(transport.writes, ["[SET:RAMP:A:2.00]"])
        ctrl.close()

    def test_setup_actions_and_real_pyside6_setup_widget(self) -> None:
        app = QApplication.instance() or QApplication([])
        transport = FakeKelvinionTransport()
        ctrl = KelvinionController(_config(), transport_factory=lambda *a, **k: transport)
        ctrl.open()

        setup = ctrl.execute_action("read_setup")
        self.assertEqual(setup["input_channels"], ["A", "B", "C", "D", "E", "F", "G", "H"])
        self.assertEqual(setup["output_channels"], ["A", "B"])
        self.assertEqual(len(setup["outputs"]["A"]["zones"]), 5)

        # Reject invalid setup before any write.
        transport.writes.clear()
        with self.assertRaises(InstrumentError) as bad_setup:
            ctrl.execute_action("apply_setup", {"sensors": {"A": {"filter": True}}})
        self.assertEqual(bad_setup.exception.code, "KELVINION_INVALID_SETUP")
        self.assertEqual(transport.writes, [])

        # Drive real PySide6 SetupWidget against backend actions.
        calls: list[tuple[str, dict]] = []

        class FakeUIAPI:
            connected = True
            editable = True

            def call_action(self, action, payload, *, read_only, on_result, on_error):
                calls.append((action, dict(payload)))
                try:
                    res = ctrl.execute_action(action, dict(payload))
                except Exception as exc:
                    on_error(str(exc))
                else:
                    on_result(res)

        widget = SetupWidget(FakeUIAPI())
        self.addCleanup(widget.close)
        widget.on_activated()
        app.processEvents()

        self.assertFalse(widget.apply_button.isEnabled())
        self.assertTrue(widget.save_button.isEnabled())

        # Verify excluded settings (Filter/Excitation/Reversal) do NOT appear in UI.
        all_labels = " ".join(
            lbl.text() for lbl in widget.findChildren(type(widget.status_label))
        )
        for excluded in ("Filter", "Excitation", "Reversal"):
            self.assertNotIn(excluded, all_labels)

        # Edit Sensor A curve, Output A range + Zone enabled, and Zone1 boundary.
        type_combo, curve_spin, _ = widget._sensor_widgets["A"]
        type_combo.setCurrentText("PT100")
        curve_spin.setValue(12)
        widget._output_widgets["A"]["range_combo"].setCurrentText("LOW")
        widget._output_widgets["A"]["zone_check"].setChecked(True)
        widget._zone_tables["A"].cellWidget(0, 1).setValue(120.0)
        app.processEvents()
        self.assertTrue(widget.apply_button.isEnabled())

        widget.apply_changes()
        app.processEvents()
        self.assertFalse(widget.apply_button.isEnabled())
        self.assertIn("[SET:T:A:PT100]", transport.writes)
        self.assertIn("[SET:W:A:12]", transport.writes)
        self.assertIn("[SET:RANGE:A:LOW]", transport.writes)
        self.assertIn("[SET:ZONE:A:ON]", transport.writes)
        self.assertIn("[SET:ZONE:A:ZONE1:BOUNDARY:120.00]", transport.writes)

        # Explicit Save to Instrument sends [SET:SAVE].
        widget.save_to_instrument()
        app.processEvents()
        self.assertIn("[SET:SAVE]", transport.writes)
        self.assertIn("[SET:SAVE]", widget.status_label.text())
        ctrl.close()

    def test_scanner_and_dual_loop_instrument_manager_integration(self) -> None:
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            configs = Path(directory) / "configs"
            configs.mkdir()
            instruments_dir = INSTRUMENT.parent
            general = configs / "general.toml"
            general.write_text(
                '[system_instruments]\ndirectory = "' + instruments_dir.as_posix() + '"\n',
                encoding="utf-8",
            )
            with patch.object(InstrumentScannerWindow, "start_scan"):
                window = InstrumentScannerWindow(configs, instruments_dir)
                self.addCleanup(window.close)
                app.processEvents()
            window._scan_completed(
                (VisaScanResult("ASRL3::INSTR", error="No IDN response"),)
            )
            page = window._instrument_pages["kelvinion"]
            window._add_instance(page["descriptor"])
            row = page["instances"][0]
            row["resource"].setCurrentIndex(row["resource"].findData("ASRL3::INSTR"))
            # Assign sample_temp role to Output 1 (control_1), keep Output 2 (control_2) as none.
            for panel_entry in row["panels"]:
                if panel_entry["descriptor"].id == "control_1":
                    role_combo = panel_entry["fields"]["role"]
                    role_combo.setCurrentIndex(role_combo.findData("sample_temp"))
                elif panel_entry["descriptor"].id == "control_2":
                    role_combo = panel_entry["fields"]["role"]
                    role_combo.setCurrentIndex(role_combo.findData("none"))
            window.pages.setCurrentIndex(window.review_page_index)
            plan = window._build_save_plan()
            self.assertEqual(plan.incomplete_instances, ())
            execute_save_plan(window.visa_path, plan)

            app_config = load_config(general)
            inst_cfg = app_config.instrument("kelvinion")
            self.assertEqual(inst_cfg.address, "ASRL3::INSTR")
            self.assertTrue(inst_cfg.extras["output_2_enabled"])

            # Run full dual-control InstrumentManager lifecycle against mocked PyVISA transport!
            transport = FakeKelvinionTransport(ramping_working_setpoint=True)

            async def run_manager() -> None:
                descriptor = load_instrument_manifest(INSTRUMENT)
                manager = InstrumentManager(
                    app_config,
                    EventManager(),
                    descriptors=(descriptor,),
                    isolate_processes=False,
                )
                # Inject fake transport into the loaded backend instance.
                manager.instruments["kelvinion"].backend._transport_factory = (  # type: ignore[attr-defined]
                    lambda *a, **k: transport
                )
                await manager.connect_all()
                snaps = await manager.poll_all()
                snap = snaps["kelvinion"]
                self.assertEqual(snap.controls["control_1"].current, 10.0)
                self.assertEqual(snap.controls["control_2"].current, 20.5)

                # Command Output 1 via standard sample_temp role & Output 2 independently!
                self.assertTrue(
                    await manager.set_target_by_kind(
                        InstrumentKind.TEMPERATURE, 40.0, 2.0
                    )
                )
                self.assertTrue(
                    await manager.set_target(
                        "kelvinion", 30.0, 1.5, control="output_2"
                    )
                )
                transport.responses["[READ:SETP:A]"] = "[15.000]\r\n"
                transport.responses["[READ:SETP:B]"] = "[22.000]\r\n"
                snaps2 = await manager.poll_all()
                self.assertEqual(
                    snaps2["kelvinion"].controls["control_1"].activity,
                    InstrumentActivity.MOVING,
                )
                self.assertEqual(
                    snaps2["kelvinion"].controls["control_2"].activity,
                    InstrumentActivity.MOVING,
                )
                self.assertEqual(snaps2["kelvinion"].controls["control_1"].target, 40.0)
                self.assertEqual(snaps2["kelvinion"].controls["control_2"].target, 30.0)

                # hold_all holds BOTH output_1 and output_2 independently!
                transport.writes.clear()
                self.assertTrue(await manager.hold_all())
                self.assertEqual(
                    transport.writes,
                    ["[SET:SETP:A:10.00K]", "[SET:SETP:B:20.50K]"],
                )
                await manager.disconnect_all()

            asyncio.run(run_manager())


if __name__ == "__main__":
    unittest.main()
