#!/usr/bin/env python3
"""
Smoke-test the imported bluetooth-proxy board catalog after sync_bluetooth_proxies.

Re-runs the extraction against a few well-known upstream configs and asserts
each lands the right board / variant / package URL / ethernet shape. Catches:

- an upstream file move or rename (broken ``remote_id`` / package URL)
- an ethernet-block change the sync silently mis-mines
- an identity-table drift (a device losing its curated name or flag)

Designed to run in CI right after ``script/sync_bluetooth_proxies.py``,
before the diff check / PR creation. Exits non-zero on the first violation.

Run locally:

    python script/check_bluetooth_proxy_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from script._repo_cache import ensure_shallow_git_repo
from script.sync_bluetooth_proxies import (
    _PROXIES_CLONE_DIR,
    _PROXIES_REPO_BRANCH,
    _PROXIES_REPO_URL,
    _iter_configs,
    _make_record,
)

_EXPECTED_OK: list[dict[str, Any]] = [
    {
        "remote_id": "olimex/olimex-esp32-poe-iso",
        "id": "olimex-esp32-poe-iso-bluetooth-proxy",
        "board": "esp32-poe-iso",
        "variant": "esp32",
        "package_import_url": (
            "github://esphome/bluetooth-proxies/olimex/olimex-esp32-poe-iso.yaml@main"
        ),
        "featured_flag": True,
        "ethernet_pins": {12, 17, 18, 23},
    },
    {
        "remote_id": "seeed/seeed-esp32-poe",
        "id": "seeed-esp32-poe-bluetooth-proxy",
        "board": "seeed_xiao_esp32_s3_plus",
        "variant": "esp32s3",
        "package_import_url": (
            "github://esphome/bluetooth-proxies/seeed/seeed-esp32-poe.yaml@main"
        ),
        "featured_flag": True,
        "ethernet_pins": {2, 7, 8, 9, 10},
    },
    {
        "remote_id": "gl-inet/gl-s10",
        "id": "gl-s10-bluetooth-proxy",
        "board": "esp32dev",
        "variant": "esp32",
        "package_import_url": "github://esphome/bluetooth-proxies/gl-inet/gl-s10.yaml@main",
        "ethernet_pins": {0, 5, 18, 23},
        "description_contains": "v2.x hardware revision",
    },
    {
        "remote_id": "esp32-generic/esp32-generic",
        "id": "esp32-generic-bluetooth-proxy",
        "board": "esp32dev",
        "variant": "esp32",
        "package_import_url": (
            "github://esphome/bluetooth-proxies/esp32-generic/esp32-generic.yaml@main"
        ),
        "is_generic": True,
        "ethernet_pins": None,
    },
]


def _check_ok(record: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Return a list of mismatch errors when *record* doesn't match *spec*."""
    remote_id = spec["remote_id"]
    esphome = record.get("esphome", {})
    errors: list[str] = [
        f"{remote_id}.{key}: expected {spec[key]!r}, got {actual!r}"
        for key, actual in (
            ("id", record.get("id")),
            ("package_import_url", record.get("package_import_url")),
            ("board", esphome.get("board")),
            ("variant", esphome.get("variant")),
        )
        if actual != spec[key]
    ]
    if spec.get("featured_flag") and record.get("featured") is not True:
        errors.append(f"{remote_id}: expected featured: true")
    if spec.get("is_generic") and record.get("is_generic") is not True:
        errors.append(f"{remote_id}: expected is_generic: true")
    if (needle := spec.get("description_contains")) and needle not in record.get("description", ""):
        errors.append(f"{remote_id}: description missing {needle!r}")
    errors.extend(_check_ethernet(record, spec))
    return errors


def _check_ethernet(record: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Match the mined ethernet signal: connectivity claim plus pin occupancy."""
    remote_id = spec["remote_id"]
    connectivity = (record.get("hardware") or {}).get("connectivity") or []
    pins = {pin["gpio"] for pin in record.get("pins") or []}
    errors: list[str] = []
    if record.get("featured_components"):
        # A featured entry would surface as an addable card that vendors the
        # pinout locally, defeating the remote-package shape.
        errors.append(f"{remote_id}: unexpected featured_components on a package board")
    if spec["ethernet_pins"] is None:
        if "ethernet" in connectivity or pins:
            errors.append(f"{remote_id}: unexpected ethernet signal on a Wi-Fi board")
        return errors
    if "ethernet" not in connectivity:
        errors.append(f"{remote_id}: connectivity missing ethernet")
    if pins != spec["ethernet_pins"]:
        errors.append(
            f"{remote_id}.pins: expected {sorted(spec['ethernet_pins'])}, got {sorted(pins)}"
        )
    return errors


def main() -> int:
    """Re-extract the expected devices from the cached upstream and compare."""
    repo = ensure_shallow_git_repo(
        _PROXIES_REPO_URL,
        _PROXIES_CLONE_DIR,
        _PROXIES_REPO_BRANCH,
        label="bluetooth-proxies",
        # Inspect the same revision the sync just produced.
        pull=not _PROXIES_CLONE_DIR.is_dir(),
    )
    if repo is None:
        print("FAIL: bluetooth-proxies repo unavailable")
        return 1

    records: dict[str, dict[str, Any]] = {}
    for src in _iter_configs(repo):
        record, _skip = _make_record(src, "")
        if record is not None:
            records[src.remote_id] = record

    errors: list[str] = []
    for spec in _EXPECTED_OK:
        record = records.get(spec["remote_id"])
        if record is None:
            errors.append(f"{spec['remote_id']}: expected an imported record, got a skip")
            continue
        errors.extend(_check_ok(record, spec))

    if errors:
        print(f"FAIL: {len(errors)} mismatch(es)")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"OK: {len(_EXPECTED_OK)} bluetooth-proxy devices verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
