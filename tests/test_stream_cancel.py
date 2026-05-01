"""Tests for streaming-subprocess cancellation via WS ``devices/stop_stream``."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers.devices import DevicesController


class _FakeWS:
    """Minimal WebSocket stand-in capturing sent messages."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        import orjson

        self.sent.append(orjson.loads(payload))

    async def close(self) -> None:
        self.closed = True


def _make_client() -> WebSocketClient:
    return WebSocketClient(_FakeWS(), MagicMock(), authenticated=True)


def _make_controller() -> DevicesController:
    """Return a DevicesController shell that ``_stream_subprocess`` can use."""
    return DevicesController.__new__(DevicesController)


async def test_register_and_cancel_stream_runs_task_to_cancellation() -> None:
    """``cancel_stream`` cancels a registered task; uncancelled stays running."""
    client = _make_client()

    async def long_running() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(long_running())
    client.register_stream("abc", task)

    assert client.cancel_stream("abc") is True
    assert client.cancel_stream("abc") is False  # already cancelled / unregistered
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unregister_stream_is_idempotent() -> None:
    client = _make_client()
    client.register_stream("xyz", asyncio.create_task(asyncio.sleep(0)))
    client.unregister_stream("xyz")
    client.unregister_stream("xyz")  # no error


async def test_stream_subprocess_kills_proc_on_cancel(tmp_path: Any) -> None:
    """Cancellation of the streaming task terminates the subprocess.

    Uses a tiny Python child that writes one line and then sleeps; if our
    cancel path doesn't kill it, the test would hang on ``proc.wait()``.
    """
    ctrl = _make_controller()
    client = _make_client()
    sent_events: list[tuple[str, Any]] = []

    async def capture(_mid: str, event: str, data: Any = None) -> None:
        sent_events.append((event, data))

    # Patch the send_event to record events instead of going over the wire.
    client.send_event = capture  # type: ignore[method-assign]

    script = "import sys, time\nprint('hello', flush=True)\ntime.sleep(60)\n"
    cmd = [sys.executable, "-c", script]

    async def run_stream() -> None:
        await ctrl._stream_subprocess(cmd, client, "stream-1")

    task = asyncio.create_task(run_stream())
    # Wait for the first output line so we know the proc is up.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if any(ev == "output" for ev, _ in sent_events):
            break
    assert any(ev == "output" for ev, _ in sent_events), "child never produced output"

    # Cancel via the public API.
    assert client.cancel_stream("stream-1") is True

    # The task must complete promptly (proc was killed).
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    # No "result" event on cancel — the stream task ends in CANCELLED state
    # and the frontend learns it succeeded via ``devices/stop_stream``'s
    # normal response. We only sent "output" events.
    assert all(ev == "output" for ev, _ in sent_events)


async def test_stream_subprocess_normal_completion_emits_success(tmp_path: Any) -> None:
    """A subprocess that exits 0 sends a non-cancelled, success=True result."""
    ctrl = _make_controller()
    client = _make_client()
    sent_events: list[tuple[str, Any]] = []

    async def capture(_mid: str, event: str, data: Any = None) -> None:
        sent_events.append((event, data))

    client.send_event = capture  # type: ignore[method-assign]

    cmd = [sys.executable, "-c", "print('done')"]
    await ctrl._stream_subprocess(cmd, client, "stream-2")

    result_events = [data for ev, data in sent_events if ev == "result"]
    assert result_events == [{"code": 0, "success": True}]


async def test_cleanup_kills_running_streams() -> None:
    """A pending stream registered on a client is cancelled when the WS cleans up."""
    ctrl = _make_controller()
    client = _make_client()

    async def capture(_mid: str, _event: str, _data: Any = None) -> None: ...

    client.send_event = capture  # type: ignore[method-assign]

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    async def run_stream() -> None:
        await ctrl._stream_subprocess(cmd, client, "stream-3")

    task = client.create_task(run_stream())
    # Also register the stream like the real handler would (via current_task
    # inside _stream_subprocess). Give that a beat to take effect.
    for _ in range(20):
        if "stream-3" in client._stream_tasks:
            break
        await asyncio.sleep(0.05)
    assert "stream-3" in client._stream_tasks

    await client.cleanup()
    assert task.cancelled() or task.done()
