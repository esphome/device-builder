"""Regression tests for ``DeviceScanner`` device ordering.

Previously ``DeviceScanner._do_scan`` populated ``self._devices``
from a Python ``set`` of paths, so insertion order — and therefore
the order the dashboard rendered devices — was randomised by the
interpreter's hash seed. Each restart shuffled the dashboard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
)
from esphome_device_builder.models import Device


def _write_yaml(config_dir: Path, name: str) -> Path:
    path = config_dir / f"{name}.yaml"
    path.write_text(f"esphome:\n  name: {name}\n", encoding="utf-8")
    return path


def _stub_metadata(_config_dir: Path, _filename: str) -> DeviceFileMetadata:
    return DeviceFileMetadata(board_id="", ip="", expected_config_hash="")


def _fake_load(path: Path, *_args: object, **_kwargs: object) -> Device:
    """Stand-in for ``load_device_from_storage`` — names match the filename."""
    name = path.stem
    return Device(name=name, friendly_name=name, configuration=path.name)


def _make_scanner(config_dir: Path) -> DeviceScanner:
    return DeviceScanner(
        config_dir=config_dir,
        get_metadata=_stub_metadata,
        on_change=lambda _kind, _device: None,
    )


@pytest.fixture
def shuffled_yamls(tmp_path: Path) -> list[str]:
    """Create YAMLs whose creation order differs from lexicographic order."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    creation_order = ["zeta", "alpha", "mike", "bravo", "yankee", "delta"]
    for name in creation_order:
        _write_yaml(cfg, name)
    return creation_order


@pytest.fixture(autouse=True)
def _stub_load_device() -> object:
    """Bypass YAML/StorageJSON parsing — these tests are about ordering."""
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=_fake_load,
    ) as p:
        yield p


async def test_initial_scan_returns_devices_lexicographic(
    tmp_path: Path, shuffled_yamls: list[str]
) -> None:
    cfg = tmp_path / "configs"
    scanner = _make_scanner(cfg)
    await scanner.scan()

    names = [d.name for d in scanner.devices]
    assert names == sorted(shuffled_yamls)
    assert names != shuffled_yamls  # the test would be vacuous otherwise


async def test_added_yaml_inserts_in_sorted_position(
    tmp_path: Path, shuffled_yamls: list[str]
) -> None:
    cfg = tmp_path / "configs"
    scanner = _make_scanner(cfg)
    await scanner.scan()

    _write_yaml(cfg, "charlie")
    await scanner.scan()

    names = [d.name for d in scanner.devices]
    assert names == sorted([*shuffled_yamls, "charlie"])
    # Specifically: ``charlie`` should land between bravo and delta —
    # not appended at the end.
    assert names.index("charlie") == 2


async def test_removed_yaml_keeps_remaining_sorted(
    tmp_path: Path, shuffled_yamls: list[str]
) -> None:
    cfg = tmp_path / "configs"
    scanner = _make_scanner(cfg)
    await scanner.scan()

    (cfg / "mike.yaml").unlink()
    await scanner.scan()

    names = [d.name for d in scanner.devices]
    expected = sorted(n for n in shuffled_yamls if n != "mike")
    assert names == expected


async def test_update_preserves_sorted_position(tmp_path: Path, shuffled_yamls: list[str]) -> None:
    """Touching an existing YAML must not move it in the device list."""
    cfg = tmp_path / "configs"
    scanner = _make_scanner(cfg)
    await scanner.scan()
    before = [d.name for d in scanner.devices]

    # Mutate ``alpha`` so its cache key changes and it shows up as
    # an UPDATE (not ADD) on the next scan.
    alpha = cfg / "alpha.yaml"
    alpha.write_text(alpha.read_text() + "# touch\n", encoding="utf-8")
    await scanner.scan()

    after = [d.name for d in scanner.devices]
    assert after == before


async def test_order_stable_across_multiple_scans(
    tmp_path: Path, shuffled_yamls: list[str]
) -> None:
    """Re-running the scan many times must not shuffle the device list.

    Catches regressions where ``set`` iteration order leaks back in
    even after the keyset is stable.
    """
    cfg = tmp_path / "configs"
    scanner = _make_scanner(cfg)
    await scanner.scan()
    first = [d.name for d in scanner.devices]
    for _ in range(5):
        await scanner.scan()
        assert [d.name for d in scanner.devices] == first


async def test_failed_load_does_not_break_rebuild(tmp_path: Path, _stub_load_device: Any) -> None:
    """A YAML that ``load_device_from_storage`` rejects must not crash the scan.

    Pre-fix, the rebuild comprehension assumed every key in
    ``path_to_cache_key`` had a corresponding entry in ``_devices``.
    A failed load (logged + skipped in ``_load_devices``) left the
    path in ``path_to_cache_key`` only, so the rebuild hit ``KeyError``
    and aborted — taking subsequent scans down with it.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "good_one")
    _write_yaml(cfg, "broken")
    _write_yaml(cfg, "another_good")

    def _load(path: Path, *_args: object, **_kwargs: object) -> Device:
        if path.stem == "broken":
            raise ValueError("simulated YAML parse failure")
        return Device(name=path.stem, friendly_name=path.stem, configuration=path.name)

    _stub_load_device.side_effect = _load

    scanner = _make_scanner(cfg)
    await scanner.scan()

    names = [d.name for d in scanner.devices]
    assert names == ["another_good", "good_one"]  # broken silently skipped, rest sorted
