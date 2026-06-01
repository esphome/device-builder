"""Test fixtures shared across the suite.

Currently just wires Blockbuster (https://github.com/cbornet/blockbuster)
in as an autouse fixture so any blocking call made from inside an
asyncio event loop fails the test instead of silently stalling the
loop. The dashboard is async-first; a stray ``time.sleep`` or sync
``open().read()`` on the request path would tank latency without
showing up in the build, so we let CI catch them.

The fixture only activates on Linux — that's what production runs on
(ESPHome container, HA add-on), so it's the only platform where a
blocking call would actually hit users. Windows and macOS just skip
the check; their CI runs are about catching platform-specific
regressions, not async hygiene.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile as _tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest
from blockbuster import blockbuster_ctx
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers._device_mqtt_coordinator import (
    DeviceMqttCoordinator,
)
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor import ping as _ping_module
from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._metadata_store import DeviceMetadataStore
from esphome_device_builder.controllers.devices._shared_sidecar import SharedSidecarClient
from esphome_device_builder.controllers.devices._state import DevicesState
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.remote_build import (
    OffloaderController,
    ReceiverController,
)
from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.models import (
    AdoptableDevice,
    Device,
    DeviceState,
    EventType,
    QueueStatus,
)

if TYPE_CHECKING:
    from blockbuster import BlockBuster

# True when the installed esphome's StorageJSON carries a ``toolchain``
# field (>= 2026.5.0). That field is the exact dependency the offload path
# keys native-IDF detection on (``toolchain == "esp-idf"``); older esphome
# drops it on load, so tests that synthesize a native-IDF build skip there.
# Probed off the StorageJSON signature directly rather than a correlated
# const so the gate tracks the real runtime dependency. Mirrors the
# native-IDF compile e2e gate.
HAS_NATIVE_IDF_TOOLCHAIN = "toolchain" in inspect.signature(StorageJSON.__init__).parameters

# Call sites known to do bounded blocking I/O during one-time server
# startup, where the cost is paid once and not on the request path.
# Listed here as ``(filename, function_name)`` pairs so blockbuster
# only exempts that specific frame, not the function globally. Keep
# this list short — anything genuinely on a hot path should be
# refactored, not allowlisted.
_STARTUP_BLOCKING_OK: tuple[tuple[str, str], ...] = (
    # SessionStore reads the persisted JSON sessions file once at
    # AuthController construction time. Bounded by the dashboard's
    # own startup, not request volume.
    ("helpers/auth.py", "_load"),
    # Sync sibling of ``_persist_async``. Production callers always
    # wrap it via ``asyncio.to_thread`` (so blockbuster wouldn't see
    # it from a worker thread anyway); tests call it directly to seed
    # state without a round-trip through the executor.
    ("helpers/auth.py", "_persist"),
    # ``_register_frontend`` stat-checks ``index.html`` and ``assets/``
    # at app construction so a broken frontend wheel surfaces a clear
    # RuntimeError instead of mysterious 404s. Runs once at startup.
    ("device_builder.py", "_register_frontend"),
    # ``_start_ingress_site`` calls ``create_app`` (which stat-checks
    # the boards-images directory) and binds a TCP socket. Both run
    # once at HA-add-on startup as an aiohttp ``on_startup`` hook —
    # the cost is paid once, not on the request path.
    ("device_builder.py", "_start_ingress_site"),
    # ``_find_sibling_cli`` probes for ``<bin>/esphome`` and
    # ``<bin>/esptool`` to pick between a sibling script and
    # ``python -m <cli>``. The result is ``lru_cache``-d so the
    # ``os.stat`` only fires once per ``name`` per process — first
    # call is on a request path (``_run_esptool`` for chip detect,
    # ``verify_chip`` for firmware install) but every subsequent
    # one hits the cache.
    ("controllers/firmware/helpers.py", "_find_sibling_cli"),
    # ``CORE.data_dir`` walks ``CORE.config_dir`` which stats — one-time
    # at controller construction.
    ("controllers/devices/controller.py", "__init__"),
)


@pytest.fixture(autouse=True)
def blockbuster() -> Iterator[BlockBuster | None]:
    """Fail any test that performs a blocking call from inside the event loop.

    Linux-only: production targets are Linux containers, so other
    platforms skip the check and just yield ``None`` (the fixture
    return value isn't consumed by any test today).
    """
    if not sys.platform.startswith("linux"):
        yield None
        return
    # Scope the check to our package: a blocking call only fails the
    # test when the offending frame originates inside
    # ``esphome_device_builder``. Test-fixture niceties (sync
    # ``Path.mkdir`` for setup, ``tmp_path.write_text``, etc.) and
    # third-party library internals are intentionally exempt — we're
    # auditing the dashboard's request paths, not pytest itself.
    with blockbuster_ctx("esphome_device_builder") as bb:
        for fn in bb.functions.values():
            for filename, func_name in _STARTUP_BLOCKING_OK:
                fn.can_block_in(filename, func_name)
        yield bb


# ---------------------------------------------------------------------------
# Catalog fixtures
#
# ---------------------------------------------------------------------------
# CORE.config_path autouse — ``ext_storage_path`` prerequisite
#
# Every storage / build-info / firmware-bin lookup in production routes
# through ``ext_storage_path`` (or ``CORE.data_dir`` directly). Both
# crash with ``AttributeError: 'NoneType' object has no attribute
# 'is_dir'`` when ``CORE.config_path`` is the package-default ``None``.
# Production sets it once at startup; tests need an equivalent. Pinning
# the sentinel onto ``tmp_path`` keeps the resolved
# ``data_dir = tmp_path / .esphome`` aligned with where
# ``write_storage_json`` and the other storage helpers drop their files,
# so a test that just wants the storage layout to "work" gets that for
# free without per-module wiring. ``monkeypatch`` auto-restores so
# tests that override ``CORE.config_path`` (e.g. ``make_settings(
# with_core_path=True)``) still take precedence, and sibling xdist
# workers don't see leaked process-globals.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _core_config_path_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CORE, "config_path", tmp_path / "___DASHBOARD_SENTINEL___.yaml")


@pytest.fixture(autouse=True)
def _stub_icmp_privilege_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass ``PingSource.run``'s ICMP socket privilege probe.

    The real probe opens ``SOCK_RAW`` / ``SOCK_DGRAM`` against
    ``127.0.0.1``; on CI runners and locked-down dev machines
    that fails, ``run`` returns early, and tests that drive the
    ping loop see zero sweeps. Tests that exercise the probe
    itself (``test_can_use_icmp_lib_with_privilege_*``) call the
    function through their imported binding, bypassing this
    monkeypatch.
    """

    async def _probe_ok() -> bool:
        return True

    monkeypatch.setattr(_ping_module, "_can_use_icmp_lib_with_privilege", _probe_ok)


# ---------------------------------------------------------------------------
# Component catalog session fixture
#
# ``ComponentCatalog.load`` parses ~40 MB of JSON and instantiates ~900 entry
# objects — about a second wall-time per call, plus blockbuster overhead on
# Linux CI. Hoisting the load to a session-scoped fixture lets every test
# module that wants the real catalog share the same instance per xdist
# worker, instead of paying the cost in each module's setup.
# ---------------------------------------------------------------------------


class _CatalogContainer:
    """Minimal ``device_builder``-shaped object the ComponentCatalog reads from."""

    boards: BoardCatalog | None = None
    components: ComponentCatalog | None = None


# ---------------------------------------------------------------------------
# Async helpers shared across the suite
#
# ``capture_events`` + ``cancel_and_drain`` lived in
# ``test_remote_build_peer_link_client.py`` and the e2e conftest
# before being hoisted here — every test that drives a live
# :class:`EventBus` or a cancellable background task has the same
# two boilerplate shapes, and copying them per file made review
# noisy. Hoisting also lets the e2e ``paired_instances`` fixture
# import them with a plain ``from ..conftest import ...``.
# ---------------------------------------------------------------------------


class _CapturedEvents(list[dict]):
    """A list of captured event payloads with an :class:`asyncio.Event` set on each append.

    Subclassing ``list`` keeps the natural ``captured[0]["reason"]``
    / ``len(captured)`` access shape that callers expect; the
    extra :attr:`received` event lets a test ``await
    asyncio.wait_for(captured.received.wait(), timeout=...)``
    instead of polling on a ``for _ in range(N): sleep(0.01)``
    loop.
    """

    def __init__(self) -> None:
        super().__init__()
        self.received = asyncio.Event()

    def append(self, item: dict) -> None:
        super().append(item)
        self.received.set()

    async def wait_for_status(self, status: str, *, timeout: float = 2.0) -> dict:
        """Return the first captured event whose ``status`` matches *status*.

        The *timeout* bounds the total wait, not each loop iteration,
        so a stream of non-matching events can't push the deadline
        forward indefinitely.
        """

        async def _poll() -> dict:
            while True:
                for entry in self:
                    if entry.get("status") == status:
                        return entry
                self.received.clear()
                await self.received.wait()

        return await asyncio.wait_for(_poll(), timeout=timeout)


def capture_events(bus: EventBus, event_type: EventType) -> _CapturedEvents:
    """Subscribe to *event_type* on *bus* and return a list captured-as-they-fire.

    Each fired event's ``data`` payload is materialised into a
    plain dict and appended. The returned object exposes a
    ``received`` :class:`asyncio.Event` that's set on every
    append — callers ``await asyncio.wait_for(captured.received.wait(),
    timeout=...)`` to block until the first event lands rather
    than spinning on a polling loop.
    """
    captured = _CapturedEvents()
    bus.add_listener(event_type, lambda event: captured.append(dict(event.data)))
    return captured


@dataclass(frozen=True)
class RemoteBuildTestHandles:
    """Test-only bundle of the two sibling remote-build controllers.

    Production code accesses the two siblings
    (:class:`OffloaderController` and :class:`ReceiverController`)
    as separate attributes on :class:`DeviceBuilder`. Tests model
    that shape exactly: reach through ``handles.offloader`` for
    outbound-side state + methods, ``handles.receiver`` for
    inbound-side. The convenience ``start`` / ``stop`` methods
    bring both sides up together; per-side tests can skip the
    bundle and instantiate the relevant sibling directly.
    """

    offloader: OffloaderController
    receiver: ReceiverController

    @property
    def _db(self) -> Any:
        """The shared :class:`DeviceBuilder` ref both siblings hold.

        Both siblings stash the same ``DeviceBuilder`` ref in
        ``__init__``; this accessor lets test code reach
        ``handles._db.firmware`` etc. without picking a sibling
        arbitrarily.
        """
        return self.offloader._db

    async def start(self) -> None:
        """Start both siblings, in the same order ``DeviceBuilder`` does."""
        await self.receiver.start()
        await self.offloader.start()

    async def stop(self) -> None:
        """Stop both siblings, in the same order ``DeviceBuilder`` does."""
        await self.offloader.stop()
        await self.receiver.stop()


def make_remote_build_controller(
    *,
    config_dir: Path,
    bus: EventBus | None = None,
) -> RemoteBuildTestHandles:
    """Build both remote-build sibling controllers against a stub :class:`DeviceBuilder`.

    Single source of truth for the per-test stub-DB shape.
    Mounted on a real :class:`EventBus` when *bus* is provided
    (e.g. the e2e harness's two-instance setup), otherwise
    ``MagicMock`` auto-attribute resolution gives the controllers
    a no-op ``bus.fire``.

    ``db.create_background_task`` is wired to
    :func:`asyncio.create_task` rather than left as a MagicMock so
    coroutines passed through it actually run.
    """
    db = MagicMock()
    db.devices = MagicMock()
    db.devices.zeroconf = None
    db._dashboard_advertiser = None
    db.settings = MagicMock()
    db.settings.config_dir = config_dir
    db.peer_link_identity_store = PeerLinkIdentityStore(config_dir)
    db.create_background_task = asyncio.create_task
    db.firmware.queue_status_snapshot = MagicMock(
        return_value=QueueStatus(idle=True, running=False, queue_depth=0)
    )
    if bus is not None:
        db.bus = bus
    return RemoteBuildTestHandles(
        offloader=OffloaderController(db),
        receiver=ReceiverController(db),
    )


def reset_offloader_firmware_stub(
    handles: RemoteBuildTestHandles,
    *,
    reset_bus: bool = False,
    **queue_status_kwargs: Any,
) -> MagicMock:
    """Re-stub the offloader's firmware mock; ``queue_status_kwargs`` go to ``MagicMock(...)``."""
    if reset_bus:
        handles.offloader._db.bus = MagicMock()
    firmware = handles.offloader._db.firmware = MagicMock()
    firmware.queue_status_snapshot = MagicMock(**queue_status_kwargs)
    return firmware


def wire_firmware_remote_peer_api_mocks(firmware: Any, jobs_by_id: dict[str, Any]) -> None:
    """Wire a MagicMock firmware controller's remote-peer lookup API against *jobs_by_id*.

    Assigns ``firmware.state.jobs = jobs_by_id`` and installs
    ``side_effect`` callables on ``find_remote_peer_job`` and
    ``remote_peer_job_ids`` so they return what the real
    :class:`FirmwareController` would. Single source of truth
    so the e2e harness and unit tests share the same stub
    semantics.
    """
    firmware.state.jobs = jobs_by_id
    firmware.find_remote_peer_job.side_effect = lambda *, remote_peer, remote_job_id: next(
        (
            j
            for j in jobs_by_id.values()
            if j.remote_peer == remote_peer and j.remote_job_id == remote_job_id
        ),
        None,
    )
    firmware.remote_peer_job_ids.side_effect = lambda *, remote_peer: [
        j.remote_job_id for j in jobs_by_id.values() if j.remote_peer == remote_peer
    ]


async def cancel_and_drain(task: asyncio.Task[Any]) -> None:
    """Cancel *task* and await its termination, swallowing the resulting CancelledError.

    Equivalent to ``task.cancel(); contextlib.suppress(...) +
    await``, but written with :func:`asyncio.gather` so the
    exception is captured by the gather aggregation rather than
    propagating into the test body. Use at end-of-test cleanup
    where the cancellation was the test's intended teardown
    signal — not where the test asserts on the cancellation.
    """
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# submit_job test helpers
#
# Bundle construction + wire-frame builders shared between the
# unit tests in ``test_remote_build_submit_job.py`` and the
# integration tests in ``test_remote_build_peer_link.py``. Each
# is a one-liner factory but they were duplicated across the
# two files before this lived here.
# ---------------------------------------------------------------------------


def make_tar_bundle(yaml_filename: str, yaml_body: bytes) -> bytes:
    """Build a minimal gzipped-tar bundle carrying a single YAML.

    Pure-shape: the receiver-side
    ``prepare_bundle_for_compile`` validates a manifest +
    canonical layout that this helper deliberately doesn't
    construct. Tests that exercise the receive-loop dispatch
    stub ``prepare_bundle_for_compile`` rather than feeding it
    a real esphome bundle.
    """
    import tarfile  # noqa: PLC0415 — keep tarfile out of the conftest top-level
    from io import BytesIO  # noqa: PLC0415

    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=yaml_filename)
        info.size = len(yaml_body)
        tar.addfile(info, BytesIO(yaml_body))
    return buf.getvalue()


def make_submit_job_frames(
    *,
    job_id: str,
    configuration_filename: str,
    target: str,
    bundle: bytes,
    device_name: str = "",
    device_friendly_name: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the wire-shape ``submit_job`` header + chunk frames for *bundle*.

    Returns ``(header, chunks)`` as plain dicts so callers can
    feed them through either the receiver-side handler
    directly or as JSON-encoded ciphertext over a real WS.
    Wraps :func:`chunk_bundle` so the chunk envelope (b64
    encoding, indices, ``is_last`` flag, repeated ``job_id``)
    lives in one place.

    ``device_name`` / ``device_friendly_name`` are the
    ``NotRequired`` display fields the receiver stamps onto
    :class:`FirmwareJob` for the firmware-tasks UI title. Both
    default to empty so existing callers stay one-liners; a
    test that wants to pin the round-trip passes them
    explicitly.
    """
    from esphome_device_builder.helpers.peer_link_bundle import (  # noqa: PLC0415
        chunk_bundle,
        compute_bundle_sha256,
        encode_chunk,
    )

    chunks = [
        {
            "type": "submit_job_chunk",
            "job_id": job_id,
            "chunk_index": index,
            "data_b64": encode_chunk(raw),
            "is_last": is_last,
        }
        for index, raw, is_last in chunk_bundle(bundle)
    ]
    header = {
        "type": "submit_job",
        "job_id": job_id,
        "configuration_filename": configuration_filename,
        "target": target,
        "total_bundle_bytes": len(bundle),
        "num_chunks": len(chunks),
        "bundle_sha256": compute_bundle_sha256(bundle),
        "device_name": device_name,
        "device_friendly_name": device_friendly_name,
    }
    return header, chunks


@pytest.fixture(scope="session")
def session_board_catalog() -> BoardCatalog:
    """Real ``BoardCatalog`` loaded once per xdist worker."""
    cat = BoardCatalog()
    cat.load()
    return cat


@pytest.fixture(scope="session")
def session_component_catalog(session_board_catalog: BoardCatalog) -> ComponentCatalog:
    """Real ``ComponentCatalog`` (with featured registry built) loaded once per worker.

    The fixture returns a single shared instance for every test in
    the session, so tests that mutate the catalog must restore it
    before the test finishes (typically via ``try / finally``).
    """
    container = _CatalogContainer()
    container.boards = session_board_catalog
    container.components = ComponentCatalog(container)
    container.components.load()
    return container.components


# ---------------------------------------------------------------------------
# WebSocketClient stand-in
#
# Three test files (``test_subscribe_events_cleanup.py`` and the two
# ``controllers/firmware/test_follow_job*_race.py`` files) had grown
# near-identical ``_FakeClient`` shells that capture ``send_event`` /
# ``send_result`` calls without standing up a real aiohttp WebSocket.
# Centralising means the next handler-test that needs to record
# streaming output gets a battle-tested stub for free.
# ---------------------------------------------------------------------------


class FakeWebSocketClient:
    """Minimal ``WebSocketClient`` stand-in capturing send calls in order.

    ``events`` records ``(message_id, event, data)`` tuples; ``results``
    records ``(message_id, result)``. Tests inspect those lists directly
    instead of poking at a real aiohttp WebSocket.

    ``yield_per_event=True`` makes ``send_event`` ``await
    asyncio.sleep(0)`` before recording. The firmware
    ``follow_job`` race tests need it: without the yield, a tight
    history-snapshot loop would drain in a single uninterrupted task
    slice and the "fire JOB_OUTPUT mid-history" race-window the fix
    targets would never be observable. Defaults to ``False`` because
    most callers just want a passive recorder.
    """

    def __init__(self, *, yield_per_event: bool = False) -> None:
        self.events: list[tuple[str, str, Any]] = []
        self.results: list[tuple[str, Any]] = []
        self._yield_per_event = yield_per_event
        self._stream_tasks: dict[str, asyncio.Task] = {}

    def register_stream(self, message_id: str, task: asyncio.Task) -> None:
        self._stream_tasks[message_id] = task

    def unregister_stream(self, message_id: str) -> None:
        self._stream_tasks.pop(message_id, None)

    def cancel_stream(self, message_id: str) -> bool:
        task = self._stream_tasks.pop(message_id, None)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def send_event(self, message_id: str, event: str, data: Any) -> None:
        if self._yield_per_event:
            await asyncio.sleep(0)
        self.events.append((message_id, event, data))

    async def send_result(self, message_id: str, result: Any) -> None:
        self.results.append((message_id, result))

    def events_for(self, name: str) -> list[Any]:
        """Return the ``data`` payloads for every captured ``send_event(..., event=name, ...)``.

        ``send_event`` takes ``(message_id, event, data)`` —
        ``name`` here filters by the ``event`` argument. Most
        assertions only care about the data attached to a
        specific event ("show me every ``output`` line");
        collapsing the (message_id, event, data) tuple unpacking
        into one helper keeps the test bodies readable.
        """
        return [data for (_mid, event, data) in self.events if event == name]

    def indices_for(self, name: str) -> list[int]:
        """Return the positional indices where ``send_event`` was called with ``event=name``.

        Pair with :meth:`events_for` when a test needs to assert
        relative ordering between events of different names —
        e.g. "the snapshot frame landed before the first
        ``job_queued`` event" — without re-doing the (_mid, event,
        data) unpack at every call site.
        """
        return [i for i, (_mid, event, _data) in enumerate(self.events) if event == name]

    def first_index_for(self, name: str) -> int:
        """Return the first index where ``send_event`` was called with ``event=name``.

        Raises ``StopIteration`` if no match — the contract mirrors
        ``next(iter(...))`` so a missing event produces a clear
        traceback at the assertion site rather than a confusing
        ``IndexError`` later.
        """
        return next(i for i, (_mid, event, _data) in enumerate(self.events) if event == name)


# ---------------------------------------------------------------------------
# DashboardSettings factory
#
# Several test modules build a ``DashboardSettings`` rooted at ``tmp_path``
# without going through ``parse_args`` — config_controller, executor pool,
# device_builder lifecycle, ws handler branches, ha addon failsafe. They
# all reproduce the same two-line wiring (``config_dir`` /
# ``absolute_config_dir``); some additionally patch ``CORE.config_path``
# because the catalog loaders crash on ``CORE.config_dir.is_dir`` when
# the package-default ``None`` leaks in.
# ---------------------------------------------------------------------------


class MakeSettingsFactory(Protocol):
    """Shape of the ``make_settings`` fixture's return value."""

    def __call__(self, *, with_core_path: bool = ...) -> DashboardSettings: ...


@pytest.fixture
def make_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MakeSettingsFactory:
    """Return a factory that builds a ``DashboardSettings`` rooted at ``tmp_path``.

    The minimal shape ``DeviceBuilder.__init__`` /
    ``ConfigController`` / ``DashboardSettings.rel_path`` need:
    ``config_dir`` and ``absolute_config_dir``.

    ``with_core_path=True`` additionally patches ``CORE.config_path``
    to the same sentinel filename ``DashboardSettings.parse_args``
    writes in production (``_DASHBOARD_SENTINEL_FILE`` in
    ``controllers/config.py``). Without it, code paths that
    reach into the catalog loaders crash on
    ``CORE.config_dir.is_dir`` because ``CORE.config_path`` is the
    package-default ``None``. Routing through ``monkeypatch``
    auto-restores the value after the test so sibling tests in the
    same xdist worker don't see leaked process-globals.
    """

    def _make(*, with_core_path: bool = False) -> DashboardSettings:
        settings = DashboardSettings()
        settings.config_dir = tmp_path
        settings.absolute_config_dir = tmp_path.resolve()
        if with_core_path:
            monkeypatch.setattr(CORE, "config_path", tmp_path / "___DASHBOARD_SENTINEL___.yaml")
        return settings

    return _make


# ---------------------------------------------------------------------------
# Hermetic ``DeviceBuilder.start()`` — stubs network / subprocess surfaces
# so any test that wants a fully-wired DeviceBuilder (lifecycle smoke,
# mDNS-advertise wiring, command-registry walks) doesn't depend on a
# real esphome install / ICMP / MQTT / multicast bind.
# ---------------------------------------------------------------------------


@pytest.fixture
def _hermetic_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the network / subprocess surfaces so ``DeviceBuilder.start()`` runs hermetically.

    - ``_find_esphome_cmd`` returns a fake invocation so the resolver
      doesn't depend on a real esphome install.
    - ``_verify_esphome_importable`` short-circuits to ``(True, "")``
      so the firmware controller's startup probe doesn't spawn a
      15-second subprocess.
    - ``DeviceStateMonitor.start/stop`` are AsyncMocks at the class
      level so any instance constructed by ``DevicesController``
      picks them up. ``_zeroconf`` stays at its ``None`` default,
      which the dashboard-advertise wiring treats as "skip".
    - ``DeviceMqttCoordinator.reconcile/stop`` are AsyncMocks for the
      same reason — production opens a paho broker connection.
    - ``BoardCatalog.load`` / ``ComponentCatalog.load`` are no-oped
      because they do synchronous ``Path.glob`` walks of the
      bundled definitions tree — under blockbuster (Linux CI) the
      ``ScandirIterator`` calls fail event-loop checks. Wiring
      (``BoardCatalog()`` construction + ``@api_command`` decorator
      collection) still runs, so the tests still pin the
      controller-attr and command-handler contract; the catalog's
      *contents* are exercised in the catalog-specific tests.
    - ``FirmwareController._load_jobs`` is AsyncMock'd so the
      controller's startup doesn't try to read jobs from disk.
    """
    fake_cmd = ["python", "-m", "esphome"]
    for module in (
        "esphome_device_builder.controllers.firmware.controller",
        "esphome_device_builder.controllers.editor",
        "esphome_device_builder.controllers.devices.controller",
    ):
        monkeypatch.setattr(f"{module}._find_esphome_cmd", lambda: fake_cmd)
    monkeypatch.setattr(FirmwareController, "_load_jobs", AsyncMock())
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.controller._verify_esphome_importable",
        AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(DeviceStateMonitor, "start", AsyncMock())
    monkeypatch.setattr(DeviceStateMonitor, "stop", AsyncMock())
    monkeypatch.setattr(DeviceMqttCoordinator, "reconcile", AsyncMock())
    monkeypatch.setattr(DeviceMqttCoordinator, "stop", AsyncMock())
    # The remote-build feature wires a second mDNS browser
    # behind ``OffloaderController.start``. The lifecycle tests use
    # the same "stub start/stop on the class" trick to keep the
    # smoke test hermetic — the per-controller test file
    # ``test_remote_build_controller.py`` exercises the real browser.
    monkeypatch.setattr(OffloaderController, "start", AsyncMock())
    monkeypatch.setattr(OffloaderController, "stop", AsyncMock())
    monkeypatch.setattr(ReceiverController, "start", AsyncMock())
    monkeypatch.setattr(ReceiverController, "stop", AsyncMock())
    monkeypatch.setattr(BoardCatalog, "load", lambda self: None)
    monkeypatch.setattr(ComponentCatalog, "load", lambda self: None)
    # ``CORE`` is a process-global; without restoration via
    # monkeypatch a leaked ``config_path`` poisons sibling tests in
    # the same xdist worker.
    monkeypatch.setattr(CORE, "config_path", None)


# ---------------------------------------------------------------------------
# Bypass-init DevicesController + real EventBus + captured listener
#
# The handler-test-style ``make_controller`` factory in
# ``tests/controllers/devices/conftest.py`` is the right tool for handler-
# level coverage. The shape this helper builds is for the *callback* tests
# at the top of the tree (``test_device_state_event.py`` /
# ``test_state_fanout_duplicate_names.py``) — they exercise
# ``_on_state_change`` / ``_on_ip_change`` etc. against a minimal scanner +
# real EventBus, and were each carrying a near-identical ``_make_controller``
# helper before this lived in one place.
# ---------------------------------------------------------------------------


def make_devices_controller_with_bus(
    devices: list[Device],
    *,
    create_background_task: Callable[[Any], Any] | None = None,
) -> tuple[DevicesController, list[Event]]:
    """Bypass-init a ``DevicesController`` wired to a real ``EventBus``.

    Returns the controller and a live ``list[Event]`` capture for
    *every* ``EventType`` fired on the bus. Tests assert on the
    flat list instead of poking ``MagicMock.assert_called_once_with``
    / ``call_args_list`` — the contract being tested is "what
    fanned out to the bus", not the call shape of the mock that
    recorded it.

    Captures every event type by default (rather than an explicit
    subset) so a refactor that fires an unexpected event surfaces
    in the test instead of silently passing through. Tests that
    only care about a subset filter the list themselves
    (``[e for e in captured if e.event_type == X]``).

    The scanner is a ``MagicMock`` exposing ``devices`` and a
    ``get_by_name(name)`` lambda derived from *devices*; mirrors
    the production ``DeviceScanner``'s name-keyed grouping closely
    enough for the callback paths these tests exercise.

    ``create_background_task`` lets callers wire a side-effect
    function (e.g. closing the coroutine to avoid
    ``RuntimeWarning: coroutine was never awaited``) for the
    persist-async branches.
    """
    bus = EventBus()
    captured: list[Event] = []
    for event_type in EventType:
        bus.add_listener(event_type, captured.append)
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller.state = DevicesState()
    if create_background_task is not None:
        controller._db.create_background_task = MagicMock(side_effect=create_background_task)
    controller._db.bus = bus
    controller._scanner = MagicMock()
    controller._scanner.devices = devices
    by_name: dict[str, list[Device]] = {}
    for device in devices:
        by_name.setdefault(device.name, []).append(device)
    controller._scanner.get_by_name = lambda name: by_name.get(name, [])
    # Real metadata stores anchored at a TemporaryDirectory whose
    # lifetime is pinned to the controller; ``__del__`` cleans up
    # the dir when the test releases its reference.
    tmp_dir_obj = _tempfile.TemporaryDirectory(prefix="dmstore_")
    tmp_dir = Path(tmp_dir_obj.name)
    controller._tmpdir = tmp_dir_obj  # keep alive
    controller._shutdown_callbacks = []
    controller._metadata_store = DeviceMetadataStore(
        config_dir=tmp_dir,
        data_dir=tmp_dir,
        shutdown_register=controller._shutdown_callbacks.append,
    )
    controller._shared_sidecar = SharedSidecarClient(tmp_dir)
    return controller, captured


# ---------------------------------------------------------------------------
# DeviceStateMonitor callback recorder
#
# Every ``test_mdns_*.py`` file at the top of the tree was carrying a
# near-identical ``_monitor()`` helper that wired ``MagicMock(side_effect=
# _flip)`` callbacks per field, then asserted via ``cb.assert_called_with``.
# The ``_flip`` side-effect was specifically there to mirror what the real
# DevicesController callback does — without it, the monitor's dedupe
# (which keys off the device's *own* field) doesn't observe the
# post-callback state and every repeat call would re-fire.
# ---------------------------------------------------------------------------


class RecordingMonitorCallbacks:
    """Capture monitor callback calls + apply production state-flip side-effects.

    Mirrors every callback ``DeviceStateMonitor`` invokes on its
    owning ``DevicesController`` (``on_state_change`` /
    ``on_ip_change`` / ``on_version_change`` /
    ``on_config_hash_change`` / ``on_api_encryption_change`` /
    ``on_importable_added`` / ``on_importable_removed``). Each
    invocation lands in ``self.calls`` as a flat tuple
    ``(callback_name, *args)``; tests assert on the list directly
    instead of poking ``MagicMock.assert_called_*``.

    Per-device callbacks also write the new value back onto the
    matching ``Device`` in *devices* — the same write the production
    ``DevicesController._on_*_change`` callback does. Without that
    side-effect, the monitor's dedupe (keyed off the device's own
    field) would never observe the post-callback state and every
    repeat call would re-fire. The importable callbacks just record
    (no per-device state to mirror).

    Type-resistant to typos: ``cb.assertt_called_once_with`` would
    spawn a fresh ``MagicMock`` attribute and silently pass; calling
    ``callbacks.assertt_called_once_with`` raises ``AttributeError``.

    Helpers:
    - ``calls_for(name)`` filters the log by callback name — the
      mDNS lifecycle tests frequently want "every importable_added
      that fired" without re-doing the list-comp.
    """

    def __init__(self, devices: list[Device]) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._devices = devices

    def _flip(self, name: str, attr: str, value: Any) -> None:
        """Write *value* to *attr* on every matching device."""
        for device in self._devices:
            if device.name == name:
                setattr(device, attr, value)

    def calls_for(self, callback_name: str) -> list[tuple[Any, ...]]:
        """Return every recorded call whose first element equals *callback_name*."""
        return [call for call in self.calls if call[0] == callback_name]

    def on_state_change(self, name: str, state: DeviceState, source: str) -> None:
        self.calls.append(("on_state_change", name, state, source))
        self._flip(name, "state", state)

    def on_ip_change(self, name: str, ip: str, addresses: list[str]) -> None:
        self.calls.append(("on_ip_change", name, ip, list(addresses)))
        self._flip(name, "ip", ip)
        self._flip(name, "ip_addresses", list(addresses))

    def on_version_change(self, name: str, version: str) -> None:
        self.calls.append(("on_version_change", name, version))
        self._flip(name, "deployed_version", version)

    def on_config_hash_change(self, name: str, config_hash: str) -> None:
        self.calls.append(("on_config_hash_change", name, config_hash))
        self._flip(name, "deployed_config_hash", config_hash)

    def on_api_encryption_change(self, name: str, encryption: str) -> None:
        self.calls.append(("on_api_encryption_change", name, encryption))
        self._flip(name, "api_encryption_active", encryption)

    def on_mac_address_change(self, name: str, mac: str) -> None:
        self.calls.append(("on_mac_address_change", name, mac))
        self._flip(name, "mac_address", mac)

    def on_importable_added(self, device: AdoptableDevice) -> None:
        self.calls.append(("on_importable_added", device))

    def on_importable_removed(self, name: str) -> None:
        self.calls.append(("on_importable_removed", name))


def make_state_monitor_with_callbacks(
    devices: list[Device],
) -> tuple[DeviceStateMonitor, RecordingMonitorCallbacks]:
    """Build a ``DeviceStateMonitor`` + ``RecordingMonitorCallbacks`` pair.

    Centralises the per-test ``_monitor()`` helper that every
    ``test_mdns_*.py`` file was carrying. Returns the live monitor
    plus the callbacks recorder; tests assert on
    ``callbacks.calls``.
    """
    callbacks = RecordingMonitorCallbacks(devices)
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=callbacks.on_state_change,
        on_ip_change=callbacks.on_ip_change,
        on_importable_added=callbacks.on_importable_added,
        on_importable_removed=callbacks.on_importable_removed,
        on_version_change=callbacks.on_version_change,
        on_config_hash_change=callbacks.on_config_hash_change,
        on_api_encryption_change=callbacks.on_api_encryption_change,
        on_mac_address_change=callbacks.on_mac_address_change,
    )
    return monitor, callbacks


def make_device(name: str = "kitchen", **overrides: Any) -> Device:
    """Build a ``Device`` deriving friendly_name / configuration / address from *name*."""
    base: dict[str, Any] = {
        "name": name,
        "friendly_name": name.title(),
        "configuration": f"{name}.yaml",
        "address": f"{name}.local",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


def make_peer_link_session(
    *,
    dashboard_id: str = "alpha",
    with_terminate: bool = True,
) -> Any:
    """Stub ``PeerLinkSession`` with ``send_app_frame`` (and optionally terminate) as AsyncMock."""
    session = MagicMock()
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    if with_terminate:
        session.terminate = AsyncMock()
    return session


def close_scheduled_coro(coro: object) -> object:
    """Close a coroutine handed to ``create_background_task`` so it doesn't leak un-awaited."""
    if hasattr(coro, "close"):
        coro.close()
    return coro


def record_scheduled_coros(coros: list[object]) -> Callable[[object], object]:
    """Build a ``create_background_task`` side-effect that appends to *coros* and closes."""

    def _impl(coro: object) -> object:
        coros.append(coro)
        return close_scheduled_coro(coro)

    return _impl
