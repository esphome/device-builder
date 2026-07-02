"""
Device connectivity monitor — MQTT discovery for one broker.

Wraps paho-mqtt's threaded client in an asyncio-friendly task: the paho
network loop runs in its own thread, callbacks are bounced onto the
event loop via :meth:`asyncio.AbstractEventLoop.call_soon_threadsafe`,
and discovered devices are pushed into the supplied callbacks.

paho-mqtt is an optional runtime dependency — it ships with the
``[esphome]`` extra. When it isn't importable the monitor logs once and
disables itself; mDNS / ping discovery keeps working.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:  # pragma: no cover — paho-mqtt arrives via the [esphome] extra
    paho_mqtt = None  # type: ignore[assignment]


from ..helpers.async_ import drain_tasks, run_in_executor
from ..helpers.json import JSONDecodeError, loads
from ..models import DeviceState

_LOGGER = logging.getLogger(__name__)

_DISCOVER_TOPIC = "esphome/discover/#"
_DISCOVER_PUBLISH_TOPIC = "esphome/discover"
_PING_INTERVAL = 2.0  # seconds between discover requests
_OFFLINE_TIMEOUT = 10.0  # seconds without a response before marking offline
_RECONNECT_DELAY = 5.0  # delay before reconnecting after broker errors
_CONNECT_TIMEOUT = 10.0  # seconds to wait for CONNACK before giving up
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
    def key(self) -> tuple[str, int, str | None]:
        """Identifier for grouping devices to a single broker session (host, port, username)."""
        return (self.host, self.port, self.username)


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
        # Set by ``_connect_and_listen`` the moment CONNACK succeeds.
        # Read+cleared in ``_log_reconnect_failure`` so the loud-log
        # gates below re-arm only when we actually managed to connect
        # this session — not when ``_connect_and_listen`` returns
        # cleanly (which production rarely does, since the inner
        # TaskGroup parks until cancelled or raises). Bug #324.
        self._connected_this_session = False
        # Per-category log gates. Each flips True after a loud log
        # and back to False on the next failure that observed a
        # successful CONNACK. Tracked separately so a TimeoutError
        # loop doesn't suppress the first appearance of a different
        # unexpected exception class.
        self._connect_error_logged = False
        self._unexpected_error_logged = False

    @staticmethod
    def is_available() -> bool:
        """Return True when paho-mqtt is importable."""
        return paho_mqtt is not None

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
                "paho-mqtt not installed — MQTT device discovery disabled. "
                "Install the [esphome] extra to enable it."
            )
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the connect/listen task and forget all observations."""
        if self._task is None:
            return
        await drain_tasks((self._task,), log_exceptions=True)
        self._task = None
        self._last_seen.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        client_id = f"esphome-dashboard-{secrets.token_hex(6)}"
        _LOGGER.info("MQTT discovery starting — broker=%s:%s", self._broker.host, self._broker.port)

        while True:
            try:
                await self._connect_and_listen(client_id)
            except asyncio.CancelledError:
                raise
            except (TimeoutError, OSError, ConnectionError) as err:
                self._log_reconnect_failure(err, expected=True)
            except Exception as err:  # noqa: BLE001 — reconnect loop must outlive any unexpected error
                self._log_reconnect_failure(err, expected=False)
            # Drop last-seen on any failure but leave device state
            # alone so a brief broker blip doesn't trigger an offline
            # storm.
            self._last_seen.clear()
            await asyncio.sleep(_RECONNECT_DELAY)

    def _log_reconnect_failure(self, err: BaseException, *, expected: bool) -> None:
        """Log a reconnect failure, collapsing repeats while gates are tripped.

        ``expected`` (TimeoutError / OSError / ConnectionError) and
        unexpected exceptions track separate gates so a TimeoutError
        loop doesn't suppress the first appearance of a different
        exception class. A successful CONNACK during the failed
        iteration re-arms both gates so the next outage logs loudly
        again — tracked via ``self._connected_this_session``.
        """
        delay = int(_RECONNECT_DELAY)
        if self._connected_this_session:
            self._connect_error_logged = False
            self._unexpected_error_logged = False
            self._connected_this_session = False

        if expected:
            if self._connect_error_logged:
                _LOGGER.debug(
                    "MQTT broker %s:%s still unreachable (%s) — reconnecting in %ss",
                    self._broker.host,
                    self._broker.port,
                    err,
                    delay,
                )
            else:
                _LOGGER.warning(
                    "MQTT broker %s:%s unreachable (%s) — reconnecting in %ss",
                    self._broker.host,
                    self._broker.port,
                    err,
                    delay,
                )
                self._connect_error_logged = True
            return

        # Unexpected exception class — keep the loud ERROR with
        # traceback the first time round so genuine bugs are visible.
        # Repeats fall back to a DEBUG line that still includes the
        # class + message so the operator can tell what's looping
        # without flipping the log level.
        if self._unexpected_error_logged:
            _LOGGER.debug(
                "MQTT broker %s:%s error %s: %s (suppressed traceback) — reconnecting in %ss",
                self._broker.host,
                self._broker.port,
                type(err).__name__,
                err,
                delay,
            )
            return
        _LOGGER.exception(
            "MQTT broker %s:%s error — reconnecting in %ss",
            self._broker.host,
            self._broker.port,
            delay,
        )
        self._unexpected_error_logged = True

    async def _connect_and_listen(self, client_id: str) -> None:
        assert paho_mqtt is not None  # type narrowing — checked in start()
        loop = asyncio.get_running_loop()

        message_queue: asyncio.Queue[Any] = asyncio.Queue()
        connected = asyncio.Event()
        connect_failed: list[int] = []

        def on_connect(_client: Any, _userdata: Any, _flags: Any, rc: int) -> None:
            if rc == 0:
                loop.call_soon_threadsafe(connected.set)
            else:
                connect_failed.append(rc)
                loop.call_soon_threadsafe(connected.set)

        def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
            loop.call_soon_threadsafe(message_queue.put_nowait, msg)

        client = paho_mqtt.Client(client_id=client_id, clean_session=True)
        client.on_connect = on_connect
        client.on_message = on_message
        if self._broker.username:
            client.username_pw_set(self._broker.username, self._broker.password or "")

        await run_in_executor(client.connect, self._broker.host, self._broker.port)
        client.loop_start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=_CONNECT_TIMEOUT)
            if connect_failed:
                msg = f"broker rejected connection (rc={connect_failed[0]})"
                raise ConnectionError(msg)

            _LOGGER.info("MQTT connected to %s:%s", self._broker.host, self._broker.port)
            # Signal to ``_run`` that this iteration achieved a real
            # connection — read+cleared in its except branches to
            # re-arm the loud-log gates. Set here rather than relying
            # on a clean ``_connect_and_listen`` return because the
            # inner TaskGroup parks until cancelled or raises, so the
            # ``else`` branch on the caller's ``try`` rarely runs in
            # production.
            self._connected_this_session = True
            client.subscribe(_DISCOVER_TOPIC)
            client.publish(_DISCOVER_PUBLISH_TOPIC, payload=None, retain=False)

            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._listen(message_queue))
                tg.create_task(self._ping_loop(client))
        finally:
            # Synchronous teardown — paho's loop_stop joins its thread,
            # usually under a second, so no need for run_in_executor here.
            client.loop_stop()
            client.disconnect()

    async def _listen(self, queue: asyncio.Queue[Any]) -> None:
        """Push discovery responses into the state and IP callbacks."""
        loop = asyncio.get_running_loop()
        while True:
            message = await queue.get()
            # Retained discover/<name> messages are stale broker cache —
            # they get delivered immediately on subscribe and would
            # falsely flip a dead device online until the offline timeout
            # catches it. Wait for a fresh response to our next discover
            # publish instead.
            if getattr(message, "retain", False):
                continue
            payload = _decode_payload(message.payload)
            if not payload:
                continue
            try:
                data = loads(payload)
            except JSONDecodeError:
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
            client.publish(_DISCOVER_PUBLISH_TOPIC, payload=None, retain=False)


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
