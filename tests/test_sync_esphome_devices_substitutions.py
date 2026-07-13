"""Page ``substitutions:`` blocks resolve over the config tree before extraction."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _extract_featured_components,
    _resolve_page_substitutions,
)

pytest.importorskip("esphome")


@pytest.mark.parametrize(
    ("substitutions", "raw", "expected"),
    [
        pytest.param({"baud": 9600}, "${baud}", 9600, id="whole_scalar_keeps_type"),
        pytest.param(
            {"friendly_name": "Blind Switch"},
            "${friendly_name} S1 input",
            "Blind Switch S1 input",
            id="interpolation",
        ),
        pytest.param({"open_switch": "P23"}, "$open_switch", "P23", id="bare_var"),
        pytest.param(
            {"base": "kitchen", "device_name": "${base}-light"},
            "${device_name}",
            "kitchen-light",
            id="chained",
        ),
        pytest.param(
            {"defined": "yes"}, "${not_defined} Uptime", "${not_defined} Uptime", id="undefined"
        ),
    ],
)
def test_reference_forms_resolve(substitutions: dict[str, Any], raw: str, expected: Any) -> None:
    parsed: dict[str, Any] = {
        "substitutions": substitutions,
        "sensor": [{"platform": "uptime", "update_interval": raw}],
    }
    resolved = _resolve_page_substitutions(parsed, "board")
    assert resolved["sensor"][0]["update_interval"] == expected


def test_substitutions_key_absent_from_result() -> None:
    parsed: dict[str, Any] = {
        "substitutions": {"pin": "GPIO4"},
        "switch": [{"platform": "gpio", "pin": "${pin}"}],
    }
    assert "substitutions" not in _resolve_page_substitutions(parsed, "board")


@pytest.mark.parametrize(
    "parsed",
    [
        pytest.param({"switch": [{"platform": "gpio", "pin": 4}]}, id="absent"),
        pytest.param({"substitutions": {}, "switch": []}, id="empty"),
        pytest.param({"substitutions": ["not", "a", "map"], "switch": []}, id="non_mapping"),
    ],
)
def test_unusable_substitutions_block_is_a_no_op(parsed: dict[str, Any]) -> None:
    assert _resolve_page_substitutions(parsed, "board") is parsed


def test_pass_failure_returns_parsed_unchanged(caplog: pytest.LogCaptureFixture) -> None:
    """An invalid substitution key aborts the pass; the board imports as before."""
    parsed: dict[str, Any] = {
        "substitutions": {"bad key!": "x"},
        "switch": [{"platform": "gpio", "pin": "${bad key!}"}],
    }
    with caplog.at_level(logging.WARNING):
        assert _resolve_page_substitutions(parsed, "board") is parsed
    assert "substitution pass failed for board" in caplog.text


_INDEX: dict[str, dict[str, Any]] = {
    "binary_sensor.gpio": {
        "config_entries": [{"key": "pin", "type": "pin"}, {"key": "name", "type": "string"}]
    },
    "switch.gpio": {
        "config_entries": [
            {"key": "pin", "type": "pin"},
            {"key": "interlock_wait_time", "type": "string"},
            {"key": "name", "type": "string"},
        ]
    },
}


def test_loratap_shaped_page_extracts_after_resolution() -> None:
    """The #1988 shape: substitution-valued pins and durations extract like literals."""
    parsed: dict[str, Any] = {
        "substitutions": {
            "device_friendly_name": "Blind Switch",
            "open_switch": "P23",
            "open_relay": "P26",
            "interlock_time": "200ms",
        },
        "binary_sensor": [
            {
                "platform": "gpio",
                "name": "${device_friendly_name} S1 switch input",
                "pin": "${open_switch}",
                "id": "open_cover_switch",
            }
        ],
        "switch": [
            {
                "platform": "gpio",
                "pin": "${open_relay}",
                "name": "Relay #1",
                "id": "relay1",
                "interlock_wait_time": "${interlock_time}",
            }
        ],
    }
    resolved = _resolve_page_substitutions(parsed, "loratap_sc411wsc")
    featured, _, _ = _extract_featured_components(resolved, _INDEX)
    by_cid = {entry["component_id"]: entry for entry in featured}
    sensor = by_cid["binary_sensor.gpio"]
    assert sensor["fields"]["pin"] == {"value": 23, "locked": True}
    assert sensor["fields"]["name"] == "Blind Switch S1 switch input"
    switch = by_cid["switch.gpio"]
    assert switch["fields"]["pin"] == {"value": 26, "locked": True}
    assert switch["fields"]["interlock_wait_time"] == "200ms"
