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
    (frontend / "index.html").write_text(
        "<!doctype html><html><head>"
        '<base href="__ESPHOME_BASE_HREF__" />'
        "</head><body></body></html>"
    )
    # Use 16-char hex hashes to match what rspack actually emits (xxhash64)
    # so the cache-header regex (``\.[a-f0-9]{8,}\.``) classifies them
    # as immutable.
    (frontend / "app.abc123def4567890.js").write_text("// bundle")
    (frontend / "vendors.def4567890abcdef.js").write_text("// vendors")
    (frontend / "vendors.def4567890abcdef.js.LICENSE.txt").write_text("/* license */")

    assets = frontend / "assets"
    (assets / "logo").mkdir(parents=True)
    (assets / "logo" / "esphome.svg").write_text("<svg/>")
    return frontend


def _make_app(frontend: Path, *, dev_mode: bool = False) -> web.Application:
    app = web.Application()
    DeviceBuilder._register_frontend(app, frontend, dev_mode=dev_mode)
    return app


async def test_register_frontend_serves_index_at_root(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/")
    assert resp.status == 200
    assert "<!doctype html>" in (await resp.text())


async def test_register_frontend_dev_mode_index_is_no_cache(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``dev_mode=True`` opts in to ``Cache-Control: no-cache`` for the SPA shell.

    Without this, a re-deployed wheel during development would be
    masked by a browser-cached ``index.html`` pointing at a now-deleted
    hashed bundle.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path), dev_mode=True))
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_register_frontend_default_index_omits_cache_control(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Production (default) leaves caching up to the browser's default heuristic."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/")
    assert resp.status == 200
    assert "Cache-Control" not in resp.headers


async def test_register_frontend_hashed_bundle_is_always_immutable(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Hashed bundles are content-addressed → safe to cache forever in either mode."""
    frontend = _make_frontend(tmp_path)
    for dev_mode in (False, True):
        client = await aiohttp_client(_make_app(frontend, dev_mode=dev_mode))
        resp = await client.get("/app.abc123def4567890.js")
        assert resp.status == 200, dev_mode
        assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable", dev_mode


async def test_register_frontend_dev_mode_spa_fallback_is_no_cache(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Dev-mode SPA-fallback ``index.html`` for a deep link is also no-cache."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path), dev_mode=True))
    resp = await client.get("/device/foo.yaml")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-cache"


async def test_register_frontend_serves_top_level_bundles(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Hashed JS bundles next to index.html are reachable."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    app_resp = await client.get("/app.abc123def4567890.js")
    vendors_resp = await client.get("/vendors.def4567890abcdef.js")
    assert (await app_resp.text()) == "// bundle"
    assert (await vendors_resp.text()) == "// vendors"


async def test_register_frontend_root_request_renders_base_href_root(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``GET /`` substitutes the base placeholder with ``/``."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/")
    body = await resp.text()
    assert '<base href="/" />' in body
    assert "__ESPHOME_BASE_HREF__" not in body


async def test_register_frontend_deep_link_renders_base_href_root(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """SPA deep link ``/device/foo.yaml`` strips the route tail back to ``/``.

    Without this, the deferred app script's relative ``src``
    would resolve to ``/device/app.<hash>.js`` and the page would
    white-screen on a hard reload — that's the bug
    ``_BASE_HREF_PLACEHOLDER`` exists to fix.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/device/foo.yaml")
    body = await resp.text()
    assert '<base href="/" />' in body


async def test_register_frontend_honours_x_forwarded_prefix(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Reverse proxies that strip a path prefix announce it via ``X-Forwarded-Prefix``."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/", headers={"X-Forwarded-Prefix": "/dashboard"})
    body = await resp.text()
    assert '<base href="/dashboard/" />' in body


async def test_register_frontend_ingress_style_path_strips_route_tail(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """An HA-ingress-style path on a deep link resolves to the ingress base."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get(
        "/api/hassio_ingress/TOKEN/device/foo.yaml",
        headers={"X-Forwarded-Prefix": "/api/hassio_ingress/TOKEN"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/TOKEN/" />' in body


async def test_register_frontend_404s_asset_shaped_paths(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Asset-shaped deep paths get 404, not the SPA shell.

    Without this, a deep ``/device/foo`` hard-reload that misses
    the base injection would ask for ``/device/app.<hash>.js``,
    receive ``index.html`` via SPA fallback, and white-screen
    when the browser parsed HTML as JS.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    for path in (
        "/device/app.abc123def4567890.js",
        "/secrets/styles.css",
        "/some/where/source.map",
        "/icons/font.woff2",
    ):
        resp = await client.get(path)
        assert resp.status == 404, path


async def test_register_frontend_yaml_deep_link_still_falls_back(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``.yaml`` is a real SPA route segment, not an asset — fall through to the shell."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/device/acfloatmonitor32.yaml")
    assert resp.status == 200
    assert "<!doctype html>" in (await resp.text())


async def test_register_frontend_missing_base_placeholder_raises(
    tmp_path: Path,
) -> None:
    """A wheel whose ``index.html`` lacks the placeholder is a deployment error."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<!doctype html><body></body>")
    (frontend / "assets").mkdir()
    app = web.Application()
    with pytest.raises(RuntimeError, match="missing the '__ESPHOME_BASE_HREF__'"):
        DeviceBuilder._register_frontend(app, frontend)


async def test_register_frontend_escapes_base_href(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A hostile ``X-Forwarded-Prefix`` can't break out of the ``<base href>`` attribute.

    ``html.escape(..., quote=True)`` escapes ``"``/``<``/``>``/``&``,
    so the closing-quote-then-script-tag payload becomes inert text
    inside the attribute value rather than leaking past the
    terminator.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/", headers={"X-Forwarded-Prefix": '/"><script>alert(1)</script>'})
    body = await resp.text()
    assert "&quot;" in body
    assert '<base href="/&quot;' in body
    # ``<script>`` and its closing tag escape to ``&lt;script&gt;``,
    # so the literal tag never appears in the rendered HTML.
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&lt;/script&gt;" in body


async def test_register_frontend_collapses_double_leading_slash_in_prefix(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``X-Forwarded-Prefix: //evil.com`` doesn't yield a protocol-relative base.

    A protocol-relative ``<base href="//evil.com/">`` would point
    relative URLs at an attacker-controlled origin. Collapse runs
    of leading slashes to one so the value always resolves
    on-origin.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/", headers={"X-Forwarded-Prefix": "//evil.com"})
    body = await resp.text()
    assert '<base href="/evil.com/" />' in body
    assert '<base href="//evil.com' not in body


async def test_register_frontend_collapses_double_trailing_slash_in_prefix(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``X-Forwarded-Prefix: /dashboard//`` doesn't produce ``//`` in the base.

    A trailing-double-slash would yield ``<base href="/dashboard//">``
    so a relative ``app.HASH.js`` resolves to
    ``/dashboard//app.HASH.js`` — the ``//`` run can confuse
    middlewares (some collapse, some don't) and cache keys
    upstream. Collapse to exactly one trailing slash so the
    rendered base is canonical.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/", headers={"X-Forwarded-Prefix": "/dashboard//"})
    body = await resp.text()
    assert '<base href="/dashboard/" />' in body
    assert '<base href="/dashboard//' not in body


async def test_register_frontend_multi_segment_deep_link_strips_full_tail(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Future SPA routes with multiple path segments get their full tail stripped.

    The backend doesn't track the SPA route table — it slices the
    aiohttp-matched ``tail`` off ``request.path`` directly. A
    hypothetical ``/settings/network`` route resolves to base
    ``/`` regardless of whether the backend's been told about it.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/settings/network")
    body = await resp.text()
    assert '<base href="/" />' in body


async def test_register_frontend_shell_response_carries_vary_header(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``Vary: X-Ingress-Path, X-Forwarded-Prefix`` so caches don't serve cross-prefix shells."""
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    for path in ("/", "/device/foo.yaml", "/settings/network"):
        resp = await client.get(path)
        assert resp.status == 200, path
        assert resp.headers.get("Vary") == "X-Ingress-Path, X-Forwarded-Prefix", path


async def test_register_frontend_honours_x_ingress_path(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """HA core's ingress proxy announces the prefix via ``X-Ingress-Path``.

    See ``homeassistant/components/hassio/ingress.py:_init_header`` —
    HA core sets ``X-Ingress-Path: /api/hassio_ingress/<token>``
    (no trailing slash) and the supervisor's ingress proxy passes
    it through unchanged. The add-on never sees
    ``X-Forwarded-Prefix`` on this path, so the backend must read
    ``X-Ingress-Path`` independently or every ingress hard-reload
    of a deep SPA link white-screens (the asset-shaped relative
    URL resolves against the bare host).
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get(
        "/device/foo.yaml",
        headers={"X-Ingress-Path": "/api/hassio_ingress/TOKEN"},
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/TOKEN/" />' in body


async def test_register_frontend_x_ingress_path_takes_precedence_over_forwarded(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``X-Ingress-Path`` wins when both headers arrive.

    Production deployments only set one or the other, but if both
    show up we trust the HA-specific signal — it's the canonical
    per-token prefix from core's ingress proxy.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get(
        "/",
        headers={
            "X-Ingress-Path": "/api/hassio_ingress/TOKEN",
            "X-Forwarded-Prefix": "/dashboard",
        },
    )
    body = await resp.text()
    assert '<base href="/api/hassio_ingress/TOKEN/" />' in body


async def test_register_frontend_serves_top_level_license_sidecar(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A top-level *.LICENSE.txt no longer crashes startup or 404s.

    Regression: the previous code passed each top-level file to
    aiohttp's add_static, which only accepts directories and raised
    "is not a directory" on this exact filename.
    """
    client = await aiohttp_client(_make_app(_make_frontend(tmp_path)))
    resp = await client.get("/vendors.def4567890abcdef.js.LICENSE.txt")
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
    (frontend / "alias.js").symlink_to(frontend / "app.abc123def4567890.js")

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
