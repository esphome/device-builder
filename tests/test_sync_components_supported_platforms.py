"""Supported-platforms derivation and a libretiny-umbrella catalog guard."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_libretiny_family_provides,
    _derive_supported_platforms,
    _expand_libretiny,
    _libretiny_families,
    _propagate_platform_constraints,
)

_COMPONENTS_INDEX = (
    Path(__file__).resolve().parent.parent
    / "esphome_device_builder"
    / "definitions"
    / "components.index.json"
)


@cache
def _index_by_id() -> dict[str, dict]:
    return {c["id"]: c for c in json.loads(_COMPONENTS_INDEX.read_text())["components"]}


def test_libretiny_families_non_empty() -> None:
    families = _libretiny_families()
    assert "bk72xx" in families
    assert "libretiny" not in families


def test_expand_libretiny_replaces_umbrella() -> None:
    assert _expand_libretiny(["libretiny"]) == list(_libretiny_families())
    assert _expand_libretiny(["esp32"]) == ["esp32"]
    # umbrella + a family already present de-duplicates, order preserved
    assert _expand_libretiny(["libretiny", "bk72xx"]) == list(_libretiny_families())


def test_umbrella_component_expands_to_families() -> None:
    assert _derive_supported_platforms("libretiny", [], {"is_target_platform": True}) == list(
        _libretiny_families()
    )


def test_libretiny_dependency_expands_to_families() -> None:
    assert _derive_supported_platforms("libretiny_pwm", ["libretiny"], {}) == list(
        _libretiny_families()
    )


def test_plain_platform_dependency_unchanged() -> None:
    assert _derive_supported_platforms("esp32_ble_tracker", ["esp32"], {}) == ["esp32"]


def test_target_platform_reports_itself() -> None:
    assert _derive_supported_platforms("bk72xx", [], {"is_target_platform": True}) == ["bk72xx"]


def test_no_platform_dependency_is_unconstrained() -> None:
    assert _derive_supported_platforms("dht", ["uart"], {}) == []


def test_family_platform_provides_libretiny() -> None:
    entries = [
        {"id": "bk72xx", "provides": ["libretiny"]},
        {"id": "rtl87xx", "provides": []},
        {"id": "ln882x"},
        {"id": "esp32", "provides": []},
    ]
    _apply_libretiny_family_provides(entries)
    by_id = {e["id"]: e for e in entries}
    assert by_id["bk72xx"]["provides"] == ["libretiny"]
    assert by_id["rtl87xx"]["provides"] == ["libretiny"]
    assert by_id["ln882x"]["provides"] == ["libretiny"]
    assert by_id["esp32"]["provides"] == []


def test_propagate_narrows_through_hub() -> None:
    entries = [
        {"id": "i2s_audio", "dependencies": ["esp32"], "supported_platforms": ["esp32"]},
        {"id": "microphone.i2s_audio", "dependencies": ["i2s_audio"], "supported_platforms": []},
    ]
    _propagate_platform_constraints(entries)
    assert entries[1]["supported_platforms"] == ["esp32"]


def test_propagate_narrows_through_chain() -> None:
    entries = [
        {"id": "esp32_ble_tracker", "dependencies": ["esp32"], "supported_platforms": ["esp32"]},
        {"id": "ble_client", "dependencies": ["esp32_ble_tracker"], "supported_platforms": []},
        {"id": "sensor.ble_client", "dependencies": ["ble_client"], "supported_platforms": []},
    ]
    _propagate_platform_constraints(entries)
    assert entries[1]["supported_platforms"] == ["esp32"]
    assert entries[2]["supported_platforms"] == ["esp32"]


def test_propagate_uses_dependency_constraint_without_platform_deps() -> None:
    # ethernet's list comes from an override, not a target-platform dependency.
    entries = [
        {"id": "ethernet", "dependencies": [], "supported_platforms": ["esp32", "rp2040"]},
        {
            "id": "text_sensor.ethernet_info",
            "dependencies": ["ethernet"],
            "supported_platforms": [],
        },
    ]
    _propagate_platform_constraints(entries)
    assert entries[1]["supported_platforms"] == ["esp32", "rp2040"]


def test_propagate_keeps_constrained_entry_untouched() -> None:
    platforms = ["rp2040", "esp32"]
    entries = [
        {"id": "i2s_audio", "dependencies": [], "supported_platforms": ["esp32", "rp2040"]},
        {
            "id": "speaker.i2s_audio",
            "dependencies": ["i2s_audio"],
            "supported_platforms": platforms,
        },
    ]
    _propagate_platform_constraints(entries)
    # equal set: original list object and order survive
    assert entries[1]["supported_platforms"] is platforms
    assert platforms == ["rp2040", "esp32"]


def test_propagate_skips_unknown_and_unconstrained_dependencies() -> None:
    entries = [
        {"id": "uart", "dependencies": [], "supported_platforms": []},
        {"id": "sensor.dht", "dependencies": ["uart", "one_wire"], "supported_platforms": []},
    ]
    _propagate_platform_constraints(entries)
    assert entries[1]["supported_platforms"] == []


def test_propagate_resolves_dotted_dependency_ids() -> None:
    entries = [
        {"id": "ota.http_request", "dependencies": ["esp32"], "supported_platforms": ["esp32"]},
        {
            "id": "update.http_request",
            "dependencies": ["ota.http_request"],
            "supported_platforms": [],
        },
    ]
    _propagate_platform_constraints(entries)
    assert entries[1]["supported_platforms"] == ["esp32"]


def test_propagate_tolerates_cycles() -> None:
    entries = [
        {"id": "a", "dependencies": ["b"], "supported_platforms": []},
        {"id": "b", "dependencies": ["a"], "supported_platforms": []},
    ]
    _propagate_platform_constraints(entries)
    assert entries[0]["supported_platforms"] == []
    assert entries[1]["supported_platforms"] == []


def test_propagate_rejects_disjoint_constraints() -> None:
    entries = [
        {"id": "esp_hub", "dependencies": [], "supported_platforms": ["esp32"]},
        {"id": "rp_hub", "dependencies": [], "supported_platforms": ["rp2040"]},
        {
            "id": "sensor.impossible",
            "dependencies": ["esp_hub", "rp_hub"],
            "supported_platforms": [],
        },
    ]
    with pytest.raises(RuntimeError, match=r"sensor\.impossible"):
        _propagate_platform_constraints(entries)


def test_committed_microphone_i2s_audio_is_esp32_only() -> None:
    assert _index_by_id()["microphone.i2s_audio"].get("supported_platforms") == ["esp32"]


def test_committed_ethernet_info_inherits_ethernet_platforms() -> None:
    assert _index_by_id()["text_sensor.ethernet_info"].get("supported_platforms") == [
        "esp32",
        "rp2040",
    ]


def test_committed_catalog_has_no_libretiny_umbrella_leak() -> None:
    for c in _index_by_id().values():
        deps = c.get("dependencies") or []
        platforms = c.get("supported_platforms") or []
        if "libretiny" in deps:
            assert platforms, f"{c['id']} depends on libretiny but is unconstrained"
        assert "libretiny" not in platforms, (
            f"{c['id']} uses the bare 'libretiny' umbrella token; expand to families"
        )


def test_committed_libretiny_umbrella_is_constrained_to_families() -> None:
    families = list(_libretiny_families())
    assert _index_by_id()["libretiny"].get("supported_platforms") == families


def test_committed_family_platforms_provide_libretiny() -> None:
    index = _index_by_id()
    for fam in _libretiny_families():
        assert "libretiny" in (index[fam].get("provides") or []), (
            f"{fam} should provide libretiny so its block satisfies the dependency"
        )
