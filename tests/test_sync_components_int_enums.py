"""Int-keyed enum refinement: live enum key types drive string → integer (#2272)."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import esphome.config_validation as cv  # noqa: E402
import sync_components  # noqa: E402
from sync_components import RefinedType  # noqa: E402


def _refined(schema: cv.Schema) -> dict:
    return sync_components._collect_refined_types(SimpleNamespace(config_schema=schema))


def test_int_one_of_refines_to_integer() -> None:
    schema = cv.Schema({cv.Optional("stop_bits"): cv.one_of(1, 2, int=True)})
    assert _refined(schema).get(("stop_bits",)) == RefinedType("integer")


def test_int_keyed_enum_refines_to_integer() -> None:
    schema = cv.Schema({cv.Optional("bits_per_sample"): cv.enum({16: "a", 24: "b", 32: "c"})})
    assert _refined(schema).get(("bits_per_sample",)) == RefinedType("integer")


def test_string_keyed_enum_stays_unrefined() -> None:
    """The cc1101 shape: upstream rejects a bare ``8``, so the catalog must keep string."""
    schema = cv.Schema({cv.Optional("wait_time"): cv.enum({"8": "a", "16": "b"}, upper=False)})
    assert ("wait_time",) not in _refined(schema)


def test_mixed_value_one_of_stays_unrefined() -> None:
    schema = cv.Schema({cv.Optional("weight"): cv.one_of(100, "bold")})
    assert ("weight",) not in _refined(schema)


def test_bool_keyed_enum_stays_unrefined() -> None:
    schema = cv.Schema({cv.Optional("polarity"): cv.enum({True: "a", False: "b"})})
    assert ("polarity",) not in _refined(schema)


def test_ensure_list_item_fields_are_walked() -> None:
    schema = cv.Schema(
        {cv.Required("uart"): cv.ensure_list({cv.Optional("stop_bits"): cv.one_of(1, 2, int=True)})}
    )
    assert _refined(schema).get(("uart", "stop_bits")) == RefinedType("integer")


def test_shared_subschema_is_refined_at_both_list_and_direct_paths() -> None:
    """The wifi ``networks[].eap`` / top-level ``eap`` shape: no path shadows the other."""
    shared = {cv.Optional("rate"): cv.one_of(1, 2, int=True)}
    schema = cv.Schema(
        {
            cv.Optional("networks"): cv.ensure_list({cv.Optional("inner"): cv.Schema(shared)}),
            cv.Optional("inner"): cv.Schema(shared),
        }
    )
    refined = _refined(schema)
    assert refined.get(("networks", "inner", "rate")) == RefinedType("integer")
    assert refined.get(("inner", "rate")) == RefinedType("integer")


def test_extract_enum_values_ignores_other_validators() -> None:
    assert sync_components._extract_enum_values(cv.string) is None
    assert sync_components._extract_enum_values(cv.boolean) is None
    assert sync_components._extract_enum_values(object()) is None


def test_apply_flips_string_options_entry_keeping_options() -> None:
    entries = [
        {
            "key": "adc_averaging",
            "type": "string",
            "options": [{"label": "4", "value": "4"}, {"label": "16", "value": "16"}],
        }
    ]
    sync_components._apply_refined_types(entries, {("adc_averaging",): RefinedType("integer")})
    assert entries[0]["type"] == "integer"
    assert entries[0]["options"] == [
        {"label": "4", "value": "4"},
        {"label": "16", "value": "16"},
    ]


def test_real_weikai_uart_channel_fields_refine() -> None:
    loader = sync_components._get_esphome_loader()
    refined = sync_components._collect_refined_types(loader.get_component("wk2132_i2c"))
    assert refined.get(("uart", "stop_bits")) == RefinedType("integer")
    assert refined.get(("uart", "data_bits")) == RefinedType("integer")


def test_real_cc1101_string_enums_stay_unrefined() -> None:
    loader = sync_components._get_esphome_loader()
    refined = sync_components._collect_refined_types(loader.get_component("cc1101"))
    assert ("wait_time",) not in refined
    assert ("filter_length_fsk_msk",) not in refined


_BITS = RefinedType("string", option_suffix="bit")


def test_literal_unit_before_int_enum_refines_to_suffixed_string() -> None:
    bits = cv.float_with_unit("bits", "bit")
    schema = cv.Schema(
        {
            cv.Optional("one_of"): cv.All(bits, cv.int_, cv.one_of(8, 16, int=True)),
            cv.Optional("enum"): cv.All(bits, cv.enum({16: "a", 32: "b"})),
        }
    )
    refined = _refined(schema)
    assert refined.get(("one_of",)) == _BITS
    assert refined.get(("enum",)) == _BITS


def test_optional_or_grouped_unit_before_int_enum_is_not_suffixed() -> None:
    schema = cv.Schema(
        {
            cv.Optional("optional"): cv.All(
                cv.float_with_unit("current", "mA", optional_unit=True), cv.one_of(4, 8, int=True)
            ),
            cv.Optional("grouped"): cv.All(cv.frequency, cv.one_of(4, 8, int=True)),
        }
    )
    refined = _refined(schema)
    assert refined[("optional",)].option_suffix is None
    assert refined[("grouped",)].option_suffix is None


def test_apply_suffixes_options_and_retypes_string() -> None:
    entries = [
        {
            "key": "bits_per_sample",
            "type": "integer",
            "options": [{"label": "16", "value": "16"}, {"label": "32", "value": "32"}],
        }
    ]
    sync_components._apply_refined_types(entries, {("bits_per_sample",): _BITS})
    assert entries[0]["type"] == "string"
    assert entries[0]["options"] == [
        {"label": "16bit", "value": "16bit"},
        {"label": "32bit", "value": "32bit"},
    ]


def test_real_i2s_speaker_bits_per_sample_refines_suffixed() -> None:
    loader = sync_components._get_esphome_loader()
    refined = sync_components._collect_refined_types(loader.get_platform("speaker", "i2s_audio"))
    assert refined.get(("bits_per_sample",)) == _BITS


def test_unrecognised_float_with_unit_pattern_fails_the_sync() -> None:
    def float_with_unit() -> Any:
        pattern = re.compile(r"^(\d+)bit$")
        optional_unit = False

        def validator(value: Any) -> Any:
            return pattern, optional_unit, value

        return validator

    with pytest.raises(SystemExit, match="float_with_unit internals changed"):
        sync_components._literal_unit_suffix(float_with_unit())


def test_float_with_unit_without_optional_unit_fails_the_sync() -> None:
    def float_with_unit() -> Any:
        pattern = re.compile(r"^([-+]?[0-9]*)\s*(\w*?)bit$")

        def validator(value: Any) -> Any:
            return pattern, value

        return validator

    with pytest.raises(SystemExit, match="float_with_unit internals changed"):
        sync_components._literal_unit_suffix(float_with_unit())
