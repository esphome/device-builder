#!/usr/bin/env python3
"""
Smoke-test the imported device-board catalog after sync_esphome_devices.

Re-runs ``sync_esphome_devices.py`` worth of extraction logic against a
small list of well-known upstream pages and asserts that each lands the
right ``board:``, ``variant:``, and at least one expected featured
component preset. Catches:

- A breaking change in the upstream front-matter contract
- A SoC family or variant rename
- Featured-component extraction regressions
- A change in the components catalog that quietly drops a
  ``component_id`` we depend on

Designed to run in CI right after ``script/sync_esphome_devices.py``,
before the diff check / PR creation. Exits non-zero on the first
violation with a "[device].[expectation] expected X, got Y" message so
the operator can read the workflow log without spelunking.

Run locally:

    python script/check_device_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Allow running via ``python script/check_device_catalog.py`` without
# installing the package — keeps the smoke test runnable in an
# uninstalled CI checkout, same pattern as ``check_catalog.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from script.sync_esphome_devices import (
    _DEVICES_CLONE_DIR,
    _ensure_devices_repo,
    _get_repo_revision,
    _iter_devices,
    _load_components_index,
    _make_record,
)

# A handful of upstream pages we expect to import cleanly with the
# right shape. Picked to cover:
#
# - the simplest classic case (Sonoff BASIC R2 v1.4 — esp8266/esp8285)
# - an esp32 variant-only config (Shelly EM Gen3 — esp32c3 + esp-idf)
# - a multi-output light bulb (Athom BR30 — five PWM outputs + rgbct)
#
# Each entry asserts the platform / board / variant / framework
# resolved by ``_make_record`` plus a list of (component_id, field, key,
# expected_value) tuples that must appear *somewhere* in the resulting
# featured_components. The smoke test fails fast on any mismatch.
_EXPECTED_OK: list[dict[str, Any]] = [
    {
        "remote_id": "Sonoff-BASIC-R2-v1.4",
        "platform": "esp8266",
        "board": "esp8285",
        "variant": None,
        "framework": None,
        "featured": [
            ("switch.gpio", "pin", "value", 12),
            ("binary_sensor.gpio", "pin", "value", {"number": 0}),
            ("light.status_led", "pin", "value", {"number": 13}),
        ],
    },
    {
        "remote_id": "Shelly-EM-Gen3",
        "platform": "esp32",
        "board": "esp32-c3-devkitm-1",
        "variant": "esp32c3",
        "framework": "esp-idf",
        "featured": [
            ("switch.gpio", "pin", "value", 0),
            ("sensor.adc", "pin", "value", 3),
        ],
    },
    {
        "remote_id": "Athom-BR30-Bulb",
        "platform": "esp8266",
        "board": "esp8285",
        "variant": None,
        "framework": None,
        "featured": [
            ("output.esp8266_pwm", "pin", "value", 4),  # red
            ("output.esp8266_pwm", "pin", "value", 12),  # green
            ("output.esp8266_pwm", "pin", "value", 14),  # blue
            ("output.esp8266_pwm", "pin", "value", 5),  # white
            ("output.esp8266_pwm", "pin", "value", 13),  # ct
        ],
    },
]

# Negative fixtures used to assert "should skip" cases would be brittle
# against a community-maintained upstream — a contributor could later
# add an inline yaml block to a page we currently expect to skip and
# break our CI without anything actually regressing in our code.
# Instead, we rely on the upstream-wide sync run (in CI, the previous
# step) to surface skip-rate drift via its summary; the smoke test
# only enforces the positive cases we care about.


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _expect_value(actual: Any, expected: Any) -> bool:
    """Match *actual* against *expected*, allowing partial dict containment."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(_expect_value(actual.get(k), v) for k, v in expected.items())
    return actual == expected


def _check_ok(record: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Return a list of mismatch errors when *record* doesn't match *spec*."""
    errors: list[str] = []
    remote_id = spec["remote_id"]

    esphome = record.get("esphome", {}) or {}
    for key in ("platform", "board", "variant", "framework"):
        actual = esphome.get(key)
        expected = spec[key]
        if actual != expected:
            errors.append(f"{remote_id}.esphome.{key}: expected {expected!r}, got {actual!r}")

    featured = record.get("featured_components") or []
    for component_id, field_key, value_key, expected_value in spec["featured"]:
        if not _featured_has(featured, component_id, field_key, value_key, expected_value):
            errors.append(
                f"{remote_id}: missing featured {component_id}.{field_key}.{value_key}={expected_value!r}"
            )
    return errors


def _featured_has(
    featured: list[dict[str, Any]],
    component_id: str,
    field_key: str,
    value_key: str,
    expected_value: Any,
) -> bool:
    """Return True when *featured* contains an entry matching the given coordinates."""
    for entry in featured:
        if entry.get("component_id") != component_id:
            continue
        fields = entry.get("fields") or {}
        preset = fields.get(field_key)
        if not isinstance(preset, dict):
            continue
        if _expect_value(preset.get(value_key), expected_value):
            return True
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the smoke test, returning ``0`` on success and ``1`` on any mismatch."""
    # Use the cache the sync step just produced — skipping the pull
    # keeps us pinned to the same upstream revision that generated the
    # manifests under review.
    repo = _ensure_devices_repo(pull=False)
    if repo is None:
        print("ERROR: Could not clone devices.esphome.io.", file=sys.stderr)
        return 1
    revision = _get_repo_revision(repo)
    components_index = _load_components_index()

    by_remote_id: dict[str, dict[str, Any]] = {}
    skipped_by_remote_id: dict[str, str] = {}

    expected_remote_ids = {s["remote_id"] for s in _EXPECTED_OK}

    for src in _iter_devices(repo):
        if src.folder_name not in expected_remote_ids:
            continue
        record, skip_reason = _make_record(src, components_index, revision)
        if skip_reason is not None:
            skipped_by_remote_id[src.folder_name] = skip_reason
        else:
            assert record is not None
            by_remote_id[src.folder_name] = record

    errors: list[str] = []

    for spec in _EXPECTED_OK:
        rid = spec["remote_id"]
        if rid not in by_remote_id:
            errors.append(
                f"{rid}: expected import but got skip ({skipped_by_remote_id.get(rid, '<not seen>')})"
            )
            continue
        errors.extend(_check_ok(by_remote_id[rid], spec))

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"\n{len(errors)} error(s) found", file=sys.stderr)
        return 1

    print(f"OK: {len(_EXPECTED_OK)} expected imports, all match")
    return 0


if __name__ == "__main__":
    # Surface the cache-dir hint so a missing cache doesn't look like a
    # mysterious failure mode in CI logs.
    if not _DEVICES_CLONE_DIR.exists():
        print(
            f"NOTE: cache {_DEVICES_CLONE_DIR} does not exist — will be cloned on first run.",
            file=sys.stderr,
        )
    sys.exit(main())
