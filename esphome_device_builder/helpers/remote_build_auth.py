"""
Remote-build receiver auth helpers.

Phase 3b2 of issue #106: bearer-token validation middleware for
the ``/remote-build/v1/*`` route group. Tokens are minted by
phase 3b1's :mod:`controllers.remote_build` token CRUD and
matched against the offloader's
``Authorization: Bearer {token_id}.{secret}`` header.

Verification model:

* The wire bearer is split on the first ``.``: the left half is
  the lookup key (``token_id``), the right half is the secret.
* The presented secret is SHA-256 hashed and compared to the
  stored ``secret_sha256`` via :func:`hmac.compare_digest` — the
  comparison is constant-time so an attacker can't side-channel
  the secret out of timing differences.
* SHA-256 of the presented secret is computed unconditionally
  (even on unknown ``token_id``), so the timing of "unknown
  token" matches "known token, wrong secret".

A per-IP :class:`helpers.auth.RateLimiter` wraps the validator;
failed attempts trigger a 429 with ``Retry-After`` rather than a
401, giving a probing scanner a clear "stop" signal and bounding
log-spam from tight retry loops.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

from ..models import StoredToken
from .auth import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable


_LOGGER = logging.getLogger(__name__)


# Per-IP rate limit on FAILED bearer attempts. 256-bit secrets make
# online brute force infeasible regardless, but the limiter closes
# off log-spam and side-channel timing reconnaissance. Tunable via
# the ``rate_limiter`` argument to ``make_remote_build_auth_middleware``.
_RATE_LIMIT_MAX_ATTEMPTS = 10
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_LOCKOUT_SECONDS = 300.0


# Stored ``secret_sha256`` is lowercase hex of SHA-256, so 64 chars.
# Used as the constant-time placeholder when the token_id misses,
# to keep "unknown token" indistinguishable from "wrong secret"
# under timing analysis.
_DUMMY_HASH = "0" * 64


def _parse_bearer_credentials(auth_header: str | None) -> tuple[str, str] | None:
    """
    Split ``Authorization: Bearer {token_id}.{secret}`` into ``(id, secret)``.

    Returns ``None`` for any malformed header (missing, wrong
    scheme, no dot, empty halves). RFC 7235 §2.1 makes the scheme
    case-insensitive and RFC 7230 §3.2.3 allows BWS (space / tab)
    between scheme and credentials; ``str.split(None, 1)``
    collapses any whitespace run into the single delimiter.
    """
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    bearer = parts[1].strip()
    if "." not in bearer:
        return None
    token_id, _, secret = bearer.partition(".")
    if not token_id or not secret:
        return None
    return token_id, secret


def verify_bearer(
    auth_header: str | None,
    lookup: Callable[[str], StoredToken | None],
) -> StoredToken | None:
    """
    Parse a bearer header, return the matching :class:`StoredToken` or ``None``.

    Returns ``None`` for any failure mode (missing header, wrong
    scheme, malformed wire form, unknown ``token_id``, hash
    mismatch). The caller decides 401 vs 429 vs success at a
    layer above this function.

    The hash compute happens unconditionally so an attacker
    timing the response can't distinguish "no such token_id"
    from "wrong secret".
    """
    parsed = _parse_bearer_credentials(auth_header)
    if parsed is None:
        return None
    token_id, secret = parsed
    # Encode as UTF-8 (never raises) rather than ASCII (would
    # raise ``UnicodeEncodeError`` on a malformed header carrying
    # non-ASCII bytes, turning a 401 into a 500). Genuine bearers
    # come from ``secrets.token_urlsafe`` and are pure ASCII; an
    # attacker-supplied non-ASCII bearer just produces a hash
    # that doesn't match anything stored.
    presented_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    stored = lookup(token_id)
    if stored is None:
        # Burn the same compare_digest cost the success path
        # would so timing can't leak token_id existence.
        hmac.compare_digest(_DUMMY_HASH, presented_hash)
        return None
    if not hmac.compare_digest(stored.secret_sha256, presented_hash):
        return None
    return stored


def make_remote_build_auth_middleware(
    lookup: Callable[[str], StoredToken | None],
    rate_limiter: RateLimiter | None = None,
) -> _Callable:
    """
    Build the aiohttp middleware that gates ``/remote-build/v1/*``.

    *lookup* is the ``token_id -> StoredToken`` accessor
    (typically the controller's in-memory index built from the
    on-disk token list at startup and refreshed on every CRUD
    mutation).

    *rate_limiter* defaults to a fresh per-instance
    :class:`helpers.auth.RateLimiter` with the module-level
    constants; tests can pass a custom instance to drive
    threshold-specific assertions. The limiter records FAILED
    bearer attempts only — a successful auth does not "clear"
    the IP because there's no notion of "this peer is
    trustworthy now"; per-pairing trust is the binding step in
    phase 3b3.

    On 401 / 429 the middleware emits a warning log line with
    the peer IP and the request path so an operator hunting
    "why is my offloader getting kicked" has a paper trail.
    Successful auth doesn't log (would spam the dashboard's log
    on every build status poll); the audit-log shape for
    successful requests lands in phase 3b2's first real RPC,
    not the always-on middleware.
    """
    limiter = rate_limiter or RateLimiter(
        max_attempts=_RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        lockout_seconds=_RATE_LIMIT_LOCKOUT_SECONDS,
    )

    @web.middleware
    async def middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        peer_ip = request.remote or "?"
        locked = limiter.remaining_lockout(peer_ip)
        if locked > 0:
            _LOGGER.warning(
                "Remote-build auth: %s locked out for %.0fs (path=%s)",
                peer_ip,
                locked,
                request.path,
            )
            return web.Response(
                status=429,
                text="rate limited",
                headers={"Retry-After": str(int(locked) + 1)},
            )
        token = verify_bearer(request.headers.get("Authorization"), lookup)
        if token is None:
            limiter.record_failure(peer_ip)
            _LOGGER.warning(
                "Remote-build auth: rejected request from %s (path=%s)",
                peer_ip,
                request.path,
            )
            return web.Response(
                status=401,
                text="unauthorized",
                headers={"WWW-Authenticate": 'Bearer realm="remote-build"'},
            )
        # Stash the matched token on the request for the handler
        # / phase 3b3's first-use binding to consume.
        request["remote_build_token"] = token
        return await handler(request)

    return middleware
