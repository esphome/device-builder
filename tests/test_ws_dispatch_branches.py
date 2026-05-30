"""Coverage for the small dispatch / serialization branches in ``api/ws.py``.

The shared dispatcher (``WebSocketClient._handle_command``) and the
result/error/event serialisers each carry a handful of one-or-two-line
branches that can silently break if a future refactor reshapes them
— e.g. a result type without ``to_dict`` would no longer get coerced,
or a missing-handler branch would raise instead of returning a typed
error. The tests below pin one assertion per branch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers.auth import AuthError
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _make_client(*, authenticated: bool = True) -> tuple[WebSocketClient, AsyncMock]:
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    db = MagicMock()
    client = WebSocketClient(ws, db, authenticated=authenticated)
    return client, ws


def _last_payload(ws: AsyncMock) -> dict[str, Any]:
    """Return the dict passed to the most-recent ``send_json`` call."""
    assert ws.send_json.await_count >= 1
    return ws.send_json.await_args.args[0]


# ---------------------------------------------------------------------------
# Property accessors
# ---------------------------------------------------------------------------


def test_token_property_returns_value_set_by_authenticator() -> None:
    """``token`` reflects what ``set_authenticated`` recorded.

    Pin the read path so a refactor that drops ``_token`` (or adds
    a wrapper that mangles it) breaks here rather than silently
    desyncing the bearer-validation cache from the per-connection
    state.
    """
    client, _ = _make_client(authenticated=False)
    assert client.token is None
    client.set_authenticated("session-abc")
    assert client.token == "session-abc"
    assert client.authenticated is True


# ---------------------------------------------------------------------------
# Result serialisation
# ---------------------------------------------------------------------------


async def test_send_result_invokes_to_dict_on_dataclass_results() -> None:
    """``send_result`` flattens ``to_dict``-shaped results before sending.

    Most handler returns are mashumaro dataclasses; the wire layer
    serialises them via the dataclass's own ``to_dict`` so the
    JSON shape stays under model control instead of getting
    auto-derived from ``__dict__``.
    """
    client, ws = _make_client()

    class _Result:
        def to_dict(self) -> dict[str, str]:
            return {"flavoured": "yes"}

    await client.send_result("m1", _Result())

    payload = _last_payload(ws)
    assert payload["result"] == {"flavoured": "yes"}
    assert payload["message_id"] == "m1"


async def test_send_event_serialises_eventmessage() -> None:
    """``send_event`` packages the event into the ``EventMessage`` envelope."""
    client, ws = _make_client()

    await client.send_event("m1", "scan_progress", data={"completed": 3})

    payload = _last_payload(ws)
    assert payload["event"] == "scan_progress"
    assert payload["data"] == {"completed": 3}
    assert payload["message_id"] == "m1"


# ---------------------------------------------------------------------------
# _handle_command — error branches
# ---------------------------------------------------------------------------


async def test_handle_command_invalid_message_format() -> None:
    """A raw payload that fails ``CommandMessage.from_dict`` returns INVALID_MESSAGE.

    Pin the empty-string ``message_id`` — the client doesn't yet
    have a parsed id (parsing is what failed), so the error must
    use the unauthenticated/empty-id sentinel.
    """
    client, ws = _make_client()

    # Missing ``command`` field — ``CommandMessage.from_dict`` raises.
    await client._handle_command({"message_id": "m1"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.INVALID_MESSAGE.value
    assert payload["message_id"] == ""


async def test_handle_command_unknown_command_returns_typed_error() -> None:
    """Unrecognised commands surface as ``UNKNOWN_COMMAND``, not as a 500.

    The dispatcher has to look up handlers by string key — a typo'd
    command from the client must not be allowed to crash the
    handler loop. Pin the typed error so regressions show up here.
    """
    client, ws = _make_client()
    client.device_builder.command_handlers = {}

    await client._handle_command({"message_id": "m1", "command": "garbage/nonexistent"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.UNKNOWN_COMMAND.value
    assert "garbage/nonexistent" in payload["details"]


async def test_handle_command_pre_auth_blocks_non_auth_commands() -> None:
    """A non-``auth`` command before authentication is refused with NOT_AUTHENTICATED.

    The pre-auth guard is the one thing standing between an
    un-authenticated client and the rest of the API — pin it so a
    refactor that flipped the inversion (or skipped it for a
    "convenience" command) is caught immediately.
    """
    client, ws = _make_client(authenticated=False)
    # Having a handler registered makes sure the rejection happens
    # in the auth gate, not because of a missing handler.
    client.device_builder.command_handlers = {"devices/list": AsyncMock()}

    await client._handle_command({"message_id": "m1", "command": "devices/list"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.NOT_AUTHENTICATED.value
    client.device_builder.command_handlers["devices/list"].assert_not_called()


async def test_handle_command_passes_through_auth_error_code() -> None:
    """``AuthError`` from a handler keeps its own code/message verbatim.

    The auth controller raises typed errors (rate-limited login,
    expired session, etc.) that the frontend matches on by code —
    a generic ``INTERNAL_ERROR`` re-raise would lose that signal
    and force the client into its "unknown failure" UI.
    """
    client, ws = _make_client()
    client.device_builder.command_handlers = {
        "auth/login": AsyncMock(side_effect=AuthError(ErrorCode.RATE_LIMITED, "slow down")),
    }

    await client._handle_command({"message_id": "m1", "command": "auth/login"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.RATE_LIMITED.value
    assert payload["details"] == "slow down"


async def test_handle_command_passes_through_command_error_code() -> None:
    """``CommandError`` from a handler also keeps its own typed code."""
    client, ws = _make_client()
    client.device_builder.command_handlers = {
        "devices/rename": AsyncMock(
            side_effect=CommandError(ErrorCode.INVALID_ARGS, "bad name"),
        ),
    }

    await client._handle_command({"message_id": "m1", "command": "devices/rename"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.INVALID_ARGS.value
    assert payload["details"] == "bad name"


async def test_handle_command_unexpected_exception_becomes_internal_error() -> None:
    """A bare ``Exception`` from a handler is logged and surfaced as INTERNAL_ERROR.

    Pin the catch-all so a future ``raise`` from anywhere inside a
    handler can never crash the dispatcher — the client always
    gets *some* terminal frame on its message id.
    """
    client, ws = _make_client()
    client.device_builder.command_handlers = {
        "devices/list": AsyncMock(side_effect=RuntimeError("boom")),
    }

    await client._handle_command({"message_id": "m1", "command": "devices/list"})

    payload = _last_payload(ws)
    assert payload["error_code"] == ErrorCode.INTERNAL_ERROR.value
    assert "devices/list" in payload["details"]
