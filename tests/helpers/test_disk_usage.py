"""Tests for the disk usage walker."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from esphome_device_builder.helpers import disk_usage
from esphome_device_builder.helpers.disk_usage import (
    ToolchainSizeCache,
    measure_disk_usage,
    refresh_volume_space,
)


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """Config dir holding a default-mode data dir, plus a toolchain root outside both."""
    config = tmp_path / "config"
    data = config / ".esphome"
    toolchains = tmp_path / "platformio"
    _write(config / "kitchen.yaml", 100)
    _write(config / ".git" / "objects" / "ab", 50)
    _write(data / "build" / "kitchen" / "firmware.bin", 1000)
    _write(data / ".remote_builds" / "venvs" / "esphome-2026.11.0" / "lib.py", 300)
    _write(data / "external_components" / "foo.h", 20)
    _write(data / "storage" / "kitchen.yaml.json", 10)
    _write(toolchains / "packages" / "toolchain-xtensa" / "gcc", 5000)
    monkeypatch.setattr(disk_usage, "toolchain_dirs", lambda: [toolchains.resolve()])
    return config, data, toolchains


def test_categories_are_disjoint(layout: tuple[Path, Path, Path]) -> None:
    config, data, _ = layout
    usage = measure_disk_usage(config, data, ToolchainSizeCache())
    assert usage.builds_bytes == 1000
    assert usage.remote_builds_bytes == 300
    assert usage.toolchains_bytes == 5000
    assert usage.cache_bytes == 30
    assert usage.config_bytes == 150
    assert 0 < usage.free_bytes <= usage.total_bytes


def test_toolchain_root_inside_data_dir_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    pio = data / "platformio"
    _write(pio / "packages" / "gcc", 400)
    _write(data / "external_components" / "foo.h", 20)
    monkeypatch.setattr(
        disk_usage, "toolchain_dirs", lambda: [pio.resolve(), (pio / "packages").resolve()]
    )
    usage = measure_disk_usage(tmp_path / "config", data, ToolchainSizeCache())
    assert usage.toolchains_bytes == 400
    assert usage.cache_bytes == 20


def test_hardlinked_file_counts_once(layout: tuple[Path, Path, Path]) -> None:
    config, data, _ = layout
    os.link(data / "build" / "kitchen" / "firmware.bin", config / "firmware-copy.bin")
    usage = measure_disk_usage(config, data, ToolchainSizeCache())
    assert usage.builds_bytes == 1000
    assert usage.config_bytes == 150


def test_symlinks_are_not_followed(layout: tuple[Path, Path, Path]) -> None:
    config, data, toolchains = layout
    (config / "toolchains-link").symlink_to(toolchains)
    usage = measure_disk_usage(config, data, ToolchainSizeCache())
    assert usage.config_bytes == 150


def test_toolchain_size_reused_until_a_package_changes(
    layout: tuple[Path, Path, Path],
) -> None:
    config, data, toolchains = layout
    cache = ToolchainSizeCache()
    measure_disk_usage(config, data, cache)
    # Grows a file without touching any dir entry: the cached total stands.
    _write(toolchains / "packages" / "toolchain-xtensa" / "gcc", 6000)
    assert measure_disk_usage(config, data, cache).toolchains_bytes == 5000
    _write(toolchains / "packages" / "framework-espidf" / "idf.py", 700)
    assert measure_disk_usage(config, data, cache).toolchains_bytes == 6700


def test_missing_dirs_measure_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_usage, "toolchain_dirs", list)
    usage = measure_disk_usage(tmp_path / "config", tmp_path / "data", ToolchainSizeCache())
    assert usage.builds_bytes == usage.cache_bytes == usage.config_bytes == 0
    assert usage.total_bytes > 0


def test_refresh_volume_space_keeps_categories(
    layout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data, _ = layout
    usage = measure_disk_usage(config, data, ToolchainSizeCache())
    monkeypatch.setattr(disk_usage, "read_volume_space", lambda _d: (900, 123))
    fresh = refresh_volume_space(usage, data)
    assert (fresh.total_bytes, fresh.free_bytes) == (900, 123)
    assert fresh.builds_bytes == usage.builds_bytes
    assert fresh.measured_at == usage.measured_at
