"""Tests for the whole-file legacy-spelling canonicalizer."""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.automations.canonicalize import render_canonicalize
from esphome_device_builder.controllers.automations.parsing import parse_device_yaml

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


def test_legacy_api_block_and_items() -> None:
    result = render_canonicalize(_LEGACY_API_YAML)
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
    assert render_canonicalize(canonical) is None


def test_legacy_items_under_canonical_block() -> None:
    text = _LEGACY_API_YAML.replace("services:", "actions:")
    result = render_canonicalize(text)
    assert result is not None
    new_text, _diff = result
    assert "- service:" not in new_text
    assert "- action: pause_va" in new_text


def test_homeassistant_id_and_field() -> None:
    result = render_canonicalize(_LEGACY_HA_YAML)
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
    result = render_canonicalize(text)
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
    result = render_canonicalize(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.action:" in new_text
    # Both keys kept — respelling would emit a duplicate ``action:``.
    assert "service: light.turn_on" in new_text
    assert "action: light.turn_off" in new_text


def test_flow_style_inline_body() -> None:
    text = """esphome:
  name: demo
  on_boot:
    then:
      - homeassistant.service: {service: light.toggle}
"""
    result = render_canonicalize(text)
    assert result is not None
    new_text, _diff = result
    assert "- homeassistant.action: {action: light.toggle}" in new_text


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
    assert render_canonicalize(text) is None


def test_inline_legacy_services_key_respelled() -> None:
    text = """api:
  services: []
"""
    result = render_canonicalize(text)
    assert result is not None
    new_text, _diff = result
    assert "actions: []" in new_text


def test_multiple_sites_single_spanning_diff() -> None:
    ha_script = _LEGACY_HA_YAML[_LEGACY_HA_YAML.index("script:") :].replace(
        "homeassistant.action:", "homeassistant.service:"
    )
    text = _LEGACY_API_YAML + "\n" + ha_script
    result = render_canonicalize(text)
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
    result = render_canonicalize(text)
    assert result is not None
    new_text, _diff = result
    assert "homeassistant.action:" in new_text
    assert "action: switch.toggle" in new_text
    assert "- action: run_it" in new_text


@pytest.mark.parametrize("text", ["", "esphome:\n  name: demo\n"])
def test_no_automations_returns_none(text: str) -> None:
    assert render_canonicalize(text) is None


def test_api_without_actions_list_returns_none() -> None:
    assert render_canonicalize("api:\n  reboot_timeout: 0s\n") is None


def test_bodyless_anchor_respells_id_only() -> None:
    result = render_canonicalize(
        "esphome:\n  on_boot:\n    then:\n      - homeassistant.service:\n"
    )
    assert result is not None
    new_text, _diff = result
    assert "- homeassistant.action:" in new_text
