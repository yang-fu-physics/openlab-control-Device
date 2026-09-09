"""把固定 OpenLab LabVIEW DLL ABI 接入 System Instrument 生命周期。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from labcontrol.instruments.base import (
    InstrumentError,
    InstrumentWarning,
    SystemInstrument,
)

from .labview_dll import LabVIEWDLL, LabVIEWDLLFailure


DLL_PATH = Path(__file__).with_name("driver.dll")


class LabVIEWSystemInstrument(SystemInstrument):
    """一个进程只加载一次 DLL，并把框架操作一对一转交给 LabVIEW。"""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._driver = LabVIEWDLL(DLL_PATH, expected_kind="system_instrument")
        self.dll_functions = self._driver.description.get("manual_functions", [])
        self._close_required = False

    @staticmethod
    def _raise_failure(error: LabVIEWDLLFailure) -> NoReturn:
        exception = InstrumentWarning if error.severity == "warning" else InstrumentError
        raise exception(str(error), error.code, error.context) from error

    def open(self) -> None:
        self._close_required = True
        try:
            self._driver.open(
                {
                    "id": self.config.id,
                    "address": self.config.address,
                    "settings": dict(self.config.extras),
                    "operation_timeout_seconds": self.config.operation_timeout_seconds,
                }
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def close(self) -> None:
        if not self._close_required:
            return
        try:
            self._driver.close()
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)
        self._close_required = False

    def read_status(self) -> dict[str, Any]:
        try:
            return self._driver.invoke("read_status", {})
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def read_measurement(self) -> dict[str, Any]:
        try:
            return self._driver.invoke("read_measurement", {})
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def set_target(
        self,
        value: float,
        rate_per_minute: float,
        mode: str = "Settle",
        *,
        control: str,
    ) -> None:
        panel = next(
            panel
            for panel in self.config.panels
            if panel.enabled
            and panel.template == "controller"
            and panel.control_id == control
        )
        if not panel.min_value <= value <= panel.max_value:
            raise InstrumentError(
                "Target exceeds the configured safety envelope",
                "LABVIEW_TARGET_LIMIT",
                control,
            )
        if not 0 < rate_per_minute <= panel.max_rate_per_minute:
            raise InstrumentError(
                "Rate exceeds the configured safety envelope",
                "LABVIEW_RATE_LIMIT",
                control,
            )
        try:
            self._driver.invoke(
                "set_target",
                {
                    "control": control,
                    "value": value,
                    "rate_per_minute": rate_per_minute,
                    "mode": mode,
                },
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def hold(self, *, control: str) -> None:
        try:
            self._driver.invoke("hold", {"control": control})
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def execute_sequence_command(self, command_id: str) -> None:
        try:
            self._driver.invoke("sequence_command", {"command_id": command_id})
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)

    def execute_dll_function(
        self,
        function_id: str,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self._driver.invoke(
                "manual_function",
                {
                    "function_id": function_id,
                    "parameters": parameters,
                    "operation_timeout_seconds": (
                        self.config.operation_timeout_seconds
                    ),
                },
            )
        except LabVIEWDLLFailure as error:
            self._raise_failure(error)


__all__ = ["LabVIEWSystemInstrument"]
