"""Tests for the shared inline-trigger key classifier."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.automation_keys import TRIGGER_KEY_PREFIXES, is_trigger_key


@pytest.mark.parametrize(
    "key",
    ["on_press", "on_value", "on_value_range", "on_boot", "on_calibration"],
)
def test_trigger_keys(key: str) -> None:
    """``on_*`` keys are inline triggers."""
    assert is_trigger_key(key) is True


@pytest.mark.parametrize(
    "key",
    ["set_action", "open_action", "close_action", "heat_action", "auto_mode", "then", "lambda"],
)
def test_action_field_keys(key: str) -> None:
    """Action-fields and control-flow keys are not inline triggers."""
    assert is_trigger_key(key) is False


def test_prefixes_constant() -> None:
    """The prefix tuple is the single source of the ``on_`` literal."""
    assert TRIGGER_KEY_PREFIXES == ("on_",)
