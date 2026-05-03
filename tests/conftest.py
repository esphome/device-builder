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
from typing import TYPE_CHECKING, Any

import pytest
from blockbuster import blockbuster_ctx

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog

if TYPE_CHECKING:
    from collections.abc import Iterator

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

    Tests that mutate the catalog must restore it before yielding back —
    the instance is shared across every test in the session.
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
        """Return the ``data`` payloads for every captured ``send_event(name, …)``.

        Most assertions only care about the data attached to a
        specific event ("show me every ``output`` line"); collapsing
        the (message_id, event, data) tuple unpack into one helper
        keeps the test bodies readable.
        """
        return [data for (_mid, event, data) in self.events if event == name]
