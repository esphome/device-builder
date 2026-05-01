"""
Device connectivity monitor — MQTT discovery for one broker.

Connects to a single MQTT broker, subscribes to ``esphome/discover/#``,
and pushes online/offline observations into the supplied callbacks.
Devices announce themselves on the topic when prodded; an absence of
announcement within ``_OFFLINE_TIMEOUT`` flips the state to offline.

Multi-broker setups are handled one level up by
:class:`DeviceMqttCoordinator`, which spawns one monitor per unique
broker referenced by device YAML.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:
    import aiomqtt
except ImportError:  # pragma: no cover — aiomqtt is optional at runtime
    aiomqtt = None  # type: ignore[assignment]

from ..models import DeviceState

_LOGGER = logging.getLogger(__name__)

_DISCOVER_TOPIC = "esphome/discover/#"
_DISCOVER_PUBLISH_TOPIC = "esphome/discover"
_PING_INTERVAL = 2.0  # seconds between discover requests
_OFFLINE_TIMEOUT = 10.0  # seconds without a response before marking offline
_RECONNECT_DELAY = 5.0  # delay before reconnecting after broker errors
_DEFAULT_PORT = 1883

# Callbacks ignore the return value — typed as ``object`` so callers can
# pass through the bool ``applied`` flag returned by
# :meth:`DeviceStateMonitor.apply` without an extra wrapper.
StateCallback = Callable[[str, DeviceState], object]
IPCallback = Callable[[str, str], object]


@dataclass(frozen=True)
class MqttBrokerConfig:
    """Connection parameters for an MQTT broker."""

    host: str
    port: int = _DEFAULT_PORT
    username: str | None = None
    password: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        """Identifier for grouping devices to a single broker session."""
        return (self.host, self.port)


class DeviceMqttMonitor:
    """
    Drive device state from one broker's ``esphome/discover`` messages.

    Lifecycle:
      * ``start()`` — spawn the connect/listen task. Idempotent; calling
                      again while running is a no-op.
      * ``stop()``  — cancel the task, drop any state.

    The class never owns device state directly: every observation is
    forwarded through the supplied callbacks so :class:`DeviceStateMonitor`
    remains the single source of truth for source priority.
    """

    def __init__(
        self,
        broker: MqttBrokerConfig,
        on_state_change: StateCallback,
        on_ip_change: IPCallback,
    ) -> None:
        self._broker = broker
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._task: asyncio.Task[None] | None = None
        # device name → monotonic timestamp of the last MQTT response
        self._last_seen: dict[str, float] = {}

    @staticmethod
    def is_available() -> bool:
        """Return True when aiomqtt is importable."""
        return aiomqtt is not None

    @property
    def running(self) -> bool:
        """Return True while the connect/listen task is active."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the MQTT connect/listen task. No-op if already running."""
        if self.running:
            return
        if not self.is_available():
            _LOGGER.warning(
                "aiomqtt not installed — MQTT device discovery disabled. "
                "Install with: pip install aiomqtt"
            )
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the connect/listen task and forget all observations."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        self._last_seen.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        client_id = f"esphome-dashboard-{secrets.token_hex(6)}"
        _LOGGER.info("MQTT discovery starting — broker=%s:%s", self._broker.host, self._broker.port)

        delay = int(_RECONNECT_DELAY)
        while True:
            try:
                await self._connect_and_listen(client_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if aiomqtt is not None and isinstance(exc, aiomqtt.MqttError):
                    _LOGGER.warning(
                        "MQTT broker %s:%s error: %s — reconnecting in %ss",
                        self._broker.host,
                        self._broker.port,
                        exc,
                        delay,
                    )
                else:
                    _LOGGER.exception(
                        "Unexpected MQTT error for %s:%s — reconnecting in %ss",
                        self._broker.host,
                        self._broker.port,
                        delay,
                    )
                # Drop last-seen but leave device state alone so a brief
                # broker blip doesn't trigger an offline storm.
                self._last_seen.clear()
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_listen(self, client_id: str) -> None:
        assert aiomqtt is not None  # type narrowing — checked in start()

        async with aiomqtt.Client(
            hostname=self._broker.host,
            port=self._broker.port,
            username=self._broker.username,
            password=self._broker.password,
            identifier=client_id,
        ) as client:
            _LOGGER.info("MQTT connected to %s:%s", self._broker.host, self._broker.port)
            await client.subscribe(_DISCOVER_TOPIC)
            await client.publish(_DISCOVER_PUBLISH_TOPIC, payload=None, retain=False)

            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._listen(client))
                tg.create_task(self._ping_loop(client))

    async def _listen(self, client: Any) -> None:
        """Push discovery responses into the state and IP callbacks."""
        loop = asyncio.get_running_loop()
        async for message in client.messages:
            payload = _decode_payload(message.payload)
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                _LOGGER.debug("Ignoring non-JSON payload on %s", message.topic)
                continue

            name = data.get("name")
            if not isinstance(name, str) or not name:
                continue

            self._last_seen[name] = loop.time()
            self._on_state_change(name, DeviceState.ONLINE)

            ip = _extract_ip(data)
            if ip:
                self._on_ip_change(name, ip)

    async def _ping_loop(self, client: Any) -> None:
        """Sweep stale devices offline and re-prod the broker for announcements."""
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            now = loop.time()
            stale = [
                name for name, last in self._last_seen.items() if now - last > _OFFLINE_TIMEOUT
            ]
            for name in stale:
                self._on_state_change(name, DeviceState.OFFLINE)
                self._last_seen.pop(name, None)
            await client.publish(_DISCOVER_PUBLISH_TOPIC, payload=None, retain=False)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _extract_ip(data: dict[str, Any]) -> str:
    """
    Pull the first IP-shaped field from a discovery payload.

    ESPHome devices expose their addresses as ``ip``, ``ip0``, ``ip1``,
    ... — returns the first non-empty value, or empty string when none
    are present.
    """
    for key in ("ip", "ip0", "ip1", "ip2"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _decode_payload(payload: Any) -> str:
    """
    Decode an MQTT payload to text.

    Returns the empty string for ``None`` or unsupported payload types;
    ``backslashreplace`` keeps malformed UTF-8 readable.
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload).decode(errors="backslashreplace")
    return ""
