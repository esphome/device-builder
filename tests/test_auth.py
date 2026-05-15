"""Tests for the authentication subsystem.

Covers the helpers (``SessionStore``, ``RateLimiter``, header parsing),
the ``AuthController`` login flow including rate-limit interaction, and
``DashboardSettings.check_password`` semantics. The full WebSocket auth
gate is exercised indirectly here — instantiating the WS app brings in
the esphome dependency chain, which is out of scope for unit tests.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers.auth import AuthController, AuthError
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.helpers.auth import (
    RateLimiter,
    Session,
    SessionStore,
    extract_bearer_token,
    hash_password,
    parse_basic_auth,
)
from esphome_device_builder.helpers.origin import origin_matches_host
from esphome_device_builder.models import ErrorCode

# ---------------------------------------------------------------------------
# SessionStore
# ---------------------------------------------------------------------------


async def test_session_store_create_and_validate(tmp_path: Path) -> None:
    """A freshly created session is recoverable by its token."""
    store = SessionStore(tmp_path)
    session = await store.create()

    assert session.token
    assert len(session.token) >= 32  # token_urlsafe(32) → 43+ chars
    assert session.expires_at > session.created_at

    fetched = await store.validate(session.token)
    assert fetched is not None
    assert fetched.token == session.token


async def test_session_store_validate_unknown_token(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert await store.validate("not-a-real-token") is None
    assert await store.validate("") is None


async def test_session_store_validate_refreshes_expiry(tmp_path: Path) -> None:
    """Each successful validate slides the expiry window forward."""
    store = SessionStore(tmp_path, ttl_seconds=60)
    session = await store.create()
    initial_expiry = session.expires_at

    # Force a measurable time delta — without sleeping, time.time() may
    # return the same value twice on fast systems.
    await _advance_clock(0.01)
    refreshed = await store.validate(session.token)
    assert refreshed is not None
    assert refreshed.expires_at >= initial_expiry


async def test_session_store_validate_drops_expired(tmp_path: Path) -> None:
    """Expired sessions are pruned on validate and return None."""
    store = SessionStore(tmp_path, ttl_seconds=0)  # immediate expiry
    session = await store.create()

    # Even at ttl=0, monotonic float comparisons may briefly tie; force
    # the session expiry to the past.
    store._sessions[session.token].expires_at = time.time() - 1

    assert await store.validate(session.token) is None
    assert store.active_count == 0


async def test_session_store_revoke(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = await store.create()

    await store.revoke(session.token)
    assert await store.validate(session.token) is None

    # Idempotent — revoking unknown tokens is a no-op.
    await store.revoke("unknown")


async def test_session_store_revoke_all(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    s1 = await store.create()
    s2 = await store.create()

    await store.revoke_all()
    assert await store.validate(s1.token) is None
    assert await store.validate(s2.token) is None


async def test_session_store_persists_across_instances(tmp_path: Path) -> None:
    """Sessions written by one store are visible to a fresh instance."""
    store1 = SessionStore(tmp_path)
    session = await store1.create()

    store2 = SessionStore(tmp_path)
    fetched = await store2.validate(session.token)
    assert fetched is not None
    assert fetched.token == session.token


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file modes don't apply on Windows; ACLs are a different story.",
)
async def test_session_store_persists_with_restrictive_permissions(tmp_path: Path) -> None:
    """The persisted file is mode 0600 — readable only by the owner."""
    store = SessionStore(tmp_path)
    await store.create()

    persisted = tmp_path / ".device-builder-sessions.json"
    assert persisted.exists()
    assert persisted.stat().st_mode & 0o777 == 0o600


async def test_session_store_skips_expired_on_load(tmp_path: Path) -> None:
    """Loading a store drops sessions that already expired on disk."""
    store1 = SessionStore(tmp_path, ttl_seconds=60)
    fresh = await store1.create()
    # Inject a stale session directly so we can persist it.
    stale = Session(
        token="stale-token",
        created_at=time.time() - 7200,
        last_used_at=time.time() - 7200,
        expires_at=time.time() - 3600,
    )
    store1._sessions[stale.token] = stale
    store1._persist()

    store2 = SessionStore(tmp_path)
    assert await store2.validate(fresh.token) is not None
    assert await store2.validate(stale.token) is None


async def test_session_store_load_skips_garbage_entries(tmp_path: Path) -> None:
    """A corrupt session row can't take down the whole store."""
    persisted = tmp_path / ".device-builder-sessions.json"
    persisted.write_text(
        json.dumps(
            {
                "sessions": [
                    "not-a-dict",
                    {"token": "missing-fields"},
                    {
                        "token": "wrong-types",
                        "created_at": "yesterday",
                        "last_used_at": 0,
                        "expires_at": 0,
                    },
                    {
                        "token": "good",
                        "created_at": time.time(),
                        "last_used_at": time.time(),
                        "expires_at": time.time() + 60,
                    },
                ]
            }
        )
    )

    store = SessionStore(tmp_path)
    assert await store.validate("good") is not None
    assert await store.validate("missing-fields") is None
    assert await store.validate("wrong-types") is None


async def test_session_store_load_handles_top_level_garbage(tmp_path: Path) -> None:
    """A non-dict top-level payload is treated as an empty store."""
    persisted = tmp_path / ".device-builder-sessions.json"
    persisted.write_text(json.dumps(["not", "a", "dict"]))
    store = SessionStore(tmp_path)
    assert store.active_count == 0


async def test_session_store_load_handles_corrupt_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A truncated / non-JSON sessions file logs a warning and starts fresh.

    Disk-level corruption (interrupted write, accidental edit) shouldn't
    take the dashboard out — the store should warn and continue with no
    sessions, the same shape as a missing file.
    """
    persisted = tmp_path / ".device-builder-sessions.json"
    persisted.write_text("{not-valid-json", encoding="utf-8")

    with caplog.at_level("WARNING", logger="esphome_device_builder.helpers.auth"):
        store = SessionStore(tmp_path)

    assert store.active_count == 0
    assert any("corrupt" in rec.message.lower() for rec in caplog.records)


async def test_session_store_load_handles_unreadable_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-FileNotFoundError ``OSError`` on read is logged and skipped.

    Replacing the sessions file with a directory makes ``read_bytes``
    raise ``IsADirectoryError`` — a concrete ``OSError`` subclass that
    isn't ``FileNotFoundError``. The store must catch it, log, and
    start with no sessions instead of crashing the auth controller.
    """
    persisted = tmp_path / ".device-builder-sessions.json"
    # Directory at the sessions path — ``read_bytes`` raises
    # ``IsADirectoryError`` (subclass of ``OSError``), exercising the
    # generic ``except OSError`` arm distinct from the missing-file path.
    persisted.mkdir()

    with caplog.at_level("WARNING", logger="esphome_device_builder.helpers.auth"):
        store = SessionStore(tmp_path)

    assert store.active_count == 0
    assert any("could not read sessions file" in rec.message.lower() for rec in caplog.records)


async def test_session_store_validate_debounces_persist(tmp_path: Path) -> None:
    """Validate doesn't rewrite the file on every refresh — only when expiry advances enough."""
    store = SessionStore(tmp_path, ttl_seconds=24 * 3600)
    session = await store.create()

    persisted = tmp_path / ".device-builder-sessions.json"
    mtime_before = persisted.stat().st_mtime_ns

    # Validate a few times immediately. Expiry only nudges by microseconds,
    # well under the 1 hour debounce, so the file should be left alone.
    await asyncio.sleep(0.01)
    for _ in range(5):
        await store.validate(session.token)

    assert persisted.stat().st_mtime_ns == mtime_before


async def test_session_store_validate_persists_when_threshold_crossed(tmp_path: Path) -> None:
    """When the per-session debounce threshold is crossed, validate writes again."""
    store = SessionStore(tmp_path, ttl_seconds=24 * 3600)
    session = await store.create()

    persisted = tmp_path / ".device-builder-sessions.json"
    mtime_before = persisted.stat().st_mtime_ns

    # Simulate enough time having passed for the debounce to trigger by
    # rolling the persisted-expires marker back further than the threshold.
    store._persisted_expires[session.token] = session.expires_at - 7200
    await asyncio.sleep(0.01)
    await store.validate(session.token)

    assert persisted.stat().st_mtime_ns > mtime_before


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_under_threshold() -> None:
    rl = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
    rl.record_failure("1.1.1.1")
    rl.record_failure("1.1.1.1")
    assert rl.remaining_lockout("1.1.1.1") == 0


def test_rate_limiter_locks_after_threshold() -> None:
    rl = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
    for _ in range(3):
        rl.record_failure("2.2.2.2")
    assert rl.remaining_lockout("2.2.2.2") > 0


def test_rate_limiter_clear_releases_lockout() -> None:
    rl = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
    rl.record_failure("3.3.3.3")
    rl.record_failure("3.3.3.3")
    assert rl.remaining_lockout("3.3.3.3") > 0

    rl.clear("3.3.3.3")
    assert rl.remaining_lockout("3.3.3.3") == 0


def test_rate_limiter_per_ip_isolation() -> None:
    """Failures on one IP don't lock out a different IP."""
    rl = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=60)
    rl.record_failure("4.4.4.4")
    rl.record_failure("4.4.4.4")
    assert rl.remaining_lockout("4.4.4.4") > 0
    assert rl.remaining_lockout("5.5.5.5") == 0


def test_rate_limiter_window_drops_old_failures() -> None:
    """Failures older than the window don't count toward the threshold."""
    rl = RateLimiter(max_attempts=3, window_seconds=0.05, lockout_seconds=60)
    rl.record_failure("6.6.6.6")
    rl.record_failure("6.6.6.6")
    time.sleep(0.06)
    rl.record_failure("6.6.6.6")
    # Two old failures dropped, only one fresh — well under the threshold.
    assert rl.remaining_lockout("6.6.6.6") == 0


def test_rate_limiter_lockout_expires() -> None:
    rl = RateLimiter(max_attempts=2, window_seconds=60, lockout_seconds=0.05)
    rl.record_failure("7.7.7.7")
    rl.record_failure("7.7.7.7")
    assert rl.remaining_lockout("7.7.7.7") > 0

    time.sleep(0.06)
    assert rl.remaining_lockout("7.7.7.7") == 0


def test_rate_limiter_prunes_stale_entries() -> None:
    """IPs whose only failures aged out of the window get evicted from the dict."""
    rl = RateLimiter(max_attempts=10, window_seconds=0.05, lockout_seconds=60)
    rl.record_failure("8.8.8.1")
    rl.record_failure("8.8.8.2")
    assert len(rl._attempts) == 2

    # Force the prune interval to trigger on the next record_failure call.
    # Use a deeply-negative sentinel rather than 0.0 — ``time.monotonic()``
    # is not anchored to wall time, and on a freshly booted CI runner it
    # commonly reads at ~60s, which is *inside* the 60s prune interval
    # relative to 0.0 and would silently skip the prune.
    rl._last_prune = -1e9
    time.sleep(0.06)
    rl.record_failure("8.8.8.3")

    assert "8.8.8.1" not in rl._attempts
    assert "8.8.8.2" not in rl._attempts
    assert "8.8.8.3" in rl._attempts


def test_rate_limiter_prune_keeps_locked_out_ips() -> None:
    """Locked-out IPs must not be pruned even if their attempts aged out."""
    rl = RateLimiter(max_attempts=2, window_seconds=0.05, lockout_seconds=60)
    rl.record_failure("9.9.9.1")
    rl.record_failure("9.9.9.1")  # triggers lockout

    # See ``test_rate_limiter_prunes_stale_entries`` for why we don't use 0.0.
    rl._last_prune = -1e9
    time.sleep(0.06)
    rl.record_failure("9.9.9.2")

    assert "9.9.9.1" in rl._lockouts
    assert rl.remaining_lockout("9.9.9.1") > 0


# ---------------------------------------------------------------------------
# WebSocket Origin check
# ---------------------------------------------------------------------------


def test_origin_matches_host_accepts_same_origin() -> None:
    assert origin_matches_host("http://homeassistant.local:6052", "homeassistant.local:6052")
    assert origin_matches_host("https://esphome.example.com", "esphome.example.com")


def test_origin_matches_host_rejects_cross_origin() -> None:
    assert not origin_matches_host("https://attacker.com", "homeassistant.local:6052")
    assert not origin_matches_host("http://homeassistant.local:9999", "homeassistant.local:6052")


def test_origin_matches_host_rejects_garbage() -> None:
    assert not origin_matches_host("", "homeassistant.local:6052")
    assert not origin_matches_host("not-a-url", "homeassistant.local:6052")


def test_origin_matches_host_rejects_invalid_ipv6_url() -> None:
    """An Origin with an unclosed IPv6 bracket makes ``urlparse`` raise.

    ``urlparse("http://[invalid")`` raises ``ValueError("Invalid
    IPv6 URL")`` rather than returning a parsed result. The
    ``except ValueError`` branch turns that into a clean rejection
    — without it, the cross-origin gate would 500 on a malformed
    Origin header instead of returning the 403 the rest of the
    rejection branches produce.
    """
    assert not origin_matches_host("http://[invalid", "homeassistant.local:6052")


# ---------------------------------------------------------------------------
# Authorization header parsing
# ---------------------------------------------------------------------------


def test_extract_bearer_token() -> None:
    assert extract_bearer_token("Bearer abc123") == "abc123"
    assert extract_bearer_token("bearer xyz") == "xyz"
    assert extract_bearer_token("BEARER  with-spaces  ") == "with-spaces"


def test_extract_bearer_token_rejects_other_schemes() -> None:
    assert extract_bearer_token("Basic dXNlcjpwYXNz") is None
    assert extract_bearer_token("") is None
    assert extract_bearer_token("Bearer") is None
    assert extract_bearer_token("Bearer ") is None


def test_parse_basic_auth() -> None:
    # base64("user:pass") = "dXNlcjpwYXNz"
    assert parse_basic_auth("Basic dXNlcjpwYXNz") == ("user", "pass")
    assert parse_basic_auth("basic dXNlcjpwYXNz") == ("user", "pass")


def test_parse_basic_auth_rejects_bad_input() -> None:
    assert parse_basic_auth("") is None
    assert parse_basic_auth("Bearer dXNlcjpwYXNz") is None
    assert parse_basic_auth("Basic not-base64!") is None
    # Decodes successfully but missing colon separator.
    # base64("nocolon") = "bm9jb2xvbg=="
    assert parse_basic_auth("Basic bm9jb2xvbg==") is None


# ---------------------------------------------------------------------------
# DashboardSettings.check_password
# ---------------------------------------------------------------------------


def test_check_password_matches() -> None:
    s = DashboardSettings()
    s.using_password = True
    s.username = "admin"
    s.password_hash = hash_password("hunter2")
    assert s.check_password("admin", "hunter2") is True


def test_check_password_rejects_wrong_credentials() -> None:
    s = DashboardSettings()
    s.using_password = True
    s.username = "admin"
    s.password_hash = hash_password("hunter2")
    assert s.check_password("admin", "wrong") is False
    assert s.check_password("not-admin", "hunter2") is False


def test_check_password_returns_false_when_unconfigured() -> None:
    """check_password returns False when no password is configured."""
    s = DashboardSettings()
    assert s.using_password is False
    assert s.check_password("anything", "anything") is False


# ---------------------------------------------------------------------------
# AuthController
# ---------------------------------------------------------------------------


def _make_controller(tmp_path: Path) -> tuple[AuthController, Any]:
    """Build an AuthController with a stub DeviceBuilder."""
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.using_password = True
    settings.username = "admin"
    settings.password_hash = hash_password("hunter2")

    stub_db = MagicMock()
    stub_db.settings = settings
    return AuthController(stub_db), stub_db


def _make_client(remote: str = "9.9.9.9") -> MagicMock:
    client = MagicMock()
    client.remote = remote
    client.token = None
    client._authenticated = False

    def _set_authenticated(token: str | None) -> None:
        client._authenticated = True
        client.token = token

    client.set_authenticated = _set_authenticated
    return client


async def test_auth_controller_login_password_success(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()

    result = await auth.login(client=client, username="admin", password="hunter2")
    assert "token" in result
    assert "expires_at" in result
    assert client._authenticated is True
    assert client.token == result["token"]


async def test_auth_controller_login_wrong_password(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()

    with pytest.raises(AuthError) as exc:
        await auth.login(client=client, username="admin", password="wrong")
    assert exc.value.code == ErrorCode.NOT_AUTHENTICATED
    assert client._authenticated is False


async def test_auth_controller_login_token_reuse(tmp_path: Path) -> None:
    """A token issued by one login can be replayed on a later login."""
    auth, _ = _make_controller(tmp_path)
    client_a = _make_client()
    client_b = _make_client()

    first = await auth.login(client=client_a, username="admin", password="hunter2")

    second = await auth.login(client=client_b, token=first["token"])
    assert second["token"] == first["token"]
    assert client_b._authenticated is True


async def test_auth_controller_login_invalid_token(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()

    with pytest.raises(AuthError) as exc:
        await auth.login(client=client, token="bogus")
    assert exc.value.code == ErrorCode.NOT_AUTHENTICATED


async def test_auth_controller_rate_limits_after_repeated_failures(tmp_path: Path) -> None:
    """After enough wrong passwords from one IP, further attempts are rate-limited."""
    auth, _ = _make_controller(tmp_path)
    auth.rate_limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
    client = _make_client(remote="10.0.0.1")

    for _ in range(3):
        with pytest.raises(AuthError):
            await auth.login(client=client, username="admin", password="wrong")

    with pytest.raises(AuthError) as exc:
        await auth.login(client=client, username="admin", password="hunter2")
    assert exc.value.code == ErrorCode.RATE_LIMITED


async def test_auth_controller_success_clears_rate_limit(tmp_path: Path) -> None:
    """A successful login wipes the failure history for that IP."""
    auth, _ = _make_controller(tmp_path)
    auth.rate_limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)
    client = _make_client(remote="10.0.0.2")

    # Two wrong attempts (under the threshold), then a correct one.
    for _ in range(2):
        with pytest.raises(AuthError):
            await auth.login(client=client, username="admin", password="wrong")
    await auth.login(client=client, username="admin", password="hunter2")

    # The history should be cleared — three more wrong attempts again
    # land on NOT_AUTHENTICATED, not RATE_LIMITED.
    fresh_client = _make_client(remote="10.0.0.2")
    for _ in range(2):
        with pytest.raises(AuthError) as exc:
            await auth.login(client=fresh_client, username="admin", password="wrong")
        assert exc.value.code == ErrorCode.NOT_AUTHENTICATED


async def test_auth_controller_rate_limit_per_ip(tmp_path: Path) -> None:
    """Different IPs accumulate rate-limit counters independently."""
    auth, _ = _make_controller(tmp_path)
    auth.rate_limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)

    attacker = _make_client(remote="11.0.0.1")
    for _ in range(3):
        with pytest.raises(AuthError):
            await auth.login(client=attacker, username="admin", password="wrong")

    # The attacker is locked out, but another IP is fine.
    other = _make_client(remote="11.0.0.2")
    result = await auth.login(client=other, username="admin", password="hunter2")
    assert result["token"]


async def test_auth_controller_token_path_skips_rate_limit(tmp_path: Path) -> None:
    """
    Token replay is exempt from password rate limits.

    Brute-forcing a 256-bit token is infeasible, and rate-limiting
    valid token reconnections would lock legitimate clients out
    after a network blip.
    """
    auth, _ = _make_controller(tmp_path)
    auth.rate_limiter = RateLimiter(max_attempts=3, window_seconds=60, lockout_seconds=60)

    client = _make_client(remote="12.0.0.1")
    first = await auth.login(client=client, username="admin", password="hunter2")

    # Land the IP in lockout via password failures from a separate client
    # state — same remote IP though.
    fail_client = _make_client(remote="12.0.0.1")
    for _ in range(3):
        with pytest.raises(AuthError):
            await auth.login(client=fail_client, username="admin", password="wrong")

    # Token replay from the same IP still succeeds.
    replay_client = _make_client(remote="12.0.0.1")
    result = await auth.login(client=replay_client, token=first["token"])
    assert result["token"] == first["token"]


async def test_auth_controller_logout_revokes_token(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()
    await auth.login(client=client, username="admin", password="hunter2")
    token = client.token

    await auth.logout(client=client)
    client.schedule_close.assert_called_once()

    # The token is no longer valid.
    fresh_client = _make_client()
    with pytest.raises(AuthError):
        await auth.login(client=fresh_client, token=token)


async def test_auth_controller_refresh_extends_session(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()
    await auth.login(client=client, username="admin", password="hunter2")

    refreshed = await auth.refresh(client=client)
    assert refreshed["token"] == client.token


async def test_auth_controller_refresh_without_session_raises(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_client()
    client.token = None

    with pytest.raises(AuthError) as exc:
        await auth.refresh(client=client)
    assert exc.value.code == ErrorCode.NOT_AUTHENTICATED


# ---------------------------------------------------------------------------
# WebSocketClient dispatch gate
# ---------------------------------------------------------------------------


def _make_ws_client(
    auth_ctrl: AuthController, *, authenticated: bool = False, remote: str = "127.0.0.1"
) -> Any:
    """Build a WebSocketClient with a mocked underlying WebSocket."""
    db = MagicMock()
    db.settings = auth_ctrl._db.settings
    db.auth = auth_ctrl
    db.command_handlers = {
        "auth/login": auth_ctrl.login,
        "auth": auth_ctrl.login,
        "ping": _stub_ping,
    }

    fake_ws = MagicMock()
    sent: list[dict] = []

    async def _send_json(data: Any, *, dumps: Any = None) -> None:
        # Mirror aiohttp's behaviour: the production code always
        # passes a custom ``dumps`` callable (the orjson wrapper), and
        # only the serialised string ever lands on the wire. Round-
        # trip through that callable here so the test catches payloads
        # that aren't actually JSON-serialisable — without this the
        # mock silently accepts anything (e.g. a dict with non-string
        # keys) and the bug would only surface in production.
        if dumps is not None:
            sent.append(json.loads(dumps(data)))
        else:
            sent.append(data)

    async def _close(*_: Any, **__: Any) -> None:
        pass

    fake_ws.send_json = _send_json
    fake_ws.close = _close

    client = WebSocketClient(fake_ws, db, remote=remote, authenticated=authenticated, token=None)
    client._sent = sent  # type: ignore[attr-defined]
    return client


async def _stub_ping(**_: Any) -> dict:
    return {"pong": True}


async def test_ws_pre_auth_rejects_non_auth_command(tmp_path: Path) -> None:
    """An unauthenticated WS connection cannot call arbitrary commands."""
    auth, _ = _make_controller(tmp_path)
    client = _make_ws_client(auth, authenticated=False)

    await client._handle_command({"command": "ping", "message_id": "1", "args": {}})

    sent = client._sent
    assert len(sent) == 1
    assert sent[0]["error_code"] == ErrorCode.NOT_AUTHENTICATED.value


async def test_ws_pre_auth_allows_auth_command(tmp_path: Path) -> None:
    """The auth command is the one exception during the pre-auth gate."""
    auth, _ = _make_controller(tmp_path)
    client = _make_ws_client(auth, authenticated=False)

    await client._handle_command(
        {
            "command": "auth/login",
            "message_id": "2",
            "args": {"username": "admin", "password": "hunter2"},
        }
    )

    sent = client._sent
    assert len(sent) == 1
    assert "result" in sent[0]
    assert "token" in sent[0]["result"]
    assert client.authenticated is True


async def test_ws_authenticated_can_call_normal_commands(tmp_path: Path) -> None:
    auth, _ = _make_controller(tmp_path)
    client = _make_ws_client(auth, authenticated=True)

    await client._handle_command({"command": "ping", "message_id": "3", "args": {}})

    sent = client._sent
    assert sent[0]["result"] == {"pong": True}


async def test_ws_auth_alias_command_works(tmp_path: Path) -> None:
    """The bare `auth` command is registered as an alias for `auth/login`."""
    auth, _ = _make_controller(tmp_path)
    client = _make_ws_client(auth, authenticated=False)

    await client._handle_command(
        {
            "command": "auth",
            "message_id": "4",
            "args": {"username": "admin", "password": "hunter2"},
        }
    )

    sent = client._sent
    assert "result" in sent[0]
    assert "token" in sent[0]["result"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _advance_clock(seconds: float) -> None:
    """Block briefly so ``time.time()`` reads a fresh value."""
    await asyncio.sleep(seconds)
