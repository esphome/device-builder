"""Authentication helpers — session store, rate limiter, REST middleware."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

_SESSIONS_FILENAME = ".device-builder-sessions.json"
_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days, sliding
_TOKEN_BYTES = 32  # 256 bits of entropy

# 10 failed attempts in 5 minutes locks the IP for 5 minutes.
_RATE_LIMIT_MAX_ATTEMPTS = 10
_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
_RATE_LIMIT_LOCKOUT_SECONDS = 5 * 60


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> bytes:
    """Hash a password into a fixed-size digest for constant-time comparison."""
    # SHA-256 is sufficient because the digest is only compared against a
    # single in-memory value via hmac.compare_digest — never persisted.
    return hashlib.sha256(password.encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """A persisted authentication session."""

    token: str
    created_at: float
    last_used_at: float
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        """Return True if the session has passed its expiry timestamp."""
        return (now if now is not None else time.time()) >= self.expires_at


class SessionStore:
    """
    Persistent store of opaque session tokens.

    Tokens auto-expire after ``ttl_seconds`` of inactivity — the
    expiry is a sliding window refreshed on each ``validate`` call.
    Persisted to a JSON file in the config directory so sessions
    survive server restarts.
    """

    def __init__(self, config_dir: Path, ttl_seconds: int = _TOKEN_TTL_SECONDS) -> None:
        self._path = config_dir / _SESSIONS_FILENAME
        self._ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._load()

    async def create(self) -> Session:
        """Mint a new session and return it."""
        async with self._lock:
            now = time.time()
            session = Session(
                token=secrets.token_urlsafe(_TOKEN_BYTES),
                created_at=now,
                last_used_at=now,
                expires_at=now + self._ttl,
            )
            self._sessions[session.token] = session
            self._persist()
            return session

    async def validate(self, token: str) -> Session | None:
        """
        Look up *token*, refresh its expiry on success, return the session.

        Returns ``None`` if the token is unknown or has expired.
        """
        if not token:
            return None
        async with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None
            now = time.time()
            if session.is_expired(now):
                del self._sessions[token]
                self._persist()
                return None
            session.last_used_at = now
            session.expires_at = now + self._ttl
            self._persist()
            return session

    async def revoke(self, token: str) -> None:
        """Drop *token* from the store; no-op if unknown."""
        async with self._lock:
            if self._sessions.pop(token, None) is not None:
                self._persist()

    async def revoke_all(self) -> None:
        """Drop every active session, forcing all clients to re-authenticate."""
        async with self._lock:
            self._sessions.clear()
            self._persist()

    @property
    def active_count(self) -> int:
        """Number of currently valid sessions in the store."""
        return len(self._sessions)

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("Sessions file is corrupt; starting fresh: %s", self._path)
            return
        now = time.time()
        for entry in data.get("sessions", []):
            try:
                session = Session(**entry)
            except TypeError:
                continue
            if not session.is_expired(now):
                self._sessions[session.token] = session

    def _persist(self) -> None:
        data = {"sessions": [asdict(s) for s in self._sessions.values()]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """
    Per-IP sliding-window rate limiter for login attempts.

    Once an IP exceeds ``max_attempts`` failures within
    ``window_seconds``, it is locked out for ``lockout_seconds``.
    A successful login should call ``clear`` to drop the IP's
    history immediately.
    """

    def __init__(
        self,
        max_attempts: int = _RATE_LIMIT_MAX_ATTEMPTS,
        window_seconds: float = _RATE_LIMIT_WINDOW_SECONDS,
        lockout_seconds: float = _RATE_LIMIT_LOCKOUT_SECONDS,
    ) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._lockout = lockout_seconds
        # monotonic clock — wall-clock jumps must not extend or shorten lockouts
        self._attempts: dict[str, deque[float]] = {}
        self._lockouts: dict[str, float] = {}

    def remaining_lockout(self, ip: str) -> float:
        """Seconds until *ip* is unlocked, or ``0`` if it isn't locked."""
        now = time.monotonic()
        unlock_at = self._lockouts.get(ip)
        if unlock_at is None:
            return 0.0
        remaining = unlock_at - now
        if remaining <= 0:
            del self._lockouts[ip]
            self._attempts.pop(ip, None)
            return 0.0
        return remaining

    def record_failure(self, ip: str) -> None:
        """Record a failed attempt for *ip*, triggering a lockout at the threshold."""
        now = time.monotonic()
        cutoff = now - self._window
        attempts = self._attempts.setdefault(ip, deque())
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        attempts.append(now)
        if len(attempts) >= self._max:
            self._lockouts[ip] = now + self._lockout
            _LOGGER.warning(
                "Rate limit triggered for %s (%d failed attempts in %ds); locked for %ds",
                ip,
                len(attempts),
                int(self._window),
                int(self._lockout),
            )

    def clear(self, ip: str) -> None:
        """Drop *ip*'s failure history and any active lockout."""
        self._attempts.pop(ip, None)
        self._lockouts.pop(ip, None)


# ---------------------------------------------------------------------------
# Authorization header parsing
# ---------------------------------------------------------------------------


def extract_bearer_token(authorization: str) -> str | None:
    """Return the token from an ``Authorization: Bearer ...`` header, or None."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def parse_basic_auth(authorization: str) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic ...`` header into ``(username, password)``."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


# ---------------------------------------------------------------------------
# REST auth middleware
# ---------------------------------------------------------------------------

# /ws bypasses the gate because auth happens in-band on the WebSocket —
# browsers can't set Authorization headers on `new WebSocket(...)`.
# Frontend assets are public so the login page can load before login.
_PUBLIC_PATHS = frozenset({"/", "/ws", "/favicon.ico", "/manifest.json"})
_PUBLIC_PREFIXES = ("/assets/", "/boards/images/")


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """
    Gate REST endpoints by ``Authorization`` header.

    Accepts ``Bearer <token>`` (preferred) or ``Basic <user:pass>``.
    Returns ``401`` with a ``WWW-Authenticate`` challenge otherwise.
    """
    db = request.app["device_builder"]
    settings = db.settings

    if not settings.using_password:
        return await handler(request)

    if request.method == "OPTIONS":
        return await handler(request)

    path = request.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await handler(request)

    header = request.headers.get("Authorization", "")
    ip = request.remote or "?"

    token = extract_bearer_token(header)
    if token and await db.auth.session_store.validate(token) is not None:
        return await handler(request)

    creds = parse_basic_auth(header)
    if creds is not None:
        if db.auth.rate_limiter.remaining_lockout(ip) > 0:
            return _unauthorized("Too many failed attempts; try again later")
        username, password = creds
        if settings.check_password(username, password):
            db.auth.rate_limiter.clear(ip)
            return await handler(request)
        db.auth.rate_limiter.record_failure(ip)

    return _unauthorized()


def _unauthorized(message: str = "Authentication required") -> web.Response:
    return web.Response(
        status=401,
        text=message,
        headers={
            "WWW-Authenticate": 'Basic realm="ESPHome Device Builder", Bearer',
        },
    )
