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
        on_change=lambda _kind, _device, _previous: None,
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


# ----------------------------------------------------------------------
# Name-keyed index — used by ``DeviceStateMonitor.apply_*`` for O(1)
# lookups when an mDNS / ping / MQTT observation arrives. The index
# has to stay in lockstep with ``_devices`` across add / update /
# rename / remove or the monitor's dedupe will silently miss devices
# (or fan out to deleted ones).
# ----------------------------------------------------------------------


async def test_index_returns_added_device(tmp_path: Path) -> None:
    """A freshly-scanned device is queryable via ``get_by_name``."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    scanner = _make_scanner(cfg)
    await scanner.scan()

    bucket = scanner.get_by_name("kitchen")
    assert len(bucket) == 1
    assert bucket[0].configuration == "kitchen.yaml"


async def test_index_drops_removed_device(tmp_path: Path) -> None:
    """A removed YAML clears its bucket — no zombie entries."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    scanner = _make_scanner(cfg)
    await scanner.scan()
    assert scanner.get_by_name("kitchen")

    (cfg / "kitchen.yaml").unlink()
    await scanner.scan()
    assert scanner.get_by_name("kitchen") == []


async def test_index_handles_yaml_rename(tmp_path: Path, _stub_load_device: Any) -> None:
    """Editing a YAML's ``esphome.name`` re-buckets it under the new name.

    Without this, mDNS lookups under the new name would miss while
    the old name's bucket would fan out to a stale Device — the
    "non-canonical copy stays Unknown" symptom that motivated the
    fan-out work in the first place.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    yaml_path = _write_yaml(cfg, "device_a")

    # First scan: bucket keyed by the YAML's name.
    name_box = {"current": "kitchen"}

    def _load(path: Path, *_args: object, **_kwargs: object) -> Device:
        return Device(
            name=name_box["current"],
            friendly_name=name_box["current"],
            configuration=path.name,
        )

    _stub_load_device.side_effect = _load
    scanner = _make_scanner(cfg)
    await scanner.scan()
    assert [d.configuration for d in scanner.get_by_name("kitchen")] == ["device_a.yaml"]

    # Simulate the user renaming the device's ``esphome.name`` and
    # touching the file (cache-key change → UPDATED scan).
    name_box["current"] = "lounge"
    yaml_path.write_text(yaml_path.read_text() + "# touch\n", encoding="utf-8")
    await scanner.scan()

    assert scanner.get_by_name("kitchen") == []
    assert [d.configuration for d in scanner.get_by_name("lounge")] == ["device_a.yaml"]


async def test_index_fans_out_to_duplicate_names(tmp_path: Path, _stub_load_device: Any) -> None:
    """Two YAMLs sharing an ``esphome.name`` end up in the same bucket.

    ``foo (1).yaml`` copies and ``dashboard_import`` siblings can
    both broadcast under the same name. mDNS broadcasts must fan
    out to every match, so the bucket has to carry both Devices.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    _write_yaml(cfg, "kitchen_copy")

    def _load(path: Path, *_args: object, **_kwargs: object) -> Device:
        # Force both YAMLs to claim the same ``esphome.name``.
        return Device(name="kitchen", friendly_name="kitchen", configuration=path.name)

    _stub_load_device.side_effect = _load
    scanner = _make_scanner(cfg)
    await scanner.scan()

    bucket = scanner.get_by_name("kitchen")
    assert sorted(d.configuration for d in bucket) == ["kitchen.yaml", "kitchen_copy.yaml"]

    # Removing one YAML must leave the sibling.
    (cfg / "kitchen.yaml").unlink()
    await scanner.scan()
    bucket = scanner.get_by_name("kitchen")
    assert [d.configuration for d in bucket] == ["kitchen_copy.yaml"]


async def test_get_by_name_returns_a_snapshot(tmp_path: Path, _stub_load_device: Any) -> None:
    """Mutations to the returned list must not corrupt the scanner's index.

    ``devices`` already returns a fresh list per call; ``get_by_name``
    matches that semantic so a careless caller (a misguided
    ``.append()`` or ``.sort()``) can't poison the name index and
    silently break dedupe / fan-out for the next mDNS announcement.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    _stub_load_device.side_effect = lambda path, *_a, **_kw: Device(
        name="kitchen", friendly_name="kitchen", configuration=path.name
    )
    scanner = _make_scanner(cfg)
    await scanner.scan()

    bucket = scanner.get_by_name("kitchen")
    bucket.clear()  # caller misbehaves
    bucket.append(Device(name="kitchen", friendly_name="kitchen", configuration="ghost.yaml"))

    fresh = scanner.get_by_name("kitchen")
    assert [d.configuration for d in fresh] == ["kitchen.yaml"]


async def test_index_bucket_order_is_deterministic(tmp_path: Path, _stub_load_device: Any) -> None:
    """Buckets with duplicate names must keep a stable ordering across scans.

    ``_find_device_by_name`` returns ``bucket[0]`` and ``apply()``
    dedupes state against that single device — if the load loop's
    iteration order is set-derived, "first match" is non-
    deterministic and dedupe can flip flop between Devices on every
    scan, leaking spurious state events. Sort by the configuration
    filename so the bucket order is reproducible.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    # Three siblings, intentionally written in non-alphabetical
    # creation order so any set-iteration shuffling shows up.
    for name in ("zebra", "alpha", "mike"):
        _write_yaml(cfg, name)
    _stub_load_device.side_effect = lambda path, *_a, **_kw: Device(
        name="duplicate", friendly_name="duplicate", configuration=path.name
    )

    scanner = _make_scanner(cfg)
    await scanner.scan()
    first = [d.configuration for d in scanner.get_by_name("duplicate")]

    # Touch each YAML in a different order — re-runs of ``_load_devices``
    # must produce the same bucket order regardless.
    for name in ("alpha", "zebra", "mike"):
        path = cfg / f"{name}.yaml"
        path.write_text(path.read_text() + "# touch\n", encoding="utf-8")
    await scanner.scan()

    second = [d.configuration for d in scanner.get_by_name("duplicate")]
    assert first == second
    assert first == sorted(first)  # specifically: lexicographic by configuration
