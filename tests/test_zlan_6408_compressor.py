from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import struct
import sys
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT.parent / "OpenLabControl" / "src"
if CORE_SRC.is_dir():
    sys.path.insert(0, str(CORE_SRC))
sys.path.insert(0, str(ROOT / "instruments"))

from labcontrol.instruments.base import InstrumentError  # noqa: E402
from labcontrol.instruments.manifest import (  # noqa: E402
    load_instrument_manifest,
)
from zlan_6408_compressor.backend import (  # noqa: E402
    ZlanRelayController,
)
from zlan_6408_compressor.zlan_6408 import (  # noqa: E402
    ModbusTcpTransport,
    ZlanProtocolError,
    tcp_endpoint,
)


def _frame(
    transaction_id: int,
    pdu: bytes,
    unit_id: int = 1,
) -> bytes:
    return struct.pack(
        ">HHHB",
        transaction_id,
        0,
        len(pdu) + 1,
        unit_id,
    ) + pdu


class FakeSocket:
    def __init__(self, responses: bytes) -> None:
        self.responses = bytearray(responses)
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def recv(self, size: int) -> bytes:
        result = bytes(self.responses[:size])
        del self.responses[:size]
        return result

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, outputs: tuple[bool, ...] = (False,) * 8) -> None:
        self.outputs = outputs
        self.reads: list[int] = []
        self.writes: list[tuple[int, bool]] = []
        self.closed = False
        self.write_error: Exception | None = None

    def read_outputs(self, count: int) -> tuple[bool, ...]:
        self.reads.append(count)
        return self.outputs[:count]

    def write_output(self, channel: int, enabled: bool) -> None:
        self.writes.append((channel, enabled))
        if self.write_error is not None:
            raise self.write_error

    def close(self) -> None:
        self.closed = True


def _config(**overrides) -> SimpleNamespace:
    return SimpleNamespace(
        id="compressor",
        operation_timeout_seconds=10.0,
        extras={
            "host": "192.168.1.20",
            "port": 502,
            "io_timeout_seconds": 1.0,
            "unit_id": 1,
            "output_count": "4",
            "on_channels": ["DO1"],
            "off_channels": ["DO1"],
            "step_delay_seconds": 0.0,
            **overrides,
        },
    )


class ZlanProtocolTests(unittest.TestCase):
    def test_tcp_endpoint_requires_internal_transport_address_format(self) -> None:
        self.assertEqual(
            tcp_endpoint("tcp://192.168.1.20:502"),
            ("192.168.1.20", 502),
        )
        self.assertEqual(
            tcp_endpoint("tcp://[2001:db8::1]:502"),
            ("2001:db8::1", 502),
        )
        for address in (
            "192.168.1.20:502",
            "tcp://192.168.1.20",
            "tcp://user@192.168.1.20:502",
            "tcp://192.168.1.20:502/path",
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    tcp_endpoint(address)

    def test_reads_do_coils_only_with_modbus_tcp_function_01(self) -> None:
        socket = FakeSocket(_frame(1, b"\x01\x01\xc0"))
        endpoints: list[tuple[tuple[str, int], float]] = []

        def factory(endpoint, timeout):
            endpoints.append((endpoint, timeout))
            return socket

        transport = ModbusTcpTransport(
            "tcp://192.168.1.20:502",
            1.5,
            1,
            factory,
        )
        self.assertEqual(socket.timeout, 1.5)
        self.assertEqual(transport.read_outputs(8), (False,) * 6 + (True, True))
        self.assertEqual(
            socket.sent,
            [_frame(1, b"\x01\x00\x10\x00\x08")],
        )
        self.assertEqual(
            endpoints,
            [(('192.168.1.20', 502), 1.5)],
        )

    def test_writes_do1_and_requires_the_exact_echo(self) -> None:
        on = b"\x05\x00\x10\xff\x00"
        off = b"\x05\x00\x10\x00\x00"
        socket = FakeSocket(_frame(1, on) + _frame(2, off))
        transport = ModbusTcpTransport(
            "tcp://192.168.1.20:502",
            1.0,
            1,
            lambda _endpoint, _timeout: socket,
        )

        transport.write_output(1, True)
        transport.write_output(1, False)

        self.assertEqual(
            socket.sent,
            [_frame(1, on), _frame(2, off)],
        )

        mismatch = FakeSocket(
            _frame(1, b"\x05\x00\x10\x00\x00")
        )
        mismatched_transport = ModbusTcpTransport(
            "tcp://192.168.1.20:502",
            1.0,
            1,
            lambda _endpoint, _timeout: mismatch,
        )
        with self.assertRaises(ZlanProtocolError):
            mismatched_transport.write_output(1, True)

    def test_do7_and_do8_use_their_own_addresses(self) -> None:
        steps = [
            (7, True, b"\x05\x00\x16\xff\x00"),
            (8, True, b"\x05\x00\x17\xff\x00"),
            (8, False, b"\x05\x00\x17\x00\x00"),
            (7, False, b"\x05\x00\x16\x00\x00"),
        ]
        frames = [_frame(index, pdu) for index, (_, _, pdu) in enumerate(steps, 1)]
        socket = FakeSocket(b"".join(frames))
        transport = ModbusTcpTransport(
            "tcp://192.168.1.20:502", 1.0, 1, lambda *_: socket,
        )
        for channel, enabled, _ in steps:
            transport.write_output(channel, enabled)
        self.assertEqual(socket.sent, frames)


class ZlanBackendTests(unittest.TestCase):
    def test_open_and_poll_read_only_do_coils_without_changing_outputs(self) -> None:
        transport = FakeTransport(outputs=(True, False, False, False))
        endpoints: list[tuple[str, float, int]] = []

        def transport_factory(address, timeout, unit_id):
            endpoints.append((address, timeout, unit_id))
            return transport

        controller = ZlanRelayController(
            _config(),
            transport_factory,
        )

        controller.open()
        self.assertEqual(
            endpoints,
            [("tcp://192.168.1.20:502", 1.0, 1)],
        )
        self.assertEqual(controller.read_status(), {"value": 1.0})
        self.assertEqual(transport.reads, [4, 4])
        self.assertEqual(transport.writes, [])

        controller.close()
        self.assertTrue(transport.closed)
        self.assertEqual(transport.writes, [])

    def test_actions_run_in_configured_order_with_delay_between_steps(self) -> None:
        transport = FakeTransport()
        controller = ZlanRelayController(
            _config(output_count="8", on_channels=["DO7", "DO8"],
                    off_channels=["DO8", "DO7"], step_delay_seconds=0.3),
            lambda _address, _timeout, _unit_id: transport,
        )
        controller.open()

        delays = []
        with patch("zlan_6408_compressor.backend.sleep",
                   side_effect=lambda delay: delays.append((delay, list(transport.writes)))):
            controller.execute_sequence_command("compressor_on")
            controller.execute_sequence_command("compressor_off")
        self.assertEqual(transport.writes, [(7, True), (8, True), (8, False), (7, False)])
        self.assertEqual(delays, [
            (0.3, [(7, True)]),
            (0.3, [(7, True), (8, True), (8, False)]),
        ])
        self.assertEqual(controller.read_status(), {"value": 0.0})
        transport.outputs = (False,) * 6 + (True, False)
        self.assertEqual(controller.read_status(), {"value": None})

    def test_unconfirmed_step_continues_on_fresh_connection_for_both_actions(self) -> None:
        for enabled, command, channels in (
            (True, "compressor_on", [7, 8]),
            (False, "compressor_off", [8, 7]),
        ):
            with self.subTest(command=command):
                first, second = FakeTransport(), FakeTransport()
                connections = iter((first, second))
                controller = ZlanRelayController(
                    _config(output_count="8", on_channels=["DO7", "DO8"],
                            off_channels=["DO8", "DO7"]),
                    lambda *_: next(connections),
                )
                controller.open()
                first.write_error = TimeoutError("no reply")
                with self.assertRaisesRegex(
                    InstrumentError, f"step 1 DO{channels[0]} unconfirmed",
                ) as captured:
                    controller.execute_sequence_command(command)
                self.assertEqual(captured.exception.code, "INSTRUMENT_WRITE_RESULT_UNKNOWN")
                self.assertEqual(first.writes, [(channels[0], enabled)])
                self.assertTrue(first.closed)
                self.assertEqual(second.writes, [(channels[1], enabled)])

    def test_impossible_step_budget_is_rejected_before_open(self) -> None:
        with self.assertRaisesRegex(ValueError, "reduce the I/O timeout or step delay"):
            ZlanRelayController(_config(
                output_count="8", on_channels=["DO7", "DO8"], step_delay_seconds=8.0,
            ))

    def test_manifest_selects_switch_panel_and_commands(self) -> None:
        instrument = ROOT / "instruments" / "zlan_6408_compressor"
        descriptor = load_instrument_manifest(instrument)
        self.assertTrue(descriptor.valid, descriptor.error)
        self.assertEqual(
            [command.label for command in descriptor.sequence_commands],
            ["Compressor On", "Compressor Off"],
        )
        with (instrument / "instrument.toml").open("rb") as handle:
            manifest = tomllib.load(handle)
        self.assertEqual(
            [field["id"] for field in manifest["config_fields"]],
            ["host", "port", "io_timeout_seconds", "unit_id", "output_count",
             "on_channels", "off_channels", "step_delay_seconds"],
        )
        self.assertEqual(
            manifest["panels"],
            [
                {
                    "id": "compressor",
                    "label": "Compressor Relays",
                    "template": "switch",
                    "reading": "compressor_state",
                    "commands": ["compressor_on", "compressor_off"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
