"""
Tests for the logger ``hardware_uart`` per-platform UART combobox.

Pins that ``hardware_uart`` surfaces a per-variant ``platform_options`` map
(introspected from the logger module's ``UART_SELECTION_*``, which the schema
bundle can't carry) plus ``allow_custom_value``, keyed to match the
``platform_defaults`` key form so the component controller resolves both off
the same target.
"""

from __future__ import annotations

import orjson

from esphome_device_builder.controllers.components import variant_to_key
from script.sync_components import (  # type: ignore[import-not-found]
    _LOGGER_UART_PLATFORM_OPTIONS,
    _OUTPUT_BODIES_DIR,
    _apply_logger_uart_options,
    _logger_uart_platform_options,
)
from script.sync_components import (  # type: ignore[import-not-found]
    variant_to_key as _sync_variant_to_key,
)


def _hardware_uart_entry() -> dict | None:
    body = orjson.loads((_OUTPUT_BODIES_DIR / "logger.json").read_bytes())
    for entry in body.get("config_entries", []):
        if entry.get("key") == "hardware_uart":
            return entry
    return None


def test_uart_keys_use_shared_variant_normaliser() -> None:
    """Sync keys come from the runtime ``variant_to_key``; one source, no divergence.

    Build-time keys and the controller's lookup must agree or the lookup
    silently falls back to the base platform; sharing the function pins it.
    """
    assert _sync_variant_to_key is variant_to_key
    assert variant_to_key("ESP32C3") == "esp32_c3"
    assert variant_to_key("bk72xx") == "bk72xx"


def test_uart_options_cover_every_platform() -> None:
    """Introspection yields one option list per supported chip / variant."""
    opts = _logger_uart_platform_options()
    assert {"esp32", "esp32_c3", "esp32_s2", "esp8266", "rp2040", "bk72xx", "nrf52"} <= set(opts)
    # Variant-specific divergence: C3 has USB_SERIAL_JTAG, base esp32 does not.
    esp32_values = {o["value"] for o in opts["esp32"]}
    c3_values = {o["value"] for o in opts["esp32_c3"]}
    assert esp32_values == {"UART0", "UART1", "UART2"}
    assert "USB_SERIAL_JTAG" in c3_values
    assert "USB_SERIAL_JTAG" not in esp32_values


def test_options_are_label_equals_value_pairs() -> None:
    """Each option is a ``{label, value}`` with both sides equal (the UART name)."""
    for options in _LOGGER_UART_PLATFORM_OPTIONS.values():
        assert all(o["label"] == o["value"] for o in options)


def test_apply_logger_uart_options_sets_combobox() -> None:
    """The hardware_uart entry gains platform_options + ``allow_custom_value``."""
    entries = [
        {"key": "level", "type": "string"},
        {"key": "hardware_uart", "type": "string"},
    ]
    _apply_logger_uart_options("logger", entries)
    hw = entries[1]
    assert hw["allow_custom_value"] is True
    assert hw["platform_options"] == _LOGGER_UART_PLATFORM_OPTIONS
    assert "platform_options" not in entries[0]  # untouched


def test_apply_logger_uart_options_noop_for_other_component() -> None:
    """Only logger is decorated; a stray hardware_uart elsewhere is left alone."""
    entries = [{"key": "hardware_uart", "type": "string"}]
    _apply_logger_uart_options("uart", entries)
    assert "platform_options" not in entries[0]
    assert "allow_custom_value" not in entries[0]


def test_shipped_catalog_logger_uart_is_combobox() -> None:
    """The generated logger body carries the per-platform UART combobox."""
    hw = _hardware_uart_entry()
    assert hw is not None
    assert hw["allow_custom_value"] is True
    assert hw["platform_options"]["esp32_c3"]
    # Keys line up with platform_defaults so resolution hits the same target.
    assert set(hw["platform_options"]) == set(hw["platform_defaults"])
