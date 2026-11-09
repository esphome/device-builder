"""Tests for the firmware repository and update endpoints."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from esphome_device_builder.controllers import firmware_repository as repository_module
from esphome_device_builder.controllers.firmware_repository import FirmwareRepositoryController
from esphome_device_builder.helpers.async_ import run_in_executor
from esphome_device_builder.helpers.auth import auth_middleware


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FirmwareRepositoryController:
    settings = SimpleNamespace(config_dir=tmp_path, host="127.0.0.1")
    controller = FirmwareRepositoryController(SimpleNamespace(settings=settings))
    controller._load_index()
    binary = tmp_path / "build" / "firmware.bin"
    binary.parent.mkdir()
    binary.write_bytes(b"first firmware")
    (binary.parent / "build_info.json").write_text(
        json.dumps(
            {
                "build_time": 1760000000,
                "config_hash": 0x1234ABCD,
                "esphome_version": "2026.11.0b0",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        repository_module,
        "resolve_storage_path",
        lambda _configuration: tmp_path / "storage.json",
    )
    monkeypatch.setattr(
        repository_module.StorageJSON,
        "load",
        lambda _path: SimpleNamespace(
            firmware_bin_path=binary,
            build_path=binary.parent,
            esphome_version="2026.11.0b0",
            target_platform="ESP32S3",
            core_platform="esp32",
        ),
    )
    return controller


def _write_yaml(config_dir: Path, version: str) -> None:
    (config_dir / "garden.yaml").write_text(
        f"esphome:\n  name: garden\n  project:\n    name: example.garden\n    version: {version}\n",
        encoding="utf-8",
    )


def _write_resolution_yaml(path: Path) -> None:
    path.write_text(
        "substitutions:\n  device_id: garden\n  fw_version: 3.2.1\n"
        "esphome:\n  name: ${device_id}\n  project:\n    version: ${fw_version}\n"
        "packages:\n  metadata: !include package.yaml\n",
        encoding="utf-8",
    )


def _write_variants(build_dir: Path) -> None:
    (build_dir / "firmware.ota.bin").write_bytes(b"ota firmware")
    (build_dir / "firmware.factory.bin").write_bytes(b"factory firmware")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolved_config(
    device: str, version: str | None, friendly_name: str | None = None
) -> dict[str, Any]:
    project = {"name": "example.garden"}
    if version is not None:
        project["version"] = version
    esphome: dict[str, Any] = {"name": device, "project": project}
    if friendly_name is not None:
        esphome["friendly_name"] = friendly_name
    return {"esphome": esphome}


def test_save_publish_and_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _write_yaml(tmp_path, "1.0.0")
    first = controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))

    assert first["status"] == "saved"
    assert first["friendly_name"] == "garden"
    assert first["compiled_at"] == "2025-10-09T08:53:20+00:00"
    assert first["esphome_version"] == "2026.11.0b0"
    assert first["config_hash"] == "1234abcd"
    with pytest.raises(FileExistsError, match="Build date"):
        controller._save_sync("garden.yaml", _resolved_config("garden", "1.1.0"))

    build_info_path = tmp_path / "build" / "build_info.json"
    build_info = _read_json(build_info_path)
    build_info["build_time"] += 1
    build_info_path.write_text(json.dumps(build_info), encoding="utf-8")
    second = controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))
    assert second["compiled_at"] != first["compiled_at"]

    controller._publish_sync("garden", "1.0.0")
    with pytest.raises(FileExistsError):
        controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))
    build_info["build_time"] += 1
    build_info_path.write_text(json.dumps(build_info), encoding="utf-8")
    controller._save_sync("garden.yaml", _resolved_config("garden", "2.0.0"))
    controller._publish_sync("garden", "2.0.0")
    assert controller._records[("garden", "1.0.0")]["status"] == "obsolete"
    assert controller._records[("garden", "2.0.0")]["status"] == "published"

    controller._delete_sync("garden", "1.0.0")
    assert ("garden", "1.0.0") not in controller._records


def test_save_falls_back_to_build_esphome_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(repository_module, "_installed_esphome_version", "2026.10.2")
    _write_yaml(tmp_path, "")
    record = controller._save_sync("garden.yaml", _resolved_config("garden", None))
    assert record["version"] == "2026.11.0b0"


@pytest.mark.asyncio
async def test_public_listener_stays_closed_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    monkeypatch.setattr(
        repository_module,
        "resolve_bind_host",
        lambda _host: pytest.fail("public update listener must stay closed"),
    )

    await controller.start()

    assert controller._runner is None


@pytest.mark.asyncio
async def test_public_listener_preference_starts_and_stops_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    start_listener = AsyncMock()
    monkeypatch.setattr(controller, "_start_public_listener", start_listener)

    await controller.set_public_enabled(enabled=True)
    start_listener.assert_awaited_once()

    runner = SimpleNamespace(cleanup=AsyncMock())
    controller._runner = runner
    await controller.set_public_enabled(enabled=False)
    runner.cleanup.assert_awaited_once()
    assert controller._runner is None


@pytest.mark.asyncio
async def test_save_resolves_substitutions_and_release_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_variants, tmp_path / "build")
    await run_in_executor(_write_resolution_yaml, tmp_path / "garden.yaml")
    devices = object()
    firmware = SimpleNamespace(_validate_configuration_boundary=AsyncMock())
    firmware.get_binaries = AsyncMock(
        return_value=[
            {"type": "ota", "file": "firmware.ota.bin"},
            {"type": "factory", "file": "firmware.factory.bin"},
        ]
    )
    controller._db.devices = devices
    controller._db.firmware = firmware
    unresolved = _resolved_config("${device_id}", "${fw_version}", "${display_name}")
    resolved = _resolved_config("resolved-garden", "7.8.9", "Garden Display Name")
    monkeypatch.setattr(
        repository_module, "resolve_config", AsyncMock(return_value=(unresolved, True))
    )
    subprocess_resolve = AsyncMock(return_value=resolved)
    monkeypatch.setattr(repository_module, "resolve_config_subprocess", subprocess_resolve)

    record = await controller.save_firmware(
        configuration="garden.yaml",
        summary="Garden controller release",
        release_url="https://example.com/releases/7.8.9",
    )

    subprocess_resolve.assert_awaited_once_with(devices, tmp_path / "garden.yaml")
    assert record["device"] == "resolved-garden"
    assert record["friendly_name"] == "Garden Display Name"
    assert record["summary"] == "Garden controller release"
    assert record["release_url"] == "https://example.com/releases/7.8.9"
    assert record["ota_bin"] == "resolved-garden/7.8.9.ota.bin"
    assert record["factory_bin"] == "resolved-garden/7.8.9.factory.bin"


@pytest.mark.asyncio
async def test_public_manifest_download_and_runtime_md5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_variants, tmp_path / "build")
    await run_in_executor(_write_yaml, tmp_path, "1.0.0")
    record = await run_in_executor(
        controller._save_sync,
        "garden.yaml",
        _resolved_config("garden", "1.0.0", "Garden Display Name"),
        {"ota": "firmware.ota.bin", "factory": "firmware.factory.bin"},
        "Garden release 1.0.0",
        "https://example.com/releases/1.0.0",
    )
    await run_in_executor(controller._publish_sync, "garden", "1.0.0")
    app = web.Application()
    app.router.add_get("/update", controller._http_update_manifest)
    app.router.add_get("/update/download", controller._http_download)
    app.router.add_get("/update/md5", controller._http_md5)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        manifest_response = await client.get("/update?device=garden")
        manifest = await manifest_response.json()
        assert manifest["name"] == "Garden Display Name"
        assert manifest["release_url"] == "https://example.com/releases/1.0.0"
        build = manifest["builds"][0]
        assert build["chipFamily"] == "esp32"
        assert build["ota"]["summary"] == "Garden release 1.0.0"
        assert build["factory"]["summary"] == "Garden release 1.0.0"
        assert build["ota"]["path"] == ("/update/download?device=garden&version=1.0.0&type=ota")

        ota_response = await client.get("/update/download?device=garden&type=ota")
        assert await ota_response.read() == b"ota firmware"
        factory_response = await client.get("/update/download?device=garden&type=factory")
        assert await factory_response.read() == b"factory firmware"
        assert (await client.get("/update/download?device=garden")).status == 400

        ota_path = controller.root / record["ota_bin"]
        await run_in_executor(ota_path.write_bytes, b"changed image")
        expected_md5 = hashlib.md5(b"changed image", usedforsecurity=False).hexdigest()
        md5_response = await client.get("/update/md5?device=garden&type=ota")
        assert await md5_response.text() == expected_md5
        assert md5_response.headers["Content-Disposition"] == (
            'attachment; filename="garden-1.0.0-ota.md5"'
        )

        controller._records[("garden", "1.0.0")]["status"] = "obsolete"
        obsolete_response = await client.get(
            "/update/download?device=garden&version=1.0.0&type=factory"
        )
        assert await obsolete_response.read() == b"factory firmware"
        obsolete_md5 = await client.get("/update/md5?device=garden&version=1.0.0&type=factory")
        assert await obsolete_md5.text() == record["factory_md5"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_device_json_is_private_version_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_variants, tmp_path / "build")
    await run_in_executor(_write_yaml, tmp_path, "1.0.0")
    await run_in_executor(
        controller._save_sync,
        "garden.yaml",
        _resolved_config("garden", "1.0.0", "Garden Display Name"),
        {"ota": "firmware.ota.bin", "factory": "firmware.factory.bin"},
        "Release one",
        None,
    )
    await run_in_executor(controller._publish_sync, "garden", "1.0.0")
    private_index = await run_in_executor(_read_json, controller.root / "garden.json")
    assert private_index["device"] == "garden"
    assert private_index["firmwares"][0]["version"] == "1.0.0"
    assert private_index["firmwares"][0]["summary"] == "Release one"
    assert "builds" not in private_index


@pytest.mark.asyncio
async def test_internal_download_route_serves_repository_artifacts_with_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_variants, tmp_path / "build")
    await run_in_executor(_write_yaml, tmp_path, "1.0.0")
    record = await run_in_executor(
        controller._save_sync,
        "garden.yaml",
        _resolved_config("garden", "1.0.0"),
        {"ota": "firmware.ota.bin", "factory": "firmware.factory.bin"},
        "Release one",
        None,
    )
    settings = SimpleNamespace(
        using_password=True,
        check_password=lambda username, password: username == "admin" and password == "test-pass",
    )
    auth = SimpleNamespace(
        rate_limiter=SimpleNamespace(
            remaining_lockout=lambda _ip: 0,
            clear=lambda _ip: None,
            record_failure=lambda _ip: None,
        )
    )
    app = web.Application(middlewares=[auth_middleware])
    app["device_builder"] = SimpleNamespace(
        settings=settings, auth=auth, firmware_repository=controller
    )
    app.router.add_get(
        "/api/firmware_repository/download", repository_module.http_repository_download
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        url = "/api/firmware_repository/download?device=garden&version=1.0.0&file=factory"
        assert (await client.get(url)).status == 401
        authorization = "Basic " + base64.b64encode(b"admin:test-pass").decode("ascii")
        response = await client.get(url, headers={"Authorization": authorization})
        assert await response.read() == b"factory firmware"
        assert record["factory_md5"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_load_index_migrates_legacy_public_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_variants, tmp_path / "build")
    await run_in_executor(_write_yaml, tmp_path, "2.0.0")
    current = await run_in_executor(
        controller._save_sync,
        "garden.yaml",
        _resolved_config("garden", "2.0.0", "Garden Display Name"),
        {"ota": "firmware.ota.bin", "factory": "firmware.factory.bin"},
        "Release two",
        None,
    )
    controller._records[("garden", "1.0.0")] = {
        "device": "garden",
        "version": "1.0.0",
        "status": "published",
        "yaml": "garden/1.0.0.yaml",
        "bin": "garden/1.0.0.bin",
        "md5": "0" * 32,
        "ota_bin": "garden/1.0.0.ota.bin",
        "ota_md5": "0" * 32,
        "compiled_at": "2026-01-01T00:00:00+00:00",
        "published_at": "2026-01-01T00:00:00+00:00",
    }
    await run_in_executor(controller._write_index)
    reloaded = FirmwareRepositoryController(controller._db)
    await run_in_executor(reloaded._load_index)
    private_index = await run_in_executor(_read_json, reloaded.root / "garden.json")
    migrated = reloaded._records[("garden", "1.0.0")]
    assert migrated["platform"] == "esp32"
    assert migrated["friendly_name"] == "Garden Display Name"
    assert current["platform"] == "esp32"
    assert {entry["version"] for entry in private_index["firmwares"]} == {"1.0.0", "2.0.0"}
