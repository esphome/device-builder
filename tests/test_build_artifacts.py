"""Tests for :mod:`helpers.build_artifacts`.

Pins the disk-walking discovery layer the receiver-side
``download_artifacts`` flow (issue #106) leans on, plus the
``firmware.bin`` flash-offset platform-detection that
keeps the offloader from having to reimplement upstream
esphome's ``CORE.is_esp32`` decision.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esphome_device_builder.helpers.build_artifacts import (
    _firmware_offset_for_platform,
    load_build_artifacts,
)

from ._storage_fixtures import write_storage_json


def _write_idedata(tmp_path: Path, name: str, payload: object) -> Path:
    """Write ``<tmp_path>/.esphome/idedata/<name>.json`` with *payload*.

    Mirrors :func:`helpers.storage_path.resolve_idedata_path`'s lookup so
    :func:`load_build_artifacts` finds it.
    """
    idedata_dir = tmp_path / ".esphome" / "idedata"
    idedata_dir.mkdir(parents=True, exist_ok=True)
    path = idedata_dir / f"{name}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_build_artifacts_happy_path_with_extras(tmp_path: Path) -> None:
    """ESP32 build with extras returns firmware.bin first + every existing extra."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    bootloader = tmp_path / "bootloader.bin"
    bootloader.write_bytes(b"BL")
    partitions = tmp_path / "partitions.bin"
    partitions.write_bytes(b"PT")

    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    _write_idedata(
        tmp_path,
        "kitchen",
        {
            "extra": {
                "flash_images": [
                    {"path": str(bootloader), "offset": "0x1000"},
                    {"path": str(partitions), "offset": "0x8000"},
                ]
            }
        },
    )

    artifacts = load_build_artifacts("kitchen.yaml")

    assert [img.path.name for img in artifacts.flash_images] == [
        "firmware.bin",
        "bootloader.bin",
        "partitions.bin",
    ]
    assert artifacts.flash_images[0].offset == "0x10000"  # ESP32 firmware partition
    assert artifacts.flash_images[1].offset == "0x1000"
    assert artifacts.flash_images[2].offset == "0x8000"
    parsed = json.loads(artifacts.idedata_bytes)
    assert parsed["extra"]["flash_images"][0]["path"] == str(bootloader)


def test_load_build_artifacts_missing_storage_raises(tmp_path: Path) -> None:
    """No StorageJSON sidecar on disk → :class:`FileNotFoundError`."""
    with pytest.raises(FileNotFoundError, match="StorageJSON sidecar missing"):
        load_build_artifacts("never-compiled.yaml")


def test_load_build_artifacts_unset_firmware_bin_raises(tmp_path: Path) -> None:
    """StorageJSON with ``firmware_bin_path=None`` → :class:`FileNotFoundError`."""
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=None)
    with pytest.raises(FileNotFoundError, match="firmware_bin_path unset"):
        load_build_artifacts("kitchen.yaml")


def test_load_build_artifacts_missing_firmware_bin_raises(tmp_path: Path) -> None:
    """StorageJSON points at a firmware.bin that's been wiped → :class:`FileNotFoundError`."""
    missing = tmp_path / "wiped" / "firmware.bin"
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=missing)
    with pytest.raises(FileNotFoundError, match="firmware_bin_path missing"):
        load_build_artifacts("kitchen.yaml")


def test_load_build_artifacts_missing_idedata_raises(tmp_path: Path) -> None:
    """StorageJSON intact but ``idedata.json`` missing → :class:`FileNotFoundError`."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    # No call to _write_idedata — idedata is absent.
    with pytest.raises(FileNotFoundError, match=r"idedata\.json missing"):
        load_build_artifacts("kitchen.yaml")


def test_load_build_artifacts_skips_missing_extra_image(tmp_path: Path) -> None:
    """An idedata-declared extra whose file doesn't exist is logged + skipped."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    _write_idedata(
        tmp_path,
        "kitchen",
        {
            "extra": {
                "flash_images": [
                    {"path": str(tmp_path / "ghost.bin"), "offset": "0x1000"},
                ]
            }
        },
    )

    artifacts = load_build_artifacts("kitchen.yaml")

    # Firmware bin survives; ghost extra is silently dropped.
    assert [img.path.name for img in artifacts.flash_images] == ["firmware.bin"]


def test_load_build_artifacts_skips_malformed_flash_image_entry(tmp_path: Path) -> None:
    """A non-dict entry in ``flash_images`` is skipped without raising."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    _write_idedata(
        tmp_path,
        "kitchen",
        {
            "extra": {
                "flash_images": [
                    "not-a-dict",  # malformed entry — must skip
                    None,  # also malformed
                ]
            }
        },
    )

    artifacts = load_build_artifacts("kitchen.yaml")

    assert [img.path.name for img in artifacts.flash_images] == ["firmware.bin"]


def test_load_build_artifacts_skips_entry_missing_path_or_offset(tmp_path: Path) -> None:
    """Dict entry with empty / missing path or offset is skipped."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    _write_idedata(
        tmp_path,
        "kitchen",
        {
            "extra": {
                "flash_images": [
                    {"path": "", "offset": "0x1000"},  # empty path
                    {"offset": "0x1000"},  # missing path
                    {"path": str(firmware_bin)},  # missing offset
                ]
            }
        },
    )

    artifacts = load_build_artifacts("kitchen.yaml")

    assert [img.path.name for img in artifacts.flash_images] == ["firmware.bin"]


def test_load_build_artifacts_handles_non_dict_extra(tmp_path: Path) -> None:
    """``extra`` field that isn't a dict (e.g. ``null``) yields no extras."""
    firmware_bin = tmp_path / "firmware.bin"
    firmware_bin.write_bytes(b"FW")
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)
    _write_idedata(tmp_path, "kitchen", {"extra": None})  # parses but isn't a dict

    artifacts = load_build_artifacts("kitchen.yaml")

    assert [img.path.name for img in artifacts.flash_images] == ["firmware.bin"]


@pytest.mark.parametrize(
    "platform,expected_offset",
    [
        ("esp32", "0x10000"),
        ("ESP32", "0x10000"),  # case-insensitive prefix match
        ("esp32s3", "0x10000"),
        ("esp32c3", "0x10000"),
        ("esp32h2", "0x10000"),
        ("esp8266", "0x0"),
        ("rp2040", "0x0"),
        ("libretiny", "0x0"),
        ("", "0x0"),  # empty string falls through
    ],
)
def test_firmware_offset_for_platform(platform: str, expected_offset: str) -> None:
    """ESP32 family → ``0x10000``; everything else → ``0x0``."""
    assert _firmware_offset_for_platform(platform) == expected_offset
