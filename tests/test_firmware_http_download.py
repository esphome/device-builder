"""End-to-end coverage for the ``GET /api/firmware/download`` HTTP route.

Downloads move to HTTP (not the WebSocket) so a large artifact like the
~14 MB ``firmware.elf`` isn't capped by a proxy's WebSocket ``max_msg_size``.
This drives the real ``auth_middleware`` + the real ``http_download`` handler
through an aiohttp test client, with an on-disk build directory — the same
shape production uses, just with a stub ``device_builder``.

Pins: serves the bytes + a sanitized ``Content-Disposition``; gated by auth
when a password is set; ``404`` for a missing file, a path-traversal ``file``,
and an unbuilt device.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from esphome_device_builder.controllers.firmware.download import http_download
from esphome_device_builder.helpers.auth import auth_middleware
from tests._storage_fixtures import write_storage_json


class _StubSessionStore:
    def __init__(self, valid: set[str]) -> None:
        self._valid = valid

    async def validate(self, token: str) -> object | None:
        return "session" if token in self._valid else None


class _StubRateLimiter:
    def remaining_lockout(self, ip: str) -> float:
        return 0.0

    def clear(self, ip: str) -> None: ...

    def record_failure(self, ip: str) -> None: ...


class _StubAuth:
    def __init__(self, valid_tokens: set[str] | None = None) -> None:
        self.session_store = _StubSessionStore(valid_tokens or set())
        self.rate_limiter = _StubRateLimiter()


class _StubSettings:
    def __init__(self, *, using_password: bool) -> None:
        self.using_password = using_password

    def check_password(self, username: str, password: str) -> bool:
        return False


class _StubFirmware:
    # The configuration-boundary traversal gate is covered in
    # test_traversal_validation.py; here it's a no-op so the route test can
    # focus on auth + file resolution + serving.
    async def _validate_configuration_boundary(self, configuration: str) -> None: ...


class _StubDeviceBuilder:
    def __init__(
        self, *, using_password: bool = False, valid_tokens: set[str] | None = None
    ) -> None:
        self.settings = _StubSettings(using_password=using_password)
        self.auth = _StubAuth(valid_tokens)
        self.firmware = _StubFirmware()


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


async def test_download_serves_bytes_with_attachment_header(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_build(tmp_path, monkeypatch, elf=b"ELFDATA-123")
    client = await aiohttp_client(_make_app(_StubDeviceBuilder(using_password=False)))

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": "firmware.elf"},
    )

    assert resp.status == 200
    assert await resp.read() == b"ELFDATA-123"
    cd = resp.headers["Content-Disposition"]
    assert cd == 'attachment; filename="kitchen-firmware.elf"'
    assert resp.headers["Content-Type"] == "application/octet-stream"


async def test_download_requires_auth_when_password_set(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_build(tmp_path, monkeypatch)
    client = await aiohttp_client(_make_app(_StubDeviceBuilder(using_password=True)))

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": "firmware.elf"},
    )

    assert resp.status == 401


async def test_download_accepts_valid_bearer_token(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_build(tmp_path, monkeypatch, elf=b"OK")
    client = await aiohttp_client(
        _make_app(_StubDeviceBuilder(using_password=True, valid_tokens={"tok"}))
    )

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": "firmware.elf"},
        headers={"Authorization": "Bearer tok"},
    )

    assert resp.status == 200
    assert await resp.read() == b"OK"


async def test_download_missing_file_is_404(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    _seed_build(tmp_path, monkeypatch)
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": "nope.bin"},
    )

    assert resp.status == 404


async def test_download_path_traversal_is_404(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A ``file`` that escapes the build dir resolves out and is rejected."""
    _seed_build(tmp_path, monkeypatch)
    # Plant a secret outside the build dir to prove it can't be reached.
    (tmp_path / "secret.txt").write_bytes(b"top secret")
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get(
        "/api/firmware/download",
        params={
            "configuration": "kitchen.yaml",
            "file": "../../../../../../secret.txt",
        },
    )

    assert resp.status == 404


async def test_download_unbuilt_device_is_404(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    # Redirect storage but write no sidecar → StorageJSON.load returns None.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": "firmware.elf"},
    )

    assert resp.status == 404


async def test_download_sanitizes_content_disposition(
    aiohttp_client: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A filename with a quote can't break out of the Content-Disposition header."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )
    build_dir = tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    build_dir.mkdir(parents=True, exist_ok=True)
    weird = 'fw".elf'
    (build_dir / weird).write_bytes(b"x")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )
    client = await aiohttp_client(_make_app(_StubDeviceBuilder()))

    resp = await client.get(
        "/api/firmware/download",
        params={"configuration": "kitchen.yaml", "file": weird},
    )

    assert resp.status == 200
    # The raw quote is sanitized away — exactly one opening/closing quote pair.
    assert resp.headers["Content-Disposition"] == 'attachment; filename="kitchen-fw_.elf"'
