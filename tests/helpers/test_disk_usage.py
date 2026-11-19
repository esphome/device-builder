"""Tests for the disk usage walker."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers import disk_usage
from esphome_device_builder.helpers.disk_usage import (
    ToolchainSizeCache,
    build_roots,
    measure_disk_usage,
    read_volume_space,
    refresh_volume_space,
)
from esphome_device_builder.models import DiskUsage


def _write(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _measure(
    config: Path,
    data: Path,
    cache: ToolchainSizeCache | None = None,
    device_build_dirs: tuple[Path, ...] = (),
) -> DiskUsage:
    builds = build_roots(data.resolve(), device_build_dirs)
    return measure_disk_usage(config, data, builds, cache or ToolchainSizeCache())


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
    usage = _measure(config, data)
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
    usage = _measure(tmp_path / "config", data)
    assert usage.toolchains_bytes == 400
    assert usage.cache_bytes == 20


def test_hardlinked_file_counts_once(layout: tuple[Path, Path, Path]) -> None:
    config, data, _ = layout
    os.link(data / "build" / "kitchen" / "firmware.bin", config / "firmware-copy.bin")
    usage = _measure(config, data)
    assert usage.builds_bytes == 1000
    assert usage.config_bytes == 150


def test_symlinks_are_not_followed(layout: tuple[Path, Path, Path]) -> None:
    config, data, toolchains = layout
    (config / "toolchains-link").symlink_to(toolchains)
    usage = _measure(config, data)
    assert usage.config_bytes == 150


def test_toolchain_size_reused_until_a_package_changes(
    layout: tuple[Path, Path, Path],
) -> None:
    config, data, toolchains = layout
    cache = ToolchainSizeCache()
    _measure(config, data, cache)
    # Grows a file without touching any dir entry: the cached total stands.
    _write(toolchains / "packages" / "toolchain-xtensa" / "gcc", 6000)
    assert _measure(config, data, cache).toolchains_bytes == 5000
    _write(toolchains / "packages" / "framework-espidf" / "idf.py", 700)
    assert _measure(config, data, cache).toolchains_bytes == 6700


def test_missing_dirs_measure_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk_usage, "toolchain_dirs", list)
    usage = _measure(tmp_path / "config", tmp_path / "data")
    assert usage.builds_bytes == usage.cache_bytes == usage.config_bytes == 0
    assert usage.total_bytes > 0


def test_refresh_volume_space_keeps_categories(
    layout: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data, _ = layout
    usage = _measure(config, data)
    monkeypatch.setattr(disk_usage, "read_volume_space", lambda _paths: (900, 123))
    fresh = refresh_volume_space(usage, (data,))
    assert (fresh.total_bytes, fresh.free_bytes) == (900, 123)
    assert fresh.builds_bytes == usage.builds_bytes
    assert fresh.measured_at == usage.measured_at


def test_build_path_env_root_outside_data_dir(
    layout: tuple[Path, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, data, _ = layout
    build_volume = tmp_path / "build-volume"
    _write(build_volume / "kitchen" / "firmware.bin", 800)
    monkeypatch.setenv("ESPHOME_BUILD_PATH", str(build_volume))
    usage = _measure(config, data)
    assert usage.builds_bytes == 800
    # The default build dir is no longer a build root, so it reads as cache.
    assert usage.cache_bytes == 1030


def test_per_device_build_path_counts_as_builds(layout: tuple[Path, Path, Path]) -> None:
    config, data, _ = layout
    custom = _write(data / "custom-builds" / "porch" / "firmware.bin", 400).parent
    usage = _measure(config, data, device_build_dirs=(custom,))
    assert usage.builds_bytes == 1400
    assert usage.cache_bytes == 30


def test_split_root_remote_extracts_count_as_remote_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    data = tmp_path / "data"
    _write(config / "kitchen.yaml", 100)
    _write(config / ".esphome" / ".remote_builds" / "abcd1234" / "porch.tar.gz", 60)
    _write(data / ".remote_builds" / "abcd1234" / ".esphome" / "build" / "porch.bin", 900)
    monkeypatch.setattr(disk_usage, "toolchain_dirs", list)
    usage = _measure(config, data)
    assert usage.remote_builds_bytes == 960
    assert usage.config_bytes == 100


def test_toolchain_size_rewalked_when_a_nested_framework_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "esphome-cache"
    _write(cache_root / "idf" / "frameworks" / "5.4.0" / "idf.py", 300)
    monkeypatch.setattr(disk_usage, "toolchain_dirs", lambda: [cache_root.resolve()])
    cache = ToolchainSizeCache()
    assert _measure(tmp_path / "config", tmp_path / "data", cache).toolchains_bytes == 300
    _write(cache_root / "idf" / "frameworks" / "5.5.1" / "idf.py", 500)
    assert _measure(tmp_path / "config", tmp_path / "data", cache).toolchains_bytes == 800


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions, non-root"
)
def test_unreadable_toolchain_dir_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "platformio"
    _write(root / "packages" / "gcc", 100)
    locked = _write(root / "packages" / "locked" / "secret", 50).parent
    monkeypatch.setattr(disk_usage, "toolchain_dirs", lambda: [root.resolve()])
    cache = ToolchainSizeCache()
    locked.chmod(0)
    try:
        assert _measure(tmp_path / "config", tmp_path / "data", cache).toolchains_bytes == 100
        assert cache.signature == ()
    finally:
        locked.chmod(0o755)
    assert _measure(tmp_path / "config", tmp_path / "data", cache).toolchains_bytes == 150


def test_read_volume_space_reports_the_fullest_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    spaces = {Path("/data"): (1000, 400), Path("/build"): (500, 50)}
    monkeypatch.setattr(disk_usage, "_volume_space", spaces.get)
    assert read_volume_space(spaces) == (500, 50)
    assert read_volume_space(()) == (0, 0)
