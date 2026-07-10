"""Tests for ``_surface_esp32_advanced_fields`` in ``script/sync_components.py``.

Upstream marks the whole esp32 ``framework.advanced`` group ``yaml_only``, so the
visibility cascade hides every child. The post-cascade promotion surfaces
``sram1_as_iram`` under the "Advanced" disclosure while leaving the group's other
expert knobs hidden; ``ota.esphome.allow_partition_access`` is promoted fully core
via ``_FIELD_OVERRIDES``. Pin both so a future sync-script edit can't regress the
fields back out of the form, or leak the whole advanced block into it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _surface_esp32_advanced_fields,
)

_COMPONENTS_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _load(component_id: str) -> dict[str, Any]:
    return orjson.loads((_COMPONENTS_DIR / f"{component_id}.json").read_bytes())


def _find(entries: list[dict], key: str) -> dict | None:
    for entry in entries:
        if entry.get("key") == key:
            return entry
        nested = _find(entry.get("config_entries") or [], key)
        if nested is not None:
            return nested
    return None


def _framework_with_advanced() -> dict:
    return {
        "key": "framework",
        "config_entries": [
            {
                "key": "advanced",
                "advanced": True,
                "hidden": True,
                "config_entries": [
                    {"key": "sram1_as_iram", "advanced": True, "hidden": True},
                    {"key": "adc_oneshot_in_iram", "advanced": True, "hidden": True},
                ],
            }
        ],
    }


def test_surface_unhides_sram1_and_group_keeps_siblings_hidden() -> None:
    """``sram1_as_iram`` and its group un-hide; the sibling stays hidden."""
    framework = _framework_with_advanced()
    _surface_esp32_advanced_fields(framework)
    advanced = framework["config_entries"][0]
    assert advanced["hidden"] is False
    assert advanced["advanced"] is True  # still a disclosure, not core
    sram1 = _find(advanced["config_entries"], "sram1_as_iram")
    assert sram1 is not None and sram1["hidden"] is False and sram1["advanced"] is True
    adc = _find(advanced["config_entries"], "adc_oneshot_in_iram")
    assert adc is not None and adc["hidden"] is True  # untouched


def test_surface_no_op_without_promotable_child() -> None:
    """A framework whose advanced group has no allow-listed child is left hidden."""
    framework = {
        "key": "framework",
        "config_entries": [
            {
                "key": "advanced",
                "advanced": True,
                "hidden": True,
                "config_entries": [
                    {"key": "adc_oneshot_in_iram", "advanced": True, "hidden": True}
                ],
            }
        ],
    }
    _surface_esp32_advanced_fields(framework)
    assert framework["config_entries"][0]["hidden"] is True


def test_esp32_catalog_surfaces_sram1_under_advanced() -> None:
    """The generated esp32 body shows ``sram1_as_iram`` under the visible Advanced group."""
    entries = _load("esp32")["config_entries"]
    advanced = _find(entries, "advanced")
    assert advanced is not None
    assert not advanced.get("hidden")  # group renders as a disclosure
    sram1 = _find(advanced["config_entries"], "sram1_as_iram")
    assert sram1 is not None
    assert not sram1.get("hidden") and sram1.get("advanced") is True
    # Scope guard: a sibling expert knob stays hidden (yaml_only).
    adc = _find(advanced["config_entries"], "adc_oneshot_in_iram")
    assert adc is not None and adc.get("hidden") is True


def test_esp32_ota_catalog_promotes_allow_partition_access() -> None:
    """The generated ota.esphome body surfaces ``allow_partition_access`` on the main form."""
    allow = _find(_load("ota.esphome")["config_entries"], "allow_partition_access")
    assert allow is not None
    assert not allow.get("advanced")  # core, not behind "Show advanced"
