"""JSON helpers — orjson wrappers, response builders, CORS middleware.

Centralises the orjson dependency so call sites import ``loads`` /
``dumps`` from here instead of pulling the C library directly. Two
benefits: the import surface stays consistent (no mix of stdlib
``json`` and ``orjson`` across the package, which silently slowed the
hottest paths), and swapping the underlying serialiser is a one-file
change.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson
from aiohttp import web

_LOGGER = logging.getLogger(__name__)

# Re-export so callers can ``except JSONDecodeError`` without importing
# orjson themselves. orjson's exception is a subclass of ValueError.
JSONDecodeError = orjson.JSONDecodeError


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON via orjson; raises ``JSONDecodeError`` on bad input."""
    return orjson.loads(data)


def dumps(obj: Any) -> bytes:
    """Serialise *obj* to a compact JSON ``bytes`` blob."""
    return orjson.dumps(obj)


def dumps_str_non_str_keys(obj: Any) -> str:
    """
    Serialise *obj* allowing dict keys whose type isn't *exactly* ``str``.

    Wraps orjson's ``OPT_NON_STR_KEYS`` — keys that are ``str``
    subclasses, ``int``, ``float``, ``bool``, ``datetime``,
    ``UUID``, etc. all serialise instead of raising ``TypeError:
    Dict key must be str``. ESPHome's ``yaml_util`` returns dicts
    whose keys are ``EStr`` (a ``str`` subclass that carries
    source-position info), which is what the legacy
    ``/json-config`` endpoint feeds in.

    Use this helper for that endpoint (and only there); the strict
    default of ``dumps`` still catches the more common bug shape —
    a dict with non-string keys leaking into a response — for
    every other call site.

    Returns ``str`` so it can be passed straight to aiohttp's
    ``web.json_response(dumps=...)`` (which expects a ``str``-
    returning callable, like ``dumps_str``).
    """
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode()


def dumps_str(obj: Any) -> str:
    """Serialise *obj* to a compact JSON ``str``.

    Adapter for aiohttp APIs that take a ``dumps`` callable returning
    ``str`` — ``WebSocketResponse.send_json(dumps=...)`` and
    ``web.json_response(dumps=...)``. Lets call sites use the standard
    aiohttp shape instead of building a raw frame manually.
    """
    return orjson.dumps(obj).decode()


def dumps_indent(obj: Any) -> bytes:
    """Serialise *obj* with two-space indentation — for human-readable files."""
    return orjson.dumps(obj, option=orjson.OPT_INDENT_2)


def json_response(data: Any, status: int = 200) -> web.Response:
    """Return a JSON response, serialising dataclasses via mashumaro."""
    body = data.to_dict() if hasattr(data, "to_dict") else data
    return web.Response(
        status=status,
        content_type="application/json",
        body=dumps(body),
    )


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Permissive CORS for local development."""
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp
