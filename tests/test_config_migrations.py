"""Tests for the whole-file config migrations."""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.automations.parsing import parse_device_yaml
from esphome_device_builder.controllers.migrations import render_migrations

_LEGACY_API_YAML = """esphome:
  name: demo

api:
  # user actions
  services:
    - service: pause_va
      then:
        - logger.log: "pausing"
    - service: resume_va
      then:
        - logger.log: "resuming"
"""

_LEGACY_HA_YAML = """esphome:
  name: demo
  on_boot:
    then:
      - homeassistant.service:
          service: light.turn_on
          data:
            entity_id: light.desk

script:
  - id: notify
    then:
      - homeassistant.action:
          service: notify.notify
          data:
            message: "hi"
"""


def _respell(text: str) -> str:
    """Unwrap a must-change canonicalize result to its new text."""
    result = render_migrations(text)
    assert result is not None
    return result[0]


def _on_boot(*body: str) -> str:
    return "esphome:\n  on_boot:\n    then:\n" + "".join(f"{line}\n" for line in body)


def test_legacy_api_block_and_items() -> None:
    result = render_migrations(_LEGACY_API_YAML)
    assert result is not None
    new_text, diff = result
    assert "services:" not in new_text
    assert "- service:" not in new_text
    assert "actions:" in new_text
    assert "- action: pause_va" in new_text
    # Comments and sibling bodies survive verbatim.
    assert "  # user actions" in new_text
    assert '"pausing"' in new_text
    parsed = parse_device_yaml(new_text)
    names = [p.location.action_name for p in parsed if p.location.kind == "api_action"]
    assert names == ["pause_va", "resume_va"]
    assert diff.fromLine <= diff.toLine


def test_already_canonical_returns_none() -> None:
    canonical = _LEGACY_API_YAML.replace("services:", "actions:").replace("- service:", "- action:")
    assert render_migrations(canonical) is None


def test_legacy_items_under_canonical_block() -> None:
    text = _LEGACY_API_YAML.replace("services:", "actions:")
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "- service:" not in new_text
    assert "- action: pause_va" in new_text


def test_homeassistant_id_and_field() -> None:
    result = render_migrations(_LEGACY_HA_YAML)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.service:" not in new_text
    assert new_text.count("homeassistant.action:") == 2
    assert "          service:" not in new_text
    assert "          action: light.turn_on" in new_text
    assert "          action: notify.notify" in new_text
    # Nested data mappings untouched.
    assert "entity_id: light.desk" in new_text
    assert 'message: "hi"' in new_text


def test_homeassistant_id_only_when_field_canonical() -> None:
    text = _LEGACY_HA_YAML.replace("service: light.turn_on", "action: light.turn_on")
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.service:" not in new_text
    assert "action: light.turn_on" in new_text


def test_collision_skips_field_but_respells_id() -> None:
    text = """esphome:
  name: demo
  on_boot:
    then:
      - homeassistant.service:
          service: light.turn_on
          action: light.turn_off
"""
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.action:" in new_text
    # Both keys kept — respelling would emit a duplicate ``action:``.
    assert "service: light.turn_on" in new_text
    assert "action: light.turn_off" in new_text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "      - homeassistant.service: {service: light.toggle}",
            "- homeassistant.action: {action: light.toggle}",
            id="plain",
        ),
        pytest.param(
            "      - homeassistant.service: {data: {service: keepme}, service: light.toggle}",
            "{data: {service: keepme}, action: light.toggle}",
            id="nested_payload_untouched",
        ),
        pytest.param(
            "      - homeassistant.service: {data: {action: keepme}, service: light.toggle}",
            "{data: {action: keepme}, action: light.toggle}",
            id="nested_canonical_decoy",
        ),
        pytest.param(
            '      - homeassistant.service: {service: "light.on", data: "{not: a-map}"}',
            '{action: "light.on", data: "{not: a-map}"}',
            id="quoted_braces",
        ),
        pytest.param("      - homeassistant.action: {action: light.on}", None, id="canonical"),
        pytest.param(
            "      - homeassistant.action: {data: {brightness: 50}}",
            None,
            id="no_field",
        ),
    ],
)
def test_flow_style_bodies(body: str, expected: str | None) -> None:
    text = _on_boot(body)
    if expected is None:
        assert render_migrations(text) is None
    else:
        assert expected in _respell(text)


def test_decoy_service_keys_untouched() -> None:
    text = """esphome:
  name: demo
  on_boot:
    then:
      - homeassistant.action:
          action: esphome.notify
          data:
            service: not_a_rename
      - logger.log: "service: literal"
"""
    assert render_migrations(text) is None


def test_inline_legacy_services_key_respelled() -> None:
    text = """api:
  services: []
"""
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "actions: []" in new_text


def test_multiple_sites_single_spanning_diff() -> None:
    ha_script = _LEGACY_HA_YAML[_LEGACY_HA_YAML.index("script:") :].replace(
        "homeassistant.action:", "homeassistant.service:"
    )
    text = _LEGACY_API_YAML + "\n" + ha_script
    result = render_migrations(text)
    assert result is not None
    new_text, diff = result
    assert "services:" not in new_text
    assert "homeassistant.service:" not in new_text
    assert 1 <= diff.fromLine < diff.toLine <= len(text.splitlines())


def test_nested_inside_api_action_body() -> None:
    text = """api:
  actions:
    - action: run_it
      then:
        - homeassistant.service:
            service: switch.toggle
"""
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.action:" in new_text
    assert "action: switch.toggle" in new_text
    assert "- action: run_it" in new_text


@pytest.mark.parametrize("text", ["", "esphome:\n  name: demo\n"])
def test_no_automations_returns_none(text: str) -> None:
    assert render_migrations(text) is None


def test_api_without_actions_list_returns_none() -> None:
    assert render_migrations("api:\n  reboot_timeout: 0s\n") is None


def test_comment_lines_do_not_pick_the_body_indent() -> None:
    text = (
        "esphome:\n  on_boot:\n    then:\n"
        "      - homeassistant.service:\n"
        "        # call the light service\n"
        "          service: light.turn_on\n"
    )
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "action: light.turn_on" in new_text
    assert "# call the light service" in new_text


def test_brace_in_trailing_comment_keeps_block_body_respell() -> None:
    text = (
        "esphome:\n  on_boot:\n    then:\n"
        "      - homeassistant.service:  # TODO {see later}\n"
        "          service: light.turn_on\n"
    )
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.action:  # TODO {see later}" in new_text
    assert "action: light.turn_on" in new_text


def test_block_scalar_contents_untouched() -> None:
    text = (
        "esphome:\n  on_boot:\n    then:\n"
        "      - lambda: |-\n"
        "          homeassistant.service: not_yaml\n"
    )
    assert render_migrations(text) is None


def test_anchor_after_block_scalar_still_respells() -> None:
    text = (
        "esphome:\n  on_boot:\n    then:\n"
        "      - lambda: |-\n"
        "          homeassistant.service: not_yaml\n"
        "\n"
        "          more text\n"
        "      - homeassistant.service:\n"
        "          service: light.on\n"
    )
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    # The scalar body keeps its legacy text; the real node respells.
    assert "homeassistant.service: not_yaml" in new_text
    assert "- homeassistant.action:" in new_text
    assert "action: light.on" in new_text


def test_api_item_with_both_discriminators_kept() -> None:
    text = "api:\n  actions:\n    - action: a\n      service: b\n      then: []\n"
    assert render_migrations(text) is None


def test_api_legacy_item_beside_collision_item_still_respells() -> None:
    text = (
        "api:\n  services:\n"
        "    - action: a\n"
        "      service: b\n"
        "      then: []\n"
        "    - service: pause\n"
        "      then: []\n"
    )
    result = render_migrations(text)
    assert result is not None
    new_text, _diff = result
    assert "actions:" in new_text
    # The collision item keeps both keys; the clean item respells.
    assert "- action: a" in new_text
    assert "service: b" in new_text
    assert "- action: pause" in new_text


def test_bodyless_anchor_respells_id_only() -> None:
    result = render_migrations("esphome:\n  on_boot:\n    then:\n      - homeassistant.service:\n")
    assert result is not None
    new_text, _diff = result
    assert "- homeassistant.action:" in new_text


_ETHERNET_YAML = """ethernet:
  type: LAN8720
  mdc_pin: GPIO23
  mdio_pin: GPIO18
  clk_mode: GPIO0_IN
"""


def test_ethernet_clk_mode_migrates_to_clk_block() -> None:
    new_text = _respell(_ETHERNET_YAML)
    assert "clk_mode" not in new_text
    assert "  clk:\n    pin: GPIO0\n    mode: CLK_EXT_IN\n" in new_text
    assert "mdc_pin: GPIO23" in new_text


def test_ethernet_clk_out_direction() -> None:
    new_text = _respell(_ETHERNET_YAML.replace("GPIO0_IN", "GPIO17_OUT"))
    assert "  clk:\n    pin: GPIO17\n    mode: CLK_OUT\n" in new_text


def test_ethernet_existing_clk_replaced_wholesale() -> None:
    text = _ETHERNET_YAML + "  clk:\n    pin: GPIO16\n    mode: CLK_OUT\n"
    new_text = _respell(text)
    assert new_text.count("clk:") == 1
    assert "pin: GPIO0" in new_text
    assert "GPIO16" not in new_text


def test_ethernet_existing_clk_with_comment_at_key_indent_replaced_wholesale() -> None:
    """A comment at the ``clk:`` key's own indent doesn't split the replaced block."""
    text = _ETHERNET_YAML + "  clk:\n  # a note\n    pin: GPIO17\n    mode: CLK_OUT\n"
    new_text = _respell(text)
    assert new_text.count("clk:") == 1
    assert new_text.count("\n    pin:") == 1
    assert "GPIO17" not in new_text


@pytest.mark.parametrize("value", ["!secret clk", "EXTERNAL", "17"])
def test_ethernet_undecodable_clk_mode_untouched(value: str) -> None:
    assert render_migrations(_ETHERNET_YAML.replace("GPIO0_IN", value)) is None


def test_ethernet_without_clk_mode_untouched() -> None:
    assert render_migrations("ethernet:\n  type: LAN8720\n  clk:\n    pin: GPIO0\n") is None


def test_ethernet_existing_clk_bounded_by_next_key() -> None:
    text = (
        "ethernet:\n"
        "  type: LAN8720\n"
        "  clk:\n"
        "    pin: GPIO16\n"
        "  mdc_pin: GPIO23\n"
        "  clk_mode: GPIO0_IN\n"
    )
    new_text = _respell(text)
    assert new_text.count("clk:") == 1
    assert "pin: GPIO0" in new_text
    assert "GPIO16" not in new_text
    assert "mdc_pin: GPIO23" in new_text


def test_body_scan_skips_a_scalar_member() -> None:
    text = _on_boot(
        "      - homeassistant.action:",
        "          variables: |-",
        "            x",
        "          service: light.on",
    )
    new_text = _respell(text)
    assert "action: light.on" in new_text
    assert "variables: |-" in new_text


def test_ethernet_clk_replacement_keeps_the_blank_separator() -> None:
    text = (
        "ethernet:\n"
        "  type: LAN8720\n"
        "  clk_mode: GPIO0_IN\n"
        "  clk:\n"
        "    pin: GPIO16\n"
        "    mode: CLK_OUT\n"
        "\n"
        "wifi:\n"
        "  ssid: foo\n"
    )
    new_text = _respell(text)
    assert "\n\nwifi:" in new_text
    assert new_text.count("clk:") == 1


def test_ethernet_out_of_table_pin_untouched() -> None:
    assert render_migrations("ethernet:\n  clk_mode: GPIO5_OUT\n") is None


def test_ethernet_clk_mode_with_comment_and_quotes() -> None:
    new_text = _respell('ethernet:\n  clk_mode: "GPIO0_IN"  # rmii clock\n')
    assert "clk:  # rmii clock\n" in new_text
    assert "pin: GPIO0" in new_text


def test_block_scalar_indent_indicator_untouched() -> None:
    text = (
        "esphome:\n  on_boot:\n    then:\n"
        "      - lambda: |2-\n"
        "          homeassistant.service: not_yaml\n"
    )
    assert render_migrations(text) is None
