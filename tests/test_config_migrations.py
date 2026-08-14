"""Tests for the whole-file config migrations."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from esphome_device_builder.controllers import migrations
from esphome_device_builder.controllers.automations.parsing import parse_device_yaml
from esphome_device_builder.controllers.migrations import render_migrations
from esphome_device_builder.definitions import MigrationRule

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


_RP2040_YAML = """esphome:
  name: pico

rp2040:  # target platform
  board: rpipicow
  framework:
    platform_version: 1.2.0

wifi:
  ssid: foo
"""


_RP2_RULE = MigrationRule(kind="component_key", old="rp2040", new="rp2")


def test_rp2040_platform_key_respelled_to_rp2(generated_rules: Callable[..., None]) -> None:
    generated_rules(_RP2_RULE)
    new_text = _respell(_RP2040_YAML)
    assert "rp2:  # target platform\n" in new_text
    assert "rp2040" not in new_text
    assert "  board: rpipicow\n" in new_text
    assert "    platform_version: 1.2.0\n" in new_text
    assert "wifi:\n  ssid: foo\n" in new_text


def test_rp2040_absent_untouched(generated_rules: Callable[..., None]) -> None:
    generated_rules(_RP2_RULE)
    assert render_migrations("esphome:\n  name: pico\n\nrp2:\n  board: rpipicow\n") is None


def test_rp2040_beside_existing_rp2_untouched(generated_rules: Callable[..., None]) -> None:
    generated_rules(_RP2_RULE)
    text = _RP2040_YAML + "\nrp2:\n  board: rpipico\n"
    assert render_migrations(text) is None


def test_rp2040_prefixed_components_untouched(generated_rules: Callable[..., None]) -> None:
    generated_rules(_RP2_RULE)
    text = "rp2040:\n  board: rpipicow\n\noutput:\n  - platform: rp2040_pwm\n    pin: 1\n"
    new_text = _respell(text)
    assert new_text.startswith("rp2:\n")
    assert "platform: rp2040_pwm" in new_text


def test_rp2040_composes_with_other_rules(generated_rules: Callable[..., None]) -> None:
    generated_rules(_RP2_RULE)
    text = "api:\n  services:\n    - service: hi\n      then: []\n\n" + _RP2040_YAML
    new_text = _respell(text)
    assert "  actions:\n" in new_text
    assert "- action: hi\n" in new_text
    assert "rp2:  # target platform\n" in new_text


def test_committed_artifact_migrates_rp2040() -> None:
    """The shipped migration_rules.index.json carries the rp2040 → rp2 respell."""
    new_text = _respell("rp2040:\n  board: rpipicow\n")
    assert new_text.startswith("rp2:\n")


_VOC_RULE = MigrationRule(
    kind="platform_item_field", old="voc", new="voc_index", domain="sensor", platform="sgp4x"
)
_BLOCK_RULE = MigrationRule(
    kind="component_block_field", old="old_key", new="new_key", component="mycomp"
)

_SGP4X_YAML = """sensor:
  - platform: sgp4x
    # gas indexes
    voc:  # keep me
      name: "VOC Index"
    nox:
      name: NOx
  - platform: dht
    voc: decoy
"""


@pytest.fixture
def generated_rules(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Inject synthetic artifact rules into the migration fold."""

    def _set(*rules: MigrationRule) -> None:
        monkeypatch.setattr(migrations, "load_migration_rules_index", lambda: rules)

    return _set


def test_platform_item_rename_scoped_to_matching_platform(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    new_text = _respell(_SGP4X_YAML)
    assert "    voc_index:  # keep me\n" in new_text
    assert '      name: "VOC Index"\n' in new_text
    # The dht item's same-named key is not an sgp4x field.
    assert "    voc: decoy\n" in new_text


def test_platform_item_rename_on_the_dash_line(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = "sensor:\n  - voc:\n      name: x\n    platform: sgp4x\n"
    new_text = _respell(text)
    assert "  - voc_index:\n" in new_text
    assert "platform: sgp4x" in new_text


def test_platform_item_collision_skips_the_item(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = "sensor:\n  - platform: sgp4x\n    voc: a\n    voc_index: b\n"
    assert render_migrations(text) is None


def test_platform_item_rename_ignores_deeper_decoys(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = "sensor:\n  - platform: sgp4x\n    compensation:\n      voc: nested\n"
    assert render_migrations(text) is None


def test_platform_item_rename_skips_block_scalars(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = "sensor:\n  - platform: sgp4x\n    filters:\n      - lambda: |\n        voc: fake\n"
    assert render_migrations(text) is None


def test_platform_value_tolerates_quotes_and_comment(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = 'sensor:\n  - platform: "sgp4x"  # gas\n    voc:\n      name: x\n'
    assert "voc_index:" in _respell(text)


def test_platform_rule_without_domain_block_is_a_noop(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    assert render_migrations("binary_sensor:\n  - platform: sgp4x\n    voc: x\n") is None


def test_component_block_field_rename(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    text = "mycomp:\n  old_key: 1  # note\n  other: 2\n"
    new_text = _respell(text)
    assert "  new_key: 1  # note\n" in new_text
    assert "  other: 2\n" in new_text


def test_component_block_field_collision_is_a_noop(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    assert render_migrations("mycomp:\n  old_key: 1\n  new_key: 2\n") is None


def test_component_block_field_ignores_deeper_decoys(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    assert render_migrations("mycomp:\n  child:\n    old_key: 1\n") is None


def test_component_block_field_absent_component_is_a_noop(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    assert render_migrations("other:\n  old_key: 1\n") is None


def test_component_block_field_list_form_renames_every_item(generated_rules) -> None:
    """The multi_conf shape (xiaomi_rtcgq02lm esp32_ble_id -> ble_hub_id)."""
    generated_rules(_BLOCK_RULE)
    text = "mycomp:\n  - old_key: a  # note\n    other: 1\n  - old_key: b\n"
    new_text = _respell(text)
    assert "  - new_key: a  # note\n" in new_text
    assert "  - new_key: b\n" in new_text
    assert "    other: 1\n" in new_text


def test_component_block_field_list_item_collision_skips_that_item(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    text = "mycomp:\n  - old_key: a\n    new_key: b\n  - old_key: c\n"
    new_text = _respell(text)
    assert "  - old_key: a\n" in new_text
    assert "  - new_key: c\n" in new_text


def test_component_block_field_list_ignores_deeper_decoys(generated_rules) -> None:
    generated_rules(_BLOCK_RULE)
    assert render_migrations("mycomp:\n  - child:\n      old_key: 1\n") is None


def test_component_block_field_mapping_with_nested_list_stays_mapping(generated_rules) -> None:
    """A dash inside a mapping body is a nested list, not the multi_conf form."""
    generated_rules(_BLOCK_RULE)
    text = "mycomp:\n  seq:\n    - old_key: nested\n  old_key: 1\n"
    new_text = _respell(text)
    assert "  new_key: 1\n" in new_text
    assert "    - old_key: nested\n" in new_text


def test_generated_and_bespoke_rules_share_one_diff(generated_rules) -> None:
    generated_rules(_VOC_RULE)
    text = _LEGACY_API_YAML + "\n" + _SGP4X_YAML
    result = render_migrations(text)
    assert result is not None
    new_text, diff = result
    assert "actions:" in new_text
    assert "voc_index:" in new_text
    assert diff.fromLine <= diff.toLine
