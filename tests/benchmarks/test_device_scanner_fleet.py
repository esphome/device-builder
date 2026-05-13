"""DeviceScanner inner-loop benchmarks at fleet sizes 50 / 200."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
)

from ._fleet import synthesize_fleet


def _stub_metadata(_config_dir: Path, _filename: str) -> DeviceFileMetadata:
    """Empty-field metadata; isolates ``_load_devices`` from sidecar IO."""
    return DeviceFileMetadata(
        board_id="esp32-c3-devkitm-1",
        ip="",
        expected_config_hash="",
    )


def _make_scanner(config_dir: Path) -> DeviceScanner:
    return DeviceScanner(
        config_dir=config_dir,
        get_metadata=_stub_metadata,
        on_change=lambda _kind, _device: None,
    )


# Only fleet_size=50 here: per-device materialise pays the full YAML
# parse + sidecar read, and [200] cost too much CodSpeed sample
# budget. Cheaper fleet-size benches (cache keys, to_dict) keep
# the 50/200 ladder below.
@pytest.mark.parametrize("fleet_size", [50])
def test_load_devices_fleet(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
    fleet_size: int,
) -> None:
    """Per-device materialise cost (YAML parse + sidecar read) at fleet size N."""
    paths = synthesize_fleet(tmp_path, fleet_size)
    paths_set = set(paths)
    scanner = _make_scanner(tmp_path)

    warm = scanner._load_devices(paths_set)
    assert len(warm) == fleet_size

    @benchmark
    def run() -> None:
        scanner._load_devices(paths_set)


@pytest.mark.parametrize("fleet_size", [50, 200])
def test_build_cache_keys_fleet(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
    fleet_size: int,
) -> None:
    """N stat-per-file cost: the rescan overhead with nothing changed."""
    synthesize_fleet(tmp_path, fleet_size)
    scanner = _make_scanner(tmp_path)

    warm = scanner._build_cache_keys()
    assert len(warm) == fleet_size

    @benchmark
    def run() -> None:
        scanner._build_cache_keys()
