"""Pin that ``_materialise_entry`` copies ``exclusive_group``.

It rebuilds ``ConfigEntry`` field-by-field, so a new field is silently
dropped on the ``get_body`` path unless copied (the registry one-of
dropdown depends on this one surviving).
"""

from __future__ import annotations

from esphome_device_builder.controllers.components._resolve import _materialise_entry
from esphome_device_builder.models.common import ConfigEntry, ConfigEntryType


def test_materialise_entry_preserves_exclusive_group() -> None:
    entry = ConfigEntry(
        key="raw",
        type=ConfigEntryType.NESTED,
        label="Raw",
        exclusive_group="binary_sensor.remote_receiver",
    )
    assert _materialise_entry(entry, None).exclusive_group == "binary_sensor.remote_receiver"


def test_materialise_entry_recurses_into_nested_children() -> None:
    entry = ConfigEntry(
        key="parent",
        type=ConfigEntryType.NESTED,
        label="Parent",
        config_entries=[
            ConfigEntry(
                key="raw",
                type=ConfigEntryType.NESTED,
                label="Raw",
                exclusive_group="grp",
            )
        ],
    )
    out = _materialise_entry(entry, None)
    assert out.config_entries[0].exclusive_group == "grp"
