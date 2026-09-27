"""Contract tests for time-period precision and scalar-bodied actions in the sync."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
import voluptuous as vol

from script.sync_components import (  # type: ignore[import-not-found]
    RefinedType,
    _apply_refined_types,
    _convert_automation_action,
    _convert_automation_condition,
    _convert_field,
    _convert_registry_entry,
    _duration_min_unit_for_extends_ref,
    _duration_min_unit_of,
    _refined_types_in_schema,
)

_UNUSED_SCHEMA_DIR = Path("/unused")
_UNIT_KEYS = {"days", "hours", "minutes", "seconds", "milliseconds", "microseconds"}


@pytest.fixture
def cv() -> ModuleType:
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


@pytest.mark.parametrize(
    ("ref", "unit"),
    [
        ("core.positive_time_period_nanoseconds", "ns"),
        ("core.positive_time_period_microseconds", "us"),
        ("core.positive_time_period_milliseconds", "ms"),
        ("core.positive_time_period_seconds", "s"),
        ("core.positive_time_period_minutes", "min"),
        ("core.positive_time_period", None),
        ("core.time_period", None),
        ("core.positive_float", None),
        ("sensor.DELTA_SCHEMA", None),
    ],
)
def test_duration_min_unit_for_extends_ref(ref: str, unit: str | None) -> None:
    """The ref's precision suffix names the finest unit; no suffix means unbounded."""
    assert _duration_min_unit_for_extends_ref(ref) == unit


def test_convert_field_stamps_duration_min_unit() -> None:
    """A field typed from a precision ref carries that precision."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {"extends": ["core.positive_time_period_milliseconds"]},
    }
    entry = _convert_field("expire_after", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "time_period"
    assert entry["duration_min_unit"] == "ms"


def test_convert_field_omits_duration_min_unit_without_precision() -> None:
    """A plain time period accepts every unit, so nothing is stamped."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {"extends": ["core.positive_time_period"]},
    }
    entry = _convert_field("timeout", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "time_period"
    assert "duration_min_unit" not in entry


def test_polymorphic_time_period_field_keeps_unit_keys_out() -> None:
    """A scalar-or-mapping field lists its own keys, not the scalar's dict-form units."""
    entry = _convert_registry_entry(
        name="pulse",
        body={
            "schema": {
                "config_vars": {
                    "transition_length": {
                        "key": "Optional",
                        "type": "schema",
                        "schema": {
                            "config_vars": {
                                "off_length": {
                                    "key": "Required",
                                    "type": "schema",
                                    "schema": {
                                        "extends": ["core.positive_time_period_milliseconds"]
                                    },
                                },
                            },
                            "extends": ["core.positive_time_period_milliseconds"],
                        },
                    },
                },
            },
            "type": "schema",
        },
        label_domain="light",
        applies_to=["light"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    (transition,) = entry["config_entries"]
    assert [child["key"] for child in transition["config_entries"]] == ["off_length"]


def test_registry_entry_stamps_duration_min_unit() -> None:
    """A scalar time-period filter carries its precision beside ``value_type``."""
    entry = _convert_registry_entry(
        name="throttle",
        body={
            "schema": {"extends": ["core.positive_time_period_milliseconds"]},
            "type": "schema",
        },
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    assert entry["value_type"] == "time_period"
    assert entry["duration_min_unit"] == "ms"


def _delay_body() -> dict:
    return {
        "schema": {"extends": ["core.positive_time_period_milliseconds"]},
        "templatable": True,
        "type": "schema",
        "docs": "Wait.",
    }


def test_scalar_bodied_action_has_a_value_not_fields() -> None:
    """``delay`` is one templatable duration, not six unit fields."""
    action = _convert_automation_action(
        top_key="core",
        domain="core",
        wire_prefix="core",
        name="delay",
        body=_delay_body(),
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert action is not None
    assert action["config_entries"] == []
    assert action["value_type"] == "time_period"
    assert action["templatable"] is True
    assert action["duration_min_unit"] == "ms"
    assert action["scalar_shorthand_key"] is None


def test_scalar_bodied_condition_has_a_value_not_fields() -> None:
    """A condition whose body is one scalar gets the same shape."""
    condition = _convert_automation_condition(
        top_key="core",
        domain="core",
        wire_prefix="core",
        name="elapsed",
        body=_delay_body(),
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert condition is not None
    assert condition["config_entries"] == []
    assert condition["value_type"] == "time_period"
    assert condition["duration_min_unit"] == "ms"


def test_mapping_action_has_no_value_type() -> None:
    """An action with its own fields is not scalar-bodied."""
    action = _convert_automation_action(
        top_key="core",
        domain="core",
        wire_prefix="core",
        name="wait",
        body={
            "schema": {
                "config_vars": {
                    "timeout": {
                        "key": "Optional",
                        "type": "schema",
                        "schema": {"extends": ["core.positive_time_period_milliseconds"]},
                    },
                },
            },
            "type": "schema",
        },
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert action is not None
    assert "value_type" not in action
    assert [e["key"] for e in action["config_entries"]] == ["timeout"]
    assert not _UNIT_KEYS & {e["key"] for e in action["config_entries"]}


def test_duration_min_unit_of_live_validators(cv: ModuleType) -> None:
    """Live precision validators resolve through their wrappers."""
    assert _duration_min_unit_of(cv.positive_time_period_milliseconds) == "ms"
    assert _duration_min_unit_of(cv.positive_time_period_microseconds) == "us"
    assert _duration_min_unit_of(cv.positive_time_period_seconds) == "s"
    assert _duration_min_unit_of(cv.positive_time_period_minutes) == "min"
    assert _duration_min_unit_of(cv.update_interval) == "ms"
    assert _duration_min_unit_of(cv.templatable(cv.positive_time_period_milliseconds)) == "ms"
    assert _duration_min_unit_of(vol.All(cv.positive_time_period_seconds, vol.Range(min=1))) == "s"


def test_duration_min_unit_of_is_none_without_one_precision(cv: ModuleType) -> None:
    """No precision check, a non-duration, or disagreeing branches yield None."""
    assert _duration_min_unit_of(cv.positive_time_period) is None
    assert _duration_min_unit_of(cv.float_) is None
    assert (
        _duration_min_unit_of(
            vol.Any(cv.positive_time_period_seconds, cv.positive_time_period_milliseconds)
        )
        is None
    )


def test_refined_types_carry_duration_min_unit(cv: ModuleType) -> None:
    """The live walk records the precision of a time-period field."""
    schema = cv.Schema({cv.Optional("update_interval", default="60s"): cv.update_interval})
    refined = _refined_types_in_schema(schema)
    assert refined[("update_interval",)].duration_min_unit == "ms"


def test_apply_refined_types_stamps_only_time_period_entries() -> None:
    """The precision lands on a ``time_period`` entry and never retypes another."""
    entries = [
        {"key": "interval", "type": "time_period"},
        {"key": "label", "type": "string"},
    ]
    refined = {
        ("interval",): RefinedType("time_period", duration_min_unit="ms"),
        ("label",): RefinedType("time_period", duration_min_unit="ms"),
    }
    _apply_refined_types(entries, refined)
    assert entries[0] == {"key": "interval", "type": "time_period", "duration_min_unit": "ms"}
    assert entries[1] == {"key": "label", "type": "string"}
