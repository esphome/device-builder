"""Tests for the phase-3b2 remote-build bearer auth middleware."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from esphome_device_builder.helpers.auth import RateLimiter
from esphome_device_builder.helpers.remote_build_auth import (
    BindingMismatch,
    make_remote_build_auth_middleware,
    verify_bearer,
)
from esphome_device_builder.models import StoredToken

_DEFAULT_ID = "tid12345"
_DEFAULT_SECRET = "the-cleartext-secret"


def _stored(token_id: str = _DEFAULT_ID, secret: str = _DEFAULT_SECRET) -> StoredToken:
    """Build a ``StoredToken`` whose hash matches *secret*."""
    return StoredToken(
        token_id=token_id,
        label="Green",
        secret_sha256=hashlib.sha256(secret.encode("ascii")).hexdigest(),
        created_at=1.0,
    )


def _table_lookup(rows: list[StoredToken]) -> Callable[[str], StoredToken | None]:
    """Build a lookup callable from a list of stored tokens."""
    return {t.token_id: t for t in rows}.get


# ---------------------------------------------------------------------------
# verify_bearer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("token-without-scheme", id="no-scheme"),
        pytest.param("Basic dXNlcjpwYXNz", id="wrong-scheme"),
        pytest.param("Bearer ", id="bearer-empty"),
        pytest.param("Bearer no-dot-separator", id="no-dot"),
        pytest.param("Bearer .secret-only", id="empty-id"),
        pytest.param("Bearer token-id-only.", id="empty-secret"),
    ],
)
def test_verify_bearer_rejects_malformed_headers(header: str | None) -> None:
    """Headers that don't carry a parseable ``{id}.{secret}`` return ``None``."""
    stored = _stored()
    assert verify_bearer(header, _table_lookup([stored])) is None


def test_verify_bearer_rejects_unknown_token_id() -> None:
    """A bearer with an unknown ``token_id`` half returns ``None``."""
    stored = _stored(token_id="known", secret="s")
    assert verify_bearer("Bearer unknown.s", _table_lookup([stored])) is None


def test_verify_bearer_rejects_wrong_secret() -> None:
    """Right ``token_id``, wrong secret returns ``None``."""
    stored = _stored(token_id="known", secret="right-secret")
    assert verify_bearer("Bearer known.wrong-secret", _table_lookup([stored])) is None


def test_verify_bearer_returns_token_on_match() -> None:
    """A valid bearer returns the matching ``StoredToken``."""
    stored = _stored(token_id="known", secret="right-secret")
    matched = verify_bearer("Bearer known.right-secret", _table_lookup([stored]))
    assert matched is stored


@pytest.mark.parametrize(
    "header",
    [
        pytest.param("bearer known.right-secret", id="lowercase"),
        pytest.param("BEARER known.right-secret", id="uppercase"),
        pytest.param("BeArEr known.right-secret", id="mixed-case"),
        pytest.param("Bearer\tknown.right-secret", id="tab-delimited"),
        pytest.param("Bearer  known.right-secret", id="double-space"),
    ],
)
def test_verify_bearer_accepts_case_insensitive_scheme_and_bws(header: str) -> None:
    """RFC 7235 §2.1 + RFC 7230 §3.2.3: scheme is case-insensitive, BWS allowed."""
    stored = _stored(token_id="known", secret="right-secret")
    matched = verify_bearer(header, _table_lookup([stored]))
    assert matched is stored


def test_verify_bearer_handles_non_ascii_secret_without_raising() -> None:
    """
    A non-ASCII secret half is rejected as 401, not 500.

    A genuine bearer is base64url (always ASCII), so a non-ASCII
    payload is either a malformed client or an attacker's probe.
    The pre-fix code did ``secret.encode("ascii")`` which raised
    ``UnicodeEncodeError`` and turned the auth failure into a
    500. Pin the rejection-not-crash contract.
    """
    stored = _stored(token_id="known", secret="right-secret")
    # ``é`` is non-ASCII; would have raised under the old encode("ascii").
    matched = verify_bearer("Bearer known.café", _table_lookup([stored]))
    assert matched is None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


_DEFAULT_DASHBOARD_ID = "green-dashboard-id"


async def _hit_middleware(
    middleware: Any,
    *,
    auth_header: str | None = None,
    dashboard_id: str | None = _DEFAULT_DASHBOARD_ID,
    peer_ip: str = "10.0.0.42",
) -> web.StreamResponse:
    """
    Drive the middleware against a fake request, returning the response.

    Wraps a noop downstream handler that returns 200 so we can
    distinguish "middleware allowed through" (200 from the handler)
    from "middleware short-circuited" (whatever it returned).

    *dashboard_id* defaults to a non-empty value so tests that
    only care about bearer-side behaviour don't have to spell
    out the binding header. Pass ``None`` explicitly to test
    the missing-header path.
    """
    headers: dict[str, str] = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    if dashboard_id is not None:
        headers["X-Dashboard-ID"] = dashboard_id
    request = make_mocked_request(
        "GET", "/remote-build/v1/health", headers=headers, client_max_size=0
    )
    request._transport_peername = (peer_ip, 12345)  # used by request.remote

    async def _noop(req: web.Request) -> web.StreamResponse:
        return web.Response(status=200, text="ok")

    return await middleware(request, _noop)


@pytest.mark.asyncio
async def test_middleware_401_without_bearer() -> None:
    """No ``Authorization`` header → 401 with ``WWW-Authenticate``."""
    middleware = make_remote_build_auth_middleware(_table_lookup([_stored()]))
    response = await _hit_middleware(middleware)
    assert response.status == 401
    assert response.headers.get("WWW-Authenticate", "").startswith("Bearer ")


@pytest.mark.asyncio
async def test_middleware_401_with_bad_bearer() -> None:
    """Wrong secret → 401."""
    stored = _stored(token_id="abc", secret="right")
    middleware = make_remote_build_auth_middleware(_table_lookup([stored]))
    response = await _hit_middleware(middleware, auth_header="Bearer abc.wrong")
    assert response.status == 401


@pytest.mark.asyncio
async def test_middleware_200_with_good_bearer_and_stashes_token() -> None:
    """A valid bearer + dashboard_id reaches the handler and stashes the token."""
    stored = _stored(token_id="abc", secret="right")

    received: dict[str, Any] = {}

    async def _spy_handler(request: web.Request) -> web.StreamResponse:
        received["token"] = request.get("remote_build_token")
        return web.Response(status=200, text="ok")

    # No bind callback → middleware treats unbound tokens as
    # already-matching (test-only convenience; the production
    # callback in 3b3 persists the binding atomically).
    auth = make_remote_build_auth_middleware(_table_lookup([stored]))

    request = make_mocked_request(
        "GET",
        "/remote-build/v1/health",
        headers={
            "Authorization": "Bearer abc.right",
            "X-Dashboard-ID": _DEFAULT_DASHBOARD_ID,
        },
        client_max_size=0,
    )
    request._transport_peername = ("10.0.0.42", 12345)
    response = await auth(request, _spy_handler)
    assert response.status == 200
    assert received["token"] is stored


@pytest.mark.asyncio
async def test_middleware_429_after_rate_limit_lockout() -> None:
    """
    Repeated bad-bearer attempts from one IP get locked out with 429.

    Pin the limiter at a tiny threshold so the test doesn't have
    to hammer the middleware to trigger the lockout.
    """
    limiter = RateLimiter(max_attempts=2, window_seconds=60.0, lockout_seconds=300.0)
    middleware = make_remote_build_auth_middleware(
        _table_lookup([_stored(token_id="abc", secret="right")]),
        rate_limiter=limiter,
    )

    # Two failed attempts → IP gets locked out.
    for _ in range(2):
        response = await _hit_middleware(middleware, auth_header="Bearer abc.wrong")
        assert response.status == 401

    # Next attempt is short-circuited with 429.
    response = await _hit_middleware(middleware, auth_header="Bearer abc.wrong")
    assert response.status == 429
    assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_middleware_rate_limit_per_ip() -> None:
    """
    A different source IP isn't punished for another IP's failures.

    Pin that the limiter is keyed off ``request.remote`` and not a
    process-wide counter.
    """
    limiter = RateLimiter(max_attempts=2, window_seconds=60.0, lockout_seconds=300.0)
    middleware = make_remote_build_auth_middleware(
        _table_lookup([_stored(token_id="abc", secret="right")]),
        rate_limiter=limiter,
    )

    # Attacker IP burns through the quota.
    for _ in range(3):
        await _hit_middleware(middleware, auth_header="Bearer abc.wrong", peer_ip="1.2.3.4")

    # Honest peer with a valid bearer still gets through.
    response = await _hit_middleware(
        middleware, auth_header="Bearer abc.right", peer_ip="10.0.0.42"
    )
    assert response.status == 200


# ---------------------------------------------------------------------------
# First-use binding (phase 3b3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header_value",
    [
        pytest.param(None, id="missing"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("has spaces", id="non-base64url-chars"),
        pytest.param("has\x00null", id="control-chars"),
        pytest.param("x" * 100, id="overlong"),
    ],
)
@pytest.mark.asyncio
async def test_middleware_400_when_dashboard_id_missing_or_malformed(
    header_value: str | None,
) -> None:
    """Bearer valid but X-Dashboard-ID missing / malformed → 400."""
    stored = _stored(token_id="abc", secret="right")
    auth = make_remote_build_auth_middleware(_table_lookup([stored]))
    response = await _hit_middleware(
        auth, auth_header="Bearer abc.right", dashboard_id=header_value
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_middleware_400_path_is_rate_limited() -> None:
    """
    The 400-on-malformed-X-Dashboard-ID path counts against the rate limiter.

    A 400 confirms the bearer is valid (the 401 path comes
    earlier), so a peer holding a stolen valid bearer could
    otherwise probe the binding surface unlimited times. The
    rate limiter caps the probe rate at the same threshold bad
    bearers face.
    """
    stored = _stored(token_id="abc", secret="right")
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0, lockout_seconds=300.0)
    auth = make_remote_build_auth_middleware(
        _table_lookup([stored]),
        rate_limiter=limiter,
    )

    # Burn three attempts on the 400 path (valid bearer, no
    # X-Dashboard-ID header).
    for _ in range(3):
        response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id=None)
        assert response.status == 400

    # Fourth attempt — same valid bearer, same missing header —
    # gets 429 instead of 400 because the IP is now locked out.
    response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id=None)
    assert response.status == 429
    assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_middleware_binds_token_on_first_use_and_passes() -> None:
    """First authenticated request persists the binding and reaches the handler."""
    stored = _stored(token_id="abc", secret="right")

    persisted: list[tuple[str, str]] = []

    async def _bind(token_id: str, dashboard_id: str) -> StoredToken:
        persisted.append((token_id, dashboard_id))
        return StoredToken(
            token_id=stored.token_id,
            label=stored.label,
            secret_sha256=stored.secret_sha256,
            created_at=stored.created_at,
            bound_dashboard_id=dashboard_id,
        )

    auth = make_remote_build_auth_middleware(_table_lookup([stored]), bind_first_use=_bind)
    response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id="green-1")
    assert response.status == 200
    assert persisted == [("abc", "green-1")]


@pytest.mark.asyncio
async def test_middleware_passes_when_dashboard_id_matches_existing_binding() -> None:
    """Second request from the same offloader reaches the handler (no rebind)."""
    stored = StoredToken(
        token_id="abc",
        label="Green",
        secret_sha256=hashlib.sha256(b"right").hexdigest(),
        created_at=1.0,
        bound_dashboard_id="green-1",
    )

    persisted: list[tuple[str, str]] = []

    async def _bind(token_id: str, dashboard_id: str) -> StoredToken:
        persisted.append((token_id, dashboard_id))
        return stored

    auth = make_remote_build_auth_middleware(_table_lookup([stored]), bind_first_use=_bind)
    response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id="green-1")
    assert response.status == 200
    # Already bound — bind callback NOT called.
    assert persisted == []


@pytest.mark.asyncio
async def test_middleware_403_when_dashboard_id_mismatches_binding() -> None:
    """Stolen-bearer scenario: same token, different dashboard_id → 403."""
    stored = StoredToken(
        token_id="abc",
        label="Green",
        secret_sha256=hashlib.sha256(b"right").hexdigest(),
        created_at=1.0,
        bound_dashboard_id="green-1",
    )

    mismatch_calls: list[BindingMismatch] = []

    auth = make_remote_build_auth_middleware(
        _table_lookup([stored]),
        on_binding_mismatch=mismatch_calls.append,
    )
    response = await _hit_middleware(
        auth,
        auth_header="Bearer abc.right",
        dashboard_id="laptop-2",  # different from bound "green-1"
    )
    assert response.status == 403
    # ``race_loss=False`` because the token was already bound
    # before this request — the more-suspicious case.
    assert mismatch_calls == [
        BindingMismatch(
            token_id="abc",
            presented_dashboard_id="laptop-2",
            bound_dashboard_id="green-1",
            peer_ip="10.0.0.42",
            race_loss=False,
        )
    ]


@pytest.mark.asyncio
async def test_middleware_403_when_first_use_bind_loses_race() -> None:
    """
    Concurrent first-use: loser observes a different binding → 403 + event.

    Two offloaders race on a fresh token. The first wins the
    metadata transaction and persists its dashboard_id. The
    second's ``bind_first_use`` returns a token bound to the
    winner's id; the middleware sees the mismatch and 403s.
    """
    stored = _stored(token_id="abc", secret="right")
    winner = "first-offloader"
    loser = "second-offloader"

    async def _bind(token_id: str, dashboard_id: str) -> StoredToken:
        # Winner already wrote; loser's call returns the
        # winner-bound token.
        return StoredToken(
            token_id=stored.token_id,
            label=stored.label,
            secret_sha256=stored.secret_sha256,
            created_at=stored.created_at,
            bound_dashboard_id=winner,
        )

    mismatch_calls: list[BindingMismatch] = []

    auth = make_remote_build_auth_middleware(
        _table_lookup([stored]),
        bind_first_use=_bind,
        on_binding_mismatch=mismatch_calls.append,
    )
    response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id=loser)
    assert response.status == 403
    # ``race_loss=True``: the second offloader lost the
    # concurrent first-use bind. The Settings UI uses this to
    # soften the wording (likely a paste-into-two mistake).
    assert mismatch_calls == [
        BindingMismatch(
            token_id="abc",
            presented_dashboard_id=loser,
            bound_dashboard_id=winner,
            peer_ip="10.0.0.42",
            race_loss=True,
        )
    ]


@pytest.mark.asyncio
async def test_middleware_403_when_token_removed_during_bind() -> None:
    """A token revoked between verify and bind → 403."""
    stored = _stored(token_id="abc", secret="right")

    async def _bind(token_id: str, dashboard_id: str) -> StoredToken | None:
        return None  # token is gone

    auth = make_remote_build_auth_middleware(_table_lookup([stored]), bind_first_use=_bind)
    response = await _hit_middleware(auth, auth_header="Bearer abc.right", dashboard_id="green-1")
    assert response.status == 403
