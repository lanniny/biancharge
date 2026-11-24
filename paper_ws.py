"""Minimal client for the public Binance USD-M market WebSocket.

Stdlib only. It does not read proxy variables, does not send orders, and does
not sign requests. Server frames are parsed; client frames are masked.
"""

from __future__ import annotations

import os
import socket
import ssl
import struct
from urllib.parse import urlsplit


class PaperFeedError(RuntimeError):
    """The public market feed could not be used. Callers must fail closed."""


def build_client_frame(opcode: int, payload: bytes = b"") -> bytes:
    if opcode not in {0x1, 0x8, 0x9, 0xA}:
        raise PaperFeedError(f"unsupported client opcode {opcode}")
    mask = os.urandom(4)
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([0x80 | length])
    elif length < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", length)
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", length)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return header + mask + masked


def parse_server_frames(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Parse unmasked server frames. Returns (frames, remainder)."""
    frames: list[tuple[int, bytes]] = []
    offset = 0
    while offset + 2 <= len(buffer):
        first = buffer[offset]
        second = buffer[offset + 1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        cursor = offset + 2
        if length == 126:
            if cursor + 2 > len(buffer):
                break
            length = struct.unpack("!H", buffer[cursor : cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if cursor + 8 > len(buffer):
                break
            length = struct.unpack("!Q", buffer[cursor : cursor + 8])[0]
            cursor += 8
        mask = b""
        if masked:
            if cursor + 4 > len(buffer):
                break
            mask = buffer[cursor : cursor + 4]
            cursor += 4
        if cursor + length > len(buffer):
            break
        payload = buffer[cursor : cursor + length]
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        frames.append((opcode, payload))
        offset = cursor + length
        if not (first & 0x80):
            raise PaperFeedError("fragmented websocket frames are not accepted")
    return frames, buffer[offset:]


class PublicUmSocket:
    """One connection to wss://fstream.binance.com. No proxy and no API key."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "wss" or parsed.hostname != "fstream.binance.com":
            raise PaperFeedError(f"refusing feed URL {url!r}; only wss://fstream.binance.com is allowed")
        self.url = url
        self.host = parsed.hostname
        self.path = parsed.path or "/"
        if parsed.query:
            self.path = f"{self.path}?{parsed.query}"
        self.timeout = timeout
        self._socket: ssl.SSLSocket | None = None
        self._buffer = b""

    def connect(self) -> str:
        # Raw TLS. Environment proxies are not consulted.
        raw = socket.create_connection((self.host, 443), timeout=self.timeout)
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=self.host)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: biancharge-paper-forward/1.0\r\n"
            "\r\n"
        )
        sock.sendall(request.encode("ascii"))
        sock.settimeout(self.timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > 8192:
                break
        head, _, rest = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
        if " 101 " not in f" {status} ":
            sock.close()
            raise PaperFeedError(f"public USDT-M websocket refused: {status}")
        self._socket = sock
        self._buffer = rest
        return status

    def _send(self, opcode: int, payload: bytes = b"") -> None:
        if self._socket is None:
            raise PaperFeedError("feed socket is closed")
        self._socket.sendall(build_client_frame(opcode, payload))

    def read_text(self, timeout: float) -> str | None:
        if self._socket is None:
            raise PaperFeedError("feed socket is closed")
        self._socket.settimeout(timeout)
        while True:
            frames, self._buffer = parse_server_frames(self._buffer)
            for opcode, payload in frames:
                if opcode == 0x1:
                    return payload.decode("utf-8")
                if opcode == 0x9:
                    self._send(0xA, payload)
                elif opcode == 0x8:
                    raise PaperFeedError("public USDT-M websocket closed")
                elif opcode == 0xA:
                    continue
                else:
                    raise PaperFeedError(f"unexpected websocket opcode {opcode}")
            try:
                chunk = self._socket.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise PaperFeedError("public USDT-M websocket closed")
            self._buffer += chunk

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._send(0x8, b"")
            except Exception:
                pass
            try:
                self._socket.close()
            finally:
                self._socket = None
