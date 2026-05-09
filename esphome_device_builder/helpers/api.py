"""API command registration helpers."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from ..models import ErrorCode

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
        func._api_command = command  # type: ignore[attr-defined]
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
            handlers[method._api_command] = method
    return handlers
