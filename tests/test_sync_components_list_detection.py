"""Tests for the live bare-list detection in ``script/sync_components.py``.

ESPHome marks a field ``is_list`` in the schema bundle only when it
flows through ``cv.ensure_list``; a raw ``[item]`` (often inside
``cv.All([item], extra)``) bypasses that path, so the bundle types it
as a scalar. ``_is_list_validator`` recovers the list shape from the
live validator, and the collect/apply pair promotes such fields to
``multi_value`` so the editor renders a list instead of one input that
drops the YAML list on save. ``binary_sensor.on_multi_click``'s
``timing`` and ``esp32_camera``'s ``data_pins`` are the canonical
cases.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import voluptuous as vol

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import esphome.config_validation as cv  # noqa: E402
import sync_components  # noqa: E402


def test_is_list_validator_detects_bare_list() -> None:
    assert sync_components._is_list_validator([cv.string]) is True


def test_is_list_validator_detects_all_wrapping_a_list() -> None:
    """``cv.All([item], extra)`` — the ``on_multi_click.timing`` shape."""
    assert sync_components._is_list_validator(cv.All([cv.string], cv.Length(min=1))) is True


def test_is_list_validator_rejects_scalar_and_listless_all() -> None:
    assert sync_components._is_list_validator(cv.string) is False
    assert sync_components._is_list_validator(cv.All(cv.string, cv.Length(min=1))) is False


def test_is_list_validator_rejects_a_list_of_mappings() -> None:
    """A list-of-dicts (``demo``'s entity lists) is not a scalar ``multi_value`` field."""
    schema = cv.Schema({cv.Optional("name"): cv.string})
    assert sync_components._is_list_validator([schema]) is False
    assert sync_components._is_list_validator([{cv.Optional("name"): cv.string}]) is False


def test_is_list_validator_rejects_a_scalar_or_list_union() -> None:
    """``vol.Any(scalar, list)`` keeps its scalar form, so it must not be a list."""
    assert sync_components._is_list_validator(vol.Any(cv.string, [cv.string])) is False


def test_is_list_validator_accepts_a_union_where_every_branch_is_a_list() -> None:
    assert sync_components._is_list_validator(vol.Any([cv.string], [cv.int_])) is True


def test_apply_list_fields_marks_matching_path_multi_value() -> None:
    entries = [
        {"key": "data_pins", "type": "string", "multi_value": False},
        {"key": "name", "type": "string", "multi_value": False},
    ]
    sync_components._apply_list_fields(entries, {("data_pins",): True})
    by_key = {e["key"]: e for e in entries}
    assert by_key["data_pins"]["multi_value"] is True
    assert by_key["name"]["multi_value"] is False


def test_live_trigger_list_params_finds_multi_click_timing() -> None:
    """The bare-list ``timing`` param is recovered from the live binary_sensor schema."""
    params = sync_components._live_trigger_list_params("binary_sensor")
    assert ("on_multi_click", "timing") in params


def test_live_trigger_list_params_empty_for_listless_component() -> None:
    assert sync_components._live_trigger_list_params("switch") == frozenset()


def test_collect_list_fields_flags_data_pins() -> None:
    """``esp32_camera.data_pins`` is a bare ``[pin]`` list the bundle types as a scalar."""
    loader = sync_components._get_esphome_loader()
    manifest = loader.get_component("esp32_camera")
    assert ("data_pins",) in sync_components._collect_list_fields(manifest)


def test_collect_list_fields_empty_for_schemaless_manifest() -> None:
    assert sync_components._collect_list_fields(SimpleNamespace(config_schema=None)) == {}


def test_introspect_component_surfaces_list_fields() -> None:
    """The component introspection carries ``list_fields`` into the build path."""
    introspection = sync_components.introspect_component("esp32_camera")
    assert ("data_pins",) in introspection["list_fields"]


def test_apply_list_fields_is_a_noop_without_fields() -> None:
    entries = [{"key": "x", "multi_value": False}]
    sync_components._apply_list_fields(entries, {})
    assert entries[0]["multi_value"] is False


def test_validates_mapping_recurses_into_a_combinator_branch() -> None:
    """A mapping reached only through a combinator's ``validators`` still counts."""
    assert sync_components._validates_mapping(SimpleNamespace(validators=({"k": cv.string},)))


def test_automation_schema_dict_rejects_non_callable() -> None:
    assert sync_components._automation_schema_dict(42) is None


def test_automation_schema_dict_swallows_extraction_errors() -> None:
    def boom(_value: object) -> object:
        raise ValueError("nope")

    assert sync_components._automation_schema_dict(boom) is None


def test_automation_schema_dict_rejects_non_dict_and_thenless() -> None:
    assert sync_components._automation_schema_dict(lambda _v: "scalar") is None
    assert sync_components._automation_schema_dict(lambda _v: {"timing": [cv.string]}) is None


def test_automation_schema_dict_returns_schema_with_a_then_key() -> None:
    schema = {"then": object(), "timing": [cv.string]}
    assert sync_components._automation_schema_dict(lambda _v: schema) is schema


def test_scan_schema_for_list_triggers_stops_at_max_depth() -> None:
    out: set[tuple[str, str]] = set()
    sync_components._scan_schema_for_list_triggers({"on_x": "y"}, set(), out, depth=7)
    assert out == set()


def test_live_trigger_list_params_handles_an_unimportable_module() -> None:
    assert sync_components._live_trigger_list_params("no_such_component_zzz") == frozenset()


def test_live_trigger_list_params_skips_unreadable_module_globals(monkeypatch) -> None:
    class FakeModule:
        def __dir__(self) -> list[str]:
            return ["boom", "ok"]

        @property
        def boom(self) -> object:
            raise RuntimeError("unreadable")

        @property
        def ok(self) -> dict:
            return {}  # a dict global with no ``on_*`` triggers

    monkeypatch.setattr(sync_components.importlib, "import_module", lambda _name: FakeModule())
    assert sync_components._live_trigger_list_params("fake_module_xyz") == frozenset()
