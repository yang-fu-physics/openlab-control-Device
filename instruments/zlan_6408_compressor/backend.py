"""按现场配置，用 ZLAN 继电器顺序控制压缩机。"""

from __future__ import annotations

from collections.abc import Callable
import math
from time import sleep

from labcontrol.instruments.base import InstrumentError, SystemInstrument

from .zlan_6408 import ModbusTcpTransport, ZlanProtocolError


class ZlanRelayController(SystemInstrument):
    """手动和 SEQ 都使用扫描器配置的继电器顺序。"""

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
        self._count = int(config.extras["output_count"])
        self._on = tuple(int(channel[2:]) for channel in config.extras["on_channels"])
        self._off = tuple(int(channel[2:]) for channel in config.extras["off_channels"])
        self._status_inputs: list[int] = []
        self._status_outputs: list[int] = []
        for channel in config.extras["status_conditions"]:
            number = int(channel[2:])
            if number > self._count:
                raise ValueError(
                    f"Status condition {channel} is outside the selected "
                    f"{self._count}-channel ZLAN model"
                )
            group = self._status_inputs if channel.startswith("DI") else self._status_outputs
            group.append(number)
        self._delay = float(config.extras["step_delay_seconds"])
        if not math.isfinite(self._io_timeout) or self._io_timeout <= 0.0:
            raise ValueError("io_timeout_seconds must be positive")
        if not 1 <= self._unit_id <= 247:
            raise ValueError("unit_id must be from 1 to 247")
        # 首次使用已有连接；每个未确认的步骤可能使下一步需要新连接。
        steps = max(len(self._on), len(self._off))
        budget = (2 * steps - 1) * self._io_timeout + (steps - 1) * self._delay
        operation_limit = config.operation_timeout_seconds
        if budget >= operation_limit:
            raise InstrumentError(
                f"Instrument I/O timeout setting is too high for {steps} relay steps: "
                f"I/O timeout {self._io_timeout:g} s and step delay {self._delay:g} s "
                f"need up to {budget:g} s; must be less than {operation_limit:g} s. "
                "Reduce the I/O timeout or step delay in Instrument Scanner.",
                "INSTRUMENT_IO_TIMEOUT_TOO_HIGH",
                "io_timeout_seconds",
            )
        if (
            self._status_inputs and self._status_outputs
            and 2 * self._io_timeout >= operation_limit
        ):
            raise InstrumentError(
                f"Instrument I/O timeout setting is too high: "
                f"DI and DO status reads can take {2 * self._io_timeout:g} s "
                f"with an I/O timeout of {self._io_timeout:g} s; "
                f"must be less than {operation_limit:g} s. "
                "Reduce the I/O timeout in Instrument Scanner.",
                "INSTRUMENT_IO_TIMEOUT_TOO_HIGH",
                "io_timeout_seconds",
            )
        self._transport_factory = transport_factory
        self._transport: ModbusTcpTransport | None = None

    def _connect(self) -> ModbusTcpTransport:
        return self._transport_factory(self._address, self._io_timeout, self._unit_id)

    def open(self) -> None:
        try:
            self._transport = self._connect()
            self._read_state()
        except (OSError, ZlanProtocolError) as exc:
            self.close()
            raise InstrumentError(
                f"Could not connect to ZLAN: {exc}",
                "ZLAN_CONNECT_FAILED",
                self._address,
            ) from exc

    def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def read_status(self) -> dict[str, float | None]:
        try:
            return {"value": self._read_state()}
        except (OSError, ZlanProtocolError) as exc:
            raise InstrumentError(
                f"Could not read ZLAN status: {exc}",
                "ZLAN_STATUS_READ_FAILED",
                self._address,
            ) from exc

    def _read_state(self) -> float | None:
        transport = self._require_transport()
        if not self._status_inputs and not self._status_outputs:
            # 没配置状态条件时仍确认通讯，不能把连通性当作 On/Off。
            transport.read_outputs(self._count)
            return None
        states: list[bool] = []
        if self._status_inputs:
            inputs = transport.read_inputs(self._count)
            states.extend(inputs[channel - 1] for channel in self._status_inputs)
        if self._status_outputs:
            outputs = transport.read_outputs(self._count)
            states.extend(outputs[channel - 1] for channel in self._status_outputs)
        return float(all(states))

    def execute_sequence_command(self, command_id: str) -> None:
        if command_id == "compressor_on":
            enabled = True
            channels = self._on
        elif command_id == "compressor_off":
            enabled = False
            channels = self._off
        else:
            raise InstrumentError(
                f"Unknown ZLAN compressor command: {command_id}",
                "UNSUPPORTED_SEQUENCE_COMMAND",
                command_id,
            )
        self._require_transport()
        failures = []
        for index, channel in enumerate(channels):
            if index and self._delay:
                sleep(self._delay)
            if self._transport is None:
                try:
                    self._transport = self._connect()
                except OSError as exc:
                    failures.append(f"step {index + 1} DO{channel} not sent: {exc}")
                    continue
            try:
                self._transport.write_output(channel, enabled)
            except (OSError, ZlanProtocolError) as exc:
                failures.append(f"step {index + 1} DO{channel} unconfirmed: {exc}")
                # 迟到的回包不能用于下一步。丢弃连接，不重发本步。
                self.close()
        if failures:
            raise InstrumentError(
                f"{command_id}: " + "; ".join(failures),
                "INSTRUMENT_WRITE_RESULT_UNKNOWN",
                self._address,
            )

    def _require_transport(self) -> ModbusTcpTransport:
        if self._transport is None:
            raise InstrumentError(
                "ZLAN is not connected",
                "ZLAN_NOT_CONNECTED",
                self._address,
            )
        return self._transport
