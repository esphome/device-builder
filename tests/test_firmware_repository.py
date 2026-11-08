"""Tests for the firmware repository and public update endpoints."""

from __future__ import annotations

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


def _controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FirmwareRepositoryController:
    settings = SimpleNamespace(config_dir=tmp_path, host="127.0.0.1")
    device_builder = SimpleNamespace(settings=settings)
    controller = FirmwareRepositoryController(device_builder)
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


def _resolved_config(device: str, version: str | None) -> dict[str, Any]:
    project = {"name": "example.garden"}
    if version is not None:
        project["version"] = version
    return {"esphome": {"name": device, "project": project}}


def test_save_publish_replace_obsolete_and_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    _write_yaml(tmp_path, "1.0.0")

    first = controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))
    assert first["status"] == "saved"
    assert first["compiled_at"] == "2025-10-09T08:53:20+00:00"
    assert first["esphome_version"] == "2026.11.0b0"
    assert first["config_hash"] == "1234abcd"
    assert (controller.root / first["yaml"]).is_file()
    assert (controller.root / first["bin"]).read_bytes() == b"first firmware"

    binary = tmp_path / "build" / "firmware.bin"
    binary.write_bytes(b"replacement firmware")
    replaced = controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))
    assert replaced["status"] == "saved"
    assert (controller.root / replaced["bin"]).read_bytes() == b"replacement firmware"

    controller._publish_sync("garden", "1.0.0")
    with pytest.raises(FileExistsError):
        controller._save_sync("garden.yaml", _resolved_config("garden", "1.0.0"))

    _write_yaml(tmp_path, "2.0.0")
    second = controller._save_sync("garden.yaml", _resolved_config("garden", "2.0.0"))
    controller._publish_sync("garden", "2.0.0")
    assert controller._records[("garden", "1.0.0")]["status"] == "obsolete"
    assert controller._records[("garden", "2.0.0")]["status"] == "published"

    controller._publish_sync("garden", "1.0.0")
    assert controller._records[("garden", "1.0.0")]["status"] == "published"
    assert controller._records[("garden", "2.0.0")]["status"] == "obsolete"
    controller._delete_sync("garden", "1.0.0")
    assert not (controller.root / "garden.json").exists()
    assert not (controller.root / first["yaml"]).exists()
    assert (controller.root / second["yaml"]).exists()
    persisted = json.loads((controller.root / "repository.json").read_text(encoding="utf-8"))
    assert persisted["firmwares"][0]["status"] == "obsolete"


def test_save_uses_build_esphome_version_when_project_version_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(repository_module, "_installed_esphome_version", "2026.10.2")
    (tmp_path / "garden.yaml").write_text("esphome:\n  name: garden\n", encoding="utf-8")

    record = controller._save_sync("garden.yaml", _resolved_config("garden", None))

    assert record["version"] == "2026.11.0b0"


@pytest.mark.asyncio
async def test_save_uses_resolved_substitution_and_package_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_resolution_yaml, tmp_path / "garden.yaml")
    devices = object()
    firmware = SimpleNamespace(_validate_configuration_boundary=AsyncMock())
    controller._db.devices = devices
    controller._db.firmware = firmware
    resolved = _resolved_config("resolved-garden", "7.8.9")
    resolve_mock = AsyncMock(return_value=(resolved, True))
    monkeypatch.setattr(repository_module, "resolve_config", resolve_mock)

    record = await controller.save_firmware(configuration="garden.yaml")

    resolve_mock.assert_awaited_once_with(devices, tmp_path / "garden.yaml")
    assert record["device"] == "resolved-garden"
    assert record["version"] == "7.8.9"
    assert (controller.root / record["yaml"]).is_file()


@pytest.mark.asyncio
async def test_public_update_routes_serve_published_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = await run_in_executor(_controller, tmp_path, monkeypatch)
    await run_in_executor(_write_yaml, tmp_path, "1.0.0")
    record = await run_in_executor(
        controller._save_sync, "garden.yaml", _resolved_config("garden", "1.0.0")
    )
    await run_in_executor(controller._publish_sync, "garden", "1.0.0")

    app = web.Application()
    app.router.add_get("/update/firmware", controller._http_firmware)
    app.router.add_get("/update/md5", controller._http_md5)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        manifest_response = await client.get("/update/firmware?device=garden")
        assert manifest_response.status == 200
        manifest: dict[str, Any] = await manifest_response.json()
        assert manifest["version"] == "1.0.0"
        assert manifest["compiled_at"] == "2025-10-09T08:53:20+00:00"
        assert manifest["esphome_version"] == "2026.11.0b0"
        assert manifest["config_hash"] == "1234abcd"

        firmware_response = await client.get("/update/firmware?device=garden&version=1.0.0")
        assert firmware_response.headers["Content-Disposition"] == (
            'attachment; filename="1.0.0.bin"'
        )
        assert await firmware_response.read() == b"first firmware"

        md5_response = await client.get("/update/md5?device=garden&version=1.0.0")
        assert (
            await md5_response.text()
            == hashlib.md5(b"first firmware", usedforsecurity=False).hexdigest()
        )

        hidden_response = await client.get("/update/firmware?device=garden&version=2.0.0")
        assert hidden_response.status == 404
        assert record["status"] == "saved"
    finally:
        await client.close()
