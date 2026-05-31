"""Coverage for the ``firmware/download_token`` WS command.

It mints the single-use capability token (+ the canonical filename) that
authorizes one HTTP artifact download (``GET /api/firmware/download``). It
validates the configuration boundary and resolves the artifact up front, so a
missing file fails here and the returned filename matches what the download
saves under (``Content-Disposition``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _seed_build(tmp_path: Path, monkeypatch: Any, *, files: tuple[str, ...]) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.download.resolve_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )
    build_dir = tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in files:
        (build_dir / name).write_bytes(b"x")
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=build_dir / "firmware.bin",
        overrides={"esp_platform": "esp32"},
    )


async def test_download_token_mints_token_and_filename(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    _seed_build(tmp_path, monkeypatch, files=("firmware.elf",))
    controller = firmware_controller_factory()

    result = await controller.download_token(configuration="kitchen.yaml", file="firmware.elf")

    # The filename matches what http_download will set in Content-Disposition.
    assert result["filename"] == "kitchen-firmware.elf"
    token = result["token"]
    # Single-use, bound to the requested artifact.
    assert controller.download_tokens.consume(token) == ("kitchen.yaml", "firmware.elf")
    assert controller.download_tokens.consume(token) is None


async def test_download_token_missing_artifact_errors(
    tmp_path: Path, monkeypatch: Any, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A built device missing the requested file fails at mint, not at download."""
    _seed_build(tmp_path, monkeypatch, files=())  # build dir present, no firmware.elf
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller.download_token(configuration="kitchen.yaml", file="firmware.elf")
    assert excinfo.value.code == ErrorCode.NOT_FOUND


async def test_download_token_rejects_traversal(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/download_token`` re-validates the configuration boundary."""
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller.download_token(configuration="../etc/passwd", file="firmware.bin")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
