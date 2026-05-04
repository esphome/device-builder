"""Unit tests for ``_FIELD_OVERRIDES`` in ``script/sync_components.py``.

The schema bundle drops the inner schema for fields wrapped in custom
validators (``api.encryption``, ``wifi.ap``) — they come back as a
bare key with a docs blurb and no type. Without an override they
would render as a free-form string in the visual editor, which is
the bug behind #308.

Pin the override shape here so a future sync-script edit (or a
schema-bundle change that only partially captures these fields)
can't quietly regress to ``type=string``.
"""

from __future__ import annotations

from script.sync_components import _FIELD_OVERRIDES  # type: ignore[import-not-found]


def test_wifi_ap_override_renders_as_nested_group_on_main_form() -> None:
    """``wifi.ap`` flips from string to a non-advanced nested group with the right label."""
    override = _FIELD_OVERRIDES.get(("wifi", "ap"))
    assert override is not None, "missing wifi.ap override — issue #308 will regress"
    assert override["type"] == "nested"
    assert override["label"] == "Fallback Access Point"
    assert override["advanced"] is False
    assert override["help_link"] == "https://esphome.io/components/wifi#access-point-mode"


def test_wifi_ap_override_inner_fields_match_wifi_network_ap_schema() -> None:
    """Inner ConfigEntries cover the WIFI_NETWORK_AP fields the AP block accepts."""
    override = _FIELD_OVERRIDES[("wifi", "ap")]
    inner = {e["key"]: e for e in override["config_entries"]}
    assert set(inner) == {"ssid", "password", "channel", "ap_timeout", "manual_ip"}

    assert inner["ssid"]["type"] == "string"
    assert inner["password"]["type"] == "secure_string"
    assert inner["channel"]["type"] == "integer"
    assert inner["channel"]["range"] == [1, 14]
    assert inner["ap_timeout"]["type"] == "time_period"
    assert inner["ap_timeout"]["default_value"] == "90s"
    assert inner["manual_ip"]["type"] == "nested"


def test_wifi_ap_override_manual_ip_inner_fields_match_sta_manual_ip_schema() -> None:
    """The nested ``manual_ip`` block carries the same fields as STA_MANUAL_IP_SCHEMA."""
    override = _FIELD_OVERRIDES[("wifi", "ap")]
    manual_ip = next(e for e in override["config_entries"] if e["key"] == "manual_ip")
    inner = {e["key"]: e for e in manual_ip["config_entries"]}
    assert set(inner) == {"static_ip", "gateway", "subnet", "dns1", "dns2"}
    # static_ip / gateway / subnet are required in the upstream schema.
    assert inner["static_ip"]["required"] is True
    assert inner["gateway"]["required"] is True
    assert inner["subnet"]["required"] is True


def test_api_encryption_override_still_present() -> None:
    """``api.encryption`` is the original case that established the override pattern."""
    override = _FIELD_OVERRIDES.get(("api", "encryption"))
    assert override is not None
    assert override["type"] == "nested"
    assert override["advanced"] is False
    assert any(e["key"] == "key" for e in override["config_entries"])


def test_uart_debug_override_renders_as_nested_group_on_main_form() -> None:
    """``uart.debug`` flips from string to a non-advanced nested group with a Direction picker."""
    override = _FIELD_OVERRIDES.get(("uart", "debug"))
    assert override is not None, "missing uart.debug override"
    assert override["type"] == "nested"
    assert override["advanced"] is False
    inner = {e["key"]: e for e in override["config_entries"]}
    assert set(inner) == {"direction", "debug_prefix", "dummy_receiver", "after"}
    # ``direction`` is the canonical BOTH/RX/TX picker, not free-form.
    assert {o["value"] for o in inner["direction"]["options"]} == {"BOTH", "RX", "TX"}
    # ``after`` is the inner accumulator-config nested group.
    assert inner["after"]["type"] == "nested"
    after_inner = {e["key"] for e in inner["after"]["config_entries"]}
    assert after_inner == {"bytes", "timeout", "delimiter"}


def test_ble_nus_debug_override_shares_uart_debug_shape() -> None:
    """``ble_nus.debug`` reuses ``uart.maybe_empty_debug`` upstream — overrides stay in lockstep."""
    uart_override = _FIELD_OVERRIDES[("uart", "debug")]
    ble_override = _FIELD_OVERRIDES.get(("ble_nus", "debug"))
    assert ble_override is not None, "missing ble_nus.debug override"
    # Description is per-component; everything else mirrors uart.
    assert ble_override["type"] == uart_override["type"]
    assert ble_override["config_entries"] == uart_override["config_entries"]
    assert ble_override["description"] != uart_override["description"]
    assert "BLE NUS" in ble_override["description"]
