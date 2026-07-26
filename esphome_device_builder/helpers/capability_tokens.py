"""Single-use, short-TTL capability tokens for HTTP-route authorization."""

from __future__ import annotations

import secrets
import time


class CapabilityTokens[T]:
    """Single-use, short-TTL tokens each bound to a payload of type ``T``.

    Minted over the authenticated WebSocket and consumed by an HTTP route in
    ``auth_middleware``'s public allowlist, so the token is the route's sole
    authorization. Each is unguessable (:mod:`secrets`), single-use, and
    fast-expiring. Subclasses expose typed ``create`` / ``consume`` over the
    ``_mint`` / ``_redeem`` core.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, tuple[T, float]] = {}

    def _mint(self, payload: T) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (payload, time.monotonic() + self._ttl)
        return token

    def _redeem(self, token: str) -> T | None:
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        payload, expiry = entry
        if time.monotonic() > expiry:
            return None
        return payload

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, exp) in self._tokens.items() if now > exp]:
            del self._tokens[token]
