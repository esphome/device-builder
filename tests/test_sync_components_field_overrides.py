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

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _BAUD_RATE_OPTIONS,
    _FIELD_OVERRIDES,
    _OUTPUT_BODIES_DIR,
)


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


def test_uart_baud_rate_override_is_a_common_rate_combo_box() -> None:
    """``uart.baud_rate`` gains curated rates + a 115200 default; type/required untouched."""
    override = _FIELD_OVERRIDES.get(("uart", "baud_rate"))
    assert override is not None, "missing uart.baud_rate override"
    assert override["default_value"] == 115200
    assert override["allow_custom_value"] is True
    values = [o["value"] for o in override["options"]]
    # ConfigValueOption.value is str; the list covers the LD2410 rate too.
    assert all(isinstance(v, str) for v in values)
    assert {"2400", "115200", "256000", "921600"} <= set(values)
    # type/required are NOT overridden so the field stays a required integer:
    # the required-field seed commits 115200 and bus_constraints can override it.
    assert "type" not in override
    assert "required" not in override


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


def test_esphome_comment_override_marks_field_advanced() -> None:
    """``esphome.comment`` is forced advanced so it stays off the main form."""
    override = _FIELD_OVERRIDES.get(("esphome", "comment"))
    assert override is not None, "missing esphome.comment override"
    assert override["advanced"] is True


def test_shipped_catalog_esphome_comment_is_advanced() -> None:
    """The generated esphome body marks ``comment`` advanced."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "esphome.json").read_bytes())
    comment = next(e for e in body["config_entries"] if e["key"] == "comment")
    assert comment["advanced"] is True


def test_logger_hardware_uart_override_promotes_to_main_form() -> None:
    """``logger.hardware_uart`` is forced non-advanced so it shows on the main form."""
    override = _FIELD_OVERRIDES.get(("logger", "hardware_uart"))
    assert override is not None, "missing logger.hardware_uart override"
    assert override["advanced"] is False


def test_logger_baud_rate_override_offers_combobox_with_disable_option() -> None:
    """``logger.baud_rate`` reuses the shared rates plus the ``0`` disable sentinel."""
    override = _FIELD_OVERRIDES.get(("logger", "baud_rate"))
    assert override is not None, "missing logger.baud_rate override"
    assert override["allow_custom_value"] is True
    values = [o["value"] for o in override["options"]]
    assert values[0] == "0"
    assert override["options"][0]["label"] == "0 (disable logging)"
    # The standard rates follow, reused verbatim from the shared list.
    assert override["options"][1:] == _BAUD_RATE_OPTIONS
    # Merge-only: type/required/advanced are left to the schema-derived entry.
    assert {"type", "required", "advanced"}.isdisjoint(override)


def test_uart_and_logger_baud_rate_share_the_rate_list() -> None:
    """Both baud combo boxes draw from one ``_BAUD_RATE_OPTIONS`` source."""
    assert _FIELD_OVERRIDES[("uart", "baud_rate")]["options"] is _BAUD_RATE_OPTIONS
    assert {"2400", "115200", "921600"} <= {o["value"] for o in _BAUD_RATE_OPTIONS}


def test_shipped_catalog_logger_baud_rate_is_combobox() -> None:
    """The generated logger body renders ``baud_rate`` as a custom-allowed select."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "logger.json").read_bytes())
    baud = next(e for e in body["config_entries"] if e["key"] == "baud_rate")
    assert baud["allow_custom_value"] is True
    labels = [o["label"] for o in baud["options"]]
    assert "0 (disable logging)" in labels
    assert {"2400", "115200", "921600"} <= {o["value"] for o in baud["options"]}


def test_web_server_sorting_groups_override_gates_on_version_3() -> None:
    """``web_server.sorting_groups`` hides unless the sibling ``version`` is 3."""
    override = _FIELD_OVERRIDES.get(("web_server", "sorting_groups"))
    assert override is not None, "missing web_server.sorting_groups override"
    assert override["depends_on"] == "version"
    # Both YAML scalar shapes: ``version: 3`` and ``version: "3"``.
    assert override["depends_on_value_any"] == [3, "3"]
    # Merge-only: everything else stays schema-derived.
    assert {"type", "required", "advanced", "config_entries"}.isdisjoint(override)


def test_shipped_catalog_web_server_sorting_groups_is_version_gated() -> None:
    """The generated web_server body carries the version-3 gate."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "web_server.json").read_bytes())
    groups = next(e for e in body["config_entries"] if e["key"] == "sorting_groups")
    assert groups["depends_on"] == "version"
    assert groups["depends_on_value_any"] == [3, "3"]


def test_web_server_version_override_promotes_to_main_form() -> None:
    """``web_server.version`` picks the UI flavor, so it stays off Advanced."""
    override = _FIELD_OVERRIDES.get(("web_server", "version"))
    assert override is not None, "missing web_server.version override"
    assert override == {"advanced": False}


def test_shipped_catalog_web_server_version_is_main_form() -> None:
    """The generated web_server body keeps ``version`` off Advanced with its enum intact."""
    body = orjson.loads((_OUTPUT_BODIES_DIR / "web_server.json").read_bytes())
    version = next(e for e in body["config_entries"] if e["key"] == "version")
    assert not version.get("advanced")
    assert [o["value"] for o in version["options"]] == ["1", "2", "3"]
