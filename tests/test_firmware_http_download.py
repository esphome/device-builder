"""End-to-end coverage for the ``GET /api/firmware/download`` HTTP route.

Downloads move to HTTP (not the WebSocket) so a large artifact like the
~14 MB ``firmware.elf`` isn't capped by a proxy's WebSocket ``max_msg_size``,
and a plain navigation streams it straight to disk (mobile-friendly). The route
carries its own single-use capability token (minted over the authenticated WS
by ``firmware/download_token``) instead of a bearer header, so it's in
``auth_middleware``'s public allowlist and the handler validates the token.

This drives the real ``auth_middleware`` + the real ``http_download`` handler
(+ ``DownloadTokens``) through an aiohttp test client with an on-disk build
directory. Pins: a valid token serves the bytes + a sanitized
``Content-Disposition`` even with a password set (proving the allowlist); a
missing / unknown / reused / expired token, a traversal ``file``, and an
unbuilt device all ``404``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from esphome_device_builder.controllers.firmware.download import (
    DownloadTokens,
    http_download,
)
from esphome_device_builder.helpers.auth import auth_middleware
from tests._storage_fixtures import write_storage_json


class _StubSessionStore:
    async def validate(self, token: str) -> object | None:
        return None


class _StubRateLimiter:
    def remaining_lockout(self, ip: str) -> float:
        return 0.0

    def clear(self, ip: str) -> None: ...

    def record_failure(self, ip: str) -> None: ...


class _StubAuth:
    def __init__(self) -> None:
        self.session_store = _StubSessionStore()
        self.rate_limiter = _StubRateLimiter()


class _StubSettings:
    def __init__(self, *, using_password: bool) -> None:
        self.using_password = using_password

    def check_password(self, username: str, password: str) -> bool:
        return False


class _StubFirmware:
    def __init__(self, *, token_ttl: float = 60.0) -> None:
        self.download_tokens = DownloadTokens(ttl_seconds=token_ttl)

    # The WS download_token command's boundary gate is covered in
    # test_download_token.py; here it's a no-op so the route test can focus on
    # token validation + file resolution + serving.
    async def _validate_configuration_boundary(self, configuration: str) -> None: ...


class _StubDeviceBuilder:
    def __init__(self, *, using_password: bool = False, token_ttl: float = 60.0) -> None:
        self.settings = _StubSettings(using_password=using_password)
        self.auth = _StubAuth()
        self.firmware = _StubFirmware(token_ttl=token_ttl)


def _make_app(db: _StubDeviceBuilder) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["device_builder"] = db
    app.router.add_get("/api/firmware/download", http_download)
    return app


def _seed_build(tmp_path: Path, monkeypatch: Any, *, elf: bytes = b"ELF-BYTES") -> None:
    """Lay down a built ``kitchen`` device with ``firmware.elf`` on disk."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )
    build_dir = tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "firmware.elf").write_bytes(elf)
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )


async def test_valid_token_serves_bytes_even_with_password_set(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_build(tmp_path, monkeypatch, elf=b"ELFDATA-123")
    # using_password=True proves the route is reachable via the token alone,
    # with no Authorization header (auth_middleware allowlist).
    db = _StubDeviceBuilder(using_password=True)
    token = db.firmware.download_tokens.create("kitchen.yaml", "firmware.elf")
    client = await aiohttp_client(_make_app(db))

    resp = await client.get("/api/firmware/download", params={"token": token})

    assert resp.status == 200
    assert await resp.read() == b"ELFDATA-123"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="kitchen-firmware.elf"'
    assert resp.headers["Content-Type"] == "application/octet-stream"


async def test_missing_token_is_404(aiohttp_client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _seed_build(tmp_path, monkeypatch)
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get("/api/firmware/download")

    assert resp.status == 404


async def test_unknown_token_is_404(aiohttp_client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _seed_build(tmp_path, monkeypatch)
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get("/api/firmware/download", params={"token": "not-a-real-token"})

    assert resp.status == 404


async def test_token_is_single_use(aiohttp_client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _seed_build(tmp_path, monkeypatch)
    db = _StubDeviceBuilder()
    token = db.firmware.download_tokens.create("kitchen.yaml", "firmware.elf")
    client = await aiohttp_client(_make_app(db))

    first = await client.get("/api/firmware/download", params={"token": token})
    second = await client.get("/api/firmware/download", params={"token": token})

    assert first.status == 200
    assert second.status == 404


async def test_expired_token_is_404(aiohttp_client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    _seed_build(tmp_path, monkeypatch)
    db = _StubDeviceBuilder(token_ttl=-1.0)  # already expired on creation
    token = db.firmware.download_tokens.create("kitchen.yaml", "firmware.elf")
    client = await aiohttp_client(_make_app(db))

    resp = await client.get("/api/firmware/download", params={"token": token})

    assert resp.status == 404


async def test_token_bound_to_traversal_file_is_404(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Even a valid token can't escape the build dir — the file is resolved + checked."""
    _seed_build(tmp_path, monkeypatch)
    (tmp_path / "secret.txt").write_bytes(b"top secret")
    db = _StubDeviceBuilder()
    token = db.firmware.download_tokens.create("kitchen.yaml", "../../../../../../secret.txt")
    client = await aiohttp_client(_make_app(db))

    resp = await client.get("/api/firmware/download", params={"token": token})

    assert resp.status == 404


async def test_unbuilt_device_is_404(aiohttp_client: Any, tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )
    db = _StubDeviceBuilder()
    token = db.firmware.download_tokens.create("kitchen.yaml", "firmware.elf")
    client = await aiohttp_client(_make_app(db))

    resp = await client.get("/api/firmware/download", params={"token": token})

    assert resp.status == 404


# ---------------------------------------------------------------------------
# DownloadTokens
# ---------------------------------------------------------------------------


def test_download_tokens_round_trip() -> None:
    tokens = DownloadTokens()
    token = tokens.create("kitchen.yaml", "firmware.elf")
    assert tokens.consume(token) == ("kitchen.yaml", "firmware.elf")


def test_download_tokens_unknown_returns_none() -> None:
    assert DownloadTokens().consume("nope") is None
    assert DownloadTokens().consume("") is None


def test_download_tokens_are_single_use() -> None:
    tokens = DownloadTokens()
    token = tokens.create("kitchen.yaml", "firmware.elf")
    assert tokens.consume(token) is not None
    assert tokens.consume(token) is None


def test_download_tokens_expire() -> None:
    tokens = DownloadTokens(ttl_seconds=-1.0)  # expiry in the past
    token = tokens.create("kitchen.yaml", "firmware.elf")
    assert tokens.consume(token) is None


def test_download_tokens_purge_drops_expired_on_create() -> None:
    tokens = DownloadTokens(ttl_seconds=-1.0)  # everything expires immediately
    tokens.create("kitchen.yaml", "firmware.elf")
    # The next create() purges the expired entry before adding the new one, so
    # the store doesn't grow unbounded.
    tokens.create("kitchen.yaml", "firmware.bin")
    assert len(tokens._tokens) == 1
