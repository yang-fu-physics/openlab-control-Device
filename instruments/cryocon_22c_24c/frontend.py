"""Cryo-con 22C/24C 的 Instrument Setup 界面：传感器曲线、软件侧 PID 表与 24C 联动公式预览。"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class SetupWidget(QWidget):
    """按需查询并切换 Cryo-con 22C (A/B) 或 24C (A/B/C/D) 曲线与软件 PID/联动表。"""

    def __init__(self, api: Any) -> None:
        super().__init__()
        self.api = api
        self._busy = False
        self._loaded = False
        self._current_pid_file = ""
        self._channel_rows: dict[str, tuple[QLabel, QComboBox, QSpinBox]] = {}
        self._factory_max = 58
        self._user_max = 7

        layout = QVBoxLayout(self)
        self.header_label = QLabel(
            "On-demand setup (curves are read only when opened or refreshed)."
        )
        self.header_label.setWordWrap(True)
        self.header_label.setObjectName("mutedLabel")
        layout.addWidget(self.header_label)

        self.curve_group = QGroupBox("Sensor Calibration Curves")
        self.curve_layout = QFormLayout(self.curve_group)
        layout.addWidget(self.curve_group)

        self.pid_group = QGroupBox("Software PID Table")
        pid_layout = QFormLayout(self.pid_group)
        self.pid_combo = QComboBox()
        self.pid_combo.currentIndexChanged.connect(self._update_buttons)
        pid_layout.addRow("Active PID file", self.pid_combo)
        self.pid_hint_label = QLabel(
            "Takes effect on the next Set Temperature / Hold and persists to configs/instruments/."
        )
        self.pid_hint_label.setWordWrap(True)
        self.pid_hint_label.setObjectName("mutedLabel")
        pid_layout.addRow(self.pid_hint_label)
        layout.addWidget(self.pid_group)

        self.linked_group = QGroupBox("24C Linked Dual-Loop Formula Preview")
        linked_layout = QFormLayout(self.linked_group)
        self.linked_mapping_label = QLabel("—")
        self.linked_mapping_label.setWordWrap(True)
        self.linked_rules_label = QLabel("—")
        self.linked_rules_label.setWordWrap(True)
        self.linked_checkpoints_label = QLabel("—")
        self.linked_checkpoints_label.setWordWrap(True)
        self.linked_checkpoints_label.setObjectName("mutedLabel")
        linked_layout.addRow("Loop mapping", self.linked_mapping_label)
        linked_layout.addRow("Piecewise rules", self.linked_rules_label)
        linked_layout.addRow("Checkpoints", self.linked_checkpoints_label)
        self.linked_group.setVisible(False)
        layout.addWidget(self.linked_group)

        self.status_label = QLabel("Click Refresh to read current settings from the controller.")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.apply_button = QPushButton("Apply")
        self.refresh_button.clicked.connect(lambda _checked=False: self.refresh())
        self.apply_button.clicked.connect(lambda _checked=False: self.apply_changes())
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self._rebuild_channel_rows(("A", "B"))
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
        self.status_label.setText("Reading sensor curves and PID tables...")
        self._update_buttons()
        self.api.call_action(
            "read_setup",
            read_only=True,
            on_result=self._on_setup_loaded,
            on_error=self._on_error,
        )

    def apply_changes(self) -> None:
        if self._busy or not self.api.editable:
            return
        payload = self._build_apply_payload()
        if not payload:
            return
        self._busy = True
        self.status_label.setText("Applying setup changes and confirming readback...")
        self._update_buttons()
        self.api.call_action(
            "apply_setup",
            payload,
            read_only=False,
            on_result=self._on_apply_succeeded,
            on_error=self._on_error,
        )

    def _rebuild_channel_rows(self, channels: tuple[str, ...]) -> None:
        if tuple(self._channel_rows) == channels:
            return
        while self.curve_layout.rowCount() > 0:
            self.curve_layout.removeRow(0)
        self._channel_rows.clear()
        for channel in channels:
            row_widget = QWidget(self.curve_group)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            current_label = QLabel("Current: —")
            mode_combo = QComboBox()
            mode_combo.addItem("Keep current", "keep")
            mode_combo.addItem("Factory (ISENix)", "factory")
            mode_combo.addItem("User (USENix)", "user")
            index_spin = QSpinBox()
            index_spin.setRange(0, self._factory_max)
            index_spin.setEnabled(False)
            mode_combo.currentIndexChanged.connect(
                lambda _idx, ch=channel: self._on_channel_mode_changed(ch)
            )
            index_spin.valueChanged.connect(self._update_buttons)
            row_layout.addWidget(current_label, 1)
            row_layout.addWidget(mode_combo)
            row_layout.addWidget(index_spin)
            self.curve_layout.addRow(f"Input {channel}", row_widget)
            self._channel_rows[channel] = (current_label, mode_combo, index_spin)

    def _on_channel_mode_changed(self, channel: str) -> None:
        _label, mode_combo, index_spin = self._channel_rows[channel]
        mode = str(mode_combo.currentData())
        if mode == "keep":
            index_spin.setEnabled(False)
        elif mode == "factory":
            index_spin.setRange(0, self._factory_max)
            index_spin.setEnabled(True)
        else:
            index_spin.setRange(0, self._user_max)
            index_spin.setEnabled(True)
        self._update_buttons()

    def _populate_setup(self, data: dict[str, Any]) -> None:
        self._factory_max = int(data.get("factory_index_max", 58))
        self._user_max = int(data.get("user_index_max", 7))
        model = str(data.get("model", "24C"))
        channels = tuple(str(ch) for ch in data.get("channels", ("A", "B")))
        self.header_label.setText(
            f"Cryo-con {model} ({', '.join(channels)}): select only the channels or PID table to change."
        )
        self._rebuild_channel_rows(channels)
        channel_sensors = data.get("channel_sensors", {})
        for channel, (current_label, mode_combo, index_spin) in self._channel_rows.items():
            info = channel_sensors.get(channel, {}) if isinstance(channel_sensors, dict) else {}
            factory_ix = info.get("factory_index")
            user_ix = info.get("user_index")
            factory_text = "—" if factory_ix is None else str(factory_ix)
            user_text = "—" if user_ix is None else str(user_ix)
            current_label.setText(f"ISENix={factory_text}, USENix={user_text}")
            mode_combo.blockSignals(True)
            index_spin.blockSignals(True)
            mode_combo.setCurrentIndex(0)
            index_spin.setEnabled(False)
            if isinstance(user_ix, int):
                index_spin.setRange(0, self._user_max)
                index_spin.setValue(min(user_ix, self._user_max))
            elif isinstance(factory_ix, int):
                index_spin.setRange(0, self._factory_max)
                index_spin.setValue(min(factory_ix, self._factory_max))
            else:
                index_spin.setRange(0, self._factory_max)
                index_spin.setValue(0)
            mode_combo.blockSignals(False)
            index_spin.blockSignals(False)

        linked_enabled = bool(data.get("linked_loop_enabled", False))
        self._current_pid_file = str(data.get("current_pid_file", ""))
        self.pid_combo.blockSignals(True)
        self.pid_combo.clear()
        invalid_names: list[str] = []
        selected_index = -1
        for entry in data.get("pid_tables", []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            if not entry.get("valid", False):
                if name:
                    invalid_names.append(name)
                continue
            zones = entry.get("zone_count", 0)
            max_temp = entry.get("max_temperature")
            if linked_enabled:
                l_pid = entry.get("linked_pid_zone_count", 0)
                l_tgt = entry.get("linked_target_zone_count", 0)
                detail = f"{name} (Primary {zones}, Linked PID {l_pid}, Formula {l_tgt})"
            elif isinstance(max_temp, (int, float)):
                detail = f"{name} ({zones} zones, <= {max_temp:g} K)"
            else:
                detail = name
            self.pid_combo.addItem(detail, name)
            if name == self._current_pid_file:
                selected_index = self.pid_combo.count() - 1
        if selected_index >= 0:
            self.pid_combo.setCurrentIndex(selected_index)
        self.pid_combo.blockSignals(False)

        hint = (
            "Takes effect on the next Set Temperature / Hold and persists to configs/instruments/."
        )
        if invalid_names:
            hint += f" Skipped invalid/empty files: {', '.join(invalid_names)}."
        self.pid_hint_label.setText(hint)

        self.linked_group.setVisible(linked_enabled)
        if linked_enabled:
            p_loop = data.get("primary_loop", 2)
            p_in = data.get("primary_input", "B")
            l_loop = data.get("linked_loop", 1)
            l_in = data.get("linked_input", "A")
            self.linked_mapping_label.setText(
                f"Primary: Loop {p_loop} (Input {p_in})  |  Linked: Loop {l_loop} (Input {l_in})"
            )
            zones_data = data.get("linked_target_zones", [])
            if isinstance(zones_data, list) and zones_data:
                lines = []
                prev_temp: float | None = None
                for item in zones_data:
                    if not isinstance(item, dict):
                        continue
                    upper = float(item.get("temperature", 0.0))
                    formula = str(item.get("formula", ""))
                    if prev_temp is None:
                        lines.append(f"x <= {upper:g} K  ->  {formula}")
                    else:
                        lines.append(f"{prev_temp:g} K < x <= {upper:g} K  ->  {formula}")
                    prev_temp = upper
                self.linked_rules_label.setText("\n".join(lines))
            else:
                self.linked_rules_label.setText("—")
            preview_data = data.get("linked_preview", [])
            if isinstance(preview_data, list) and preview_data:
                checkpoints = ", ".join(
                    f"{item['primary_k']:g}->{item['linked_k']:g} K"
                    for item in preview_data
                    if isinstance(item, dict)
                    and "primary_k" in item
                    and "linked_k" in item
                )
                self.linked_checkpoints_label.setText(checkpoints)
            else:
                self.linked_checkpoints_label.setText("—")

        self._loaded = True

    def _build_apply_payload(self) -> dict[str, Any]:
        channels_payload: dict[str, dict[str, Any]] = {}
        for channel, (_label, mode_combo, index_spin) in self._channel_rows.items():
            mode = str(mode_combo.currentData())
            if mode in {"factory", "user"}:
                channels_payload[channel] = {
                    "kind": mode,
                    "index": int(index_spin.value()),
                }
        payload: dict[str, Any] = {}
        if channels_payload:
            payload["channels"] = channels_payload
        selected_pid = self.pid_combo.currentData()
        if (
            isinstance(selected_pid, str)
            and selected_pid
            and selected_pid != self._current_pid_file
        ):
            payload["pid_file"] = selected_pid
        return payload

    def _on_setup_loaded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate_setup(data)
        self.status_label.setText("Loaded current controller settings.")
        self._update_buttons()

    def _on_apply_succeeded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate_setup(data)
        summary = str(data.get("summary", "")).strip() or "Setup applied."
        self.status_label.setText(summary)
        self._update_buttons()

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.status_label.setText(f"Error: {message}")
        self._update_buttons()

    def _update_buttons(self, *_args: object) -> None:
        connected = bool(self.api.connected)
        editable = bool(self.api.editable)
        has_changes = bool(self._loaded and self._build_apply_payload())
        self.refresh_button.setEnabled(connected and not self._busy)
        self.apply_button.setEnabled(editable and not self._busy and has_changes)
        for _label, mode_combo, index_spin in self._channel_rows.values():
            mode_combo.setEnabled(editable and not self._busy and self._loaded)
            index_spin.setEnabled(
                editable
                and not self._busy
                and self._loaded
                and str(mode_combo.currentData()) != "keep"
            )
        self.pid_combo.setEnabled(
            editable and not self._busy and self._loaded and self.pid_combo.count() > 0
        )


__all__ = ["SetupWidget"]
