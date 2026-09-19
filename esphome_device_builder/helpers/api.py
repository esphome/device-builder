"""API command registration helpers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from ..models import ErrorCode, StreamEvent

# Type alias for command handler functions. ``CommandHandler`` is the
# erased shape used by the registry side (``collect_api_commands``);
# the decorator preserves the actual handler's signature via the
# ``_F`` TypeVar so call-sites keep their precise return types
# instead of widening to ``Any``.
CommandHandler = Callable[..., Coroutine[Any, Any, Any]]
_F = TypeVar("_F", bound=CommandHandler)


class CommandError(Exception):
    """A user-facing error raised by an ``api_command`` handler.

    The WS dispatcher catches these and forwards the carried ``code``
    + ``message`` verbatim to the client, instead of swallowing them
    as a generic ``INTERNAL_ERROR``. Use this when the failure has a
    specific reason the user can act on (file already exists, name
    invalid, etc.) — not for crashes / bugs.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CollectingClient:
    """Stream client keeping the last *tail* output lines (all if ``None``) and the result."""

    def __init__(self, tail: int | None = None) -> None:
        self.output: deque[str] = deque(maxlen=tail)
        self.truncated = False
        self.result: dict[str, Any] | None = None

    async def send_event(self, _message_id: str, event: str, data: Any = None) -> None:
        if event == StreamEvent.OUTPUT:
            self.truncated = self.truncated or len(self.output) == self.output.maxlen
            self.output.append(data)
        elif event == StreamEvent.RESULT:
            self.result = data

    def register_stream(self, message_id: str, task: Any) -> None: ...

    def unregister_stream(self, message_id: str) -> None: ...


@contextmanager
def registered_stream(client: Any, message_id: str) -> Iterator[None]:
    """
    Register the running task as a cancellable stream for the block's lifetime.

    Captures ``asyncio.current_task()`` so a ``devices/stop_stream`` for
    ``message_id`` can cancel it, and unregisters on exit. Enter before the
    first ``await`` so an early stop_stream still finds the task.
    """
    task = asyncio.current_task()
    assert task is not None
    client.register_stream(message_id, task)
    try:
        yield
    finally:
        client.unregister_stream(message_id)


def api_command(command: str) -> Callable[[_F], _F]:
    """Decorate a controller method to register it as a WebSocket API command.

    Usage:
        @api_command("boards/get_boards")
        async def get_boards(self, *, query=None, limit=50, ...) -> PagedBoardsResponse:
            ...

    The decorated method is discoverable via `_api_command` attribute.
    DeviceBuilder scans controllers for these and builds its command registry.

    Returns the function unchanged at runtime — only the
    ``_api_command`` attribute is set. The ``_F`` ``TypeVar``
    bound to ``CommandHandler`` carries the precise function
    signature through the decorator so call-sites keep their
    actual return type (e.g. ``OnboardingState``,
    ``PagedBoardsResponse``) instead of widening to
    ``Coroutine[Any, Any, Any]`` like a plain
    ``Callable[[CommandHandler], CommandHandler]`` shape would.
    """

    def decorator(func: _F) -> _F:
        # Framework metadata stamped on the decorated function; the
        # leading underscore is the "not for callers" convention,
        # not class-private state, so SLF001 doesn't apply.
        func._api_command = command  # type: ignore[attr-defined]  # noqa: SLF001
        return func

    return decorator


def collect_api_commands(obj: object) -> dict[str, CommandHandler]:
    """Scan an object for methods decorated with @api_command.

    Returns {command_name: bound_method} dict.
    """
    handlers: dict[str, CommandHandler] = {}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        method = getattr(obj, name, None)
        if callable(method) and hasattr(method, "_api_command"):
            handlers[method._api_command] = method  # noqa: SLF001 — see ``api_command``
    return handlers
