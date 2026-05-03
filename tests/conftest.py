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
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from unittest.mock import MagicMock

import pytest
from blockbuster import blockbuster_ctx
from esphome.core import CORE

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.models import Device, EventType

if TYPE_CHECKING:
    from blockbuster import BlockBuster

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
    if create_background_task is not None:
        controller._db.create_background_task = MagicMock(side_effect=create_background_task)
    controller._db.bus = bus
    controller._scanner = MagicMock()
    controller._scanner.devices = devices
    by_name: dict[str, list[Device]] = {}
    for device in devices:
        by_name.setdefault(device.name, []).append(device)
    controller._scanner.get_by_name = lambda name: by_name.get(name, [])
    return controller, captured
