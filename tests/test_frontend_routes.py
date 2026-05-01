"""Tests for frontend static file route registration."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.device_builder import DeviceBuilder


def _make_frontend(tmp_path: Path) -> Path:
    """Build a frontend directory layout matching the released wheel.

    Includes index.html, an assets/ subtree, top-level hashed JS
    bundles, and an rspack license sidecar — the latter is the file
    that historically tripped the original code, which passed it to
    add_static (which only takes directories).
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
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend)
    return app


async def test_register_frontend_serves_index_at_root(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/")
    assert resp.status == 200
    assert "<!doctype html>" in (await resp.text())


async def test_register_frontend_serves_top_level_bundles(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Hashed JS bundles next to index.html are reachable."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
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
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/vendors.def456.js.LICENSE.txt")
    assert resp.status == 200
    assert "license" in (await resp.text())


async def test_register_frontend_serves_assets_subtree(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/assets/logo/esphome.svg")
    assert resp.status == 200
    assert (await resp.text()) == "<svg/>"


async def test_register_frontend_serves_index_for_spa_deep_links(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Hard reload of a SPA route returns index.html.

    Without an SPA fallback the dashboard 404s on every refresh that
    isn't on the bare root, since the client-side router never gets a
    chance to handle the URL.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    for url in (
        "/device/apollo-r-pro-1-eth-5938e0.yaml",
        "/devices",
        "/settings/network",
    ):
        resp = await client.get(url)
        assert resp.status == 200, url
        assert "<!doctype html>" in (await resp.text()), url


async def test_register_frontend_does_not_shadow_specific_routes(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Routes registered before the frontend catch-all still match first.

    aiohttp's FIFO matching is what keeps `/api/...`, `/ws`,
    `/boards/...` etc. from being shadowed by the frontend SPA
    fallback — no per-prefix exclusion list needed in our handler.
    """
    app = web.Application()

    async def api_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/api/ping", api_handler)
    DeviceBuilder._register_frontend(app, _make_frontend(tmp_path))

    client = await aiohttp_client(app)
    resp = await client.get("/api/ping")
    assert resp.status == 200
    assert (await resp.json()) == {"ok": True}


async def test_register_frontend_multi_segment_paths_do_not_hit_disk(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Path-traversal probes can't read files anywhere.

    The catch-all handler only resolves single-segment names against
    the frontend dir; multi-segment paths (everything containing a
    ``/``) are treated as SPA routes and return ``index.html``.
    Plant a sentinel both inside and outside the frontend dir to
    catch any regression that lets a multi-segment path through.
    """
    sentinel_outside = tmp_path / "secret.txt"
    sentinel_outside.write_text("DO-NOT-LEAK")

    frontend = _make_frontend(tmp_path)
    nested = frontend / "nested" / "leak.txt"
    nested.parent.mkdir()
    nested.write_text("ALSO-DO-NOT-LEAK")

    client = await aiohttp_client(_make_app(frontend))
    for url in (
        "/../secret.txt",
        "/foo/../../secret.txt",
        "/%2E%2E/secret.txt",
        "/" + "/".join([".."] * 8) + "/secret.txt",
        "/nested/leak.txt",
    ):
        resp = await client.get(url)
        body = await resp.text()
        assert "DO-NOT-LEAK" not in body, url
        assert "ALSO-DO-NOT-LEAK" not in body, url


async def test_register_frontend_does_not_follow_symlinks_outside(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A symlink inside the frontend dir pointing outside is not served.

    Matches the default behaviour of aiohttp's ``add_static``: the
    ``resolve().is_relative_to`` check after ``is_file()`` rejects
    any symlink whose target lies outside ``frontend_dir``.
    """
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("DO-NOT-LEAK")
    frontend = _make_frontend(tmp_path)
    (frontend / "leak").symlink_to(sentinel)

    client = await aiohttp_client(_make_app(frontend))
    resp = await client.get("/leak")
    body = await resp.text()
    assert "DO-NOT-LEAK" not in body
    # Symlink rejected → caller gets the SPA shell, not the secret.
    assert "<!doctype html>" in body


async def test_register_frontend_follows_symlinks_inside_frontend_dir(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Symlinks targeting other files within the frontend dir are fine.

    Locks down that we don't accidentally over-broadly reject every
    symlink — only ones that would escape the frontend root.
    """
    frontend = _make_frontend(tmp_path)
    (frontend / "alias.js").symlink_to(frontend / "app.abc123.js")

    client = await aiohttp_client(_make_app(frontend))
    resp = await client.get("/alias.js")
    assert resp.status == 200
    assert (await resp.text()) == "// bundle"


async def test_register_frontend_url_encoded_slash_is_blocked(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """An attacker can't sneak a path separator past the guard via URL encoding."""
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("DO-NOT-LEAK")
    frontend = _make_frontend(tmp_path)

    client = await aiohttp_client(_make_app(frontend))
    for url in ("/..%2Fsecret.txt", "/foo%2F..%2F..%2Fsecret.txt"):
        resp = await client.get(url)
        body = await resp.text()
        assert "DO-NOT-LEAK" not in body, url


def test_register_frontend_raises_when_assets_missing(tmp_path: Path) -> None:
    """A wheel without assets/ should fail loudly, not 404 silently."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html>")

    app = web.Application()
    with pytest.raises(RuntimeError, match="assets/"):
        DeviceBuilder._register_frontend(app, frontend)


def test_register_frontend_raises_when_index_missing(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "assets").mkdir()

    app = web.Application()
    with pytest.raises(RuntimeError, match=r"index\.html"):
        DeviceBuilder._register_frontend(app, frontend)


def test_register_frontend_lists_all_missing_entries(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    app = web.Application()
    with pytest.raises(RuntimeError) as exc:
        DeviceBuilder._register_frontend(app, frontend)
    assert "index.html" in str(exc.value)
    assert "assets/" in str(exc.value)
