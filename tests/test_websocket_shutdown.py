"""Regression tests for the on-shutdown WebSocket closer.

A long-lived ``async for msg in ws`` handler doesn't naturally
finish when the run loop receives ``SIGTERM`` — it waits for the
client to send a CLOSE frame. aiohttp bounds that wait with
``shutdown_timeout`` (60s by default), which surfaced as 20-60s
SIGTERM-to-exit latency for the dashboard's desktop wrapper.

The fix wires :func:`esphome_device_builder.api.ws.close_active_websockets`
onto :meth:`web.Application.on_shutdown`. The handler iterates the
``WeakSet`` of active server-side ``WebSocketResponse`` instances
the WS handler maintains in ``app[WEBSOCKETS_KEY]`` and closes each
with a ``GOING_AWAY`` frame, which unblocks the per-connection
handler immediately.

These tests pin the wire-up so a future rebase can't silently
drop it and reintroduce the slow-shutdown symptom.
"""

from __future__ import annotations

import asyncio
import gc
import inspect
import weakref
from typing import Any

from aiohttp import WSCloseCode, web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.api.ws import (
    WEBSOCKETS_KEY,
    close_active_websockets,
    create_ws_routes,
    init_ws_app,
)
from esphome_device_builder.device_builder import DeviceBuilder

from .conftest import make_ws_device_builder


def _bare_app() -> web.Application:
    """Build a minimal aiohttp app wired with the WS routes + on_shutdown closer.

    Mirrors the production wire-up
    (:meth:`DeviceBuilder.create_app`) so the contract under test
    is what ships, not a hand-built shim that could drift.
    """
    device_builder = make_ws_device_builder()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True
    init_ws_app(app)
    app.router.add_routes(create_ws_routes())
    return app


async def test_active_ws_registered_on_app_state(
    aiohttp_client: AiohttpClient,
) -> None:
    """Open WS appears in ``app[WEBSOCKETS_KEY]`` while connected."""
    app = _bare_app()
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)  # let the ServerInfoMessage land
        active = app.get(WEBSOCKETS_KEY)
        assert active is not None
        assert isinstance(active, weakref.WeakSet)
        assert len(active) == 1
        await ws.close()


async def test_close_active_websockets_closes_open_connections(
    aiohttp_client: AiohttpClient,
) -> None:
    """``close_active_websockets`` actually unblocks the WS handler.

    Open a WS, kick the closer manually, verify (a) the client
    sees a CLOSE message with ``GOING_AWAY`` and (b) the
    server-side WS leaves the app's active set promptly — the
    weak-set entry is reclaimed only after the handler frame
    exits, so an empty set proves the per-connection task
    unwound. Pre-fix this took up to 60s; the 2s bound is
    generous against the ~0.3s the explicit close actually
    needs.
    """
    app = _bare_app()
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)  # ServerInfoMessage

        # Kick the closer the same way ``app.shutdown()`` would.
        await close_active_websockets(app)

        # Client should observe the CLOSE frame, not a timeout.
        msg = await ws.receive(timeout=2.0)
        assert msg.type == web.WSMsgType.CLOSE
        assert msg.data == WSCloseCode.GOING_AWAY

    # Once the client's context manager has unwound, the handler
    # frame should be gone and the WeakSet entry reclaimable. Yield
    # control + force a GC pass so the assertion isn't racing the
    # event loop's per-task post-cleanup hook (each WS handler runs
    # as its own asyncio task; the frame and its locals only
    # release when the task object itself is GC'd). 2s upper bound:
    # without the explicit close the handler would still be alive
    # at this point, well past any aiohttp-internal latency.
    deadline = asyncio.get_event_loop().time() + 2.0
    active = app[WEBSOCKETS_KEY]
    while len(active) and asyncio.get_event_loop().time() < deadline:
        gc.collect()
        await asyncio.sleep(0.01)
    assert len(active) == 0, "WS handler frame still alive after close"


async def test_app_shutdown_runs_the_closer(
    aiohttp_client: AiohttpClient,
) -> None:
    """``app.shutdown()`` triggers the closer via the ``on_shutdown`` chain.

    Verifies the registration site, not just the handler in
    isolation: a future refactor that loses the
    ``app.on_shutdown.append`` call would skip the WS closer
    entirely and re-introduce the slow-shutdown symptom even with
    the helper still present.
    """
    app = _bare_app()
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)

        await asyncio.wait_for(app.shutdown(), timeout=2.0)

        msg = await ws.receive(timeout=2.0)
        assert msg.type == web.WSMsgType.CLOSE


async def test_close_active_websockets_is_safe_on_empty_app() -> None:
    """No registered set / no clients => no-op, no exception.

    The closer fires on every shutdown including the boot-and-die
    path (e.g. ``create_app`` runs but ``run_app`` exits before any
    client connects, so ``WEBSOCKETS_KEY`` was never populated).
    """
    app = web.Application()
    await close_active_websockets(app)  # no clients, no key set yet

    app[WEBSOCKETS_KEY] = weakref.WeakSet()
    await close_active_websockets(app)  # key present but empty


async def test_close_active_websockets_tolerates_per_socket_errors() -> None:
    """A close that raises on one WS doesn't stop the rest.

    Each close runs through ``asyncio.gather(..., return_exceptions=True)``
    so an already-dropped client (broken pipe on the CLOSE write)
    can't pin the dashboard's shutdown on a single bad peer.
    """

    class _RaisingWS:
        closed = False

        async def close(self, *_: Any, **__: Any) -> None:
            raise OSError("simulated peer hard-drop")

    class _OkWS:
        closed = False
        called = False

        async def close(self, *_: Any, **__: Any) -> None:
            self.called = True

    raising = _RaisingWS()
    ok = _OkWS()

    app = web.Application()
    app[WEBSOCKETS_KEY] = weakref.WeakSet([raising, ok])  # type: ignore[arg-type]

    await close_active_websockets(app)

    assert ok.called, "ok WS still gets its close called even when peer raised"


async def test_close_uses_going_away_code(
    aiohttp_client: AiohttpClient,
) -> None:
    """Close frame carries 1001 (``GOING_AWAY``), not a generic 1000.

    Browser-side reconnect logic typically reacts only to
    ``GOING_AWAY`` with an automatic retry; a generic 1000 ("normal
    closure") can be interpreted as "the server is done, don't
    reconnect" and would strand the dashboard's frontend without
    a connection after a desktop-driven restart.
    """
    app = _bare_app()
    client = await aiohttp_client(app)
    async with client.ws_connect("/ws") as ws:
        await ws.receive(timeout=2.0)
        await close_active_websockets(app)
        msg = await ws.receive(timeout=2.0)
        assert msg.type == web.WSMsgType.CLOSE
        assert msg.data == WSCloseCode.GOING_AWAY


def test_close_handler_module_export() -> None:
    """``close_active_websockets`` is importable from ``ws_module``.

    The wire-up in ``DeviceBuilder.create_app`` imports the helper
    by name; a rename here without updating the importer would
    fail the dashboard's startup with an ``ImportError`` rather
    than silently regressing the SIGTERM latency, but it's still
    worth pinning the export shape so module-level refactors
    don't slip past review.
    """
    assert callable(ws_module.close_active_websockets)
    assert hasattr(ws_module, "WEBSOCKETS_KEY")


def test_init_ws_app_called_in_create_app() -> None:
    """The dashboard's ``create_app`` actually wires the WS init helper.

    Source-grep guard. If a future rebase loses the
    ``init_ws_app(app)`` call, SIGTERM latency would silently
    regress to the 20-60s symptom we fixed plus the WeakSet
    seed would be missing (every WS connection would crash on
    ``KeyError: '_active_websockets'``). Grep the source so the
    test fails the moment the wire-up drops off.
    """
    source = inspect.getsource(DeviceBuilder.create_app)
    assert "init_ws_app(app)" in source


def test_init_ws_app_is_idempotent() -> None:
    """A second call against the same app keeps the existing set and listener.

    If the second call overwrote ``app[WEBSOCKETS_KEY]``, any WS
    registered against the first call's WeakSet would no longer
    be reachable from ``close_active_websockets`` on shutdown,
    silently reintroducing the slow-shutdown symptom. If it
    re-appended ``close_active_websockets`` to ``on_shutdown``,
    the closer would fire twice. Pin both invariants so a
    regression can't slip past review.
    """
    app = web.Application()
    init_ws_app(app)
    first_weakset = app[WEBSOCKETS_KEY]
    first_listener_count = sum(
        1 for listener in app.on_shutdown if listener is close_active_websockets
    )

    init_ws_app(app)

    assert app[WEBSOCKETS_KEY] is first_weakset
    second_listener_count = sum(
        1 for listener in app.on_shutdown if listener is close_active_websockets
    )
    assert second_listener_count == first_listener_count == 1
