"""Dispatch concurrency contract for ``api/ws.py`` (GHSA-mg7m-j658-c6r9).

Pins the post-fix shape of the WS command dispatcher:

* Ordinary commands run **inline and sequentially** — the message
  loop awaits each ``_handle_command`` before reading the next frame,
  so two mutating commands on one connection can't interleave, and an
  ordinary command spawns **no** background task (bounded fan-out).
* Streaming commands (``subscribe_events``/``follow_job(s)``/log
  follows) are spawned as tracked tasks so a parked subscription
  doesn't wedge later commands on the same connection.
* The per-connection streaming task set is **bounded**: once
  ``_MAX_CONCURRENT_STREAMS`` are in flight, a further stream-open
  frame is rejected with ``RATE_LIMITED`` instead of fanning out.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.api.ws import _MAX_CONCURRENT_STREAMS, WebSocketClient
from esphome_device_builder.models import ErrorCode


def _make_client() -> tuple[WebSocketClient, AsyncMock]:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    db = MagicMock()
    client = WebSocketClient(ws, db, authenticated=True)
    client.device_builder.streaming_commands = set()
    return client, ws


def _last_payload(ws: AsyncMock) -> dict[str, Any]:
    assert ws.send_json.await_count >= 1
    return ws.send_json.await_args.args[0]


# ---------------------------------------------------------------------------
# Boundedness: ordinary commands don't spawn tasks; streaming ones do (capped)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_command_spawns_no_task() -> None:
    """An ordinary command is awaited inline, leaving the task set empty.

    Pre-fix every frame did ``create_task`` — the per-connection task
    set grew without bound. Inline execution is what bounds it.
    """
    client, _ = _make_client()
    handler = AsyncMock(return_value=None)
    client.device_builder.command_handlers = {"devices/list": handler}

    await client._handle_command({"message_id": "m1", "command": "devices/list"})

    handler.assert_awaited_once()
    assert len(client._tasks) == 0


@pytest.mark.asyncio
async def test_streaming_command_is_spawned_not_awaited_inline() -> None:
    """A streaming command returns control immediately and tracks one task.

    The handler parks forever; if ``_handle_command`` awaited it
    inline this call would never return. Instead it spawns a tracked
    task and returns, so subsequent frames on the connection can run.
    """
    client, _ = _make_client()
    started = asyncio.Event()

    async def parker(*, client: Any, message_id: str, **_kw: Any) -> None:
        started.set()
        await asyncio.Event().wait()  # never completes

    client.device_builder.command_handlers = {"subscribe_events": parker}
    client.device_builder.streaming_commands = {"subscribe_events"}

    await client._handle_command({"message_id": "m1", "command": "subscribe_events"})

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(client._tasks) == 1

    await client.cleanup()


@pytest.mark.asyncio
async def test_streaming_does_not_block_a_following_ordinary_command() -> None:
    """A parked stream doesn't stop a later ordinary command from completing."""
    client, ws = _make_client()

    async def parker(*, client: Any, message_id: str, **_kw: Any) -> None:
        await asyncio.Event().wait()

    ordinary = AsyncMock(return_value={"ok": True})
    client.device_builder.command_handlers = {
        "subscribe_events": parker,
        "ping": ordinary,
    }
    client.device_builder.streaming_commands = {"subscribe_events"}

    await client._handle_command({"message_id": "s1", "command": "subscribe_events"})
    await client._handle_command({"message_id": "p1", "command": "ping"})

    ordinary.assert_awaited_once()
    payload = _last_payload(ws)
    assert payload["message_id"] == "p1"
    assert payload["result"] == {"ok": True}

    await client.cleanup()


@pytest.mark.asyncio
async def test_streaming_cap_rejects_with_rate_limited() -> None:
    """Once the stream cap is saturated, further stream opens are refused.

    Fill the task set to ``_MAX_CONCURRENT_STREAMS`` with parked
    placeholder tasks, then a new streaming frame must get a
    ``RATE_LIMITED`` error and the real handler must not run.
    """
    client, ws = _make_client()

    async def _park() -> None:
        await asyncio.Event().wait()

    for _ in range(_MAX_CONCURRENT_STREAMS):
        client.create_task(_park())
    assert len(client._tasks) == _MAX_CONCURRENT_STREAMS

    handler = AsyncMock(return_value=None)
    client.device_builder.command_handlers = {"subscribe_events": handler}
    client.device_builder.streaming_commands = {"subscribe_events"}

    await client._handle_command({"message_id": "over", "command": "subscribe_events"})

    handler.assert_not_called()
    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.RATE_LIMITED.value
    assert payload["message_id"] == "over"

    await client.cleanup()


# ---------------------------------------------------------------------------
# Ordering: drive the real message loop end-to-end through aiohttp
# ---------------------------------------------------------------------------


async def test_same_connection_commands_run_in_submission_order(
    aiohttp_client: AiohttpClient,
) -> None:
    """Two ordinary commands on one connection execute without interleaving.

    Each handler records enter/exit around an ``await`` that yields
    control. Pre-fix (``create_task`` per frame) the trace would
    interleave (enter-1, enter-2, exit-1, exit-2). Inline sequential
    dispatch keeps it strictly nested (enter-1, exit-1, enter-2,
    exit-2), so dependent same-connection mutations can't race.
    """
    trace: list[str] = []

    async def recording(*, client: Any, message_id: str, **_kw: Any) -> dict[str, str]:
        trace.append(f"enter-{message_id}")
        await asyncio.sleep(0)  # hand control back to the loop
        await asyncio.sleep(0)
        trace.append(f"exit-{message_id}")
        return {"id": message_id}

    settings = MagicMock()
    settings.using_password = False
    settings.port = 6052
    settings.on_ha_addon = False
    settings.trusted_domains = []

    device_builder = MagicMock()
    device_builder.settings = settings
    device_builder.command_handlers = {"work": recording}
    device_builder.streaming_commands = set()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # skip auth/origin gates
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)
    ws = await client.ws_connect("/ws")
    await ws.receive(timeout=2.0)  # drain ServerInfoMessage

    await ws.send_str('{"message_id": "1", "command": "work"}')
    await ws.send_str('{"message_id": "2", "command": "work"}')

    # Collect both result frames (ServerInfo already drained above).
    seen = set()
    while len(seen) < 2:
        msg = await ws.receive(timeout=2.0)
        payload = msg.json()
        if "result" in payload:
            seen.add(payload["message_id"])
    await ws.close()
    assert seen == {"1", "2"}

    assert trace == ["enter-1", "exit-1", "enter-2", "exit-2"]
