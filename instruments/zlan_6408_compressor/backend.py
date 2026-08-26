"""用 ZLAN 6408I 的 DO1 控制压缩机，并以 DI1 显示实际状态。"""

from __future__ import annotations

from collections.abc import Callable
import math

from labcontrol.instruments.base import InstrumentError, SystemInstrument

from .zlan_6408 import ModbusTcpTransport, ZlanProtocolError


class Zlan6408Compressor(SystemInstrument):
    """DO1 写入与 DI1 状态相互独立的压缩机 System Instrument。"""

    def __init__(
        self,
        config,
        transport_factory: Callable[
            [str, float, int],
            ModbusTcpTransport,
        ] = ModbusTcpTransport,
    ) -> None:
        super().__init__(config)
        host = str(config.extras["host"])
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        self._address = f"tcp://{host}:{int(config.extras['port'])}"
        self._io_timeout = float(config.extras["io_timeout_seconds"])
        self._unit_id = int(config.extras["unit_id"])
        if not math.isfinite(self._io_timeout) or self._io_timeout <= 0.0:
            raise ValueError("io_timeout_seconds must be positive")
        if not 1 <= self._unit_id <= 247:
            raise ValueError("unit_id must be from 1 to 247")
        self._transport_factory = transport_factory
        self._transport: ModbusTcpTransport | None = None

    def open(self) -> None:
        try:
            transport = self._transport_factory(
                self._address,
                self._io_timeout,
                self._unit_id,
            )
        except OSError as exc:
            raise InstrumentError(
                f"Could not connect to ZLAN 6408: {exc}",
                "ZLAN_CONNECT_FAILED",
                self._address,
            ) from exc
        try:
            transport.read_di1()
        except (OSError, ZlanProtocolError) as exc:
            transport.close()
            raise InstrumentError(
                f"Could not connect to ZLAN 6408: {exc}",
                "ZLAN_CONNECT_FAILED",
                self._address,
            ) from exc
        self._transport = transport

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def read_status(self) -> dict[str, float]:
        try:
            enabled = self._require_transport().read_di1()
        except (OSError, ZlanProtocolError) as exc:
            raise InstrumentError(
                f"Could not read compressor state from DI1: {exc}",
                "ZLAN_DI1_READ_FAILED",
                self._address,
            ) from exc
        return {"value": 1.0 if enabled else 0.0}

    def execute_sequence_command(self, command_id: str) -> None:
        if command_id == "compressor_on":
            enabled = True
        elif command_id == "compressor_off":
            enabled = False
        else:
            raise InstrumentError(
                f"Unknown ZLAN compressor command: {command_id}",
                "UNSUPPORTED_SEQUENCE_COMMAND",
                command_id,
            )
        try:
            self._require_transport().write_do1(enabled)
        except (OSError, ZlanProtocolError) as exc:
            raise InstrumentError(
                f"Could not confirm DO1 write: {exc}",
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                self._address,
            ) from exc

    def _require_transport(self) -> ModbusTcpTransport:
        if self._transport is None:
            raise InstrumentError(
                "ZLAN 6408 is not connected",
                "ZLAN_NOT_CONNECTED",
                self._address,
            )
        return self._transport
