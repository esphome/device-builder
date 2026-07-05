"""Regression test for the WebSocket server-side heartbeat.

aiohttp's ``WebSocketResponse`` defaults to ``heartbeat=None`` —
no server-side ping. Idle clients behind NAT / Cloudflare / nginx
(default ``proxy_read_timeout=60s``) then silently drop and the
dashboard sits showing stale data until the user reloads. The
legacy Tornado dashboard set ``websocket_ping_interval=30.0``;
we mirror that.

This test asserts the constructor wiring rather than driving a
real ping/pong exchange (which would require the test to sleep
through the heartbeat interval). The construction site is the
single source of truth — if the kwarg drops off it, the symptom
returns regardless of what aiohttp does at runtime.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module

from .conftest import make_ws_device_builder


def test_heartbeat_constant_matches_legacy_tornado_interval() -> None:
    """Heartbeat is 30s.

    Matches the legacy ``websocket_ping_interval`` and the typical
    60s reverse-proxy idle timeout, halved so a single missed pong
    still completes inside the proxy's window.
    """
    assert ws_module._WS_HEARTBEAT_SECONDS == 30.0


async def test_websocket_response_constructed_with_heartbeat(
    aiohttp_client: AiohttpClient,
) -> None:
    """``websocket_handler`` builds ``WebSocketResponse`` with the heartbeat.

    Captures the constructor kwargs via a patched class so we
    don't depend on aiohttp's internal attribute name (``_heartbeat``
    vs ``_pong_heartbeat`` etc.) — those have changed between
    aiohttp 3.x releases. The test fails the moment the kwarg
    drops off the construction site, which is the symptom we're
    guarding against.
    """
    captured: dict[str, Any] = {}
    real_cls = web.WebSocketResponse

    class _CapturingWS(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)
            super().__init__(*args, **kwargs)

    device_builder = make_ws_device_builder()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # skip the in-band auth handshake
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    with patch.object(ws_module.web, "WebSocketResponse", _CapturingWS):
        client = await aiohttp_client(app)
        async with client.ws_connect("/ws") as ws:
            # Receive the server's initial ServerInfoMessage so the
            # handshake completes before we close the connection.
            msg = await ws.receive(timeout=2.0)
            assert msg.type.name in {"TEXT", "BINARY"}
            await ws.close()

    assert captured.get("heartbeat") == ws_module._WS_HEARTBEAT_SECONDS, (
        f"Expected heartbeat={ws_module._WS_HEARTBEAT_SECONDS}, got {captured.get('heartbeat')!r}"
    )


@pytest.mark.parametrize("heartbeat_kwarg", ["heartbeat"])
def test_construction_site_uses_named_constant(heartbeat_kwarg: str) -> None:
    """Belt-and-braces: the construction site references the constant by name.

    aiohttp 3.9+ makes ``WebSocketResponse.__init__`` keyword-only,
    so a positional refactor would raise ``TypeError`` at runtime —
    the runtime test above catches that. The risk this guards
    against is the kwarg being *removed* (or renamed by a careless
    rebase) and the named constant being inlined as a magic
    number. Reading the source verifies both stay tied together so
    a future grep for ``_WS_HEARTBEAT_SECONDS`` finds the actual
    use site, and the rationale comment above the constant doesn't
    drift from a hard-coded value somewhere else.
    """
    source = inspect.getsource(ws_module.websocket_handler)
    assert f"{heartbeat_kwarg}=_WS_HEARTBEAT_SECONDS" in source
