from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import struct
import sys
import tomllib
import unittest


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
    Zlan6408Compressor,
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
    def __init__(self, state: bool = False) -> None:
        self.state = state
        self.reads = 0
        self.writes: list[bool] = []
        self.closed = False
        self.write_error: Exception | None = None

    def read_di1(self) -> bool:
        self.reads += 1
        return self.state

    def write_do1(self, enabled: bool) -> None:
        self.writes.append(enabled)
        if self.write_error is not None:
            raise self.write_error

    def close(self) -> None:
        self.closed = True


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        id="compressor",
        extras={
            "host": "192.168.1.20",
            "port": 502,
            "io_timeout_seconds": 1.0,
            "unit_id": 1,
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

    def test_reads_di1_with_modbus_tcp_function_01(self) -> None:
        socket = FakeSocket(_frame(1, b"\x01\x01\x01"))
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
        self.assertTrue(transport.read_di1())
        self.assertEqual(
            socket.sent,
            [_frame(1, b"\x01\x00\x00\x00\x01")],
        )
        self.assertEqual(
            endpoints,
            [(('192.168.1.20', 502), 1.5)],
        )
        self.assertEqual(socket.timeout, 1.5)

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

        transport.write_do1(True)
        transport.write_do1(False)

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
            mismatched_transport.write_do1(True)


class ZlanBackendTests(unittest.TestCase):
    def test_open_and_poll_read_di1_without_changing_do1(self) -> None:
        transport = FakeTransport(state=True)
        endpoints: list[tuple[str, float, int]] = []

        def transport_factory(address, timeout, unit_id):
            endpoints.append((address, timeout, unit_id))
            return transport

        controller = Zlan6408Compressor(
            _config(),
            transport_factory,
        )

        controller.open()
        self.assertEqual(
            endpoints,
            [("tcp://192.168.1.20:502", 1.0, 1)],
        )
        self.assertEqual(controller.read_status(), {"value": 1.0})
        self.assertEqual(transport.reads, 2)
        self.assertEqual(transport.writes, [])

        controller.close()
        self.assertTrue(transport.closed)
        self.assertEqual(transport.writes, [])

    def test_actions_write_do1_once_and_do_not_predict_di1(self) -> None:
        transport = FakeTransport(state=False)
        controller = Zlan6408Compressor(
            _config(),
            lambda _address, _timeout, _unit_id: transport,
        )
        controller.open()

        controller.execute_sequence_command("compressor_on")
        controller.execute_sequence_command("compressor_off")

        self.assertEqual(transport.writes, [True, False])
        self.assertEqual(controller.read_status(), {"value": 0.0})

    def test_unconfirmed_write_uses_the_core_uncertain_result_code(self) -> None:
        transport = FakeTransport()
        controller = Zlan6408Compressor(
            _config(),
            lambda _address, _timeout, _unit_id: transport,
        )
        controller.open()
        transport.write_error = ZlanProtocolError("mismatched echo")

        with self.assertRaises(InstrumentError) as captured:
            controller.execute_sequence_command("compressor_on")

        self.assertEqual(
            captured.exception.code,
            "INSTRUMENT_WRITE_RESULT_UNKNOWN",
        )
        self.assertEqual(transport.writes, [True])

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
            ["host", "port", "io_timeout_seconds", "unit_id"],
        )
        self.assertEqual(
            manifest["panels"],
            [
                {
                    "id": "compressor",
                    "label": "Compressor",
                    "template": "switch",
                    "reading": "compressor_state",
                    "commands": ["compressor_on", "compressor_off"],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
