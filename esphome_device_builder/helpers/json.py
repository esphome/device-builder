"""JSON response helpers and CORS middleware."""

from __future__ import annotations

import logging
from typing import Any

import orjson
from aiohttp import web

_LOGGER = logging.getLogger(__name__)


def json_response(data: Any, status: int = 200) -> web.Response:
    """Return a JSON response, serialising dataclasses via mashumaro."""
    body = data.to_dict() if hasattr(data, "to_dict") else data
    return web.Response(
        status=status,
        content_type="application/json",
        body=orjson.dumps(body),
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
