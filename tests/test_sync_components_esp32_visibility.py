"""
Pin the esp32 ``framework.advanced`` and ota ``allow_partition_access`` visibility.

``sram1_as_iram`` stays surfaced under the Advanced disclosure with its siblings
hidden; ``allow_partition_access`` stays core and esp32-gated.
"""

from __future__ import annotations

from typing import Any

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _OUTPUT_BODIES_DIR,
    _surface_esp32_advanced_fields,
)


def _load(component_id: str) -> dict[str, Any]:
    return orjson.loads((_OUTPUT_BODIES_DIR / f"{component_id}.json").read_bytes())


def _find(entries: list[dict], key: str) -> dict | None:
    for entry in entries:
        if entry.get("key") == key:
            return entry
        nested = _find(entry.get("config_entries") or [], key)
        if nested is not None:
            return nested
    return None


def _framework_with_advanced(
    child_keys: tuple[str, ...] = ("sram1_as_iram", "adc_oneshot_in_iram"),
) -> dict:
    return {
        "key": "framework",
        "config_entries": [
            {
                "key": "advanced",
                "advanced": True,
                "hidden": True,
                "config_entries": [
                    {"key": key, "advanced": True, "hidden": True} for key in child_keys
                ],
            }
        ],
    }


def test_surface_unhides_sram1_and_group_keeps_siblings_hidden() -> None:
    """``sram1_as_iram`` and its group surface on the framework form; the sibling stays hidden."""
    framework = _framework_with_advanced()
    _surface_esp32_advanced_fields(framework)
    advanced = framework["config_entries"][0]
    assert advanced["hidden"] is False and advanced["advanced"] is False
    sram1 = _find(advanced["config_entries"], "sram1_as_iram")
    assert sram1 is not None and sram1["hidden"] is False and sram1["advanced"] is False
    adc = _find(advanced["config_entries"], "adc_oneshot_in_iram")
    assert adc is not None and adc["hidden"] is True  # untouched


def test_surface_no_op_without_promotable_child() -> None:
    """A framework whose advanced group has no allow-listed child is left hidden."""
    framework = _framework_with_advanced(child_keys=("adc_oneshot_in_iram",))
    _surface_esp32_advanced_fields(framework)
    assert framework["config_entries"][0]["hidden"] is True


def test_esp32_catalog_surfaces_sram1_under_advanced() -> None:
    """The generated esp32 body shows ``sram1_as_iram`` in the framework's Advanced group."""
    fw = _find(_load("esp32")["config_entries"], "framework")
    assert fw is not None
    # The Advanced group renders on the framework form, not behind "Show advanced".
    advanced = _find(fw["config_entries"], "advanced")
    assert advanced is not None
    assert not advanced.get("hidden") and not advanced.get("advanced")
    sram1 = _find(advanced["config_entries"], "sram1_as_iram")
    assert sram1 is not None
    assert not sram1.get("hidden") and not sram1.get("advanced")
    # Scope guard: a sibling expert knob stays hidden (yaml_only).
    adc = _find(advanced["config_entries"], "adc_oneshot_in_iram")
    assert adc is not None and adc.get("hidden") is True


def test_esp32_ota_catalog_promotes_allow_partition_access() -> None:
    """The generated ota.esphome body surfaces ``allow_partition_access``, esp32-gated."""
    allow = _find(_load("ota.esphome")["config_entries"], "allow_partition_access")
    assert allow is not None
    assert not allow.get("advanced")  # core, not behind "Show advanced"
    assert allow.get("supported_platforms") == ["esp32"]  # only offered where it works
