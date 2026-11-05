"""ZLAN 6408I / 6842 的 Modbus TCP 继电器通讯层。

DI1 的零基地址为 0x0000，DO1 为 0x0010；四路和八路型号共用地址布局。
写入须完整回显请求。
"""

from __future__ import annotations

from collections.abc import Callable
import socket
import struct
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit


class ZlanProtocolError(RuntimeError):
    """Modbus TCP 帧缺失、损坏或与当前请求不匹配。"""


class SocketLike(Protocol):
    def settimeout(self, value: float) -> None: ...

    def sendall(self, data: bytes) -> None: ...

    def recv(self, size: int) -> bytes: ...

    def close(self) -> None: ...


SocketFactory = Callable[[tuple[str, int], float], SocketLike]


def tcp_endpoint(address: str) -> tuple[str, int]:
    """解析扫描器保存的 ``tcp://host:port`` 地址。"""

    parsed = urlsplit(address)
    if (
        parsed.scheme.casefold() != "tcp"
        or not parsed.hostname
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "ZLAN address must use tcp://host:port without a path"
        )
    return parsed.hostname, parsed.port


class ModbusTcpTransport:
    """一个持久 TCP 连接上的同步 Modbus 请求/响应通道。"""

    def __init__(
        self,
        address: str,
        timeout_seconds: float,
        unit_id: int,
        socket_factory: SocketFactory = socket.create_connection,
    ) -> None:
        host, port = tcp_endpoint(address)
        self._socket = socket_factory((host, port), timeout_seconds)
        self._socket.settimeout(timeout_seconds)
        self._timeout = timeout_seconds
        self._unit_id = unit_id
        self._transaction_id = 0

    def read_inputs(self, count: int) -> tuple[bool, ...]:
        return self._read_bits(0x0000, count, "DI")

    def read_outputs(self, count: int) -> tuple[bool, ...]:
        return self._read_bits(0x0010, count, "DO")

    def _read_bits(self, address: int, count: int, label: str) -> tuple[bool, ...]:
        response = self._request(
            struct.pack(">BHH", 0x01, address, count)
        )
        if len(response) != 3 or response[:2] != b"\x01\x01":
            raise ZlanProtocolError(
                f"Invalid {label} read response: {response.hex(' ')}"
            )
        return tuple(bool(response[2] & (1 << index)) for index in range(count))

    def write_output(self, channel: int, enabled: bool) -> None:
        request = struct.pack(
            ">BHH",
            0x05,
            0x0010 + channel - 1,
            0xFF00 if enabled else 0x0000,
        )
        response = self._request(request)
        if response != request:
            raise ZlanProtocolError(
                f"DO{channel} write response does not match the request: "
                f"{response.hex(' ')}"
            )

    def close(self) -> None:
        self._socket.close()

    def _request(self, pdu: bytes) -> bytes:
        self._transaction_id = (self._transaction_id % 0xFFFF) + 1
        transaction_id = self._transaction_id
        frame = struct.pack(
            ">HHHB",
            transaction_id,
            0,
            len(pdu) + 1,
            self._unit_id,
        ) + pdu
        deadline = monotonic() + self._timeout
        self._socket.settimeout(self._timeout)
        self._socket.sendall(frame)

        header = self._receive_exact(7, deadline)
        response_id, protocol_id, length, unit_id = struct.unpack(
            ">HHHB",
            header,
        )
        if (
            response_id != transaction_id
            or protocol_id != 0
            or unit_id != self._unit_id
            or length < 2
        ):
            raise ZlanProtocolError(
                f"Invalid Modbus TCP header: {header.hex(' ')}"
            )
        response = self._receive_exact(length - 1, deadline)
        if response[0] == (pdu[0] | 0x80):
            exception_code = response[1] if len(response) == 2 else -1
            raise ZlanProtocolError(
                f"Modbus exception {exception_code} for function {pdu[0]}"
            )
        if response[0] != pdu[0]:
            raise ZlanProtocolError(
                f"Unexpected Modbus function {response[0]} for {pdu[0]}"
            )
        return response

    def _receive_exact(self, size: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < size:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Modbus response deadline expired")
            self._socket.settimeout(remaining)
            chunk = self._socket.recv(size - len(result))
            if not chunk:
                raise ZlanProtocolError(
                    "TCP connection closed before the Modbus response completed"
                )
            result.extend(chunk)
        return bytes(result)
