"""Supported-platforms derivation and a libretiny-umbrella catalog guard."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
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


def test_committed_catalog_has_no_libretiny_umbrella_leak() -> None:
    components = json.loads(_COMPONENTS_INDEX.read_text())["components"]
    for c in components:
        deps = c.get("dependencies") or []
        platforms = c.get("supported_platforms") or []
        if "libretiny" in deps:
            assert platforms, f"{c['id']} depends on libretiny but is unconstrained"
        assert "libretiny" not in platforms, (
            f"{c['id']} uses the bare 'libretiny' umbrella token; expand to families"
        )
