from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import importlib.util
import os
import re
import sys
import tempfile
import unittest

import pyvisa

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT.parent / "OpenLabControl"
sys.path.insert(0, str(CORE / "src"))
sys.path.insert(0, str(CORE / "tools"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from instrument_scanner import InstrumentScannerWindow, VisaScanResult, execute_save_plan
from labcontrol.config import load_config
from labcontrol.instruments.base import InstrumentError, SafetyViolation
from labcontrol.instruments.manifest import load_instrument_manifest

INSTRUMENT = ROOT / "instruments" / "mf_kelvinion"
spec = importlib.util.spec_from_file_location("kelvinion_backend", INSTRUMENT / "backend.py")
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)


class KelvinionTests(unittest.TestCase):
    def setUp(self):
        self.responses = {
            "[*IDN?]": "[MultiFields Technologies,Kelvinion,1005,V2.1.16]\r\n",
            "[READ:LOOP:A]": "[B]",
            "[READ:LOOP:B]": "[A]",
            "[READ:MODE:A]": "[A]",
            "[READ:MODE:B]": "[A]",
            "[READ:LIMIT:A]": "[300]",
            "[READ:LIMIT:B]": "[300]",
            "[READ:INPUT_LIMIT:A]": "[350]",
            "[READ:INPUT_LIMIT:B]": "[350]",
            "[READ:K:A]": "[8.5]",
            "[READ:K:B]": "[10.0]",
            "[READ:SETP:A]": "[10]",
            "[READ:RAMP:A]": "[3]",
            "[READ:POWER:A]": "[50]",
            "[READ:RANGE:B]": "[OFF]",
        }
        self.session = Mock()
        self.session.query.side_effect = self.responses.__getitem__

        def write(command):
            setting, output, value = command[5:-1].split(":")
            self.responses[f"[READ:{setting}:{output}]"] = f"[{value.removesuffix('K')}]"

        self.session.write.side_effect = write
        self.manager = Mock()
        self.manager.open_resource.return_value = self.session
        self.enterContext(patch.object(pyvisa, "ResourceManager", return_value=self.manager))
        self.config = SimpleNamespace(
            id="kelvinion", address="ASRL3::INSTR", main_reading="temp_b",
            auxiliary_readings=("temp_a", "loop_1_power", "loop_2_range"),
            min_value=3.0, max_value=300.0, max_rate_per_minute=10.0,
            operation_timeout_seconds=30.0,
            extras={"loop2_enabled": True, "loop2_input": "A",
                    "loop2_setpoint": "0.8*x+0.5", "visa_timeout_ms": 1000},
        )

    def open_driver(self):
        driver = backend.Kelvinion(self.config)
        driver.open()
        self.addCleanup(driver.close)
        return driver

    def test_manifest_defaults_and_identity(self):
        descriptor = load_instrument_manifest(INSTRUMENT)
        self.assertTrue(descriptor.valid, descriptor.error)
        self.assertRegex(self.responses["[*IDN?]"], re.compile(descriptor.identity_pattern))
        panel = descriptor.panels[0]
        self.assertEqual((panel.min_value, panel.max_value), (3.0, 300.0))
        self.assertEqual((panel.default_rate_per_minute, panel.max_rate_per_minute), (3.0, 10.0))

    def test_scanner_saves_usb_resource_without_idn_and_core_loads_formula(self):
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            configs = Path(directory) / "configs"
            configs.mkdir()
            instruments = INSTRUMENT.parent
            general = configs / "general.toml"
            general.write_text(
                '[system_instruments]\ndirectory = "' + instruments.as_posix() + '"\n',
                encoding="utf-8",
            )
            with patch.object(InstrumentScannerWindow, "start_scan"):
                window = InstrumentScannerWindow(configs, instruments)
                self.addCleanup(window.close)
                app.processEvents()
            window._scan_completed((VisaScanResult("ASRL3::INSTR", error="No IDN response"),))
            page = window._instrument_pages["mf_kelvinion"]
            window._add_instance(page["descriptor"])
            row = page["instances"][0]
            row["resource"].setCurrentIndex(row["resource"].findData("ASRL3::INSTR"))
            row["fields"]["loop2_enabled"][1].setChecked(True)
            row["fields"]["loop2_setpoint"][1].setText("0.8*x+0.5")
            window.pages.setCurrentIndex(window.review_page_index)
            plan = window._build_save_plan()
            self.assertEqual(plan.incomplete_instances, ())
            execute_save_plan(window.visa_path, plan)
            config = load_config(general).instrument("mf_kelvinion")
            self.assertEqual(config.address, "ASRL3::INSTR")
            self.assertEqual(config.extras["loop2_setpoint"], "0.8*x+0.5")
            self.assertEqual((config.min_value, config.max_value), (3, 300))

    def test_usb_and_status_and_measurement(self):
        driver = self.open_driver()
        self.assertEqual(self.manager.open_resource.call_args.args, ("ASRL3::INSTR",))
        options = self.manager.open_resource.call_args.kwargs
        self.assertEqual((options["baud_rate"], options["data_bits"], options["read_termination"]),
                         (115200, 8, "\r\n"))
        self.assertEqual(driver.read_status(), {
            "value": 10.0, "target": 10.0, "rate": 3.0,
            "auxiliary": {"temp_a": 8.5, "loop_1_power": 50.0, "loop_2_range": "OFF"},
        })
        self.session.query.reset_mock()
        self.assertEqual(driver.read_measurement(), {"value": 10.0})
        self.session.query.assert_called_once_with("[READ:K:B]")
        driver.close()
        self.session.write.assert_not_called()
        self.session.close.assert_called_once()
        self.manager.close.assert_called_once()

    def test_linked_target_and_hold(self):
        driver = self.open_driver()
        driver.set_target(20, 3, control="main")
        self.assertEqual([call.args[0] for call in self.session.write.call_args_list],
                         ["[SET:RAMP:A:3.00]", "[SET:RAMP:B:3.00]",
                          "[SET:SETP:A:20.00K]", "[SET:SETP:B:16.50K]"])
        self.session.write.reset_mock()
        driver.hold(control="main")
        self.assertEqual([call.args[0] for call in self.session.write.call_args_list],
                         ["[SET:SETP:A:10.00K]", "[SET:SETP:B:8.50K]"])

    def test_disabled_output_is_not_written(self):
        self.config.extras["loop2_enabled"] = False
        driver = self.open_driver()
        driver.set_target(20, 3, control="main")
        self.assertEqual([call.args[0] for call in self.session.write.call_args_list],
                         ["[SET:RAMP:A:3.00]", "[SET:SETP:A:20.00K]"])

    def test_linked_limits_and_invalid_settings_block_all_writes(self):
        driver = self.open_driver()
        with self.assertRaisesRegex(SafetyViolation, "Output B formula target"):
            driver.set_target(3, 3, control="main")
        self.responses["[READ:LIMIT:B]"] = "[15]"
        with self.assertRaisesRegex(SafetyViolation, "Output B target"):
            driver.set_target(20, 3, control="main")
        with self.assertRaises(SafetyViolation):
            driver.set_target(20, 10.1, control="main")
        with self.assertRaises(SafetyViolation):
            driver.set_target(20.001, 3, control="main")
        self.session.write.assert_not_called()

    def test_invalid_formula_and_division_by_zero(self):
        self.config.extras["loop2_setpoint"] = "abs(x)"
        with self.assertRaisesRegex(InstrumentError, "Invalid Output 2 target formula"):
            backend.Kelvinion(self.config)
        self.config.extras["loop2_setpoint"] = "1/(x-20)"
        driver = self.open_driver()
        with self.assertRaisesRegex(SafetyViolation, "Cannot calculate"):
            driver.set_target(20, 3, control="main")
        self.session.write.assert_not_called()

    def test_failed_readback_stops_without_resending(self):
        driver = self.open_driver()
        self.session.write.side_effect = None
        with self.assertRaises(InstrumentError) as failure:
            driver.set_target(20, 4, control="main")
        self.assertEqual(failure.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")
        self.session.write.assert_called_once_with("[SET:RAMP:A:4.00]")

    def test_linked_sensor_is_checked_even_without_its_monitor_panel(self):
        self.config.auxiliary_readings = ()
        driver = self.open_driver()
        self.responses["[READ:K:A]"] = "[0]"
        with self.assertRaisesRegex(InstrumentError, "Invalid Temp A"):
            driver.read_status()
        self.assertEqual(driver.read_measurement(), {"value": 10.0})

    def test_output_sensor_rebind_or_manual_mode_blocks_writes(self):
        driver = self.open_driver()
        self.responses["[READ:LOOP:B]"] = "[C]"
        with self.assertRaisesRegex(SafetyViolation, "expected A"):
            driver.set_target(20, 3, control="main")
        self.responses["[READ:LOOP:B]"] = "[A]"
        self.responses["[READ:MODE:B]"] = "[M]"
        with self.assertRaisesRegex(SafetyViolation, "Auto mode"):
            driver.set_target(20, 3, control="main")
        self.session.write.assert_not_called()

    def test_bad_identity_closes_without_writes(self):
        self.responses["[*IDN?]"] = "[MultiFields Technologies,Kelvinion Monitor,1005,V2.1.16]"
        with self.assertRaisesRegex(InstrumentError, "Unexpected instrument identity"):
            backend.Kelvinion(self.config).open()
        self.session.write.assert_not_called()
        self.session.close.assert_called_once()
