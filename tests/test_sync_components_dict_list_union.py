"""Mapping-or-list union detection (``_is_dict_list_union``) in the sync script."""

from __future__ import annotations

import sys
from pathlib import Path

import voluptuous as vol

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import esphome.config_validation as cv  # noqa: E402
import sync_components  # noqa: E402
from esphome.components.ntc.sensor import process_calibration  # noqa: E402
from esphome.components.uart.event import validate_event_types  # noqa: E402


def test_validator_branches_dict_and_list_needs_both() -> None:
    assert sync_components._validator_branches_dict_and_list(
        "def f(v):\n if isinstance(v, dict): pass\n if isinstance(v, list): pass\n"
    )
    assert not sync_components._validator_branches_dict_and_list(
        "def f(v):\n if isinstance(v, list): pass\n"
    )
    assert not sync_components._validator_branches_dict_and_list("def f(v):\n return v\n")


def test_validator_branches_dict_and_list_handles_tuple_isinstance() -> None:
    assert sync_components._validator_branches_dict_and_list(
        "def f(v):\n if isinstance(v, (dict, list)): pass\n"
    )


def test_is_dict_list_union_detects_real_unions() -> None:
    assert sync_components._is_dict_list_union(process_calibration) is True
    assert sync_components._is_dict_list_union(validate_event_types) is True


def test_is_dict_list_union_excludes_ensure_list_reshaper() -> None:
    """``cv.ensure_list``'s closure tests dict/list to reshape; excluded by module."""
    assert sync_components._is_dict_list_union(cv.ensure_list(cv.string)) is False


def test_is_dict_list_union_excludes_scalars_schemas_and_unions() -> None:
    assert sync_components._is_dict_list_union(cv.string) is False
    assert sync_components._is_dict_list_union(cv.Schema({})) is False
    # vol.Any carries ``validators`` — the structural peel handles it, so the
    # AST path never runs and would otherwise mis-flag a scalar-or-list union.
    assert sync_components._is_dict_list_union(vol.Any(cv.string, [cv.string])) is False


def test_collect_refined_types_flags_ntc_calibration_unknown() -> None:
    loader = sync_components._get_esphome_loader()
    manifest = loader.get_platform("sensor", "ntc")
    refined = sync_components._collect_refined_types(manifest)
    assert refined.get(("calibration",)) == sync_components.RefinedType("unknown")


def test_apply_refined_types_forces_unknown_over_a_guessed_scalar() -> None:
    entries = [
        {"key": "calibration", "type": "float"},
        {"key": "name", "type": "string"},
    ]
    refined = {("calibration",): sync_components.RefinedType("unknown")}
    sync_components._apply_refined_types(entries, refined)
    by_key = {e["key"]: e for e in entries}
    assert by_key["calibration"]["type"] == "unknown"
    assert by_key["name"]["type"] == "string"


def test_apply_refined_types_unknown_skips_structural_nested() -> None:
    entries = [{"key": "x", "type": "nested", "config_entries": [{"key": "a", "type": "string"}]}]
    sync_components._apply_refined_types(entries, {("x",): sync_components.RefinedType("unknown")})
    assert entries[0]["type"] == "nested"
