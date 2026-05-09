"""
Remote-build receiver auth helpers.

Bearer-token validation + first-use binding middleware for the
``/remote-build/v1/*`` route group. Tokens are minted by
:mod:`controllers.remote_build` token CRUD and matched against
the offloader's ``Authorization: Bearer {token_id}.{secret}``
header.

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

First-use binding (phase 3b3):

* Every authenticated request must carry an ``X-Dashboard-ID``
  header (the offloader's stable installation identifier from
  phase 3a's identity helper). Missing header → 400.
* On the first authenticated request for a token whose
  ``bound_dashboard_id`` is ``None``, the middleware persists
  the presented header value via the controller's atomic
  binding write.
* On subsequent requests with a mismatched header → 403 (NOT
  401: the token IS valid; the peer is wrong). A binding
  callback fires so the receiver Settings UI can surface the
  attempt to the operator.

A per-IP :class:`helpers.auth.RateLimiter` wraps the validator;
failed attempts trigger a 429 with ``Retry-After`` rather than a
401, giving a probing scanner a clear "stop" signal and bounding
log-spam from tight retry loops.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiohttp import web

from ..models import StoredToken
from .auth import RateLimiter

if TYPE_CHECKING:
    from collections.abc import Callable as _Callable


@dataclass(frozen=True, kw_only=True, slots=True)
class BindingMismatch:
    """
    Payload for the ``on_binding_mismatch`` callback.

    Bundled into a dataclass instead of a long positional
    argument list so callers don't have to remember the order
    and so future fields can be added without touching every
    call site. ``race_loss`` distinguishes a concurrent first-
    use bind that lost the race (likely an operator pasting
    the cleartext into two offloaders, ``True``) from a hit on
    an already-bound token (more suspicious; points at a
    stolen bearer, ``False``).
    """

    token_id: str
    presented_dashboard_id: str
    bound_dashboard_id: str
    peer_ip: str
    race_loss: bool


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

# Header carrying the offloader's stable ``dashboard_id`` (from
# phase 3a's identity helper). Used by phase 3b3's first-use
# binding to record which peer first authenticated with a given
# token; later requests with a mismatched id are rejected as 403.
_DASHBOARD_ID_HEADER = "X-Dashboard-ID"

# ``dashboard_id`` is generated via ``secrets.token_urlsafe(24)``
# in phase 3a, so 32 base64url chars. Cap the accepted length at
# 64 to defend against a runaway header carrying megabytes; the
# pattern (base64url chars only) catches probes carrying control
# bytes / non-printables.
_DASHBOARD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_DASHBOARD_ID_MAX_CHARS = 64


def _validate_dashboard_id(raw: str | None) -> str | None:
    """Return the trimmed value if the header parses cleanly, else ``None``."""
    if not raw:
        return None
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > _DASHBOARD_ID_MAX_CHARS:
        return None
    if not _DASHBOARD_ID_PATTERN.fullmatch(trimmed):
        return None
    return trimmed


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
    *,
    bind_first_use: Callable[[str, str], Awaitable[StoredToken | None]] | None = None,
    on_binding_mismatch: Callable[[BindingMismatch], None] | None = None,
    rate_limiter: RateLimiter | None = None,
) -> _Callable:
    """
    Build the aiohttp middleware that gates ``/remote-build/v1/*``.

    *lookup* is the ``token_id -> StoredToken`` accessor
    (typically the controller's in-memory index built from the
    on-disk token list at startup and refreshed on every CRUD
    mutation).

    *bind_first_use* is the async callback the middleware invokes
    when an authenticated request arrives for a token whose
    ``bound_dashboard_id`` is ``None``. Returns the post-write
    :class:`StoredToken` (with the binding applied) or ``None``
    if the token has been removed. The callback owns the disk
    I/O so the middleware stays loop-bound on the read path.
    Required for phase 3b3 enforcement; tests pass ``None`` to
    exercise the auth gate alone.

    *on_binding_mismatch* fires when an authenticated request's
    ``X-Dashboard-ID`` doesn't match the token's already-bound
    value, OR when a first-use bind raced against a concurrent
    bind that won with a different id. Receives a
    :class:`BindingMismatch` carrying the offending token id,
    the presented and bound dashboard ids, the peer IP, and a
    ``race_loss`` flag. ``race_loss=True`` when the mismatch
    happened during a first-use bind (concurrent
    legitimate-paste-into-two-offloaders is a likely cause);
    ``False`` when the token was already bound (more
    suspicious; points at a stolen-bearer or
    operator-paste-into-wrong-machine attempt). The Settings
    UI uses the flag to soften the wording on the race-loss
    case while keeping the mismatch path loud.

    *rate_limiter* defaults to a fresh per-instance
    :class:`helpers.auth.RateLimiter` with the module-level
    constants; tests can pass a custom instance to drive
    threshold-specific assertions. The limiter records both
    failed bearer attempts (401) and the 400 path for missing
    / malformed ``X-Dashboard-ID``. The 400 path counts
    because a 400 response confirms the bearer was valid;
    without rate-limiting it, a peer holding a stolen bearer
    could probe the binding surface unlimited times. The 403
    binding-mismatch path does NOT record a failure because
    that path fires the ``on_binding_mismatch`` event, which
    the receiver Settings UI surfaces to the operator;
    rate-limiting it would mask the alert under the same
    threshold as routine bad bearers. Successful auth does
    NOT clear the IP — there's no notion of "this peer is
    trustworthy now"; per-pairing trust is the binding step
    itself.

    On 401 / 429 / 403 the middleware emits a warning log line
    with the peer IP and the request path so an operator hunting
    "why is my offloader getting kicked" has a paper trail.
    Successful auth doesn't log (would spam the dashboard's log
    on every build status poll); the audit-log shape for
    successful requests lands in the first real RPC.
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

        # First-use binding: every authenticated request must
        # carry an ``X-Dashboard-ID`` header naming the
        # offloader's installation. By this point the bearer
        # has already passed ``verify_bearer`` above, so a 401
        # here would be wrong — the credentials WERE accepted.
        # A 400 accurately says "your bearer was valid but the
        # request shape is malformed (missing / bad header)";
        # the offloader's UI can render that as "you're using
        # an old client that doesn't send X-Dashboard-ID" rather
        # than the misleading "credentials rejected".
        presented_dashboard_id = _validate_dashboard_id(request.headers.get(_DASHBOARD_ID_HEADER))
        if presented_dashboard_id is None:
            # The 400 path leaks "this bearer was valid" (vs the
            # 401 path's "this bearer was rejected"). A peer
            # holding a stolen valid bearer could otherwise
            # probe the binding surface unlimited times to find
            # the right ``X-Dashboard-ID`` value. Count
            # malformed-header attempts against the same per-IP
            # limiter so the probe rate is capped at the same
            # 10/60s/IP threshold bad bearers face. Legitimate
            # clients always send the header, never hit this.
            limiter.record_failure(peer_ip)
            _LOGGER.warning(
                "Remote-build auth: missing / malformed X-Dashboard-ID from %s "
                "(path=%s, token_id=%s)",
                peer_ip,
                request.path,
                token.token_id,
            )
            return web.Response(
                status=400,
                text="missing or malformed X-Dashboard-ID",
            )

        token = await _resolve_binding(
            token,
            presented_dashboard_id,
            bind_first_use=bind_first_use,
            on_binding_mismatch=on_binding_mismatch,
            peer_ip=peer_ip,
        )
        if token is None:
            return web.Response(status=403, text="dashboard_id mismatch")

        # Stash the matched + binding-checked token on the
        # request for the handler.
        request["remote_build_token"] = token
        return await handler(request)

    return middleware


async def _resolve_binding(
    token: StoredToken,
    presented_dashboard_id: str,
    *,
    bind_first_use: Callable[[str, str], Awaitable[StoredToken | None]] | None,
    on_binding_mismatch: Callable[[BindingMismatch], None] | None,
    peer_ip: str,
) -> StoredToken | None:
    """
    Authorize *token* against *presented_dashboard_id* under the binding contract.

    Returns the (possibly newly-bound) :class:`StoredToken` on
    success, or ``None`` on mismatch (caller turns into 403).

    Three cases:

    1. ``token.bound_dashboard_id is None`` and *bind_first_use*
       is supplied: persist the binding atomically. The callback
       returns the post-write token; if a concurrent first-use
       bound to a different id (race-loss), the returned
       ``bound_dashboard_id`` won't match what we presented and
       we treat it as a mismatch.
    2. ``token.bound_dashboard_id`` matches *presented_dashboard_id*:
       allow.
    3. ``token.bound_dashboard_id`` is set and doesn't match:
       reject. The mismatch callback fires so the Settings UI
       can surface the attempt.

    When *bind_first_use* is ``None`` (test-only callers /
    pre-3b3 wiring), an unbound token is treated as already
    matching the presented id — useful for unit tests of the
    earlier auth surface that don't care about binding.
    """
    if token.bound_dashboard_id is None:
        if bind_first_use is None:
            # Unwired binding callback (tests). Treat as
            # success without persisting.
            return token
        bound = await bind_first_use(token.token_id, presented_dashboard_id)
        if bound is None:
            # Token was removed between verify and bind.
            _LOGGER.warning(
                "Remote-build auth: token %s was removed during first-use bind from %s",
                token.token_id,
                peer_ip,
            )
            return None
        if bound.bound_dashboard_id != presented_dashboard_id:
            # Race-loss: a concurrent first-use bound to a
            # different id. Treat as mismatch.
            _LOGGER.warning(
                "Remote-build auth: first-use bind race lost for token %s "
                "from %s (presented=%s, bound=%s)",
                token.token_id,
                peer_ip,
                presented_dashboard_id,
                bound.bound_dashboard_id,
            )
            if on_binding_mismatch is not None:
                # ``race_loss=True``: this mismatch was a
                # concurrent first-use bind that lost the race;
                # the most-likely cause is an operator pasting
                # the cleartext into two offloaders by mistake,
                # not a stolen-bearer attempt. The flag lets the
                # Settings UI soften the wording (and avoid
                # firing a security alert) on the first event.
                on_binding_mismatch(
                    BindingMismatch(
                        token_id=token.token_id,
                        presented_dashboard_id=presented_dashboard_id,
                        bound_dashboard_id=bound.bound_dashboard_id or "",
                        peer_ip=peer_ip,
                        race_loss=True,
                    )
                )
            return None
        return bound

    if token.bound_dashboard_id != presented_dashboard_id:
        _LOGGER.warning(
            "Remote-build auth: dashboard_id mismatch for token %s from %s "
            "(presented=%s, bound=%s)",
            token.token_id,
            peer_ip,
            presented_dashboard_id,
            token.bound_dashboard_id,
        )
        if on_binding_mismatch is not None:
            # ``race_loss=False``: the token was already bound
            # before this request arrived. Distinct from the
            # race-loss case above; points at a stolen-bearer
            # attempt OR an operator pasting into the wrong
            # machine after the binding stuck. The Settings UI
            # treats this as the loud / suspicious case.
            on_binding_mismatch(
                BindingMismatch(
                    token_id=token.token_id,
                    presented_dashboard_id=presented_dashboard_id,
                    bound_dashboard_id=token.bound_dashboard_id,
                    peer_ip=peer_ip,
                    race_loss=False,
                )
            )
        return None
    return token
