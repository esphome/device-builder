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
    by_id = {t.token_id: t for t in rows}

    def _lookup(token_id: str) -> StoredToken | None:
        return by_id.get(token_id)

    return _lookup


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


async def _hit_middleware(
    middleware: Any,
    *,
    auth_header: str | None = None,
    peer_ip: str = "10.0.0.42",
) -> web.StreamResponse:
    """
    Drive the middleware against a fake request, returning the response.

    Wraps a noop downstream handler that returns 200 so we can
    distinguish "middleware allowed through" (200 from the handler)
    from "middleware short-circuited" (whatever it returned).
    """
    headers: dict[str, str] = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
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
    """A valid bearer reaches the handler and stashes the token on the request."""
    stored = _stored(token_id="abc", secret="right")

    received: dict[str, Any] = {}

    async def _spy_handler(request: web.Request) -> web.StreamResponse:
        received["token"] = request.get("remote_build_token")
        return web.Response(status=200, text="ok")

    auth = make_remote_build_auth_middleware(_table_lookup([stored]))

    request = make_mocked_request(
        "GET",
        "/remote-build/v1/health",
        headers={"Authorization": "Bearer abc.right"},
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
