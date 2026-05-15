"""Tests for ``helpers/json.py`` orjson wrappers.

Most of the helpers are thin enough that their behavior is
self-evident from a glance, but ``dumps_str_non_str_keys`` flips
an orjson option and only one endpoint depends on the result —
worth a dedicated test so a future "let's drop the unused option"
cleanup doesn't silently regress legacy ``/json-config``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

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


def _app_with_cors(
    *,
    trusted_domains: list[str] | None = None,
    trusted_site: bool = False,
) -> web.Application:
    """Build a test app with ``cors_middleware`` + stubbed ``device_builder`` / ``trusted_site``."""
    settings = MagicMock()
    settings.trusted_domains = trusted_domains or []
    device_builder = MagicMock()
    device_builder.settings = settings

    app = web.Application(middlewares=[cors_middleware])
    app["device_builder"] = device_builder
    app["trusted_site"] = trusted_site

    async def _hello(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _echo(request: web.Request) -> web.Response:
        body = await request.text()
        return web.Response(text=body)

    app.router.add_get("/hello", _hello)
    app.router.add_post("/echo", _echo)
    return app


async def test_cors_middleware_omits_origin_header_when_no_origin(
    aiohttp_client: AiohttpClient,
) -> None:
    """No ``Origin`` → no ``Allow-Origin`` / methods / headers; ``Vary: Origin`` set."""
    client = await aiohttp_client(_app_with_cors())
    resp = await client.get("/hello")

    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert "Access-Control-Allow-Methods" not in resp.headers
    # Vary: Origin is set unconditionally so shared caches don't mis-serve the variants.
    assert resp.headers["Vary"] == "Origin"
    assert await resp.json() == {"ok": True}


async def test_cors_middleware_reflects_same_origin(
    aiohttp_client: AiohttpClient,
) -> None:
    """``Origin`` matching Host → reflected back, ``Vary: Origin`` set."""
    client = await aiohttp_client(_app_with_cors())
    host = f"{client.host}:{client.port}"
    origin = f"http://{host}"
    resp = await client.get("/hello", headers={"Origin": origin})

    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Vary"] == "Origin"
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"


async def test_cors_middleware_reflects_allowlisted_origin(
    aiohttp_client: AiohttpClient,
) -> None:
    """Cross-origin Origin in ``trusted_domains`` is reflected (reverse-proxy case)."""
    client = await aiohttp_client(_app_with_cors(trusted_domains=["dashboard.example.com"]))
    origin = "https://dashboard.example.com"
    resp = await client.get("/hello", headers={"Origin": origin})

    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Vary"] == "Origin"


async def test_cors_middleware_omits_origin_header_for_disallowed_origin(
    aiohttp_client: AiohttpClient,
) -> None:
    """Disallowed cross-origin → handler runs, but ``Access-Control-Allow-Origin`` omitted."""
    client = await aiohttp_client(_app_with_cors())
    resp = await client.get("/hello", headers={"Origin": "https://evil.example.com"})

    # Request still reaches the handler — the gating happens in the WS
    # handler / auth middleware, not here. CORS just controls what the
    # browser does with the response.
    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Vary still set so a shared cache doesn't mis-serve this no-ACAO response
    # to a peer with an allowlisted Origin (or vice versa).
    assert resp.headers["Vary"] == "Origin"


async def test_cors_middleware_reflects_origin_unconditionally_on_trusted_site(
    aiohttp_client: AiohttpClient,
) -> None:
    """``trusted_site=True`` (HA Ingress) reflects any Origin — supervisor handles the boundary."""
    client = await aiohttp_client(_app_with_cors(trusted_site=True))
    origin = "https://anything.example.com"
    resp = await client.get("/hello", headers={"Origin": origin})

    assert resp.status == 200
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Vary"] == "Origin"


async def test_cors_middleware_handles_options_preflight_without_invoking_handler(
    aiohttp_client: AiohttpClient,
) -> None:
    """``OPTIONS`` short-circuits to empty 200 without calling the handler; headers still attach."""
    handler_called: list[bool] = []

    async def _trap(_request: web.Request) -> web.Response:
        handler_called.append(True)
        return web.Response(status=418)

    app = _app_with_cors()
    app.router.add_route("OPTIONS", "/preflight", _trap)
    client = await aiohttp_client(app)

    host = f"{client.host}:{client.port}"
    origin = f"http://{host}"
    resp = await client.options("/preflight", headers={"Origin": origin})

    assert resp.status == 200
    assert handler_called == []
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    assert resp.headers["Access-Control-Allow-Headers"] == "Content-Type, Authorization"
    assert await resp.text() == ""


async def test_cors_middleware_options_preflight_omits_acao_for_disallowed_origin(
    aiohttp_client: AiohttpClient,
) -> None:
    """OPTIONS with a disallowed Origin still 200s but omits ``Access-Control-Allow-Origin``."""
    client = await aiohttp_client(_app_with_cors())
    resp = await client.options("/preflight", headers={"Origin": "https://evil.example.com"})

    assert resp.status == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert resp.headers["Vary"] == "Origin"
    assert await resp.text() == ""


async def test_cors_middleware_attaches_headers_to_non_get_methods(
    aiohttp_client: AiohttpClient,
) -> None:
    """POST / PUT / DELETE responses also get reflected CORS headers when allowed."""
    client = await aiohttp_client(_app_with_cors())
    host = f"{client.host}:{client.port}"
    origin = f"http://{host}"
    resp = await client.post("/echo", data="payload", headers={"Origin": origin})

    assert resp.status == 200
    assert await resp.text() == "payload"
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Vary"] == "Origin"


async def test_cors_middleware_attaches_headers_to_handler_error_response(
    aiohttp_client: AiohttpClient,
) -> None:
    """Non-2xx handler responses still get reflected CORS headers when Origin is allowed."""

    async def _server_error(_request: web.Request) -> web.Response:
        return web.Response(status=500, text="boom")

    app = _app_with_cors()
    app.router.add_get("/error", _server_error)
    client = await aiohttp_client(app)

    host = f"{client.host}:{client.port}"
    origin = f"http://{host}"
    resp = await client.get("/error", headers={"Origin": origin})

    assert resp.status == 500
    assert await resp.text() == "boom"
    assert resp.headers["Access-Control-Allow-Origin"] == origin
    assert resp.headers["Vary"] == "Origin"
