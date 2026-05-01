"""
Device connectivity monitor — MQTT discovery.

Connects to the configured MQTT broker, subscribes to ``esphome/discover/#``,
and pushes online/offline observations into the shared
:class:`DeviceStateMonitor`. Devices announce themselves on the topic
when prodded; an absence of announcement within ``_OFFLINE_TIMEOUT``
flips the state to offline. The monitor only runs while at least one
configured device declares an ``mqtt:`` block — otherwise the broker
connection is closed entirely.

aiomqtt is an optional runtime dependency. When the import fails, the
monitor logs once and disables itself; mDNS / ping discovery keeps
working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable
from typing import Any

try:
    import aiomqtt
except ImportError:  # pragma: no cover — aiomqtt is optional
    aiomqtt = None  # type: ignore[assignment]

from ..models import Device, DeviceState

_LOGGER = logging.getLogger(__name__)

_DISCOVER_TOPIC = "esphome/discover/#"
_DISCOVER_PUBLISH_TOPIC = "esphome/discover"
_PING_INTERVAL = 2.0  # seconds between discover requests
_OFFLINE_TIMEOUT = 10.0  # seconds without a response before marking offline
_RECONNECT_DELAY = 5.0  # delay before reconnecting after broker errors
_DEFAULT_PORT = 1883

_ENV_BROKER = "ESPHOME_DASHBOARD_MQTT_BROKER"
_ENV_PORT = "ESPHOME_DASHBOARD_MQTT_PORT"
_ENV_USERNAME = "ESPHOME_DASHBOARD_MQTT_USERNAME"
_ENV_PASSWORD = "ESPHOME_DASHBOARD_MQTT_PASSWORD"  # noqa: S105 — env var name, not a credential

# Callbacks ignore the return value — typed as ``object`` so callers can
# pass through the bool ``applied`` flag returned by
# :meth:`DeviceStateMonitor.apply` without an extra wrapper.
StateCallback = Callable[[str, DeviceState], object]
IPCallback = Callable[[str, str], object]


class DeviceMqttMonitor:
    """
    Drive device state from ``esphome/discover`` MQTT messages.

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
        get_devices: Callable[[], list[Device]],
        on_state_change: StateCallback,
        on_ip_change: IPCallback,
    ) -> None:
        self._get_devices = get_devices
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._task: asyncio.Task[None] | None = None
        # device name → monotonic timestamp of the last MQTT response
        self._last_seen: dict[str, float] = {}

    @staticmethod
    def is_available() -> bool:
        """Return True when aiomqtt is importable."""
        return aiomqtt is not None

    @staticmethod
    def is_configured() -> bool:
        """Return True when the broker env var is set."""
        return bool(os.environ.get(_ENV_BROKER, "").strip())

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
        if not self.is_configured():
            _LOGGER.info(
                "%s not set — MQTT device discovery disabled despite devices "
                "declaring mqtt: blocks",
                _ENV_BROKER,
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
        broker, port, username, password = _read_broker_config()
        client_id = f"esphome-dashboard-{secrets.token_hex(6)}"
        _LOGGER.info("MQTT discovery monitor starting — broker=%s:%s", broker, port)

        delay = int(_RECONNECT_DELAY)
        while True:
            try:
                await self._connect_and_listen(broker, port, username, password, client_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if aiomqtt is not None and isinstance(exc, aiomqtt.MqttError):
                    _LOGGER.warning("MQTT broker error: %s — reconnecting in %ss", exc, delay)
                else:
                    _LOGGER.exception("Unexpected MQTT error — reconnecting in %ss", delay)
                # Drop last-seen but leave device state alone so a brief
                # broker blip doesn't trigger an offline storm.
                self._last_seen.clear()
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_listen(
        self,
        broker: str,
        port: int,
        username: str | None,
        password: str | None,
        client_id: str,
    ) -> None:
        assert aiomqtt is not None  # type narrowing — checked in start()

        async with aiomqtt.Client(
            hostname=broker,
            port=port,
            username=username,
            password=password,
            identifier=client_id,
        ) as client:
            _LOGGER.info("MQTT connected to %s:%s", broker, port)
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
            for device in self._get_devices():
                if not device.uses_mqtt:
                    continue
                last = self._last_seen.get(device.name)
                if last is None:
                    continue
                if now - last > _OFFLINE_TIMEOUT:
                    self._on_state_change(device.name, DeviceState.OFFLINE)
                    self._last_seen.pop(device.name, None)
            await client.publish(_DISCOVER_PUBLISH_TOPIC, payload=None, retain=False)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_broker_config() -> tuple[str, int, str | None, str | None]:
    """Read broker connection parameters from the environment."""
    broker = os.environ.get(_ENV_BROKER, "").strip()
    port_raw = os.environ.get(_ENV_PORT, "").strip()
    try:
        port = int(port_raw) if port_raw else _DEFAULT_PORT
    except ValueError:
        _LOGGER.warning("Invalid %s=%r — falling back to %d", _ENV_PORT, port_raw, _DEFAULT_PORT)
        port = _DEFAULT_PORT
    username = os.environ.get(_ENV_USERNAME) or None
    password = os.environ.get(_ENV_PASSWORD) or None
    return broker, port, username, password


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
