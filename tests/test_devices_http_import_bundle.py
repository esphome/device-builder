"""Coverage for the ``POST /api/devices/import_bundle`` HTTP route + ``UploadTokens``."""

from __future__ import annotations

from typing import Any

from aiohttp import web

from esphome_device_builder.controllers.devices.import_upload import (
    UploadTokens,
    http_import_bundle,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.auth import auth_middleware
from esphome_device_builder.models import ErrorCode, ImportBundleResponse

_IMPORTED = ImportBundleResponse(
    status="imported",
    configuration="kitchen.yaml",
    conflicts=[],
    written=["kitchen.yaml"],
    kept=[],
    has_secrets=False,
    esphome_version="2026.6.0",
)


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


class _StubDevices:
    def __init__(self, *, token_ttl: float = 60.0) -> None:
        self.import_tokens = UploadTokens(ttl_seconds=token_ttl)
        self.calls: list[tuple[bytes, list[str] | None]] = []
        self.error: CommandError | None = None
        self.response = _IMPORTED

    async def import_bundle(
        self, *, bundle_bytes: bytes, overwrite: list[str] | None
    ) -> ImportBundleResponse:
        self.calls.append((bundle_bytes, overwrite))
        if self.error is not None:
            raise self.error
        return self.response


class _StubDeviceBuilder:
    def __init__(self, *, using_password: bool = False, token_ttl: float = 60.0) -> None:
        self.settings = _StubSettings(using_password=using_password)
        self.auth = _StubAuth()
        self.devices = _StubDevices(token_ttl=token_ttl)


def _make_app(db: _StubDeviceBuilder) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app["device_builder"] = db
    app.router.add_post("/api/devices/import_bundle", http_import_bundle)
    return app


async def test_valid_token_imports_even_with_password_set(aiohttp_client: Any) -> None:
    # using_password=True proves the route is reachable via the token alone,
    # with no Authorization header (auth_middleware allowlist).
    db = _StubDeviceBuilder(using_password=True)
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post(
        "/api/devices/import_bundle", params={"token": token}, data=b"\x1f\x8bBUNDLE"
    )

    assert resp.status == 200
    assert (await resp.json())["status"] == "imported"
    assert db.devices.calls == [(b"\x1f\x8bBUNDLE", None)]


async def test_resolve_mode_passes_overwrite(aiohttp_client: Any) -> None:
    db = _StubDeviceBuilder()
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post(
        "/api/devices/import_bundle",
        params=[
            ("token", token),
            ("mode", "resolve"),
            ("overwrite", "a.yaml"),
            ("overwrite", "b.yaml"),
        ],
        data=b"\x1f\x8b",
    )

    assert resp.status == 200
    assert db.devices.calls == [(b"\x1f\x8b", ["a.yaml", "b.yaml"])]


async def test_resolve_mode_with_no_overwrite_is_empty_list(aiohttp_client: Any) -> None:
    db = _StubDeviceBuilder()
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post(
        "/api/devices/import_bundle",
        params={"token": token, "mode": "resolve"},
        data=b"\x1f\x8b",
    )

    assert resp.status == 200
    assert db.devices.calls == [(b"\x1f\x8b", [])]


async def test_body_over_client_max_size_reaches_handler(aiohttp_client: Any) -> None:
    """A >1 MiB body streams to the handler, not pre-rejected by client_max_size."""
    db = _StubDeviceBuilder()
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))
    body = b"\x1f\x8b" + b"x" * (2 * 1024 * 1024)

    resp = await client.post("/api/devices/import_bundle", params={"token": token}, data=body)

    assert resp.status == 200
    assert db.devices.calls == [(body, None)]


async def test_oversize_body_is_413(aiohttp_client: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.import_upload.BUNDLE_MAX_TOTAL_BYTES",
        1024 * 1024,
    )
    db = _StubDeviceBuilder()
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post(
        "/api/devices/import_bundle", params={"token": token}, data=b"x" * (1024 * 1024 + 1)
    )

    assert resp.status == 413
    assert db.devices.calls == []


async def test_command_error_maps_to_400(aiohttp_client: Any) -> None:
    db = _StubDeviceBuilder()
    db.devices.error = CommandError(ErrorCode.INVALID_ARGS, "Not a valid ESPHome bundle: nope")
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post("/api/devices/import_bundle", params={"token": token}, data=b"junk")

    assert resp.status == 400
    payload = await resp.json()
    assert payload == {"error_code": "invalid_args", "details": "Not a valid ESPHome bundle: nope"}


async def test_missing_token_is_404(aiohttp_client: Any) -> None:
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))
    resp = await client.post("/api/devices/import_bundle", data=b"\x1f\x8b")
    assert resp.status == 404


async def test_unknown_token_is_404(aiohttp_client: Any) -> None:
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))
    resp = await client.post(
        "/api/devices/import_bundle", params={"token": "nope"}, data=b"\x1f\x8b"
    )
    assert resp.status == 404


async def test_token_is_single_use(aiohttp_client: Any) -> None:
    db = _StubDeviceBuilder()
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    first = await client.post(
        "/api/devices/import_bundle", params={"token": token}, data=b"\x1f\x8b"
    )
    second = await client.post(
        "/api/devices/import_bundle", params={"token": token}, data=b"\x1f\x8b"
    )

    assert first.status == 200
    assert second.status == 404


async def test_expired_token_is_404(aiohttp_client: Any) -> None:
    db = _StubDeviceBuilder(token_ttl=-1.0)  # expired on creation
    token = db.devices.import_tokens.create()
    client = await aiohttp_client(_make_app(db))

    resp = await client.post(
        "/api/devices/import_bundle", params={"token": token}, data=b"\x1f\x8b"
    )

    assert resp.status == 404


# ---------------------------------------------------------------------------
# UploadTokens
# ---------------------------------------------------------------------------


def test_upload_tokens_round_trip() -> None:
    tokens = UploadTokens()
    assert tokens.consume(tokens.create()) is True


def test_upload_tokens_unknown_is_false() -> None:
    assert UploadTokens().consume("nope") is False
    assert UploadTokens().consume("") is False


def test_upload_tokens_are_single_use() -> None:
    tokens = UploadTokens()
    token = tokens.create()
    assert tokens.consume(token) is True
    assert tokens.consume(token) is False


def test_upload_tokens_expire() -> None:
    tokens = UploadTokens(ttl_seconds=-1.0)
    assert tokens.consume(tokens.create()) is False


def test_upload_tokens_purge_drops_expired_on_create() -> None:
    tokens = UploadTokens(ttl_seconds=-1.0)
    tokens.create()
    tokens.create()
    assert len(tokens._tokens) == 1
