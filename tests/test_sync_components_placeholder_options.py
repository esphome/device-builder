"""Schema-extractor placeholder options and boolean defaults on option entries."""

from __future__ import annotations

from typing import Any

from esphome import config_validation as cv
from esphome.schema_extractors import SCHEMA_EXTRACT

from script.sync_components import (  # type: ignore[import-not-found]
    _PLACEHOLDER_OPTIONS,
    _apply_refined_types,
    _boolean_default_as_option,
    _rejects_own_placeholders,
)

_STATE = cv.one_of("ON", "OFF", upper=True)


def _color(value: Any) -> Any:
    if value == SCHEMA_EXTRACT:
        return ["CSS color name", "hex color value"]
    return cv.one_of("red", "green", lower=True)(value)


def _state(value: Any) -> Any:
    if value == SCHEMA_EXTRACT:
        return ["ON", "OFF"]
    return _STATE(value) == "ON"


def test_placeholders_the_validator_rejects_are_flagged() -> None:
    """Extractor values the validator itself rejects are placeholders."""
    assert _rejects_own_placeholders(_color)


def test_real_extractor_values_are_kept() -> None:
    """Extractor values the validator accepts are real options."""
    assert not _rejects_own_placeholders(_state)


def test_list_validators_are_not_flagged() -> None:
    """An ``ensure_list`` wrapper echoing the sentinel is not a placeholder enum."""
    assert not _rejects_own_placeholders(cv.ensure_list(cv.one_of("A", "B")))


def test_boolean_default_maps_to_matching_option() -> None:
    """A boolean default becomes the option esphome reads as that boolean."""
    options = [{"label": "ON", "value": "ON"}, {"label": "OFF", "value": "OFF"}]
    assert _boolean_default_as_option(True, options) == "ON"
    assert _boolean_default_as_option(False, options) == "OFF"


def test_boolean_default_without_matching_option_is_unchanged() -> None:
    """No option reading as that boolean leaves the default alone."""
    assert _boolean_default_as_option(True, [{"label": "once", "value": "once"}]) is True


def test_placeholder_refinement_drops_options_and_keeps_type() -> None:
    """The placeholder refinement removes an entry's options without retyping it."""
    entry = {"key": "color", "type": "string", "options": [{"label": "x", "value": "x"}]}
    _apply_refined_types([entry], {("color",): _PLACEHOLDER_OPTIONS})
    assert "options" not in entry
    assert entry["type"] == "string"
