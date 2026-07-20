"""
Minimal in-process MQTT 3.1.1 broker for e2e tests.

Just enough server for real paho clients: CONNECT auth against a plain
username/password table (anonymous optional), QoS 0 PUBLISH routing
with ``+`` / ``#`` filter matching, PINGREQ, and QoS 1 PUBACK so an
incoming QoS 1 publish doesn't wedge the sender. Exists because every
PyPI broker either pins pyyaml against esphome's pin (amqtt) or speaks
MQTT 5 only (mqttools).
"""

from __future__ import annotations

import asyncio
import contextlib

_CONNECT = 0x10
_CONNACK = 0x20
_PUBLISH = 0x30
_PUBACK = 0x40
_SUBSCRIBE = 0x80
_SUBACK = 0x90
_PINGREQ = 0xC0
_PINGRESP = 0xD0
_DISCONNECT = 0xE0

_RC_BAD_CREDENTIALS = 4
_RC_NOT_AUTHORIZED = 5


class MiniMqttBroker:
    """Loopback MQTT 3.1.1 broker; ``start()`` binds an ephemeral port."""

    def __init__(
        self, users: dict[str, str] | None = None, *, allow_anonymous: bool = True
    ) -> None:
        self.users = users or {}
        self.allow_anonymous = allow_anonymous
        self.port = 0
        self._server: asyncio.Server | None = None
        self._sessions: set[_Session] = set()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._on_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        for session in list(self._sessions):
            session.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def route(self, topic: str, payload: bytes) -> None:
        levels = topic.split("/")
        for session in self._sessions:
            if session.matches(levels):
                session.send_publish(topic, payload)

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = _Session(self, reader, writer)
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


class _Session:
    def __init__(
        self, broker: MiniMqttBroker, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._broker = broker
        self._reader = reader
        self._writer = writer
        self._filters: list[list[str]] = []

    def matches(self, levels: list[str]) -> bool:
        return any(_filter_matches(f, levels) for f in self._filters)

    def send_publish(self, topic: str, payload: bytes) -> None:
        encoded_topic = topic.encode()
        body = len(encoded_topic).to_bytes(2) + encoded_topic + payload
        self._writer.write(bytes([_PUBLISH]) + _encode_length(len(body)) + body)

    def close(self) -> None:
        self._writer.close()

    async def run(self) -> None:
        while True:
            try:
                first = (await self._reader.readexactly(1))[0]
                body = await self._reader.readexactly(await self._read_length())
            except (asyncio.IncompleteReadError, OSError):
                return
            packet_type = first & 0xF0
            if packet_type == _CONNECT:
                if not self._handle_connect(body):
                    return
            elif packet_type == _PUBLISH:
                self._handle_publish(first, body)
            elif packet_type == _SUBSCRIBE:
                self._handle_subscribe(body)
            elif packet_type == _PINGREQ:
                self._writer.write(bytes([_PINGRESP, 0]))
            elif packet_type == _DISCONNECT:
                return

    async def _read_length(self) -> int:
        value = 0
        shift = 0
        while True:
            byte = (await self._reader.readexactly(1))[0]
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def _handle_connect(self, body: bytes) -> bool:
        cursor = _Cursor(body)
        cursor.read_bytes()  # protocol name
        cursor.skip(1)  # protocol level
        flags = cursor.read_byte()
        cursor.skip(2)  # keepalive
        cursor.read_bytes()  # client id
        if flags & 0x04:  # will topic + payload
            cursor.read_bytes()
            cursor.read_bytes()
        username = cursor.read_bytes().decode() if flags & 0x80 else None
        password = cursor.read_bytes().decode() if flags & 0x40 else None

        if username is None:
            rc = 0 if self._broker.allow_anonymous else _RC_NOT_AUTHORIZED
        else:
            rc = 0 if self._broker.users.get(username) == password else _RC_BAD_CREDENTIALS
        self._writer.write(bytes([_CONNACK, 2, 0, rc]))
        return rc == 0

    def _handle_publish(self, first: int, body: bytes) -> None:
        cursor = _Cursor(body)
        topic = cursor.read_bytes().decode()
        if (first >> 1) & 0x03:  # QoS 1+: ack so the sender doesn't wedge
            self._writer.write(bytes([_PUBACK, 2]) + cursor.read(2))
        self._broker.route(topic, cursor.rest())

    def _handle_subscribe(self, body: bytes) -> None:
        cursor = _Cursor(body)
        packet_id = cursor.read(2)
        granted = bytearray()
        while not cursor.done():
            self._filters.append(cursor.read_bytes().decode().split("/"))
            cursor.skip(1)  # requested qos
            granted.append(0)
        self._writer.write(bytes([_SUBACK, 2 + len(granted)]) + packet_id + granted)


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, count: int) -> bytes:
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def read_byte(self) -> int:
        return self.read(1)[0]

    def read_bytes(self) -> bytes:
        return self.read(int.from_bytes(self.read(2)))

    def skip(self, count: int) -> None:
        self._pos += count

    def rest(self) -> bytes:
        return self._data[self._pos :]

    def done(self) -> bool:
        return self._pos >= len(self._data)


def _filter_matches(pattern: list[str], levels: list[str]) -> bool:
    for index, part in enumerate(pattern):
        if part == "#":
            return True
        if index >= len(levels) or part not in ("+", levels[index]):
            return False
    return len(pattern) == len(levels)


def _encode_length(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)
