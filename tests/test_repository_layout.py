from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT.parent / "OpenLabControl" / "src"
if CORE_SRC.is_dir():
    sys.path.insert(0, str(CORE_SRC))

from labcontrol.instruments.manifest import (  # noqa: E402
    load_instrument_manifest,
)


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class RepositoryLayoutTests(unittest.TestCase):
    def test_every_instrument_has_manifest_and_backend(self) -> None:
        self.assertFalse((ROOT / "plugins").exists())
        instrument_paths = sorted((ROOT / "instruments").iterdir())
        self.assertTrue(instrument_paths)
        for instrument_path in instrument_paths:
            with self.subTest(instrument=instrument_path.name):
                self.assertTrue((instrument_path / "instrument.toml").is_file())
                self.assertTrue((instrument_path / "backend.py").is_file())
                self.assertFalse((instrument_path / "device.toml").exists())
                self.assertFalse((instrument_path / "runtime.json").exists())

    def test_every_manifest_uses_named_api_v4_templates(self) -> None:
        for instrument_path in sorted((ROOT / "instruments").iterdir()):
            with self.subTest(instrument=instrument_path.name):
                descriptor = load_instrument_manifest(instrument_path)
                self.assertTrue(descriptor.valid, descriptor.error)
                with (instrument_path / "instrument.toml").open("rb") as handle:
                    manifest = tomllib.load(handle)

                self.assertEqual(manifest["api_version"], "4")
                self.assertNotIn("panel", manifest)
                self.assertNotIn("settings", manifest)
                self.assertNotIn("main_reading", manifest)
                self.assertNotIn("main_readings", manifest)

                readings = set(manifest["readings"])
                commands = {
                    command["id"]
                    for command in manifest.get("sequence_commands", [])
                }
                controls = {
                    control["id"] for control in manifest.get("controls", [])
                }
                panels = manifest["panels"]
                panel_ids = [panel["id"] for panel in panels]
                self.assertEqual(len(panel_ids), len(set(panel_ids)))
                self.assertTrue(all(IDENTIFIER.fullmatch(item) for item in panel_ids))

                field_ids = [
                    field["id"]
                    for field in manifest.get("config_fields", [])
                ]
                self.assertEqual(len(field_ids), len(set(field_ids)))

                for panel in panels:
                    template = panel["template"]
                    if template == "controller":
                        self.assertIn(panel["control"], controls)
                        self.assertIn(
                            panel["default_reading"],
                            panel["reading_options"],
                        )
                        self.assertLessEqual(
                            set(panel["reading_options"]), readings
                        )
                    elif template in {"readout", "readout_grid"}:
                        self.assertLessEqual(set(panel["readings"]), readings)
                    elif template == "switch":
                        self.assertIn(panel["reading"], readings)
                        self.assertLessEqual(set(panel["commands"]), commands)
                    else:
                        self.fail(f"Unknown panel template: {template}")


if __name__ == "__main__":
    unittest.main()
