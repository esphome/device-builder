"""The component catalog models omit a field that holds its default, and nothing else."""

from __future__ import annotations

from dataclasses import MISSING, fields

import pytest

from esphome_device_builder.models import (
    ComponentCatalogEntry,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    ConfigEntry,
    ConfigEntryType,
)
from esphome_device_builder.models.common import DashboardModel


def _entry(**overrides: object) -> ConfigEntry:
    return ConfigEntry(key="fast_connect", type=ConfigEntryType.BOOLEAN, label="Fast", **overrides)


def test_a_config_entry_at_its_defaults_serialises_to_its_required_fields() -> None:
    assert _entry().to_dict() == {"key": "fast_connect", "type": "boolean", "label": "Fast"}


def test_a_false_default_value_is_not_a_model_default_and_survives() -> None:
    wire = _entry(default_value=False, platform_defaults={"esp32": False}).to_dict()
    assert wire["default_value"] is False
    assert wire["platform_defaults"] == {"esp32": False}


def test_a_set_flag_survives_and_a_cleared_one_is_omitted() -> None:
    wire = _entry(advanced=True, required=False).to_dict()
    assert wire["advanced"] is True
    assert "required" not in wire


def test_a_component_without_fields_omits_its_empty_collections() -> None:
    component = ComponentCatalogEntry(
        id="logger", name="Logger", description="", category=ComponentCategory.CORE
    )
    assert component.to_dict() == {
        "id": "logger",
        "name": "Logger",
        "description": "",
        "category": "core",
    }
    assert ComponentCatalogEntry.from_dict(component.to_dict()) == component


def test_an_index_entry_omits_its_defaults() -> None:
    index = ComponentCatalogIndexEntry(
        id="logger", name="Logger", description="", category=ComponentCategory.CORE
    )
    assert set(index.to_dict()) == {"id", "name", "description", "category"}


def test_a_nested_entry_round_trips() -> None:
    nested = _entry(
        config_entries=[_entry(default_value=False)],
        supported_platforms=["esp32"],
    )
    assert ConfigEntry.from_dict(nested.to_dict()) == nested


@pytest.mark.parametrize("model", [ConfigEntry, ComponentCatalogEntry, ComponentCatalogIndexEntry])
def test_no_declared_default_is_truthy(model: type) -> None:
    """An absent field reads as false or empty only while no default is truthy."""
    truthy = [
        f.name
        for f in fields(model)
        if (f.default is not MISSING and f.default)
        or (f.default_factory is not MISSING and f.default_factory())
    ]
    assert not truthy


def test_to_wire_serialises_a_model_or_the_models_one_container_down() -> None:
    entry = _entry(advanced=True)
    wire = entry.to_dict()
    assert DashboardModel.to_wire(entry) == wire
    assert DashboardModel.to_wire({"a": entry, "n": 1}) == {"a": wire, "n": 1}
    assert DashboardModel.to_wire([entry, "text"]) == [wire, "text"]


@pytest.mark.parametrize("value", [None, "text", 3, {"nested": {"deep": 1}}, ["a", "b"]])
def test_to_wire_passes_plain_values_through(value: object) -> None:
    assert DashboardModel.to_wire(value) == value
