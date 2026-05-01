"""Tests for frontend static file route registration."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.device_builder import DeviceBuilder


def _make_frontend(tmp_path: Path) -> Path:
    """Build a frontend directory layout matching the released wheel.

    Includes index.html, an assets/ subtree, top-level hashed JS
    bundles, and an rspack license sidecar — the latter is the file
    that historically tripped add_static (which only takes dirs).
    """
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><body></body>")
    (frontend / "app.abc123.js").write_text("// bundle")
    (frontend / "vendors.def456.js").write_text("// vendors")
    (frontend / "vendors.def456.js.LICENSE.txt").write_text("/* license */")

    assets = frontend / "assets"
    (assets / "logo").mkdir(parents=True)
    (assets / "logo" / "esphome.svg").write_text("<svg/>")
    return frontend


async def test_register_frontend_serves_index_at_root(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    frontend = _make_frontend(tmp_path)
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend)

    client = await aiohttp_client(app)
    resp = await client.get("/")
    assert resp.status == 200
    assert "<!doctype html>" in (await resp.text())


async def test_register_frontend_serves_top_level_bundles(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Hashed JS bundles next to index.html are reachable."""
    frontend = _make_frontend(tmp_path)
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend)

    client = await aiohttp_client(app)
    app_resp = await client.get("/app.abc123.js")
    vendors_resp = await client.get("/vendors.def456.js")
    assert (await app_resp.text()) == "// bundle"
    assert (await vendors_resp.text()) == "// vendors"


async def test_register_frontend_serves_top_level_license_sidecar(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A top-level *.LICENSE.txt no longer crashes startup or 404s.

    Regression: the previous code passed each top-level file to
    aiohttp's add_static, which only accepts directories and raised
    "is not a directory" on this exact filename.
    """
    frontend = _make_frontend(tmp_path)
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend)

    client = await aiohttp_client(app)
    resp = await client.get("/vendors.def456.js.LICENSE.txt")
    assert resp.status == 200
    assert "license" in (await resp.text())


async def test_register_frontend_serves_assets_subtree(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    frontend = _make_frontend(tmp_path)
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend)

    client = await aiohttp_client(app)
    resp = await client.get("/assets/logo/esphome.svg")
    assert resp.status == 200
    assert (await resp.text()) == "<svg/>"


async def test_register_frontend_does_not_shadow_api_routes(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """API routes registered before the frontend catch-all still win.

    The frontend catch-all is intentionally an "/" prefix add_static,
    so we rely on aiohttp's FIFO route lookup to keep API endpoints
    reachable. Lock that ordering down.
    """
    frontend = _make_frontend(tmp_path)
    app = web.Application()

    async def api_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/api/ping", api_handler)
    DeviceBuilder._register_frontend(app, frontend)

    client = await aiohttp_client(app)
    resp = await client.get("/api/ping")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}
