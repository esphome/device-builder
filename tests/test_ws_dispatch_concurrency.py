"""Dispatch concurrency contract for ``api/ws.py`` (GHSA-mg7m-j658-c6r9).

Pins the post-fix shape of the WS command dispatcher:

* Every command handler runs as a **tracked, per-connection task**
  (the Home Assistant ``websocket_api`` pattern), so a slow handler
  doesn't head-of-line-block faster commands on the same connection,
  and the task set is cancelled on disconnect.
* Ordinary command fan-out is **bounded** by
  ``_MAX_CONCURRENT_COMMANDS`` and streaming fan-out by the tighter
  ``_MAX_CONCURRENT_STREAMS``; a frame past either cap is rejected
  with ``RATE_LIMITED`` instead of fanning out unbounded tasks.
* Streaming commands are tracked separately so a parked subscription
  doesn't consume the ordinary-command budget.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import ws as ws_module
from esphome_device_builder.api.ws import (
    _MAX_CONCURRENT_COMMANDS,
    _MAX_CONCURRENT_STREAMS,
    WebSocketClient,
)
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
# Tracked tasks: ordinary commands are spawned, not awaited inline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_command_is_spawned_as_tracked_task() -> None:
    """An ordinary command is dispatched as a tracked command task.

    Pre-fix every frame was awaited inline; a slow handler blocked
    the dispatch loop. Spawning a tracked task is what unblocks it.
    """
    client, _ = _make_client()
    handler = AsyncMock(return_value=None)
    client.device_builder.command_handlers = {"devices/list": handler}

    await client._handle_command({"message_id": "m1", "command": "devices/list"})

    assert len(client._command_tasks) == 1
    await asyncio.gather(*client._tasks)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_command_does_not_block_a_following_command() -> None:
    """A parked command doesn't stop a later command from completing."""
    client, ws = _make_client()

    async def parker(*, client: Any, message_id: str, **_kw: Any) -> None:
        await asyncio.Event().wait()

    ordinary = AsyncMock(return_value={"ok": True})
    client.device_builder.command_handlers = {"slow": parker, "fast": ordinary}

    await client._handle_command({"message_id": "s1", "command": "slow"})
    await client._handle_command({"message_id": "f1", "command": "fast"})

    # Hand control to the loop so the spawned fast task runs.
    for _ in range(4):
        await asyncio.sleep(0)

    ordinary.assert_awaited_once()
    payload = _last_payload(ws)
    assert payload["message_id"] == "f1"
    assert payload["result"] == {"ok": True}

    await client.cleanup()


@pytest.mark.asyncio
async def test_spawned_tasks_cancelled_on_disconnect() -> None:
    """``cleanup`` cancels the in-flight command tasks on disconnect."""
    client, _ = _make_client()

    async def parker(*, client: Any, message_id: str, **_kw: Any) -> None:
        await asyncio.Event().wait()

    client.device_builder.command_handlers = {"work": parker}

    await client._handle_command({"message_id": "m1", "command": "work"})
    assert len(client._command_tasks) == 1
    task = next(iter(client._command_tasks))

    await client.cleanup()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_command_cap_rejects_with_rate_limited() -> None:
    """Once the ordinary-command cap is saturated, further frames are refused."""
    client, ws = _make_client()

    async def _park() -> None:
        await asyncio.Event().wait()

    for _ in range(_MAX_CONCURRENT_COMMANDS):
        client.create_command_task(_park())
    assert len(client._command_tasks) == _MAX_CONCURRENT_COMMANDS

    handler = AsyncMock(return_value=None)
    client.device_builder.command_handlers = {"devices/list": handler}

    await client._handle_command({"message_id": "over", "command": "devices/list"})

    handler.assert_not_called()
    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.RATE_LIMITED.value
    assert payload["message_id"] == "over"

    await client.cleanup()


# ---------------------------------------------------------------------------
# Streaming commands: separate set, tighter cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_command_tracked_in_streaming_set() -> None:
    """A streaming command spawns a task counted in the streaming set."""
    client, _ = _make_client()
    started = asyncio.Event()

    async def parker(*, client: Any, message_id: str, **_kw: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    client.device_builder.command_handlers = {"subscribe_events": parker}
    client.device_builder.streaming_commands = {"subscribe_events"}

    await client._handle_command({"message_id": "m1", "command": "subscribe_events"})

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert len(client._streaming_tasks) == 1
    assert len(client._command_tasks) == 0

    await client.cleanup()


@pytest.mark.asyncio
async def test_streaming_cap_rejects_with_rate_limited() -> None:
    """Once the stream cap is saturated, further stream opens are refused."""
    client, ws = _make_client()

    async def _park() -> None:
        await asyncio.Event().wait()

    for _ in range(_MAX_CONCURRENT_STREAMS):
        client.create_streaming_task(_park())
    assert len(client._streaming_tasks) == _MAX_CONCURRENT_STREAMS

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
# End-to-end: a slow command doesn't wedge the real message loop
# ---------------------------------------------------------------------------


async def test_slow_command_does_not_wedge_the_message_loop(
    aiohttp_client: AiohttpClient,
) -> None:
    """A slow command completes only after a later command runs.

    ``slow`` parks on a shared event that ``fast`` sets. Both result
    frames arriving proves the dispatch loop kept reading frames and
    ran ``fast`` while ``slow`` was still parked — pre-fix inline
    serialization would have deadlocked (``slow`` never releasing).
    """
    release = asyncio.Event()

    async def slow(*, client: Any, message_id: str, **_kw: Any) -> dict[str, str]:
        await release.wait()
        return {"id": message_id}

    async def fast(*, client: Any, message_id: str, **_kw: Any) -> dict[str, str]:
        release.set()
        return {"id": message_id}

    settings = MagicMock()
    settings.using_password = False
    settings.port = 6052
    settings.on_ha_addon = False
    settings.trusted_domains = []

    device_builder = MagicMock()
    device_builder.settings = settings
    device_builder.command_handlers = {"slow": slow, "fast": fast}
    device_builder.streaming_commands = set()

    app = web.Application()
    app["device_builder"] = device_builder
    app["trusted_site"] = True  # skip auth/origin gates
    ws_module.init_ws_app(app)
    app.router.add_routes(ws_module.create_ws_routes())

    client = await aiohttp_client(app)
    ws = await client.ws_connect("/ws")
    await ws.receive(timeout=2.0)  # drain ServerInfoMessage

    await ws.send_str('{"message_id": "1", "command": "slow"}')
    await ws.send_str('{"message_id": "2", "command": "fast"}')

    seen = set()
    while len(seen) < 2:
        msg = await ws.receive(timeout=2.0)
        payload = msg.json()
        if "result" in payload:
            seen.add(payload["message_id"])
    await ws.close()
    assert seen == {"1", "2"}
