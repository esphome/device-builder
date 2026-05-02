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


def error_response(message: str, status: int = 400) -> web.Response:
    """Return a JSON error response."""
    return json_response({"error": message}, status)


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


# Content types worth gzipping. Binary types (firmware bins, images
# in their compressed wire format) and tiny payloads aren't worth the
# CPU. We match the family-level common cases that dominate the
# frontend payload: HTML shell, JS bundles, CSS, JSON config / WS
# messages, plain-text logs, and the SVG board / logo assets.
_COMPRESSIBLE_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/manifest+json",
    "application/xml",
    "application/wasm",
    "image/svg+xml",
)
# Don't bother gzipping responses smaller than this — the savings
# are dwarfed by the response-line + headers and the per-request
# CPU cost. 1 KiB matches what most CDNs use as the floor.
_COMPRESSION_MIN_BYTES = 1024


@web.middleware
async def compression_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Enable gzip/deflate compression for compressible responses.

    Calls ``response.enable_compression()`` so aiohttp negotiates
    encoding from the request's ``Accept-Encoding`` header. Backed
    by ``aiohttp-fast-zlib`` swap to ``isal_zlib``, the per-byte
    CPU cost is roughly an order of magnitude lower than stdlib
    zlib — fine even for the largest frontend bundle (~3.5 MB).

    Skipped for:
        * Clients that don't advertise gzip support.
        * Already-encoded responses (pre-compressed ``.gz`` / ``.br``
          sidecars served by ``FileResponse``, which sets
          ``Content-Encoding`` itself).
        * Non-compressible content types (firmware bins, opaque
          images, etc) — see ``_COMPRESSIBLE_TYPES``.
        * Tiny responses (< ``_COMPRESSION_MIN_BYTES``) where the
          compressed body + framing would be larger than the
          original.
        * ``Range`` requests — aiohttp can't combine partial-content
          with on-the-fly compression.
    """
    response = await handler(request)
    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept and "deflate" not in accept:
        return response
    if response.headers.get("Content-Encoding"):
        return response
    if request.headers.get("Range"):
        return response
    content_type = response.content_type or ""
    if not any(content_type.startswith(prefix) for prefix in _COMPRESSIBLE_TYPES):
        return response
    content_length = response.content_length
    if content_length is not None and content_length < _COMPRESSION_MIN_BYTES:
        return response
    response.enable_compression()
    return response
