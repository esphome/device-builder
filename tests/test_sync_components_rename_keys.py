"""``cv.rename_key`` discovery, routing, and the handled-list canary, pinned at the generator."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace

import orjson
import pytest

from script import sync_components
from script.sync_components import (  # type: ignore[import-not-found]
    _MIGRATION_RULES,
    _RENAME_SWEEP_COUNT,
    _UNHANDLED_RENAME_KEYS,
    _classify_rename_pairs,
    _collect_rename_keys,
    _emit_migration_rules_index,
    _fail_on_unhandled_rename_keys,
    _note_unhandled_rename_keys,
    introspect_component,
)


@pytest.fixture
def cv() -> ModuleType:
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


@pytest.fixture(autouse=True)
def _clean_accumulators():
    _UNHANDLED_RENAME_KEYS.clear()
    _MIGRATION_RULES.clear()
    saved_sweeps = _RENAME_SWEEP_COUNT[0]
    yield
    _UNHANDLED_RENAME_KEYS.clear()
    _MIGRATION_RULES.clear()
    _RENAME_SWEEP_COUNT[0] = saved_sweeps


def _manifest(schema) -> SimpleNamespace:
    return SimpleNamespace(config_schema=schema)


def test_top_level_rename_is_discovered_direct(cv: ModuleType) -> None:
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): True}


def test_nested_list_item_rename_is_discovered_non_direct(cv: ModuleType) -> None:
    item = cv.All(
        cv.Schema({cv.Optional("action"): cv.string}),
        cv.rename_key("service", "action"),
    )
    schema = cv.Schema({cv.Optional("actions"): cv.ensure_list(item)})
    assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): False}


def test_typed_schema_branch_rename_is_discovered_non_direct(cv: ModuleType) -> None:
    branch = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    schema = cv.typed_schema({"MODELA": branch})
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): False}


def test_codegen_enum_values_terminate(cv: ModuleType) -> None:
    """A typed_schema whose closure holds codegen MockObj values must not hang."""
    import esphome.codegen as cg  # noqa: PLC0415

    ns = cg.esphome_ns.namespace("rename_test")
    enum = ns.enum("Model", True)
    schema = cv.typed_schema(
        {"MODELA": cv.Schema({cv.Optional("name"): cv.string})},
        enum={"MODELA": enum.MODELA},
    )
    assert _collect_rename_keys(_manifest(schema)) == {}


def test_live_api_pairs_are_discovered_and_handled(cv: ModuleType) -> None:
    """The walk finds the api pairs and the handled list covers them."""
    from script.sync_components import _get_esphome_loader  # noqa: PLC0415

    manifest = _get_esphome_loader().get_component("api")
    assert _collect_rename_keys(manifest) == {
        ("services", "actions"): True,
        ("service", "action"): False,
    }
    introspect_component("api")
    assert set() == _UNHANDLED_RENAME_KEYS
    assert set() == _MIGRATION_RULES


def test_platform_manifest_renames_never_reach_the_canary(cv: ModuleType) -> None:
    """sgp4x pairs route to the artifact, never the canary, on every esphome channel."""
    introspect_component("sgp4x")
    assert set() == _UNHANDLED_RENAME_KEYS
    for kind, _component, domain, platform, _old, _new in _MIGRATION_RULES:
        assert kind == "platform_item_field"
        assert domain == "sensor"
        assert platform == "sgp4x"


def test_registry_sweep_finds_and_handles_the_homeassistant_action_pair(cv: ModuleType) -> None:
    """The registry sweep reaches schemas only the action registry references."""
    import esphome.components.api  # noqa: F401,PLC0415

    from script.sync_components import (  # noqa: PLC0415
        _iter_automation_registry_entries,
        _registry_entry_schema,
        _schema_rename_keys,
        _sweep_registry_rename_keys,
    )

    pairs = {
        registry_id: found
        for _rtype, registry_id, entry in _iter_automation_registry_entries()
        if (schema := _registry_entry_schema(entry)) is not None
        and (found := _schema_rename_keys(schema))
    }
    assert set(pairs["homeassistant.action"]) == {("service", "action")}
    _sweep_registry_rename_keys()
    assert set() == _UNHANDLED_RENAME_KEYS


def test_handled_list_matches_the_writer_constants() -> None:
    """Every _HANDLED_RENAME_KEYS entry must have a writer/canonicalizer counterpart."""
    from esphome_device_builder.controllers.automations.api_actions import (  # noqa: PLC0415
        BLOCK_KEYS,
        ITEM_KEYS,
    )
    from esphome_device_builder.controllers.migrations import (  # noqa: PLC0415
        _ACTION_NODE_RENAMES,
    )
    from script.sync_components import _HANDLED_RENAME_KEYS  # noqa: PLC0415

    derived = {("api", legacy, keys[0]) for keys in (BLOCK_KEYS, ITEM_KEYS) for legacy in keys[1:]}
    derived |= {
        (registry_id, rename.legacy_field, rename.canonical_field)
        for rename in _ACTION_NODE_RENAMES
        for registry_id in (rename.legacy_id, rename.canonical_id)
    }
    assert derived == _HANDLED_RENAME_KEYS


def test_direct_component_pair_routes_to_the_artifact() -> None:
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): True})
    assert {("component_block_field", "sgp4x", "", "", "voc", "voc_index")} == _MIGRATION_RULES
    assert set() == _UNHANDLED_RENAME_KEYS


def test_direct_platform_pair_routes_to_the_artifact() -> None:
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): True}, domain="sensor")
    assert {("platform_item_field", "", "sensor", "sgp4x", "voc", "voc_index")} == _MIGRATION_RULES
    assert set() == _UNHANDLED_RENAME_KEYS


def test_mixed_direct_and_nested_pair_classifies_nested(cv: ModuleType) -> None:
    """A pair also reachable nested must stay inexpressible (fail-loud)."""
    item = cv.All(
        cv.Schema({cv.Optional("action"): cv.string}),
        cv.rename_key("service", "action"),
    )
    schema = cv.All(
        cv.Schema({cv.Optional("actions"): cv.ensure_list(item)}),
        cv.rename_key("service", "action"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): False}


def test_shared_closure_reachable_both_ways_classifies_nested(cv: ModuleType) -> None:
    """One rename validator object placed direct and nested still votes on both paths."""
    rename = cv.rename_key("service", "action")
    item = cv.All(cv.Schema({cv.Optional("action"): cv.string}), rename)
    schema = cv.All(cv.Schema({cv.Optional("actions"): cv.ensure_list(item)}), rename)
    assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): False}


def test_list_form_component_pair_routes_to_the_canary() -> None:
    """A multi_conf component's block is a list the block rule can't address."""
    _classify_rename_pairs("uart", {("old", "new"): True}, list_form=True)
    assert set() == _MIGRATION_RULES
    assert {("uart", "old", "new")} == _UNHANDLED_RENAME_KEYS


def test_non_direct_pair_routes_to_the_canary() -> None:
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): False})
    assert set() == _MIGRATION_RULES
    assert {("sgp4x", "voc", "voc_index")} == _UNHANDLED_RENAME_KEYS


def test_handled_pair_routes_nowhere_even_when_direct() -> None:
    _classify_rename_pairs("api", {("services", "actions"): True})
    assert set() == _MIGRATION_RULES
    assert set() == _UNHANDLED_RENAME_KEYS


def test_sentinel_pair_routes_to_the_canary_despite_direct() -> None:
    sentinel = "<unreadable rename_key>"
    _classify_rename_pairs("broken", {(sentinel, sentinel): True})
    assert set() == _MIGRATION_RULES
    assert {("broken", sentinel, sentinel)} == _UNHANDLED_RENAME_KEYS


def test_emit_writes_sorted_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "migration_rules.index.json"
    monkeypatch.setattr(sync_components, "_MIGRATION_RULES_INDEX_FILE", out_path)
    _MIGRATION_RULES.add(("platform_item_field", "", "sensor", "sgp4x", "voc", "voc_index"))
    _MIGRATION_RULES.add(("component_block_field", "ethernet", "", "", "old", "new"))
    _emit_migration_rules_index()
    payload = orjson.loads(out_path.read_bytes())
    assert payload == {
        "rules": [
            {"component": "ethernet", "kind": "component_block_field", "new": "new", "old": "old"},
            {
                "domain": "sensor",
                "kind": "platform_item_field",
                "new": "voc_index",
                "old": "voc",
                "platform": "sgp4x",
            },
        ]
    }


def test_emit_writes_the_empty_steady_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_path = tmp_path / "migration_rules.index.json"
    monkeypatch.setattr(sync_components, "_MIGRATION_RULES_INDEX_FILE", out_path)
    _emit_migration_rules_index()
    assert orjson.loads(out_path.read_bytes()) == {"rules": []}


def test_unhandled_pair_fails_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("sgp4x", [("voc", "voc_index")])
    with pytest.raises(SystemExit, match="sgp4x: voc -> voc_index"):
        _fail_on_unhandled_rename_keys()


def test_handled_pairs_do_not_fail_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("api", [("services", "actions"), ("service", "action")])
    _fail_on_unhandled_rename_keys()
    assert set() == _UNHANDLED_RENAME_KEYS


def test_unreadable_rename_closure_yields_sentinel_pair() -> None:
    from script.sync_components import _rename_key_pair  # noqa: PLC0415

    def validator(value):
        return value

    validator.__qualname__ = "rename_key.<locals>.validator"
    assert _rename_key_pair(validator) == (
        "<unreadable rename_key>",
        "<unreadable rename_key>",
    )


def test_zero_sweep_fails_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 0
    with pytest.raises(SystemExit, match="walked no schemas"):
        _fail_on_unhandled_rename_keys()
