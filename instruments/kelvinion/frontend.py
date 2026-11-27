"""MultiFields Kelvinion 的 Instrument Setup 界面（按需读写，不含 Filter/Excitation/Reversal）。"""

from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)


_SENSOR_TYPES = ("CERNOX", "PT100", "PT1000", "DIODE", "RO105")
_HEATER_RANGES = ("OFF", "LOW", "MED", "HIGH")
_ZONE_NAMES = ("ZONE1", "ZONE2", "ZONE3", "ZONE4", "ZONE5")


class SetupWidget(QWidget):
    """按需查询并配置 Kelvinion 传感器类型/曲线、双回路输出设置与硬件 Zone 表。

    按当前需求，暂不提供 Filter、Excitation 与 Reversal 配置项。
    """

    def __init__(self, api: Any) -> None:
        super().__init__()
        self.api = api
        self._busy = False
        self._loaded = False
        self._data: dict[str, Any] = {}
        self._input_channels: list[str] = []
        self._output_channels: list[str] = []

        self._sensor_widgets: dict[str, tuple[QComboBox, QSpinBox, QLabel]] = {}
        self._output_widgets: dict[str, dict[str, Any]] = {}
        self._zone_tables: dict[str, QTableWidget] = {}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        hint = QLabel(
            "On-demand setup: settings are read only when opened or refreshed. "
            "Apply confirms each write via readback; use Save to Instrument ([SET:SAVE]) "
            "to persist settings to non-volatile memory."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        # Tab 1: Sensors
        self._sensors_tab = QWidget(self.tabs)
        self._sensors_layout = QVBoxLayout(self._sensors_tab)
        self._sensors_grid_host = QWidget(self._sensors_tab)
        self._sensors_grid = QGridLayout(self._sensors_grid_host)
        self._sensors_grid.setContentsMargins(4, 4, 4, 4)
        self._sensors_grid.setHorizontalSpacing(10)
        self._sensors_grid.setVerticalSpacing(6)
        self._sensors_layout.addWidget(self._sensors_grid_host)
        curve_note = QLabel(
            "Factory default curves: 0=Cernox, 1=PT100, 2=PT1000, 3=DT670 Diode "
            "(user curves MF00-MF99; exact upper bound subject to HV-08 verification)."
        )
        curve_note.setWordWrap(True)
        curve_note.setObjectName("mutedLabel")
        self._sensors_layout.addWidget(curve_note)
        self._sensors_layout.addStretch(1)
        self.tabs.addTab(self._sensors_tab, "Sensors")

        # Tab 2: Outputs
        self._outputs_tab = QWidget(self.tabs)
        self._outputs_layout = QVBoxLayout(self._outputs_tab)
        self._outputs_container = QVBoxLayout()
        self._outputs_layout.addLayout(self._outputs_container)
        self._outputs_layout.addStretch(1)
        self.tabs.addTab(self._outputs_tab, "Outputs")

        # Tab 3: Hardware Zones (ZONE1-ZONE5)
        self._zones_tab = QWidget(self.tabs)
        self._zones_layout = QVBoxLayout(self._zones_tab)
        zone_bar = QHBoxLayout()
        zone_bar.addWidget(QLabel("Output:"))
        self._zone_output_combo = QComboBox(self._zones_tab)
        self._zone_output_combo.currentIndexChanged.connect(self._switch_zone_table)
        zone_bar.addWidget(self._zone_output_combo)
        zone_bar.addStretch(1)
        self._zones_layout.addLayout(zone_bar)
        self._zones_stack_host = QVBoxLayout()
        self._zones_layout.addLayout(self._zones_stack_host, 1)
        zone_note = QLabel(
            "Hardware Zone parameters (P, I, D, Range, Upper Boundary K). "
            "Zone format and Range interaction are tagged HV-07 for hardware verification."
        )
        zone_note.setWordWrap(True)
        zone_note.setObjectName("mutedLabel")
        self._zones_layout.addWidget(zone_note)
        self.tabs.addTab(self._zones_tab, "Zones")

        self.status_label = QLabel("Click Refresh to read current settings from Kelvinion.")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.save_button = QPushButton("Save to Instrument")
        self.apply_button = QPushButton("Apply")
        self.refresh_button.clicked.connect(lambda _checked=False: self.refresh())
        self.save_button.clicked.connect(lambda _checked=False: self.save_to_instrument())
        self.apply_button.clicked.connect(lambda _checked=False: self.apply_changes())
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self._update_buttons()

    def on_activated(self) -> None:
        if self.api.connected and not self._busy:
            self.refresh()
        else:
            self._update_buttons()

    def set_editable(self, _editable: bool) -> None:
        self._update_buttons()

    def update_snapshot(self, _snapshot: Any) -> None:
        self._update_buttons()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.status_label.setText("Reading sensors, outputs, and Zone tables from Kelvinion...")
        self._update_buttons()
        self.api.call_action(
            "read_setup",
            {},
            read_only=True,
            on_result=self._on_setup_loaded,
            on_error=self._on_error,
        )

    def apply_changes(self) -> None:
        if self._busy or not self.api.editable:
            return
        payload = self._collect_changes()
        if not payload["sensors"] and not payload["outputs"]:
            return
        self._busy = True
        self.status_label.setText("Writing setup changes and confirming readback...")
        self._update_buttons()
        self.api.call_action(
            "apply_setup",
            payload,
            read_only=False,
            on_result=self._on_apply_succeeded,
            on_error=self._on_error,
        )

    def save_to_instrument(self) -> None:
        if self._busy or not self.api.editable or not self._loaded:
            return
        self._busy = True
        self.status_label.setText("Saving configuration to Kelvinion non-volatile memory ([SET:SAVE])...")
        self._update_buttons()
        self.api.call_action(
            "save_config",
            {},
            read_only=False,
            on_result=self._on_save_succeeded,
            on_error=self._on_error,
        )

    # ---- Build & populate UI ----

    def _rebuild_if_needed(self, data: dict[str, Any]) -> None:
        inputs = [str(ch) for ch in data.get("input_channels", [])]
        outputs = [str(out) for out in data.get("output_channels", [])]
        if inputs == self._input_channels and outputs == self._output_channels:
            return
        self._input_channels = inputs
        self._output_channels = outputs

        self._clear_layout(self._sensors_grid)
        self._sensor_widgets.clear()
        for col, header in enumerate(("Input", "Sensor Type", "Curve #", "Curve Limit")):
            lbl = QLabel(header)
            lbl.setStyleSheet("font-weight: 600;")
            self._sensors_grid.addWidget(lbl, 0, col)
        curve_min = int(data.get("curve_min", 0))
        curve_max = int(data.get("curve_max", 99))
        sensor_types = list(data.get("sensor_types", _SENSOR_TYPES))
        for row, ch in enumerate(inputs, start=1):
            self._sensors_grid.addWidget(QLabel(f"Input {ch}"), row, 0)
            type_combo = QComboBox(self._sensors_grid_host)
            type_combo.addItems(sensor_types)
            type_combo.currentIndexChanged.connect(self._update_buttons)
            curve_spin = QSpinBox(self._sensors_grid_host)
            curve_spin.setRange(curve_min, curve_max)
            curve_spin.valueChanged.connect(self._update_buttons)
            limit_label = QLabel("—")
            limit_label.setObjectName("mutedLabel")
            self._sensors_grid.addWidget(type_combo, row, 1)
            self._sensors_grid.addWidget(curve_spin, row, 2)
            self._sensors_grid.addWidget(limit_label, row, 3)
            self._sensor_widgets[ch] = (type_combo, curve_spin, limit_label)

        self._clear_layout(self._outputs_container)
        self._output_widgets.clear()
        heater_ranges = list(data.get("heater_ranges", _HEATER_RANGES))
        loop_options = [*inputs, "OFF"]
        for out in outputs:
            title = "Output 1 (Heater A)" if out == "A" else "Output 2 (Heater B)"
            group = QGroupBox(title, self._outputs_tab)
            form = QFormLayout(group)
            loop_combo = QComboBox(group)
            loop_combo.addItems(loop_options)
            loop_combo.currentIndexChanged.connect(self._update_buttons)
            range_combo = QComboBox(group)
            range_combo.addItems(heater_ranges)
            range_combo.currentIndexChanged.connect(self._update_buttons)
            pid_limit_spin = QDoubleSpinBox(group)
            pid_limit_spin.setRange(0.0, 999.0)
            pid_limit_spin.setDecimals(2)
            pid_limit_spin.setSuffix(" K")
            pid_limit_spin.valueChanged.connect(self._update_buttons)
            zone_check = QCheckBox("Enable Hardware Zone Table", group)
            zone_check.stateChanged.connect(self._update_buttons)
            mode_label = QLabel("—")
            mode_label.setWordWrap(True)
            mode_label.setObjectName("mutedLabel")
            form.addRow("Control Loop Input", loop_combo)
            form.addRow("Heater Range", range_combo)
            form.addRow("PID Limit", pid_limit_spin)
            form.addRow("Zone Control", zone_check)
            form.addRow("Mode / Active Zone", mode_label)
            self._outputs_container.addWidget(group)
            self._output_widgets[out] = {
                "loop_combo": loop_combo,
                "range_combo": range_combo,
                "pid_limit_spin": pid_limit_spin,
                "zone_check": zone_check,
                "mode_label": mode_label,
            }

        self._clear_layout(self._zones_stack_host)
        self._zone_tables.clear()
        self._zone_output_combo.blockSignals(True)
        self._zone_output_combo.clear()
        for out in outputs:
            label = "Output 1 (Heater A)" if out == "A" else "Output 2 (Heater B)"
            self._zone_output_combo.addItem(label, out)
            table = self._build_zone_table(heater_ranges)
            self._zone_tables[out] = table
            self._zones_stack_host.addWidget(table)
        self._zone_output_combo.blockSignals(False)
        self._switch_zone_table()

    def _build_zone_table(self, heater_ranges: list[str]) -> QTableWidget:
        table = QTableWidget(len(_ZONE_NAMES), 6, self._zones_tab)
        table.setHorizontalHeaderLabels(
            ["Zone", "Upper (K)", "P", "I", "D (%)", "Range"]
        )
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(32)
        table.setMinimumHeight(195)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)

        for row, zname in enumerate(_ZONE_NAMES):
            name_lbl = QLabel(zname)
            name_lbl.setContentsMargins(6, 0, 6, 0)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setCellWidget(row, 0, name_lbl)

            boundary = QDoubleSpinBox(table)
            boundary.setRange(0.0, 999.0)
            boundary.setDecimals(2)
            boundary.valueChanged.connect(self._update_buttons)
            table.setCellWidget(row, 1, boundary)

            kp = QDoubleSpinBox(table)
            kp.setRange(0.0, 1000.0)
            kp.setDecimals(2)
            kp.valueChanged.connect(self._update_buttons)
            table.setCellWidget(row, 2, kp)

            ki = QDoubleSpinBox(table)
            ki.setRange(0.0, 1000.0)
            ki.setDecimals(2)
            ki.valueChanged.connect(self._update_buttons)
            table.setCellWidget(row, 3, ki)

            kd = QDoubleSpinBox(table)
            kd.setRange(0.0, 200.0)
            kd.setDecimals(1)
            kd.valueChanged.connect(self._update_buttons)
            table.setCellWidget(row, 4, kd)

            rng = QComboBox(table)
            rng.addItems(heater_ranges)
            rng.currentIndexChanged.connect(self._update_buttons)
            table.setCellWidget(row, 5, rng)

        return table

    def _switch_zone_table(self, *_args: object) -> None:
        selected = self._zone_output_combo.currentData()
        for out, table in self._zone_tables.items():
            table.setVisible(out == selected)

    def _populate(self, data: dict[str, Any]) -> None:
        self._rebuild_if_needed(data)
        self._data = data
        sensors = data.get("sensors", {})
        for ch, (type_combo, curve_spin, limit_label) in self._sensor_widgets.items():
            info = sensors.get(ch, {}) if isinstance(sensors, dict) else {}
            type_combo.blockSignals(True)
            curve_spin.blockSignals(True)
            sensor_type = str(info.get("sensor_type", ""))
            idx = type_combo.findText(sensor_type)
            if idx >= 0:
                type_combo.setCurrentIndex(idx)
            curve_val = info.get("curve")
            if isinstance(curve_val, int):
                curve_spin.setValue(curve_val)
            limit_val = info.get("input_limit_k")
            limit_label.setText(
                f"{float(limit_val):.2f} K"
                if isinstance(limit_val, (int, float))
                else "—"
            )
            type_combo.blockSignals(False)
            curve_spin.blockSignals(False)

        outputs = data.get("outputs", {})
        for out, widgets in self._output_widgets.items():
            info = outputs.get(out, {}) if isinstance(outputs, dict) else {}
            loop_combo: QComboBox = widgets["loop_combo"]
            range_combo: QComboBox = widgets["range_combo"]
            pid_limit_spin: QDoubleSpinBox = widgets["pid_limit_spin"]
            zone_check: QCheckBox = widgets["zone_check"]
            mode_label: QLabel = widgets["mode_label"]

            for w in (loop_combo, range_combo, pid_limit_spin, zone_check):
                w.blockSignals(True)

            loop_idx = loop_combo.findText(str(info.get("loop_input", "OFF")))
            if loop_idx >= 0:
                loop_combo.setCurrentIndex(loop_idx)
            range_idx = range_combo.findText(str(info.get("heater_range", "OFF")))
            if range_idx >= 0:
                range_combo.setCurrentIndex(range_idx)
            pid_limit = info.get("pid_limit_k")
            if isinstance(pid_limit, (int, float)):
                pid_limit_spin.setValue(float(pid_limit))
            zone_check.setChecked(bool(info.get("zone_enabled", False)))

            expected = str(info.get("expected_input", ""))
            expected_note = f" (Scanner expects Input {expected})" if expected else ""
            mode_label.setText(
                f"{info.get('mode', '—')} / Zone: {info.get('zone_state', '—')}{expected_note}"
            )

            for w in (loop_combo, range_combo, pid_limit_spin, zone_check):
                w.blockSignals(False)

            table = self._zone_tables.get(out)
            raw_zones = info.get("zones", [])
            if table is not None and isinstance(raw_zones, list):
                zones_by_name = {
                    str(item.get("name", "")).upper(): item
                    for item in raw_zones
                    if isinstance(item, dict)
                }
                for row, zname in enumerate(_ZONE_NAMES):
                    entry = zones_by_name.get(zname, {})
                    boundary: QDoubleSpinBox = table.cellWidget(row, 1)  # type: ignore[assignment]
                    kp: QDoubleSpinBox = table.cellWidget(row, 2)  # type: ignore[assignment]
                    ki: QDoubleSpinBox = table.cellWidget(row, 3)  # type: ignore[assignment]
                    kd: QDoubleSpinBox = table.cellWidget(row, 4)  # type: ignore[assignment]
                    rng: QComboBox = table.cellWidget(row, 5)  # type: ignore[assignment]
                    for w in (boundary, kp, ki, kd, rng):
                        w.blockSignals(True)
                    if isinstance(entry.get("boundary_k"), (int, float)):
                        boundary.setValue(float(entry["boundary_k"]))
                    if isinstance(entry.get("kp"), (int, float)):
                        kp.setValue(float(entry["kp"]))
                    if isinstance(entry.get("ki"), (int, float)):
                        ki.setValue(float(entry["ki"]))
                    if isinstance(entry.get("kd"), (int, float)):
                        kd.setValue(float(entry["kd"]))
                    r_idx = rng.findText(str(entry.get("heater_range", "OFF")))
                    if r_idx >= 0:
                        rng.setCurrentIndex(r_idx)
                    for w in (boundary, kp, ki, kd, rng):
                        w.blockSignals(False)

        self._loaded = True

    def _collect_changes(self) -> dict[str, Any]:
        sensor_changes: dict[str, dict[str, Any]] = {}
        orig_sensors = self._data.get("sensors", {}) if isinstance(self._data, dict) else {}
        for ch, (type_combo, curve_spin, _limit) in self._sensor_widgets.items():
            orig = orig_sensors.get(ch, {}) if isinstance(orig_sensors, dict) else {}
            diff: dict[str, Any] = {}
            selected_type = type_combo.currentText()
            if selected_type and selected_type != orig.get("sensor_type"):
                diff["sensor_type"] = selected_type
            selected_curve = int(curve_spin.value())
            if selected_curve != orig.get("curve"):
                diff["curve"] = selected_curve
            if diff:
                sensor_changes[ch] = diff

        output_changes: dict[str, dict[str, Any]] = {}
        orig_outputs = self._data.get("outputs", {}) if isinstance(self._data, dict) else {}
        for out, widgets in self._output_widgets.items():
            orig = orig_outputs.get(out, {}) if isinstance(orig_outputs, dict) else {}
            diff = {}
            loop_val = widgets["loop_combo"].currentText()
            if loop_val and loop_val != orig.get("loop_input"):
                diff["loop_input"] = loop_val
            range_val = widgets["range_combo"].currentText()
            if range_val and range_val != orig.get("heater_range"):
                diff["heater_range"] = range_val
            limit_val = round(float(widgets["pid_limit_spin"].value()), 2)
            orig_limit = orig.get("pid_limit_k")
            if not isinstance(orig_limit, (int, float)) or not math.isclose(
                limit_val, float(orig_limit), abs_tol=1e-4
            ):
                diff["pid_limit_k"] = limit_val
            zone_enabled = bool(widgets["zone_check"].isChecked())
            if zone_enabled != bool(orig.get("zone_enabled", False)):
                diff["zone_enabled"] = zone_enabled

            table = self._zone_tables.get(out)
            orig_zones_list = orig.get("zones", [])
            orig_zones = {
                str(item.get("name", "")).upper(): item
                for item in orig_zones_list
                if isinstance(item, dict)
            }
            if table is not None:
                z_changes: dict[str, dict[str, Any]] = {}
                for row, zname in enumerate(_ZONE_NAMES):
                    zorig = orig_zones.get(zname, {})
                    zdiff: dict[str, Any] = {}
                    boundary = round(float(table.cellWidget(row, 1).value()), 2)  # type: ignore[union-attr]
                    kp = round(float(table.cellWidget(row, 2).value()), 2)  # type: ignore[union-attr]
                    ki = round(float(table.cellWidget(row, 3).value()), 2)  # type: ignore[union-attr]
                    kd = round(float(table.cellWidget(row, 4).value()), 1)  # type: ignore[union-attr]
                    rng = str(table.cellWidget(row, 5).currentText())  # type: ignore[union-attr]
                    if not isinstance(zorig.get("boundary_k"), (int, float)) or not math.isclose(
                        boundary, float(zorig["boundary_k"]), abs_tol=1e-4
                    ):
                        zdiff["boundary_k"] = boundary
                    if not isinstance(zorig.get("kp"), (int, float)) or not math.isclose(
                        kp, float(zorig["kp"]), abs_tol=1e-4
                    ):
                        zdiff["kp"] = kp
                    if not isinstance(zorig.get("ki"), (int, float)) or not math.isclose(
                        ki, float(zorig["ki"]), abs_tol=1e-4
                    ):
                        zdiff["ki"] = ki
                    if not isinstance(zorig.get("kd"), (int, float)) or not math.isclose(
                        kd, float(zorig["kd"]), abs_tol=1e-4
                    ):
                        zdiff["kd"] = kd
                    if rng and rng != zorig.get("heater_range"):
                        zdiff["heater_range"] = rng
                    if zdiff:
                        z_changes[zname] = zdiff
                if z_changes:
                    diff["zones"] = z_changes

            if diff:
                output_changes[out] = diff

        return {"sensors": sensor_changes, "outputs": output_changes}

    def _on_setup_loaded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate(data)
        model = str(data.get("model", "Kelvinion"))
        fw = str(data.get("firmware", ""))
        suffix = f" ({fw})" if fw else ""
        self.status_label.setText(f"Loaded settings from {model}{suffix}.")
        self._update_buttons()

    def _on_apply_succeeded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate(data)
        summary = str(data.get("summary", "")).strip() or "Kelvinion settings updated."
        self.status_label.setText(summary)
        self._update_buttons()

    def _on_save_succeeded(self, data: dict[str, Any]) -> None:
        self._busy = False
        summary = (
            str(data.get("summary", "")).strip()
            or "Saved Kelvinion settings to non-volatile memory."
        )
        self.status_label.setText(summary)
        self._update_buttons()

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.status_label.setText(f"Error: {message}")
        self._update_buttons()

    def _update_buttons(self, *_args: object) -> None:
        connected = bool(self.api.connected)
        editable = bool(self.api.editable)
        changes = self._collect_changes() if self._loaded else {"sensors": {}, "outputs": {}}
        has_changes = bool(changes["sensors"] or changes["outputs"])
        can_edit = editable and not self._busy and self._loaded

        self.refresh_button.setEnabled(connected and not self._busy)
        self.apply_button.setEnabled(can_edit and has_changes)
        self.save_button.setEnabled(can_edit)

        for type_combo, curve_spin, _limit in self._sensor_widgets.values():
            type_combo.setEnabled(can_edit)
            curve_spin.setEnabled(can_edit)
        for widgets in self._output_widgets.values():
            for key in ("loop_combo", "range_combo", "pid_limit_spin", "zone_check"):
                widgets[key].setEnabled(can_edit)
        for table in self._zone_tables.values():
            for row in range(table.rowCount()):
                for col in range(1, table.columnCount()):
                    widget = table.cellWidget(row, col)
                    if widget is not None:
                        widget.setEnabled(can_edit)

    @staticmethod
    def _clear_layout(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                SetupWidget._clear_layout(item.layout())


__all__ = ["SetupWidget"]
