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

from .origin import origin_in_allowlist, origin_matches_host

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
    """Origin-allowlist CORS — reflect Origin only when allowed.

    Same-origin (Origin matches Host) or in the operator's
    ``trusted_domains`` allowlist passes the gate; everything else
    has ``Access-Control-Allow-Origin`` omitted.

    Sibling of the WS handshake gate in ``api/ws.py`` — both decide
    cross-origin acceptance off the same predicate so they can't
    drift. Cross-origin requests from non-allowlisted browsers still
    reach the handler (and 401 / 403 / whatever the route returns),
    but the response carries no ``Access-Control-Allow-Origin`` so
    the browser blocks the calling JS from reading it. CLI tools /
    HA integration omit Origin entirely and bypass the gate; their
    requests are authenticated by the bearer-token / in-band auth
    chain.
    """
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)

    origin = request.headers.get("Origin")
    if not origin:
        # No Origin → same-origin browser fetch or non-browser client.
        # Neither needs CORS headers, so omit them entirely.
        return resp

    trusted_site = bool(request.app.get("trusted_site", False))
    if trusted_site:
        # HA Ingress site is bound to the supervisor's docker network
        # and trusts upstream auth — reflect Origin to keep the
        # supervisor-proxied frontend working.
        allowed = True
    else:
        device_builder = request.app.get("device_builder")
        trusted_domains: list[str] = (
            device_builder.settings.trusted_domains if device_builder is not None else []
        )
        allowed = origin_matches_host(origin, request.host) or origin_in_allowlist(
            origin, trusted_domains
        )

    if allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        # Vary so a shared cache doesn't serve the wrong Origin to a
        # different cross-origin caller — without it, an
        # allowlist-permitted response could leak to a peer.
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
        resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS

    return resp
