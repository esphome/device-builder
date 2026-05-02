"""Tests for ``WebSocketClient._handle_command`` StreamBackpressureError path.

Pin two contracts the recovery path depends on:

1. The error frame is sent to the client (so the frontend knows
   why it was disconnected).
2. ``ws.close()`` actually fires after the error is written —
   ``schedule_close`` must be called *before* ``send_error`` so
   ``send``'s post-write check sees ``_close_after_send=True``.
   Setting the flag *after* the write would leave the connection
   open with the handler task gone — the frontend would stop
   receiving events but never get the forced reconnect.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.helpers.event_bus import StreamBackpressureError
from esphome_device_builder.models import ErrorCode


def _make_client() -> tuple[WebSocketClient, AsyncMock]:
    """Build a WebSocketClient with a recording fake aiohttp WS."""
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    db = MagicMock()
    # Authenticated so the handler dispatch path runs.
    client = WebSocketClient(ws, db, authenticated=True)
    return client, ws


async def test_stream_backpressure_closes_ws_after_sending_error() -> None:
    """The error frame lands AND the WS is closed afterwards.

    Asserts the call order on the fake aiohttp WS:
    ``send_json`` (the error frame) precedes ``close()``. The
    handler raises ``StreamBackpressureError``; the dispatcher
    must:

    - Tell the client why (``send_error`` with INTERNAL_ERROR
      and the exception message).
    - Schedule the close *before* the send so ``send``'s
      post-write check actually fires the close. The earlier
      shape called ``schedule_close()`` after ``send_error()``,
      so the flag was set after the only message of this
      branch had already been written and the close never
      fired.
    """
    client, ws = _make_client()

    backpressure_msg = "stream backpressure exceeded — closing"

    async def handler(*, client: Any, message_id: str, **_kwargs: Any) -> None:
        raise StreamBackpressureError(backpressure_msg)

    client.device_builder.command_handlers = {"subscribe_events": handler}

    raw = {"message_id": "m1", "command": "subscribe_events"}
    await client._handle_command(raw)

    # Both calls happened.
    assert ws.send_json.await_count == 1
    assert ws.close.await_count == 1

    # Strict call order: send_json (error frame) before close.
    send_call_index = next(i for i, c in enumerate(ws.method_calls) if c[0] == "send_json")
    close_call_index = next(i for i, c in enumerate(ws.method_calls) if c[0] == "close")
    assert send_call_index < close_call_index, (
        "schedule_close must run before send_error so the close fires as part of the error send"
    )

    # The error frame carries INTERNAL_ERROR + the backpressure message.
    sent_payload = ws.send_json.await_args.args[0]
    assert sent_payload["error_code"] == ErrorCode.INTERNAL_ERROR.value
    assert backpressure_msg in sent_payload["details"]
    assert sent_payload["message_id"] == "m1"


async def test_other_handler_exceptions_do_not_close_ws() -> None:
    """Generic exceptions log and send INTERNAL_ERROR but keep the connection.

    The backpressure branch is the only error path that closes
    the WS. A regression that broadened the close-on-error
    behaviour to every exception (or removed the schedule_close
    from the backpressure branch) would surface here.
    """
    client, ws = _make_client()

    async def handler(*, client: Any, message_id: str, **_kwargs: Any) -> None:
        msg = "unexpected"
        raise RuntimeError(msg)

    client.device_builder.command_handlers = {"misc": handler}

    raw = {"message_id": "m2", "command": "misc"}
    await client._handle_command(raw)

    assert ws.send_json.await_count == 1
    assert ws.close.await_count == 0


async def test_stream_backpressure_send_failure_still_closes() -> None:
    """Even if the error frame fails to send, the WS still closes.

    ``send`` already wraps ``send_json`` in
    ``contextlib.suppress(ConnectionResetError)`` — the close is
    in the same coroutine, so a peer that's already gone away
    doesn't prevent us from explicitly closing. This test pins
    that contract.
    """
    client, ws = _make_client()
    ws.send_json.side_effect = ConnectionResetError("client gone")

    async def handler(*, client: Any, message_id: str, **_kwargs: Any) -> None:
        msg = "drop the connection"
        raise StreamBackpressureError(msg)

    client.device_builder.command_handlers = {"subscribe_events": handler}

    raw = {"message_id": "m3", "command": "subscribe_events"}
    # Should not raise — ConnectionResetError is suppressed by send().
    await client._handle_command(raw)

    # send_json was attempted; close still fired despite the
    # connection-reset on the send.
    assert ws.send_json.await_count == 1
    assert ws.close.await_count == 1
