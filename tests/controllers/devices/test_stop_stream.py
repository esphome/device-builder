"""End-to-end coverage for ``DevicesController.stop_stream``.

The handler is the cancel-side of the streaming-command pair
(``devices/logs`` / ``devices/validate``). It looks up the
streaming task on the issuing connection's ``WebSocketClient``
and calls ``cancel_stream`` — which cancels the task and removes
it from the per-connection registry.

Four branches to pin:

1. No ``client`` context (the dispatch layer didn't thread one
   through, e.g. legacy REST entry point) → ``{"cancelled": False}``
   without raising.
2. Valid ``stream_id`` on the client → ``cancel_stream`` returns
   ``True``, the response carries it, and the underlying task is
   actually cancelled.
3. Unknown ``stream_id`` (already finished, never registered) →
   ``{"cancelled": False}``.
4. Double-cancel: a second call for the same id reports ``False``
   because ``cancel_stream`` pops the entry on the first call.
   Pin the idempotent contract so a frontend retry doesn't
   misreport success twice.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.api.ws import WebSocketClient

from .conftest import MakeControllerFactory


def _make_ws_client() -> WebSocketClient:
    """Build a real ``WebSocketClient`` over a no-op stub WS.

    The stop-stream contract exercises ``cancel_stream`` /
    ``register_stream`` / ``unregister_stream`` — production
    methods on the real class. Standing up a stub that
    re-implements them would let a refactor of the registry
    drift undetected; using the real ``WebSocketClient`` (with a
    bare ``MagicMock`` WS that the registry never actually
    touches) keeps the test wired to the production behaviour.
    """
    return WebSocketClient(MagicMock(), MagicMock(), authenticated=True)


async def test_stop_stream_returns_false_without_client(
    tmp_path: Any, make_controller: MakeControllerFactory
) -> None:
    """No ``client`` argument → ``{"cancelled": False}``, no raise.

    Dispatch flows that don't carry a per-connection client (the
    legacy REST shim, future programmatic callers) should still
    get a typed response rather than an ``AttributeError`` from
    ``client.cancel_stream``. Pin the early-return guard.
    """
    controller = make_controller(tmp_path)

    result = await controller.stop_stream(stream_id="anything")

    assert result == {"cancelled": False}


async def test_stop_stream_cancels_registered_task(
    tmp_path: Any, make_controller: MakeControllerFactory
) -> None:
    """A registered streaming task is cancelled and the response says so.

    Drives ``register_stream`` first (production calls this from
    inside ``stream_logs`` / ``validate_config``), then asks the
    handler to cancel by ``stream_id``. Asserts both the response
    shape and that the task actually observed the cancel — a
    refactor that returned ``True`` without invoking
    ``task.cancel()`` would silently leak the stream until the
    connection drops.
    """
    controller = make_controller(tmp_path)
    ws_client = _make_ws_client()

    started = asyncio.Event()

    async def _streaming() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_streaming())
    # Yield once so the streaming task starts and parks on
    # ``asyncio.sleep`` — without this the cancel could land
    # before the task ever reached an ``await`` point.
    await started.wait()
    ws_client.register_stream("stream-1", task)

    result = await controller.stop_stream(stream_id="stream-1", client=ws_client)

    assert result == {"cancelled": True}
    # Task observed the cancel.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


async def test_stop_stream_returns_false_for_unknown_stream(
    tmp_path: Any, make_controller: MakeControllerFactory
) -> None:
    """An unknown ``stream_id`` → ``{"cancelled": False}``.

    ``cancel_stream`` returns ``False`` when nothing matches —
    "already finished", "never registered", and "double-cancel"
    all collapse to the same outcome. Pin the response shape so
    the frontend's "stop logs" button doesn't silently misreport
    success.
    """
    controller = make_controller(tmp_path)
    ws_client = _make_ws_client()

    result = await controller.stop_stream(stream_id="nonexistent", client=ws_client)

    assert result == {"cancelled": False}


async def test_stop_stream_returns_false_after_double_cancel(
    tmp_path: Any, make_controller: MakeControllerFactory
) -> None:
    """A second cancel for the same ``stream_id`` reports ``False``.

    ``cancel_stream`` pops the entry from the registry on the
    first call, so the second lookup misses. Pin the
    idempotent-cancel contract: a frontend retry doesn't
    incorrectly show "cancelled" twice.
    """
    controller = make_controller(tmp_path)
    ws_client = _make_ws_client()

    async def _idle() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(_idle())
    ws_client.register_stream("stream-1", task)

    first = await controller.stop_stream(stream_id="stream-1", client=ws_client)
    second = await controller.stop_stream(stream_id="stream-1", client=ws_client)

    assert first == {"cancelled": True}
    assert second == {"cancelled": False}

    # Drain the cancelled task to keep pytest's leak check happy.
    with pytest.raises(asyncio.CancelledError):
        await task
