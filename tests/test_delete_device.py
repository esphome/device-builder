"""
Tests for ``DevicesController._delete_single``.

The legacy dashboard wiped ``<config_dir>/.esphome/build/<name>/``
on archive (``shutil.rmtree(storage_json.build_path, ...)``); the
new backend skipped it, so repeated create-delete cycles leaked
hundreds of MB of PlatformIO state per device and a recycled name
picked up stale build artefacts on the next compile. The fix
reads ``StorageJSON.build_path`` and ``shutil.rmtree``s it before
the YAML is unlinked, so a partial failure leaves the user able
to retry.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController


def _make_controller(config_dir: Path) -> DevicesController:
    """Build a bare-bones controller wired to *config_dir* on disk."""
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.settings.rel_path = lambda configuration: config_dir / configuration
    controller._scanner = MagicMock()
    controller._scanner.scan = AsyncMock()
    return controller


def _seed_device(
    config_dir: Path, configuration: str, *, with_build_dir: bool = True
) -> tuple[Path, Path]:
    """Lay out a YAML, StorageJSON sidecar, and (optionally) the build tree.

    Returns ``(yaml_path, build_path)`` so the test can assert the
    rmtree happened on the right directory.
    """
    yaml_path = config_dir / configuration
    yaml_path.write_text(f"esphome:\n  name: {Path(configuration).stem}\n", encoding="utf-8")

    build_path = config_dir / ".esphome" / "build" / Path(configuration).stem
    if with_build_dir:
        build_path.mkdir(parents=True, exist_ok=True)
        # Drop a couple of files so the rmtree has something to remove —
        # an empty directory would still be unlinked but wouldn't catch
        # the bug where the rmtree never runs at all.
        (build_path / "firmware.bin").write_bytes(b"\x00" * 16)
        (build_path / "src").mkdir()
        (build_path / "src" / "main.cpp").write_text("// fake\n", encoding="utf-8")

    storage_dir = config_dir / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / f"{configuration}.json").write_text(
        json.dumps(
            {
                "storage_version": 1,
                "name": Path(configuration).stem,
                "comment": None,
                "esphome_version": "2026.5.0-dev",
                "src_version": 1,
                "address": "",
                "web_port": None,
                "esp_platform": "esp32",
                "board": "esp32-c3-devkitm-1",
                "build_path": str(build_path),
                "firmware_bin_path": str(build_path / ".pioenvs" / "firmware.bin"),
                "loaded_integrations": [],
                "loaded_platforms": [],
                "no_mdns": False,
                "framework": "esp-idf",
                "core_platform": "esp32",
            }
        ),
        encoding="utf-8",
    )
    return yaml_path, build_path


@pytest.fixture
def _patch_ext_storage(monkeypatch: Any, tmp_path: Path) -> None:
    """Redirect ``ext_storage_path`` away from CORE.

    ``ext_storage_path`` walks ``CORE.config_path`` which isn't set
    in the test process; pin it to the tmp config directory so the
    on-disk sidecar laid down by ``_seed_device`` is the one the
    delete path reads.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.ext_storage_path",
        lambda configuration: tmp_path / ".esphome" / "storage" / f"{configuration}.json",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("_patch_ext_storage")
async def test_delete_wipes_build_directory(tmp_path: Path) -> None:
    """The PlatformIO build tree goes away with the device.

    Without this, a recycled device name picks up stale ``.pioenvs``
    state on the next compile and we leak disk on every churn.
    """
    controller = _make_controller(tmp_path)
    yaml_path, build_path = _seed_device(tmp_path, "kitchen.yaml")

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()
    assert not build_path.exists()
    assert not (tmp_path / ".esphome" / "storage" / "kitchen.yaml.json").exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_patch_ext_storage")
async def test_delete_succeeds_when_never_compiled(tmp_path: Path) -> None:
    """A device that's never been built has no sidecar — delete must still succeed."""
    controller = _make_controller(tmp_path)
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_patch_ext_storage")
async def test_delete_tolerates_missing_build_directory(tmp_path: Path) -> None:
    """Sidecar present but build tree already wiped — delete must not raise."""
    controller = _make_controller(tmp_path)
    yaml_path, build_path = _seed_device(tmp_path, "kitchen.yaml", with_build_dir=False)
    assert not build_path.exists()

    await controller._delete_single("kitchen.yaml")

    assert not yaml_path.exists()


@pytest.mark.asyncio
async def test_delete_raises_when_yaml_missing(tmp_path: Path) -> None:
    """Missing YAML pre-check still fires before any cleanup runs."""
    controller = _make_controller(tmp_path)

    with pytest.raises(FileNotFoundError):
        await controller._delete_single("ghost.yaml")
