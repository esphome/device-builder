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
from dataclasses import dataclass, field
from typing import Any, Protocol

try:
    import paho.mqtt.client as paho_mqtt
except ImportError:  # pragma: no cover — paho-mqtt arrives via the [esphome] extra
    paho_mqtt = None  # type: ignore[assignment]


from ..helpers.async_ import drain_tasks, run_in_executor
from ..helpers.json import JSONDecodeError, loads
from ..helpers.presence_gated_loop import PresenceGatedLoop
from ..helpers.subscriber_presence import SubscriberPresence
from ..models import DeviceState

_LOGGER = logging.getLogger(__name__)

_DISCOVER_TOPIC = "esphome/discover/#"
_DISCOVER_PUBLISH_TOPIC = "esphome/discover"
# ONLINE detection is push (devices announce on broker connect), so the
# broadcast only backstops OFFLINE detection and missed announces —
# same cadence class as the 60s ICMP sweep, not a liveness poll.
_PING_INTERVAL = 30.0  # seconds between discover broadcasts while watched
_OFFLINE_TIMEOUT = 65.0  # two missed broadcasts + slack before OFFLINE
_RECONNECT_DELAY = 5.0  # delay before reconnecting after broker errors
_CONNECT_TIMEOUT = 10.0  # seconds to wait for CONNACK before giving up
_DEFAULT_PORT = 1883
# One taxonomy for "quiet reconnect": ``_run``'s except clause and the
# session TaskGroup unwrap must agree, or a concurrent group stops
# collapsing and degrades to the loud unexpected path.
_EXPECTED_SESSION_ERRORS = (TimeoutError, OSError, ConnectionError)

# Callbacks ignore the return value — typed as ``object`` so callers can
# pass through the bool ``applied`` flag returned by
# :meth:`DeviceStateMonitor.apply` without an extra wrapper.
StateCallback = Callable[[str, DeviceState], object]
IPCallback = Callable[[str, str], object]


class PublishInfo(Protocol):
    """The slice of paho's ``MQTTMessageInfo`` the monitor reads."""

    rc: int


class SubscribeClient(Protocol):
    """The slice of paho's ``Client`` the CONNACK callback drives."""

    def subscribe(self, topic: str) -> tuple[int, int]: ...


class PublishClient(Protocol):
    """The slice of paho's ``Client`` the broadcast loop drives."""

    # Mirrors paho's signature so the real client satisfies the protocol.
    def publish(
        self,
        topic: str,
        payload: bytes | None = None,
        retain: bool = False,  # noqa: FBT001, FBT002
    ) -> PublishInfo: ...


@dataclass
class _SessionSignals:
    """
    Per-session handshake state shared with paho's thread callbacks.

    ``connack_processed`` means "verdict recorded" — it is set on
    rejection too, so a refused connect fails fast instead of waiting
    out the connect timeout. ``failed`` is set alongside any ``errors``
    append; paho's auto-reconnect re-fires ``on_connect`` after the
    initial handshake, and a failure there must tear the session down,
    not land in a list nobody re-reads.
    """

    connack_processed: asyncio.Event = field(default_factory=asyncio.Event)
    failed: asyncio.Event = field(default_factory=asyncio.Event)
    subscribed: asyncio.Event = field(default_factory=asyncio.Event)
    suback_pending: asyncio.Event = field(default_factory=asyncio.Event)
    errors: list[str] = field(default_factory=list)
    messages: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)

    def record_verdict(self) -> None:
        """Run on the loop thread: flag any failure, then open the gate."""
        if self.errors:
            self.failed.set()
        self.connack_processed.set()

    def begin_suback_wait(self) -> None:
        """Run on the loop thread: a (re)connect just re-issued the subscribe.

        Scheduled before the SUBACK can arrive (same FIFO bounce), so
        the cleared latch always precedes its set.
        """
        self.subscribed.clear()
        self.suback_pending.set()


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
        presence: SubscriberPresence | None = None,
        on_connection_change: Callable[[], None] | None = None,
    ) -> None:
        self._broker = broker
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._on_connection_change = on_connection_change
        # Whether this monitor broadcasts ``esphome/discover``. The
        # coordinator designates one broadcaster per (host, port) —
        # extra same-broker logins subscribe but stay silent, since
        # every broadcast makes the whole fleet answer.
        self.is_publisher = True
        # True between CONNACK and session teardown — election input,
        # so a down login never holds the broadcaster role while a
        # healthy sibling could carry it.
        self.connected = False
        self._ping = _DiscoverPingLoop(self, presence)
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
        # WARNING once per failure streak, DEBUG on repeats — re-armed
        # by the next successful broadcast.
        self._publish_error_logged = False

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
        self._ping.unsubscribe()
        if self._task is None:
            return
        await drain_tasks((self._task,), log_exceptions=True)
        self._task = None
        self._last_seen.clear()

    def set_publisher(self, *, value: bool) -> None:
        """
        Grant or revoke the discover-broadcaster role.

        Promotion rebases the last-seen ledger — entries aged during a
        no-broadcaster gap are not evidence of death.
        """
        if value and not self.is_publisher:
            self._rebase_last_seen()
        self.is_publisher = value

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
            except _EXPECTED_SESSION_ERRORS as err:
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

    def _create_session_client(self, client_id: str, signals: _SessionSignals) -> Any:
        assert paho_mqtt is not None  # type narrowing — checked in start()
        loop = asyncio.get_running_loop()

        def on_connect(
            connected_client: SubscribeClient, _userdata: object, _flags: object, rc: int
        ) -> None:
            # Runs on paho's thread, which swallows callback raises —
            # every path must record its error and reach the set in the
            # finally, or the awaiting side times out with no cause.
            try:
                if rc != 0:
                    signals.errors.append(f"broker rejected connection (rc={rc})")
                    return
                # Subscribe from paho's own thread: its auto-reconnect
                # re-fires this callback, so the subscription survives a
                # broker blip our session logic never sees.
                loop.call_soon_threadsafe(signals.begin_suback_wait)
                result, _mid = connected_client.subscribe(_DISCOVER_TOPIC)
                if result != 0:
                    signals.errors.append(f"subscribe failed (rc={result})")
            except Exception as err:  # noqa: BLE001 — paho's thread would swallow the raise
                signals.errors.append(f"subscribe raised: {err!r}")
            finally:
                loop.call_soon_threadsafe(signals.record_verdict)

        def on_subscribe(
            _client: Any, _userdata: object, _mid: int, granted_qos: tuple[int, ...]
        ) -> None:
            # A broker ACL denies the subscription via SUBACK 0x80 —
            # ``subscribe()``'s rc cannot see it, and without this the
            # monitor sits connected with discovery permanently dark.
            if any(qos == 0x80 for qos in granted_qos):
                signals.errors.append(f"subscription denied (granted qos={list(granted_qos)})")
                loop.call_soon_threadsafe(signals.record_verdict)
            else:
                loop.call_soon_threadsafe(signals.subscribed.set)

        def on_message(_client: Any, _userdata: Any, msg: Any) -> None:
            loop.call_soon_threadsafe(signals.messages.put_nowait, msg)

        client = paho_mqtt.Client(client_id=client_id, clean_session=True)
        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_message = on_message
        if self._broker.username:
            client.username_pw_set(self._broker.username, self._broker.password or "")
        return client

    async def _connect_and_listen(self, client_id: str) -> None:
        signals = _SessionSignals()
        client = self._create_session_client(client_id, signals)

        def _connect_and_spin_up() -> None:
            client.connect(self._broker.host, self._broker.port)
            # loop_start's socketpair setup does a blocking accept();
            # keep it off the loop thread too.
            client.loop_start()

        await run_in_executor(_connect_and_spin_up)
        try:
            await asyncio.wait_for(signals.connack_processed.wait(), timeout=_CONNECT_TIMEOUT)
            if signals.errors:
                raise ConnectionError(signals.errors[0])

            _LOGGER.info("MQTT connected to %s:%s", self._broker.host, self._broker.port)
            # Signal to ``_run`` that this iteration achieved a real
            # connection — read+cleared in its except branches to
            # re-arm the loud-log gates. Set here rather than relying
            # on a clean ``_connect_and_listen`` return because the
            # inner TaskGroup parks until cancelled or raises, so the
            # ``else`` branch on the caller's ``try`` rarely runs in
            # production.
            self._connected_this_session = True
            self._set_connected(value=True)

            async def raise_when_session_failed() -> None:
                await signals.failed.wait()
                raise ConnectionError(signals.errors[-1])

            async def require_suback() -> None:
                # A broker that accepts a SUBSCRIBE but never SUBACKs
                # would leave discovery dark until the keepalive kills
                # the connection; bound the wait — re-armed for every
                # reconnect handshake, not just the first.
                while True:
                    await signals.suback_pending.wait()
                    signals.suback_pending.clear()
                    try:
                        async with asyncio.timeout(_CONNECT_TIMEOUT):
                            await signals.subscribed.wait()
                    except TimeoutError:
                        msg = "no SUBACK for the discovery subscription"
                        raise ConnectionError(msg) from None

            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._listen(signals.messages))
                    tg.create_task(self._ping_loop(client))
                    tg.create_task(raise_when_session_failed())
                    tg.create_task(require_suback())
            except BaseExceptionGroup as eg:
                # The TaskGroup wraps even a lone connection error;
                # unwrap so ``_run``'s expected/quiet reconnect path
                # keeps its flat except contract.
                raise _unwrap_session_error(eg) from None
        finally:
            # loop_stop joins paho's thread and disconnect touches its
            # socket — keep both off the loop, even during cancellation.
            def _teardown() -> None:
                # paho's documented order: disconnect while the network
                # thread still runs, so the DISCONNECT packet actually
                # goes out. The join must run even if disconnect raises,
                # or the thread (and its socket) leaks every reconnect.
                try:
                    client.disconnect()
                finally:
                    client.loop_stop()

            try:
                await run_in_executor(_teardown)
            except Exception:
                _LOGGER.exception("MQTT client teardown failed")
            finally:
                # The await is a cancellation point; the flag must
                # clear even when a second cancel lands there.
                self._set_connected(value=False)

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

    async def _ping_loop(self, client: PublishClient) -> None:
        """Run one broker session's discover loop — see :class:`_DiscoverPingLoop`."""
        await self._ping.run_session(client)

    async def _broadcast(self, client: PublishClient) -> bool:
        """
        Send one discover request; False pauses aging for the tick.

        Publishes from the executor — paho's enqueue shares a lock
        with its network thread mid-send, so the loop must not wait
        on it.
        """
        info = await run_in_executor(client.publish, _DISCOVER_PUBLISH_TOPIC)
        if info.rc == 0:
            if self._publish_error_logged:
                # The failed stretch was a no-poll period — same rebase
                # as resuming from idle.
                self._rebase_last_seen()
                self._publish_error_logged = False
            return True
        if self._publish_error_logged:
            _LOGGER.debug("discover broadcast still failing (rc=%s)", info.rc)
        else:
            _LOGGER.warning(
                "Discover broadcast to %s:%s failed (rc=%s) — device aging paused",
                self._broker.host,
                self._broker.port,
                info.rc,
            )
            self._publish_error_logged = True
        return False

    def _rebase_last_seen(self) -> None:
        now = asyncio.get_running_loop().time()
        for name in self._last_seen:
            self._last_seen[name] = now

    def _sweep_stale(self) -> None:
        """Flip entries silent past ``_OFFLINE_TIMEOUT`` to OFFLINE."""
        now = asyncio.get_running_loop().time()
        last_seen = self._last_seen
        # Materialized so the pop below can't mutate the dict mid-iteration.
        for name in [n for n, last in last_seen.items() if now - last > _OFFLINE_TIMEOUT]:
            self._on_state_change(name, DeviceState.OFFLINE)
            last_seen.pop(name, None)

    def _set_connected(self, *, value: bool) -> None:
        if self.connected == value:
            return
        self.connected = value
        if self._on_connection_change is not None:
            self._on_connection_change()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


class _DiscoverPingLoop(PresenceGatedLoop[bool]):
    """
    Broadcast discover requests and sweep stale devices offline.

    Every MQTT device answers each ``esphome/discover`` publish, so
    broadcasts run only while a dashboard client is subscribed, and
    only from the elected broadcaster. While idle the loop parks
    with the offline aging frozen — silence without polls is not
    evidence — and spontaneous announcements keep flowing through
    ``_listen``. Aging belongs to the broadcaster for the same
    reason: a silent sibling must not judge a fleet nobody is
    polling, and a tick whose own broadcast failed to send judges
    nobody either.
    """

    _label = "MQTT discover ping"
    # An unexpected crash must tear down the broker session so
    # ``_run``'s reconnect taxonomy handles it; publish failures are
    # already soft (``_broadcast`` returns False and pauses aging).
    _continue_on_error = False

    def __init__(self, monitor: DeviceMqttMonitor, presence: SubscriberPresence | None) -> None:
        super().__init__(presence)
        self._monitor = monitor
        self._client: PublishClient | None = None

    @property
    def _interval(self) -> float:  # type: ignore[override]
        # Live-read like _OFFLINE_TIMEOUT so tests patching the
        # module constant are honored.
        return _PING_INTERVAL

    async def run_session(self, client: PublishClient) -> None:
        """Run this loop for one broker session's *client*."""
        self._client = client
        try:
            await self.run()
        finally:
            # Don't pin the dead session's client until the next connect.
            self._client = None

    def _on_resume(self) -> None:
        # Rebase so the resumed timeout window starts now — otherwise
        # every device idle longer than the timeout mass-flips OFFLINE
        # before it can answer the first post-resume broadcast.
        self._monitor._rebase_last_seen()

    async def _work(self) -> bool:
        assert self._client is not None  # bound in run_session
        monitor = self._monitor
        return monitor.is_publisher and await monitor._broadcast(self._client)

    def _after_idle(self, broadcast_sent: bool) -> None:  # noqa: FBT001 — hook signature
        if broadcast_sent:
            self._monitor._sweep_stale()


def _unwrap_session_error(eg: BaseExceptionGroup) -> BaseException:
    """Return the first expected connection error inside *eg*, or *eg* itself."""
    expected, rest = eg.split(_EXPECTED_SESSION_ERRORS)
    if expected is not None and rest is None:
        if len(expected.exceptions) > 1:
            _LOGGER.debug("Collapsing concurrent session errors: %r", expected.exceptions)
        return expected.exceptions[0]
    return eg


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
