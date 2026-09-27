"""Parse / write contract for an action whose whole body is one value (``delay``)."""

from __future__ import annotations

from textwrap import dedent

import pytest

from esphome_device_builder.controllers.automations import catalog
from esphome_device_builder.controllers.automations.emitter import dump, emit_action_node
from esphome_device_builder.controllers.automations.parsing import parse_device_yaml
from esphome_device_builder.controllers.automations.writing import render_upsert
from esphome_device_builder.models.automations import ActionNode

_LAMBDA = {"_lambda": "return 1000;", "_tag": "!lambda"}


def _device(delay_body: str) -> str:
    return (
        "esphome:\n  name: x\n"
        "button:\n"
        "  - platform: template\n"
        "    id: b\n"
        "    on_press:\n"
        f"      - delay:{delay_body}\n"
    )


def _delay_params(text: str) -> dict:
    (parsed,) = parse_device_yaml(text)
    (action,) = parsed.automation.actions
    assert action.action_id == "delay"
    return action.params


def test_delay_catalog_entry_is_one_templatable_duration() -> None:
    """The catalog describes delay as a value with millisecond precision, not unit fields."""
    delay = catalog.action_by_id("delay")
    assert delay is not None
    assert delay.config_entries == []
    assert delay.value_type == "time_period"
    assert delay.templatable is True
    assert delay.duration_min_unit == "ms"


def test_delay_index_row_carries_the_value_shape() -> None:
    """The slim row the editor lists actions from describes the value too."""
    (row,) = [a for a in catalog.all_actions() if a.id == "delay"]
    assert row.value_type == "time_period"
    assert row.templatable is True
    assert row.duration_min_unit == "ms"


@pytest.mark.parametrize(
    ("body", "value"),
    [
        (" 2s", "2s"),
        (" !lambda return 1000;", _LAMBDA),
        ("\n          seconds: 2", {"seconds": 2}),
        ("\n          minutes: 1\n          seconds: 30", {"minutes": 1, "seconds": 30}),
    ],
)
def test_every_delay_form_parses_into_the_value_slot(body: str, value: object) -> None:
    """Scalar, lambda and the unit mapping all land under the one ``id`` param."""
    assert _delay_params(_device(body)) == {"id": value}


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"id": "2s"}, "- delay: 2s\n"),
        ({"id": _LAMBDA}, "- delay: !lambda return 1000;\n"),
        ({"id": {"seconds": 2}}, "- delay:\n    seconds: 2\n"),
        (
            {"id": {"minutes": 1, "seconds": 30}},
            "- delay:\n    minutes: 1\n    seconds: 30\n",
        ),
        # Unit keys sent as top-level params still write the mapping form.
        ({"seconds": "2"}, "- delay:\n    seconds: '2'\n"),
    ],
)
def test_every_delay_form_writes_back(params: dict, expected: str) -> None:
    """The value slot collapses back to ``delay: <value>``."""
    node = ActionNode(action_id="delay", params=params)
    assert dedent(dump([emit_action_node(node)])) == expected


@pytest.mark.parametrize(
    "body",
    [
        " 2s",
        " !lambda return 1000;",
        "\n          seconds: 2",
        "\n          minutes: 1\n          seconds: 30",
    ],
)
def test_delay_round_trips_unchanged(body: str) -> None:
    """Parse then re-save keeps the same value in the same slot."""
    text = _device(body)
    (parsed,) = parse_device_yaml(text)
    new_text, _diff = render_upsert(text, tree=parsed.automation, location=parsed.location)
    assert _delay_params(new_text) == _delay_params(text)
