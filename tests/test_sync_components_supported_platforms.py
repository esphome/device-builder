"""Supported-platforms derivation and a libretiny-umbrella catalog guard."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_libretiny_family_provides,
    _derive_supported_platforms,
    _expand_libretiny,
    _libretiny_families,
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
