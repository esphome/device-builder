"""End-to-end coverage for ``helpers.auth.auth_middleware``.

The middleware is the gate every REST endpoint passes through —
``firmware/download``-style binary downloads, the legacy
``/json-config`` endpoint, the static ``/boards/images/...``
asset path, etc. Without direct tests every branch was exercised
only indirectly via legacy-endpoint tests, leaving gaps:

- ``not settings.using_password`` short-circuit (auth disabled).
- ``OPTIONS`` short-circuit (CORS preflight).
- ``_PUBLIC_PATHS`` / ``_PUBLIC_PREFIXES`` allowlist.
- Bearer-token success.
- Basic-auth success / wrong-password failure.
- Rate-limit lockout while a basic-auth attempt is active.
- No-auth-header → 401 with the WWW-Authenticate challenge.

This file drives each via a real ``aiohttp`` test client with
``auth_middleware`` wired into a tiny app — the same shape
production uses, just with a stub ``device_builder`` whose
``settings`` / ``auth`` surfaces respond as the test scenario
demands.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.auth import auth_middleware


class _StubSessionStore:
    """Token-validation stub: only the ``valid_tokens`` set succeeds.

    Returns ``"session"`` (a truthy sentinel) for any valid token,
    matching the production ``SessionStore.validate`` shape that
    the middleware checks via ``is not None``.
    """

    def __init__(self, valid_tokens: set[str]) -> None:
        self._valid = valid_tokens

    async def validate(self, token: str) -> object | None:
        return "session" if token in self._valid else None


class _StubRateLimiter:
    """Records ``remaining_lockout`` / ``clear`` / ``record_failure`` calls.

    Defaults to no lockout (``remaining_lockout`` returns 0); tests
    override ``lockout_remaining`` to drive the locked-out branch.
    """

    def __init__(self, *, lockout_remaining: float = 0.0) -> None:
        self.lockout_remaining = lockout_remaining
        self.cleared: list[str] = []
        self.failures: list[str] = []

    def remaining_lockout(self, ip: str) -> float:
        return self.lockout_remaining

    def clear(self, ip: str) -> None:
        self.cleared.append(ip)

    def record_failure(self, ip: str) -> None:
        self.failures.append(ip)


class _StubSettings:
    """``DashboardSettings`` stand-in: only the fields the middleware reads."""

    def __init__(
        self,
        *,
        using_password: bool,
        username: str = "admin",
        password: str = "hunter2",  # noqa: S107 — fixture default, not a real credential
    ) -> None:
        self.using_password = using_password
        self._username = username
        self._password = password

    def check_password(self, username: str, password: str) -> bool:
        return username == self._username and password == self._password


class _StubAuth:
    """``AuthController`` stand-in: only ``session_store`` + ``rate_limiter``."""

    def __init__(
        self,
        *,
        valid_tokens: set[str] | None = None,
        rate_limiter: _StubRateLimiter | None = None,
    ) -> None:
        self.session_store = _StubSessionStore(valid_tokens or set())
        self.rate_limiter = rate_limiter or _StubRateLimiter()


class _StubDeviceBuilder:
    """``DeviceBuilder`` stand-in stored at ``app['device_builder']``."""

    def __init__(self, settings: _StubSettings, auth: _StubAuth | None = None) -> None:
        self.settings = settings
        self.auth = auth or _StubAuth()


def _build_app(device_builder: _StubDeviceBuilder) -> web.Application:
    """Wire ``auth_middleware`` into a tiny app with a few representative routes.

    - ``/api/anything`` — the generic guarded path the middleware
      protects on real installs.
    - ``/`` — listed in ``_PUBLIC_PATHS`` so the SPA shell loads
      without auth.
    - ``/assets/app.js`` — ``_PUBLIC_PREFIXES`` allowlist (the
      hashed bundles).
    - top-level content-hashed bundles (``/app.<hash>.js`` etc.) —
      the deploy-root assets the SPA needs before login.
    """
    app = web.Application(middlewares=[auth_middleware])
    app["device_builder"] = device_builder

    async def _ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/api/anything", _ok)
    app.router.add_get("/", _ok)
    app.router.add_get("/assets/app.js", _ok)
    app.router.add_get("/favicon.ico", _ok)
    app.router.add_get("/boards/images/esp32.png", _ok)
    app.router.add_route("OPTIONS", "/api/anything", _ok)
    # Top-level content-hashed bundles + sidecars served from the deploy root.
    for hashed in _HASHED_BUNDLES:
        app.router.add_get(hashed, _ok)
    # A hashed-looking asset nested under a deep-link path — must stay gated.
    app.router.add_get("/device/app.5ec0f3c42890e1a7.js", _ok)

    return app


# Representative top-level frontend bundles: entry script, a numbered lazy
# chunk, vendors, and the .js.map / .js.LICENSE.txt sidecars — all carry the
# ``.<hash>.`` segment and live at the deploy root, not under /assets/.
_HASHED_BUNDLES = (
    "/app.5ec0f3c42890e1a7.js",
    "/493.d5c6840fa646b2c4.js",
    "/vendors.25bbebc05765afee.js",
    "/app.5ec0f3c42890e1a7.js.map",
    "/app.5ec0f3c42890e1a7.js.LICENSE.txt",
)


def _basic_auth_header(username: str, password: str) -> str:
    creds = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(creds).decode("ascii")


# ---------------------------------------------------------------------------
# Short-circuit branches
# ---------------------------------------------------------------------------


async def test_auth_middleware_no_password_lets_request_through(
    aiohttp_client: AiohttpClient,
) -> None:
    """``using_password=False`` means auth is off entirely.

    Standalone deployments without USERNAME/PASSWORD should
    serve every endpoint without challenge. Pin the early
    return so a regression that broadens to "always require
    auth" surfaces here.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=False))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get("/api/anything")

    assert resp.status == 200
    assert await resp.text() == "ok"


async def test_auth_middleware_options_request_passes_through(
    aiohttp_client: AiohttpClient,
) -> None:
    """``OPTIONS`` (CORS preflight) is never auth-gated.

    The preflight contract pre-dates the request body; browsers
    don't include credentials on it. Gating ``OPTIONS`` would
    block every cross-origin request before the real auth check
    even gets to run.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.options("/api/anything")

    assert resp.status == 200


@pytest.mark.parametrize(
    "path",
    ["/", "/favicon.ico"],
    ids=["spa_shell", "favicon"],
)
async def test_auth_middleware_public_paths_pass_through(
    aiohttp_client: AiohttpClient,
    path: str,
) -> None:
    """``_PUBLIC_PATHS`` entries skip auth.

    The SPA shell, favicon, and manifest must load before the
    user has authenticated — that's how the login form gets a
    chance to render. Pin two representative entries; a
    regression that empties the set or drops one would fail here.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(path)

    assert resp.status == 200


@pytest.mark.parametrize(
    "path",
    ["/assets/app.js", "/boards/images/esp32.png"],
    ids=["assets", "boards_images"],
)
async def test_auth_middleware_public_prefixes_pass_through(
    aiohttp_client: AiohttpClient,
    path: str,
) -> None:
    """``_PUBLIC_PREFIXES`` entries skip auth.

    Hashed JS / CSS bundles under ``/assets/`` and the static
    board-image directory under ``/boards/images/`` must load
    pre-auth so the SPA shell can render the login form with
    its own JS + CSS. Both prefixes covered here.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(path)

    assert resp.status == 200


@pytest.mark.parametrize("path", _HASHED_BUNDLES)
async def test_auth_middleware_top_level_hashed_bundle_passes_through(
    aiohttp_client: AiohttpClient,
    path: str,
) -> None:
    """Top-level content-hashed bundles pass auth pre-login."""
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(path)

    assert resp.status == 200


async def test_auth_middleware_hashed_bundle_head_passes_through(
    aiohttp_client: AiohttpClient,
) -> None:
    """HEAD on a hashed bundle is allowed too (aiohttp auto-handles HEAD for GET)."""
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.head("/app.5ec0f3c42890e1a7.js")

    assert resp.status == 200


async def test_auth_middleware_unhashed_top_level_path_still_gated(
    aiohttp_client: AiohttpClient,
) -> None:
    """A top-level path with no hash segment stays gated."""
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get("/api/anything")

    assert resp.status == 401


async def test_auth_middleware_nested_hashed_path_still_gated(
    aiohttp_client: AiohttpClient,
) -> None:
    """A hashed asset under a deep-link path stays gated (top-level anchor)."""
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get("/device/app.5ec0f3c42890e1a7.js")

    assert resp.status == 401


async def test_auth_middleware_non_get_hashed_path_still_gated(
    aiohttp_client: AiohttpClient,
) -> None:
    """Only GET/HEAD bypass — a write method to a hashed path still needs auth."""

    async def _ok(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    app = _build_app(db)
    app.router.add_post("/app.5ec0f3c42890e1a7.js", _ok)
    client = await aiohttp_client(app)

    resp = await client.post("/app.5ec0f3c42890e1a7.js")

    assert resp.status == 401


# ---------------------------------------------------------------------------
# Auth happy paths
# ---------------------------------------------------------------------------


async def test_auth_middleware_accepts_valid_bearer_token(
    aiohttp_client: AiohttpClient,
) -> None:
    """A valid ``Bearer <token>`` lets the request through.

    Bearer is the preferred auth shape — set after a successful
    login via ``/auth``. Pin that the validated path lets the
    request through to the inner handler.
    """
    db = _StubDeviceBuilder(
        _StubSettings(using_password=True),
        _StubAuth(valid_tokens={"good-token"}),
    )
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": "Bearer good-token"},
    )

    assert resp.status == 200
    assert await resp.text() == "ok"


async def test_auth_middleware_rejects_invalid_bearer_token(
    aiohttp_client: AiohttpClient,
) -> None:
    """An unknown bearer token falls through to the 401 path.

    The middleware's bearer check is "token validates → pass";
    any other outcome (unknown token, revoked session, expired
    cookie) falls through and the unauth path runs.
    """
    db = _StubDeviceBuilder(
        _StubSettings(using_password=True),
        _StubAuth(valid_tokens={"good-token"}),
    )
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert resp.status == 401


async def test_auth_middleware_accepts_valid_basic_auth(
    aiohttp_client: AiohttpClient,
) -> None:
    """A correct ``Basic`` credential pair lets the request through.

    Basic is the fallback shape for clients that can't drive the
    bearer flow (curl scripts, simple HTTP libraries, the
    legacy HA REST integration before bearer support landed).
    """
    rate_limiter = _StubRateLimiter()
    db = _StubDeviceBuilder(
        _StubSettings(using_password=True, username="admin", password="hunter2"),
        _StubAuth(rate_limiter=rate_limiter),
    )
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": _basic_auth_header("admin", "hunter2")},
    )

    assert resp.status == 200
    # Successful auth clears any prior failure count for the IP.
    assert rate_limiter.cleared, "rate-limit clear must fire on success"
    assert rate_limiter.failures == []


async def test_auth_middleware_records_failure_on_wrong_basic_password(
    aiohttp_client: AiohttpClient,
) -> None:
    """Wrong basic password → 401 + ``record_failure(ip)`` for rate limiting.

    The rate limiter's per-IP counter is what powers the lockout
    branch tested below. Without ``record_failure`` here the
    counter never advances and an attacker could brute-force
    indefinitely.
    """
    rate_limiter = _StubRateLimiter()
    db = _StubDeviceBuilder(
        _StubSettings(using_password=True, username="admin", password="hunter2"),
        _StubAuth(rate_limiter=rate_limiter),
    )
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": _basic_auth_header("admin", "WRONG")},
    )

    assert resp.status == 401
    assert rate_limiter.failures, "wrong password must increment the failure counter"
    assert rate_limiter.cleared == []


async def test_auth_middleware_returns_lockout_message_when_rate_limited(
    aiohttp_client: AiohttpClient,
) -> None:
    """A locked-out IP gets a specific 401 — even with the right credentials.

    Pin the contract: once the rate limiter says "still in
    lockout", the middleware short-circuits with the
    ``Too many failed attempts`` message *without* incrementing
    the failure counter again. Otherwise a hammering attacker
    could keep the lockout alive indefinitely.
    """
    rate_limiter = _StubRateLimiter(lockout_remaining=42.0)
    db = _StubDeviceBuilder(
        _StubSettings(using_password=True, username="admin", password="hunter2"),
        _StubAuth(rate_limiter=rate_limiter),
    )
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": _basic_auth_header("admin", "hunter2")},
    )

    assert resp.status == 401
    assert "Too many failed attempts" in await resp.text()
    # No clear, no record — the lockout branch is purely
    # observational on the limiter.
    assert rate_limiter.cleared == []
    assert rate_limiter.failures == []


# ---------------------------------------------------------------------------
# Unauthorized response shape
# ---------------------------------------------------------------------------


async def test_auth_middleware_no_auth_header_returns_401_with_challenge(
    aiohttp_client: AiohttpClient,
) -> None:
    """No ``Authorization`` header → 401 with the dual-mode WWW-Authenticate.

    The challenge string names both ``Basic`` (for HTTP
    interactive clients) and ``Bearer`` (for the WS-style
    flow) so a curl-style caller can pick which to attempt.
    Pin the realm and both schemes — a regression that drops
    one arm of the challenge would silently break that
    client class.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get("/api/anything")

    assert resp.status == 401
    challenge = resp.headers.get("WWW-Authenticate", "")
    assert 'Basic realm="ESPHome Device Builder"' in challenge
    assert "Bearer" in challenge
    # Default body explains why.
    assert "Authentication required" in await resp.text()


async def test_auth_middleware_malformed_authorization_header_returns_401(
    aiohttp_client: AiohttpClient,
) -> None:
    """A header that's neither ``Bearer`` nor ``Basic`` falls through to 401.

    Defensive: a typo'd ``"Authorization: Token abc"`` or
    ``"Authorization: Bearer "`` (empty token) shouldn't be
    treated as authenticated. Both ``extract_bearer_token`` and
    ``parse_basic_auth`` return ``None`` for unrecognised shapes,
    so the middleware lands at the unauth fallthrough.
    """
    db = _StubDeviceBuilder(_StubSettings(using_password=True))
    client = await aiohttp_client(_build_app(db))

    resp = await client.get(
        "/api/anything",
        headers={"Authorization": "Token abc"},
    )

    assert resp.status == 401


# ---------------------------------------------------------------------------
# Real route table — gated-by-default guard
# ---------------------------------------------------------------------------

# Plain routes the real app intends to be reachable without credentials:
# / and the SPA catch-all serve the public shell, /ws authenticates in-band,
# /version is the healthcheck, /api/firmware/download carries its own token.
# Static dirs (/assets, /boards/images) and the catch-all are non-plain
# resources and are skipped (public frontend surfaces).
_PUBLIC_PLAIN_ROUTES = frozenset({"/", "/ws", "/version", "/api/firmware/download"})


async def test_create_app_gates_every_non_public_route(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """Every plain route in the real ``create_app`` is auth-gated unless allowlisted.

    Drives the actual route table (not a stub) so a new route landing in
    ``_PUBLIC_PATHS`` or matching the hashed-asset bypass by accident fails
    here; a deliberately-public route must be added to
    ``_PUBLIC_PLAIN_ROUTES`` to pass.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()
    settings.using_password = True
    # No auth controller needed: with no Authorization header the middleware
    # 401s before touching db.auth, and before reaching any handler.
    app = DeviceBuilder(settings).create_app(with_lifecycle=False)
    client = await aiohttp_client(app)

    gated: set[str] = set()
    for route in app.router.routes():
        canonical = route.resource.canonical
        if type(route.resource).__name__ != "PlainResource":
            continue
        if canonical in _PUBLIC_PLAIN_ROUTES:
            continue
        resp = await client.request(route.method, canonical)
        assert resp.status == 401, f"{route.method} {canonical} is reachable without auth"
        gated.add(canonical)

    # The sensitive legacy REST surface must have actually been exercised —
    # guards against the loop silently skipping everything.
    assert gated >= {"/devices", "/json-config", "/compile", "/upload"}
