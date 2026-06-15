"""Pin that ``_materialise_entry`` copies ``exclusive_group``.

It rebuilds ``ConfigEntry`` field-by-field, so a new field is silently
dropped on the ``get_body`` path unless copied (the registry one-of
dropdown depends on this one surviving).
"""

from __future__ import annotations

from esphome_device_builder.controllers.components import variant_to_key
from esphome_device_builder.controllers.components._resolve import _materialise_entry
from esphome_device_builder.models.common import (
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
)


def _uart_entry() -> ConfigEntry:
    return ConfigEntry(
        key="hardware_uart",
        type=ConfigEntryType.STRING,
        label="Hardware UART",
        allow_custom_value=True,
        default_value="UART0",
        platform_defaults={"esp32": "UART0", "esp32_c3": "USB_SERIAL_JTAG"},
        platform_options={
            "esp32": [ConfigValueOption(label="UART0", value="UART0")],
            "esp32_c3": [ConfigValueOption(label="USB_SERIAL_JTAG", value="USB_SERIAL_JTAG")],
        },
    )


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


def test_variant_to_key_normalises_esp32_variants() -> None:
    assert variant_to_key("esp32") == "esp32"
    assert variant_to_key("esp32c3") == "esp32_c3"
    assert variant_to_key("esp32s2") == "esp32_s2"


def test_materialise_entry_resolves_platform_options_by_variant() -> None:
    """A variant key wins over the base platform for both options and default."""
    out = _materialise_entry(_uart_entry(), "esp32", "esp32_c3")
    assert [o.value for o in out.options] == ["USB_SERIAL_JTAG"]
    assert out.default_value == "USB_SERIAL_JTAG"
    # platform_* maps are sync-time only; the frontend never sees them.
    assert out.platform_options is None
    assert out.platform_defaults is None


def test_materialise_entry_falls_back_to_platform_when_variant_missing() -> None:
    """An esp32 variant with no own entry uses the base ``esp32`` lists."""
    out = _materialise_entry(_uart_entry(), "esp32", "esp32_s3")
    assert [o.value for o in out.options] == ["UART0"]
    assert out.default_value == "UART0"


def test_materialise_entry_leaves_options_unresolved_without_target() -> None:
    """No platform/variant (generic catalog) leaves options None; a plain text field."""
    out = _materialise_entry(_uart_entry(), None, None)
    assert out.options is None
    assert out.platform_options is None
    assert out.allow_custom_value is True
