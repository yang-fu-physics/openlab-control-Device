"""Lake Shore Model 340 的 Instrument Setup 界面：A/B 传感器校准曲线切换。"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SetupWidget(QWidget):
    """按需查询并切换 Lake Shore 340 Input A / Input B 传感器曲线（INCRV）。"""

    def __init__(self, api: Any) -> None:
        super().__init__()
        self.api = api
        self._busy = False
        self._loaded = False
        self._current_curves: dict[str, int] = {}
        self._combos: dict[str, QComboBox] = {}

        layout = QVBoxLayout(self)
        hint = QLabel(
            "On-demand setup (curve headers and active assignments are read only when opened or refreshed)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        group = QGroupBox("Sensor Calibration Curves (INCRV)")
        form = QFormLayout(group)
        for channel in ("A", "B"):
            combo = QComboBox()
            combo.currentIndexChanged.connect(self._update_buttons)
            form.addRow(f"Input {channel}", combo)
            self._combos[channel] = combo
        layout.addWidget(group)

        self.status_label = QLabel("Click Refresh to read current curves from the Lake Shore 340.")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        layout.addWidget(self.status_label)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.apply_button = QPushButton("Apply")
        self.refresh_button.clicked.connect(lambda _checked=False: self.refresh(force_curves=True))
        self.apply_button.clicked.connect(lambda _checked=False: self.apply_changes())
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self._update_buttons()

    def on_activated(self) -> None:
        if self.api.connected and not self._busy:
            self.refresh(force_curves=not self._loaded)
        else:
            self._update_buttons()

    def set_editable(self, _editable: bool) -> None:
        self._update_buttons()

    def update_snapshot(self, _snapshot: Any) -> None:
        self._update_buttons()

    def refresh(self, *, force_curves: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        self.status_label.setText("Reading curve catalog and channel assignments...")
        self._update_buttons()
        self.api.call_action(
            "read_setup",
            {"refresh_curves": bool(force_curves)},
            read_only=True,
            on_result=self._on_setup_loaded,
            on_error=self._on_error,
        )

    def apply_changes(self) -> None:
        if self._busy or not self.api.editable:
            return
        changed = self._changed_channels()
        if not changed:
            return
        self._busy = True
        self.status_label.setText("Writing curve assignments and confirming readback...")
        self._update_buttons()
        self.api.call_action(
            "apply_setup",
            {"channels": changed},
            read_only=False,
            on_result=self._on_apply_succeeded,
            on_error=self._on_error,
        )

    def _populate(self, data: dict[str, Any]) -> None:
        curves = [
            item
            for item in data.get("curves", [])
            if isinstance(item, dict) and isinstance(item.get("number"), int)
        ]
        channel_curves = data.get("channel_curves", {})
        self._current_curves = {}
        for channel, combo in self._combos.items():
            current = channel_curves.get(channel) if isinstance(channel_curves, dict) else None
            if isinstance(current, int):
                self._current_curves[channel] = current
            combo.blockSignals(True)
            combo.clear()
            selected_idx = -1
            for item in curves:
                number = int(item["number"])
                label = str(item.get("label") or f"{number}: Curve {number}")
                combo.addItem(label, number)
                if number == current:
                    selected_idx = combo.count() - 1
            if selected_idx >= 0:
                combo.setCurrentIndex(selected_idx)
            combo.blockSignals(False)
        self._loaded = True

    def _changed_channels(self) -> dict[str, int]:
        changes: dict[str, int] = {}
        for channel, combo in self._combos.items():
            selected = combo.currentData()
            if (
                isinstance(selected, int)
                and selected != self._current_curves.get(channel)
            ):
                changes[channel] = selected
        return changes

    def _on_setup_loaded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate(data)
        self.status_label.setText("Loaded current curve assignments.")
        self._update_buttons()

    def _on_apply_succeeded(self, data: dict[str, Any]) -> None:
        self._busy = False
        self._populate(data)
        summary = str(data.get("summary", "")).strip() or "Curve assignments updated."
        self.status_label.setText(summary)
        self._update_buttons()

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.status_label.setText(f"Error: {message}")
        self._update_buttons()

    def _update_buttons(self, *_args: object) -> None:
        connected = bool(self.api.connected)
        editable = bool(self.api.editable)
        has_changes = bool(self._loaded and self._changed_channels())
        self.refresh_button.setEnabled(connected and not self._busy)
        self.apply_button.setEnabled(editable and not self._busy and has_changes)
        for combo in self._combos.values():
            combo.setEnabled(editable and not self._busy and self._loaded and combo.count() > 0)


__all__ = ["SetupWidget"]
