"""Tests for frontend static file route registration."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from esphome_device_builder.device_builder import (
    DeviceBuilder,
    spa_fallback_middleware,
)


def _make_frontend(tmp_path: Path) -> Path:
    """Build a frontend directory layout matching the released wheel.

    Includes index.html, an assets/ subtree, top-level hashed JS
    bundles, and an rspack license sidecar — the latter is the file
    that historically tripped add_static when registered as a single
    file (it requires a directory).
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


def _make_app(frontend: Path) -> web.Application:
    """Build a test app with the SPA fallback middleware wired in."""
    app = web.Application(middlewares=[spa_fallback_middleware])
    DeviceBuilder._register_frontend(app, frontend)
    return app


async def test_register_frontend_serves_index_at_root(
    tmp_path: Path, aiohttp_client: object
) -> None:
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    resp = await client.get("/")
    assert resp.status == 200
    assert "<!doctype html>" in (await resp.text())


async def test_register_frontend_serves_top_level_bundles(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """Hashed JS bundles next to index.html are reachable."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    app_resp = await client.get("/app.abc123.js")
    vendors_resp = await client.get("/vendors.def456.js")
    assert (await app_resp.text()) == "// bundle"
    assert (await vendors_resp.text()) == "// vendors"


async def test_register_frontend_serves_top_level_license_sidecar(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """A top-level *.LICENSE.txt no longer crashes startup or 404s.

    Regression: the previous code passed each top-level file to
    aiohttp's add_static, which only accepts directories and raised
    "is not a directory" on this exact filename.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    resp = await client.get("/vendors.def456.js.LICENSE.txt")
    assert resp.status == 200
    assert "license" in (await resp.text())


async def test_register_frontend_serves_assets_subtree(
    tmp_path: Path, aiohttp_client: object
) -> None:
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    resp = await client.get("/assets/logo/esphome.svg")
    assert resp.status == 200
    assert (await resp.text()) == "<svg/>"


async def test_register_frontend_serves_index_for_spa_deep_links(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """Hard reload of a SPA route returns index.html via the middleware.

    Without the middleware the dashboard 404s on every refresh that
    isn't on the bare root, since the client-side router never gets a
    chance to handle the URL.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    for url in (
        "/device/apollo-r-pro-1-eth-5938e0.yaml",
        "/devices",
        "/settings/network",
    ):
        resp = await client.get(url)
        assert resp.status == 200, url
        assert "<!doctype html>" in (await resp.text()), url


async def test_register_frontend_does_not_swallow_unknown_api_routes(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """Unknown server-side paths still 404 instead of returning HTML.

    The SPA fallback must not silently serve index.html for misrouted
    API clients — that turns "endpoint typo" into "JSON parse error".
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))  # type: ignore[operator]
    for url in ("/api/unknown", "/ws/foo", "/boards/missing"):
        resp = await client.get(url)
        assert resp.status == 404, url


async def test_register_frontend_does_not_shadow_api_routes(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """API routes registered before the frontend catch-all still win."""
    app = web.Application(middlewares=[spa_fallback_middleware])

    async def api_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/api/ping", api_handler)
    DeviceBuilder._register_frontend(app, _make_frontend(tmp_path))

    client = await aiohttp_client(app)  # type: ignore[operator]
    resp = await client.get("/api/ping")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_register_frontend_traversal_handled_by_aiohttp(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """Traversal probes never leak files outside the frontend dir.

    Static-file safety is delegated to aiohttp's ``StaticResource``;
    this test pins the contract by planting a sentinel outside the
    frontend root and asserting its contents never appear in a
    response body, no matter how the request is shaped.
    """
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("DO-NOT-LEAK")
    frontend = _make_frontend(tmp_path)

    client = await aiohttp_client(_make_app(frontend))  # type: ignore[operator]
    for url in (
        "/../secret.txt",
        "/foo/../../secret.txt",
        "/assets/../../secret.txt",
        "/%2E%2E/secret.txt",
        "/" + "/".join([".."] * 8) + "/secret.txt",
    ):
        resp = await client.get(url)
        body = await resp.text()
        assert "DO-NOT-LEAK" not in body, url


async def test_register_frontend_does_not_follow_symlinks_outside(
    tmp_path: Path, aiohttp_client: object
) -> None:
    """A symlink inside the frontend dir pointing outside is not served."""
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("DO-NOT-LEAK")

    frontend = _make_frontend(tmp_path)
    (frontend / "leak.txt").symlink_to(sentinel)

    client = await aiohttp_client(_make_app(frontend))  # type: ignore[operator]
    resp = await client.get("/leak.txt")
    body = await resp.text()
    assert "DO-NOT-LEAK" not in body
