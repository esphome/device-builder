"""Platforms binding their hub via ``cv.use_id`` get the hub in ``dependencies``."""

from __future__ import annotations

from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    SchemaIndex,
    _referenced_buses,
    _references_own_hub,
    build_component_entry,
)

_UNUSED_SCHEMA_DIR = Path("/unused")


def _use_id_var(namespace: str) -> dict:
    return {"key": "GeneratedID", "type": "use_id", "use_id_type": f"{namespace}::HubComponent"}


def _section(config_vars: dict) -> dict:
    return {"schemas": {"CONFIG_SCHEMA": {"schema": {"config_vars": config_vars}}}}


def test_references_own_hub_top_level() -> None:
    entries = [{"key": "myhub_id", "references_component": "myhub", "config_entries": None}]
    assert _references_own_hub(entries, "myhub") is True


def test_references_own_hub_nested() -> None:
    entries = [
        {
            "key": "outer",
            "references_component": None,
            "config_entries": [{"key": "myhub_id", "references_component": "myhub"}],
        }
    ]
    assert _references_own_hub(entries, "myhub") is True


def test_references_other_component_only() -> None:
    entries = [{"key": "i2c_id", "references_component": "i2c"}]
    assert _references_own_hub(entries, "myhub") is False


def test_platform_with_use_id_hub_gains_dependency() -> None:
    """A use_id hub ref with no upstream DEPENDENCIES still yields the hub dependency."""
    section = _section({"myhub_id": _use_id_var("myhub")})
    entry = build_component_entry("myhub.button", section, SchemaIndex(), _UNUSED_SCHEMA_DIR, {})
    assert entry is not None
    assert entry["id"] == "button.myhub"
    assert entry["dependencies"] == ["myhub"]


def test_existing_hub_dependency_not_duplicated() -> None:
    """The binary_sensor.ld2410 shape: upstream DEPENDENCIES already carries the hub."""
    section = _section({"myhub_id": _use_id_var("myhub")})
    index = SchemaIndex(metadata={"button.myhub": {"dependencies": ["myhub"]}})
    entry = build_component_entry("myhub.button", section, index, _UNUSED_SCHEMA_DIR, {})
    assert entry is not None
    assert entry["dependencies"] == ["myhub"]


def test_non_bus_cross_component_ref_not_unioned() -> None:
    """An optional sibling ref (output/power_supply/...) is never a hard dependency."""
    section = _section({"output_id": _use_id_var("output")})
    entry = build_component_entry("myhub.button", section, SchemaIndex(), _UNUSED_SCHEMA_DIR, {})
    assert entry is not None
    assert entry["dependencies"] == []


def test_bus_ref_gains_dependency() -> None:
    """A bus use_id ref (spi/i2c/uart) ESPHome omits from DEPENDENCIES is unioned in."""
    section = _section({"i2c_id": _use_id_var("i2c")})
    entry = build_component_entry("myhub.button", section, SchemaIndex(), _UNUSED_SCHEMA_DIR, {})
    assert entry is not None
    assert entry["dependencies"] == ["i2c"]


def test_referenced_buses_only_buses() -> None:
    entries = [
        {"key": "spi_id", "references_component": "spi"},
        {"key": "output_id", "references_component": "output"},
        {"key": "plain", "references_component": None},
    ]
    assert _referenced_buses(entries) == {"spi"}


def test_referenced_buses_nested_and_deduped() -> None:
    entries = [
        {"key": "uart_id", "references_component": "uart"},
        {
            "key": "outer",
            "references_component": None,
            "config_entries": [{"key": "uart2_id", "references_component": "uart"}],
        },
    ]
    assert _referenced_buses(entries) == {"uart"}


def test_hub_build_not_unioned() -> None:
    """Non-platform builds (``domain == ""``) never self-depend."""
    section = _section({"myhub_id": _use_id_var("myhub")})
    entry = build_component_entry("myhub", section, SchemaIndex(), _UNUSED_SCHEMA_DIR, {})
    assert entry is not None
    assert entry["dependencies"] == []
