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

from .origin import request_origin_allowed

_LOGGER = logging.getLogger(__name__)

_CORS_METHODS = "GET, POST, PUT, DELETE, OPTIONS"
_CORS_HEADERS = "Content-Type, Authorization"

# Re-export so callers can ``except JSONDecodeError`` without importing
# orjson themselves. orjson's exception is a subclass of ValueError.
JSONDecodeError = orjson.JSONDecodeError


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Parse JSON via orjson; raises ``JSONDecodeError`` on bad input."""
    return orjson.loads(data)


def dumps(obj: Any) -> bytes:
    """Serialise *obj* to a compact JSON ``bytes`` blob."""
    return orjson.dumps(obj)


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
    """Reflect Origin in CORS headers only when same-origin or in ``trusted_domains``.

    Sibling of the WS handshake gate in ``api/ws.py`` — both share
    ``request_origin_allowed`` so they can't drift.
    """
    resp = web.Response() if request.method == "OPTIONS" else await handler(request)
    # Vary: Origin unconditionally — response shape depends on Origin, so a
    # shared cache must key on it to avoid mis-serving a peer.
    resp.headers["Vary"] = "Origin"

    origin = request.headers.get("Origin")
    if origin and _cors_origin_allowed(request, origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
        resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS
    elif origin:
        _LOGGER.debug(
            "CORS: omitting Access-Control-Allow-Origin: origin=%s host=%s", origin, request.host
        )
    return resp


def _cors_origin_allowed(request: web.Request, origin: str) -> bool:
    """Return True when CORS should reflect *origin* — same predicate as the WS gate."""
    if request.app.get("trusted_site", False):
        # HA Ingress: supervisor handles the boundary upstream.
        return True
    device_builder = request.app.get("device_builder")
    trusted_domains: list[str] = (
        device_builder.settings.trusted_domains if device_builder is not None else []
    )
    return request_origin_allowed(origin, request.host, trusted_domains)
