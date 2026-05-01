"""Auth controller — login, logout, token refresh."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..helpers.api import api_command
from ..helpers.auth import RateLimiter, SessionStore
from ..models import ErrorCode

if TYPE_CHECKING:
    from ..api.ws import WebSocketClient
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    """Authentication failure carrying a wire-level ``ErrorCode``."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AuthController:
    """Manages session tokens and login attempts for the dashboard."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self.session_store = SessionStore(device_builder.settings.config_dir)
        self.rate_limiter = RateLimiter()

    @api_command("auth/login")
    async def login(
        self,
        *,
        client: WebSocketClient | None = None,
        username: str = "",
        password: str = "",
        token: str = "",
        **kwargs: Any,
    ) -> dict:
        """
        Authenticate the calling WebSocket connection.

        Accepts either ``{username, password}`` or a previously issued
        ``{token}``. Returns ``{token, expires_at}`` so the caller can
        persist the token and reuse it on reconnect.

        Raises ``AuthError`` on bad credentials, expired token, or when
        the remote IP is currently rate-limited.
        """
        if client is None:
            raise AuthError(ErrorCode.INTERNAL_ERROR, "auth/login requires a connected client")

        if token:
            # Token replay is exempt from the password rate limiter — see
            # ``test_auth_controller_token_path_skips_rate_limit`` for rationale.
            session = await self.session_store.validate(token)
            if session is None:
                raise AuthError(ErrorCode.NOT_AUTHENTICATED, "Invalid or expired token")
            client.set_authenticated(session.token)
            return {"token": session.token, "expires_at": session.expires_at}

        ip = client.remote or "?"
        remaining = self.rate_limiter.remaining_lockout(ip)
        if remaining > 0:
            raise AuthError(
                ErrorCode.RATE_LIMITED,
                f"Too many failed attempts; try again in {int(remaining) + 1}s",
            )

        if not self._db.settings.check_password(username, password):
            self.rate_limiter.record_failure(ip)
            raise AuthError(ErrorCode.NOT_AUTHENTICATED, "Invalid credentials")

        self.rate_limiter.clear(ip)
        session = await self.session_store.create()
        client.set_authenticated(session.token)
        _LOGGER.info("Authenticated WS client from %s", ip)
        return {"token": session.token, "expires_at": session.expires_at}

    @api_command("auth/logout")
    async def logout(self, *, client: WebSocketClient | None = None, **kwargs: Any) -> dict:
        """Revoke the current session token and close the connection."""
        if client is None:
            return {"logged_out": True}
        if client.token:
            await self.session_store.revoke(client.token)
        client.schedule_close()
        return {"logged_out": True}

    @api_command("auth/refresh")
    async def refresh(self, *, client: WebSocketClient | None = None, **kwargs: Any) -> dict:
        """
        Slide the current session's expiry forward and return the new value.

        Tokens auto-refresh on every validated use; this command is for
        callers that want to extend a session without making another API
        call.
        """
        if client is None or not client.token:
            raise AuthError(ErrorCode.NOT_AUTHENTICATED, "No active session")
        session = await self.session_store.validate(client.token)
        if session is None:
            raise AuthError(ErrorCode.NOT_AUTHENTICATED, "Session expired")
        return {"token": session.token, "expires_at": session.expires_at}
