#!/usr/bin/env python3
"""
Smoke-test ``definitions/components.json`` for shape regressions.

Loads the catalog via ``ComponentCatalog`` (i.e. through the same
JSON loader the API uses), then asserts that a curated list of
well-known components are present and structured the way the
frontend expects. Catches:

- A new ``ComponentCategory`` value the loader doesn't know about
- A popular component disappearing from the catalog
- A field's type changing in a way that would break form rendering
  (e.g. ``output.gpio.pin`` flipping from ``pin`` to ``string``)
- ``id`` fields regressing into spurious cross-references

Designed to run in CI right after ``script/sync_components.py``,
before the diff-budget check / PR creation. Exits non-zero on the
first violation with a clear "[component].[field] expected X, got Y"
message so the operator can read the workflow log without spelunking.

Run locally:

    python script/check_catalog.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Allow running via ``python script/check_catalog.py`` without
# installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from esphome_device_builder.controllers.components import ComponentCatalog

# Per-component shape assertions. Each entry is a tuple of
# ``(component_id, [(field_key, type, required, refs)])``. A field
# is a 4-tuple where ``type`` is a ConfigEntryType.value, ``required``
# is bool or None (don't care), and ``refs`` is the expected
# ``references_component`` value or None (don't care).
#
# The list is intentionally short — these are the components the
# frontend uses on every device's "Add component" flow. If they
# break, the whole catalog UX breaks.
_EXPECTATIONS: list[tuple[str, list[tuple[str, str, bool | None, str | None]]]] = [
    (
        "wifi",
        [
            ("ssid", "string", None, None),
            ("password", "secure_string", None, None),
            # ``ap`` is wrapped in a custom validator and would
            # regress to ``string`` if the _FIELD_OVERRIDES entry
            # were removed (issue #308).
            ("ap", "nested", None, None),
        ],
    ),
    (
        "uart",
        [
            # Same custom-validator-wrapper pattern as wifi.ap.
            ("debug", "nested", None, None),
        ],
    ),
    (
        "ble_nus",
        [
            # Reuses uart's maybe_empty_debug — share the override.
            ("debug", "nested", None, None),
        ],
    ),
    (
        "api",
        [
            ("encryption", "nested", None, None),
        ],
    ),
    (
        "esphome",
        [
            ("name", "string", True, None),
            ("comment", "string", None, None),
            ("areas", "nested", None, None),
        ],
    ),
    (
        "logger",
        [
            ("level", "string", None, None),
            ("logs", "map", None, None),
        ],
    ),
    (
        "i2c",
        [
            ("sda", "pin", None, None),
            ("scl", "pin", None, None),
        ],
    ),
    (
        "esp32",
        [
            ("variant", "string", None, None),
            ("framework", "nested", None, None),
        ],
    ),
    (
        "ota.esphome",
        [
            ("password", "secure_string", None, None),
        ],
    ),
    (
        "sensor.dht",
        [
            ("pin", "pin", True, None),
            ("temperature", "nested", None, None),
            ("humidity", "nested", None, None),
        ],
    ),
    (
        "output.gpio",
        [
            ("pin", "pin", True, None),
            # The classic regression: id used to be type=string with
            # references_component="gpio". It's the component's OWN id.
            ("id", "id", True, None),
            # power_supply IS a real cross-reference and must stay
            # one — guards the inverse regression.
            ("power_supply", "id", None, "power_supply"),
        ],
    ),
    (
        "light.binary",
        [
            ("output", "id", True, "output"),
        ],
    ),
    (
        "switch.gpio",
        [
            ("pin", "pin", True, None),
        ],
    ),
]


def main() -> int:
    catalog = ComponentCatalog()
    catalog.load()
    if not catalog._components:
        print("ERROR: catalog is empty — sync_components.py probably failed.")
        return 2

    failures: list[str] = []
    for component_id, fields in _EXPECTATIONS:
        component = catalog._by_id.get(component_id)
        if component is None:
            failures.append(f"missing component: {component_id}")
            continue
        for key, expected_type, expected_required, expected_refs in fields:
            entry = next((e for e in component.config_entries if e.key == key), None)
            if entry is None:
                failures.append(f"{component_id}: missing field {key!r}")
                continue
            actual_type = str(entry.type)
            if actual_type != expected_type:
                failures.append(
                    f"{component_id}.{key}: type expected {expected_type!r}, got {actual_type!r}"
                )
            if expected_required is not None and entry.required != expected_required:
                failures.append(
                    f"{component_id}.{key}: required expected {expected_required}, "
                    f"got {entry.required}"
                )
            if entry.references_component != expected_refs:
                failures.append(
                    f"{component_id}.{key}: references_component expected "
                    f"{expected_refs!r}, got {entry.references_component!r}"
                )

    failures.extend(_check_option_lists(catalog))
    failures.extend(_check_component_gating(catalog))

    if failures:
        print(f"FAIL: {len(failures)} catalog regression(s):")
        for line in failures:
            print(f"  - {line}")
        return 1

    field_count = sum(len(fields) for _, fields in _EXPECTATIONS)
    print(
        f"OK: {len(_EXPECTATIONS)} components, {field_count} fields, "
        f"{len(_OPTION_EXPECTATIONS)} option lists, "
        f"{len(_GATING_EXPECTATIONS)} gating rules verified."
    )
    return 0


# Per-field minimum option-count assertions. Catches regressions where
# inherited fields like ``device_class`` lose their enum values
# because of a partial-override merge bug, or where
# ``unit_of_measurement``'s curated UNIT_* list isn't being layered on.
# Each tuple is ``(component_id, field_path, min_options)`` where
# ``field_path`` is dot-separated for nested access (e.g.
# ``humidity.state_class`` for ``sensor.dht``'s humidity sub-reading).
_OPTION_EXPECTATIONS: list[tuple[str, str, int]] = [
    ("sensor.ct_clamp", "device_class", 50),
    ("sensor.ct_clamp", "unit_of_measurement", 30),
    ("sensor.ct_clamp", "state_class", 3),
    ("sensor.ct_clamp", "entity_category", 2),
    # Inherited via extends + partial-override merge — the same field
    # at a deeper level must keep its options.
    ("sensor.dht", "humidity.device_class", 50),
    ("sensor.dht", "humidity.state_class", 3),
]


# Cross-component gating assertions. Catches regressions where
# infrastructure-dependent fields (zigbee_sensor, web_server, mqtt
# discovery) lose their ``depends_on_component`` tag and start
# showing up on the form even when the named component isn't
# configured on the device.
_GATING_EXPECTATIONS: list[tuple[str, str, str]] = [
    # Zigbee + web_server entity options are inherited onto every
    # sensor via ``_SENSOR_SCHEMA``; they must be gated.
    ("sensor.ct_clamp", "zigbee_sensor", "zigbee"),
    ("sensor.ct_clamp", "web_server", "web_server"),
    # MQTT-internal fields (inside the ``mqtt`` component itself).
    # Gated even on the mqtt component because the field is only
    # meaningful when the broker is configured downstream.
    ("mqtt", "discovery", "mqtt"),
]


def _resolve_field(component: Any, path: str) -> Any:
    """Walk ``component.config_entries`` along a dotted ``path``."""
    parts = path.split(".")
    entries = component.config_entries
    entry = None
    for part in parts:
        entry = next((e for e in entries if e.key == part), None)
        if entry is None:
            return None
        entries = entry.config_entries or []
    return entry


def _check_option_lists(catalog: ComponentCatalog) -> list[str]:
    """Return a failure message per field whose option list is too small."""
    failures: list[str] = []
    for cid, path, minimum in _OPTION_EXPECTATIONS:
        component = catalog._by_id.get(cid)
        if component is None:
            failures.append(f"options check: missing component {cid}")
            continue
        entry = _resolve_field(component, path)
        if entry is None:
            failures.append(f"options check: {cid}.{path} not found")
            continue
        count = len(entry.options or [])
        if count < minimum:
            failures.append(
                f"{cid}.{path}: options count {count} < expected minimum {minimum} "
                "(inherited enum values may have been lost)"
            )
    return failures


def _check_component_gating(catalog: ComponentCatalog) -> list[str]:
    """Return a failure message per field missing its ``depends_on_component``."""
    failures: list[str] = []
    for cid, path, gate in _GATING_EXPECTATIONS:
        component = catalog._by_id.get(cid)
        if component is None:
            failures.append(f"gating check: missing component {cid}")
            continue
        entry = _resolve_field(component, path)
        if entry is None:
            failures.append(f"gating check: {cid}.{path} not found")
            continue
        if entry.depends_on_component != gate:
            failures.append(
                f"{cid}.{path}: depends_on_component expected {gate!r}, "
                f"got {entry.depends_on_component!r}"
            )
    return failures


if __name__ == "__main__":
    sys.exit(main())
