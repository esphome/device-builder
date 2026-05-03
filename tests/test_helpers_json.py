"""Tests for ``helpers/json.py`` orjson wrappers.

Most of the helpers are thin enough that their behavior is
self-evident from a glance, but ``dumps_str_non_str_keys`` flips
an orjson option and only one endpoint depends on the result —
worth a dedicated test so a future "let's drop the unused option"
cleanup doesn't silently regress legacy ``/json-config``.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.helpers.json import (
    cors_middleware,
    dumps,
    dumps_str_non_str_keys,
    loads,
)


class _StrSubclass(str):
    """Stand-in for ESPHome's ``EStr`` (a ``str`` subclass)."""


def test_dumps_rejects_str_subclass_keys() -> None:
    """Plain ``dumps`` keeps orjson's strict default.

    Non-exact-``str`` keys raise ``TypeError``. The strict default
    catches the common bug of leaking a non-string key into a JSON
    response, so this test pins it — the legacy ``/json-config``
    endpoint reaches for ``dumps_str_non_str_keys`` precisely
    because plain ``dumps`` would refuse its EStr-keyed input.
    """
    with pytest.raises(TypeError, match="Dict key must be str"):
        dumps({_StrSubclass("hello"): "world"})


def test_dumps_str_non_str_keys_serialises_str_subclass_keys() -> None:
    """``dumps_str_non_str_keys`` permits ``str``-subclass keys.

    ESPHome's ``yaml_util.load_yaml`` returns dicts whose keys are
    ``EStr`` (a ``str`` subclass carrying source-position info), and
    legacy ``/json-config`` ships those dicts straight to HA. The
    helper has to round-trip them as plain JSON strings.
    """
    payload = {_StrSubclass("esphome"): {_StrSubclass("name"): "kitchen"}}
    out = dumps_str_non_str_keys(payload)
    assert isinstance(out, str)
    assert loads(out) == {"esphome": {"name": "kitchen"}}


def test_dumps_str_non_str_keys_serialises_plain_str_keys() -> None:
    """Plain ``str`` keys round-trip through the permissive helper.

    The option name is ``OPT_NON_STR_KEYS``, and the spelling makes
    it sound like it *replaces* string-key handling — it doesn't, it
    adds non-``str`` keys to what's already accepted. Pin that so a
    future reader doesn't reach for a separate helper for the
    common case.
    """
    out = dumps_str_non_str_keys({"plain": 1, "nested": {"x": 2}})
    assert isinstance(out, str)
    assert loads(out) == {"plain": 1, "nested": {"x": 2}}


# ---------------------------------------------------------------------------
# cors_middleware
# ---------------------------------------------------------------------------


def _app_with_cors() -> web.Application:
    """Build an aiohttp app wired with ``cors_middleware`` and a couple of route shapes.

    The middleware short-circuits ``OPTIONS`` (returning an empty
    200 with the headers set) and forwards every other method to
    the inner handler. Two routes are enough to exercise both —
    a GET that returns a JSON body, and any non-GET (we use POST
    here) that exercises the ``Access-Control-Allow-Methods`` line.
    """
    app = web.Application(middlewares=[cors_middleware])

    async def _hello(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _echo(request: web.Request) -> web.Response:
        body = await request.text()
        return web.Response(text=body)

    app.router.add_get("/hello", _hello)
    app.router.add_post("/echo", _echo)
    return app


async def test_cors_middleware_attaches_headers_to_get_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """A normal GET passes through and the response gains the CORS headers.

    Pin all three header names + the wildcard origin so a
    refactor that drops or narrows any of them surfaces here.
    The ``Access-Control-Allow-Origin: *`` is deliberately
    permissive — the dashboard's design assumption is that auth
    happens at the WS / bearer layer, not via origin check; the
    middleware is for development convenience (frontend dev
    server on a different port).
    """
    client = await aiohttp_client(_app_with_cors())
    resp = await client.get("/hello")

    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"

    # Body still flows through unmolested.
    assert await resp.json() == {"ok": True}


async def test_cors_middleware_handles_options_preflight_without_invoking_handler(
    aiohttp_client: AiohttpClient,
) -> None:
    """``OPTIONS`` requests return an empty 200 without calling the inner handler.

    The CORS preflight contract: browsers send ``OPTIONS`` to
    check whether a non-simple cross-origin request is allowed.
    The middleware MUST answer that itself — forwarding to the
    inner handler would 405 (most aiohttp routes only register
    GET/POST/etc., not OPTIONS) and the browser would block the
    real request that follows. Pin both halves: status 200 +
    headers present + body empty (the spec allows an empty 2xx
    for preflight).

    Track whether the handler was reached by registering an
    ``OPTIONS`` route that records its invocation; the assertion
    below proves it never fired despite a matching path.
    """
    handler_called: list[bool] = []

    async def _trap(_request: web.Request) -> web.Response:
        handler_called.append(True)
        return web.Response(status=418)  # I'm a teapot — distinguishable

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_route("OPTIONS", "/preflight", _trap)
    client = await aiohttp_client(app)

    resp = await client.options("/preflight")

    assert resp.status == 200
    assert handler_called == [], (
        "OPTIONS preflight must not reach the inner handler — the middleware's "
        "early return is what makes preflight work for routes that don't "
        "register OPTIONS explicitly"
    )
    # Headers attach the same way they do for normal responses.
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"
    # Empty body — the middleware's ``web.Response()`` default.
    assert await resp.text() == ""


async def test_cors_middleware_attaches_headers_to_non_get_methods(
    aiohttp_client: AiohttpClient,
) -> None:
    """POST / PUT / DELETE responses also get the CORS headers.

    Sanity-check that the middleware doesn't conditionally
    attach headers (e.g. only on GET / OPTIONS) — every non-
    OPTIONS method should pass through to the handler AND get
    the post-handler header injection.
    """
    client = await aiohttp_client(_app_with_cors())
    resp = await client.post("/echo", data="payload")

    assert resp.status == 200
    assert await resp.text() == "payload"
    # Pin all three headers — a regression that conditionally
    # attached only Origin (or only the GET-shaped subset)
    # would still pass a single-header check.
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"


async def test_cors_middleware_attaches_headers_to_handler_error_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """A handler returning a non-2xx ``Response`` still gets CORS headers.

    Pin the contract that the middleware's post-await header
    injection runs on the handler's response object regardless
    of its status code — a handler that returns
    ``web.Response(status=500)`` (or 404 / 401 / etc.)
    surfaces the error to the caller WITH the CORS headers
    attached, so a browser-side ``fetch`` can read the status
    and body cross-origin.

    Note: a handler that *raises* ``web.HTTPException`` is a
    different shape — aiohttp's exception handling builds the
    response after the middleware's ``await`` raises, so those
    responses bypass this middleware's header injection. That's
    a known gap in the minimal middleware (no ``try/except``
    wrap); pinning the *return*-error path here documents the
    supported contract without locking the gap in as desired.
    """

    async def _server_error(_request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/error", _server_error)
    client = await aiohttp_client(app)

    resp = await client.get("/error")

    assert resp.status == 500
    assert await resp.text() == "boom"
    # Pin all three headers — a regression that made
    # ``Access-Control-Allow-Headers`` conditional on a 2xx
    # status code would silently break the
    # browser's ability to read the error response cross-origin
    # (preflight would still pass but the actual fetch would
    # surface as a generic network error).
    assert resp.headers["Access-Control-Allow-Origin"] == "*"
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"
