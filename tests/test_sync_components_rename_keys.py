"""``cv.rename_key`` discovery, routing, and the handled-list canary, pinned at the generator."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import orjson
import pytest

from esphome_device_builder import definitions
from script import sync_components
from script.sync_components import (  # type: ignore[import-not-found]
    _MIGRATION_RULES,
    _RENAME_SWEEP_COUNT,
    _UNHANDLED_ALIASES,
    _UNHANDLED_CHANNEL_COLORS,
    _UNHANDLED_RENAME_KEYS,
    _classify_rename_pairs,
    _collect_channel_colors_fold,
    _collect_rename_keys,
    _emit_migration_rules_index,
    _fail_on_unhandled_renames,
    _note_unhandled_rename_keys,
    _sweep_component_aliases,
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
    _UNHANDLED_ALIASES.clear()
    _UNHANDLED_CHANNEL_COLORS.clear()
    _MIGRATION_RULES.clear()
    saved_sweeps = _RENAME_SWEEP_COUNT[0]
    yield
    _UNHANDLED_RENAME_KEYS.clear()
    _UNHANDLED_ALIASES.clear()
    _UNHANDLED_CHANNEL_COLORS.clear()
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
    from esphome_device_builder.helpers.migrations import (  # noqa: PLC0415
        _ACTION_NODE_RENAMES,
    )
    from esphome_device_builder.helpers.yaml.api_actions import (  # noqa: PLC0415
        BLOCK_KEYS,
        ITEM_KEYS,
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


def test_platform_domain_pair_routes_to_the_canary() -> None:
    """A platform-domain block's ``- platform:`` items the block rule can't address."""
    _classify_rename_pairs("sensor", {("old", "new"): True}, platform_domain=True)
    assert set() == _MIGRATION_RULES
    assert {("sensor", "old", "new")} == _UNHANDLED_RENAME_KEYS


def test_multi_conf_component_pair_ships_data_driven() -> None:
    """The block rule handles the list form, so multi_conf pairs emit as rules."""
    _classify_rename_pairs("xiaomi_rtcgq02lm", {("esp32_ble_id", "ble_hub_id"): True})
    assert {
        ("component_block_field", "xiaomi_rtcgq02lm", "", "", "esp32_ble_id", "ble_hub_id")
    } == _MIGRATION_RULES
    assert set() == _UNHANDLED_RENAME_KEYS


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
    _MIGRATION_RULES.add(("component_key", "", "", "", "rp2040", "rp2"))
    _emit_migration_rules_index()
    payload = orjson.loads(out_path.read_bytes())
    assert payload == {
        "rules": [
            {"component": "ethernet", "kind": "component_block_field", "new": "new", "old": "old"},
            {"kind": "component_key", "new": "rp2", "old": "rp2040"},
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


def test_live_alias_sweep_classifies_cleanly() -> None:
    """Every alias the installed esphome declares is expressible or acknowledged."""
    pytest.importorskip("esphome.loader")
    _sweep_component_aliases()
    assert set() == _UNHANDLED_ALIASES


def _fake_loader(
    monkeypatch: pytest.MonkeyPatch,
    *,
    canonical: str,
    manifest: SimpleNamespace | None,
    platform_manifest: SimpleNamespace | None = None,
    legacy: str = "legacy_x",
) -> None:
    loader = SimpleNamespace(
        get_alias_metadata=lambda: {
            legacy: SimpleNamespace(canonical=canonical, removal_version=None)
        },
        get_component=lambda name: manifest,
        get_platform=lambda domain, stem: platform_manifest,
    )
    monkeypatch.setattr(sync_components, "_get_esphome_loader", lambda: loader)


def test_platform_component_alias_falls_to_the_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_loader(
        monkeypatch, canonical="canon_x", manifest=SimpleNamespace(is_platform_component=True)
    )
    _sweep_component_aliases()
    assert set() == _MIGRATION_RULES
    assert {("legacy_x", "canon_x")} == _UNHANDLED_ALIASES
    _RENAME_SWEEP_COUNT[0] = 1
    with pytest.raises(SystemExit, match="legacy_x -> canon_x"):
        _fail_on_unhandled_renames()


def test_platform_provider_alias_falls_to_the_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias whose canonical registers platforms under a domain is inexpressible."""
    _fake_loader(
        monkeypatch,
        canonical="canon_x",
        manifest=SimpleNamespace(is_platform_component=False),
        platform_manifest=SimpleNamespace(),
    )
    _sweep_component_aliases()
    assert set() == _MIGRATION_RULES
    assert {("legacy_x", "canon_x")} == _UNHANDLED_ALIASES


def test_unresolvable_alias_canonical_falls_to_the_canary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_loader(monkeypatch, canonical="canon_x", manifest=None)
    _sweep_component_aliases()
    assert {("legacy_x", "canon_x")} == _UNHANDLED_ALIASES


def test_target_platform_alias_needs_parser_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target-platform alias whose canonical the dashboard can't parse is inexpressible."""
    _fake_loader(
        monkeypatch,
        legacy="esp32",
        canonical="esp32_new",
        manifest=SimpleNamespace(is_platform_component=False),
    )
    _sweep_component_aliases()
    assert set() == _MIGRATION_RULES
    assert {("esp32", "esp32_new")} == _UNHANDLED_ALIASES


def test_acknowledged_alias_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_loader(
        monkeypatch, canonical="canon_x", manifest=SimpleNamespace(is_platform_component=True)
    )
    monkeypatch.setattr(sync_components, "_HANDLED_ALIASES", {"legacy_x"})
    _sweep_component_aliases()
    assert set() == _MIGRATION_RULES
    assert set() == _UNHANDLED_ALIASES


def test_unhandled_pair_fails_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("sgp4x", [("voc", "voc_index")])
    with pytest.raises(SystemExit, match="sgp4x: voc -> voc_index"):
        _fail_on_unhandled_renames()


def test_handled_pairs_do_not_fail_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("api", [("services", "actions"), ("service", "action")])
    _fail_on_unhandled_renames()
    assert set() == _UNHANDLED_RENAME_KEYS


def _fold_validator() -> Callable[[Any], Any]:
    """Return a closure whose qualname matches the fold detector."""

    def validator(value: Any) -> Any:
        return value

    validator.__qualname__ = "migrate_channel_colors.<locals>.validator"
    return validator


def test_channel_colors_fold_is_discovered_on_the_validator_chain(cv: ModuleType) -> None:
    schema = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    assert _collect_channel_colors_fold(_manifest(schema)) == "direct"
    assert _collect_channel_colors_fold(_manifest(cv.Schema({}))) is None
    assert _collect_channel_colors_fold(SimpleNamespace(config_schema=None)) is None


def test_channel_colors_fold_under_a_wrapper_is_nested(cv: ModuleType) -> None:
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    schema = cv.Schema({cv.Optional("strips"): cv.ensure_list(item)})
    assert _collect_channel_colors_fold(_manifest(schema)) == "nested"


def test_channel_colors_fold_shared_both_ways_is_nested(cv: ModuleType) -> None:
    """A shared closure reachable direct and nested still votes on both paths."""
    fold = _fold_validator()
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), fold)
    schema = cv.All(cv.Schema({cv.Optional("strips"): cv.ensure_list(item)}), fold)
    assert _collect_channel_colors_fold(_manifest(schema)) == "nested"


def test_channel_colors_fold_carrier_reachable_both_ways_is_nested(cv: ModuleType) -> None:
    """A fold-carrying node reachable direct and nested classifies nested in either pop order."""
    fold = _fold_validator()
    carrier = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), fold)
    listed = cv.Schema({cv.Optional("strips"): cv.ensure_list(carrier)})
    for schema in (cv.All(carrier, listed), cv.All(listed, carrier)):
        sync_components._CHANNEL_COLORS_MEMO.clear()
        assert _collect_channel_colors_fold(_manifest(schema)) == "nested"


def test_rename_carrier_reachable_both_ways_is_nested_in_either_order(cv: ModuleType) -> None:
    """A rename-carrying node reachable direct and nested classifies nested in either pop order."""
    rename = cv.rename_key("service", "action")
    carrier = cv.All(cv.Schema({cv.Optional("action"): cv.string}), rename)
    listed = cv.Schema({cv.Optional("actions"): cv.ensure_list(carrier)})
    for schema in (cv.All(carrier, listed), cv.All(listed, carrier)):
        assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): False}


def test_nested_platform_fold_routes_to_the_canary(cv: ModuleType) -> None:
    """The depth-1 runtime fold can't apply a nested fold, so no rule is emitted."""
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    platform = _manifest(cv.Schema({cv.Optional("strips"): cv.ensure_list(item)}))
    sync_components._classify_channel_colors_folds(
        "weird_strip", SimpleNamespace(config_schema=None), [("light", platform)]
    )
    assert set() == _MIGRATION_RULES
    assert {"weird_strip"} == _UNHANDLED_CHANNEL_COLORS


_LED_STRIP_RULE_ROW = (
    "platform_channel_colors",
    "",
    "light",
    "esp32_rmt_led_strip",
    "rgb_order",
    "channel_colors",
)


def test_channel_colors_fold_outside_a_platform_schema_fails_the_sync(cv: ModuleType) -> None:
    schema = cv.All(cv.Schema({}), _fold_validator())
    sync_components._classify_channel_colors_folds("weird_component", _manifest(schema), [])
    assert {"weird_component"} == _UNHANDLED_CHANNEL_COLORS
    _RENAME_SWEEP_COUNT[0] = 1
    with pytest.raises(SystemExit, match="weird_component"):
        _fail_on_unhandled_renames()


def test_acknowledged_channel_colors_fold_is_skipped(
    cv: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = cv.All(cv.Schema({}), _fold_validator())
    monkeypatch.setattr(sync_components, "_HANDLED_CHANNEL_COLORS", {"weird_component"})
    sync_components._classify_channel_colors_folds("weird_component", _manifest(schema), [])
    assert set() == _UNHANDLED_CHANNEL_COLORS


def test_missing_channel_colors_rules_fail_a_fold_capable_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_components, "_installed_esphome_has_channel_colors_fold", lambda: True)
    monkeypatch.setattr(sync_components, "_committed_channel_colors_platforms", set)
    with pytest.raises(SystemExit, match="platform_channel_colors"):
        sync_components._fail_on_missing_channel_colors_rules([])
    _MIGRATION_RULES.add(_LED_STRIP_RULE_ROW)
    sync_components._fail_on_missing_channel_colors_rules([])


def test_vanished_committed_rule_fails_per_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One platform's row disappearing must abort even while sibling rows survive."""
    monkeypatch.setattr(sync_components, "_installed_esphome_has_channel_colors_fold", lambda: True)
    monkeypatch.setattr(
        sync_components,
        "_committed_channel_colors_platforms",
        lambda: {("light", "esp32_rmt_led_strip"), ("light", "rp2040_pio_led_strip")},
    )
    _MIGRATION_RULES.add(_LED_STRIP_RULE_ROW)
    with pytest.raises(SystemExit, match="rp2040_pio_led_strip"):
        sync_components._fail_on_missing_channel_colors_rules([])


def test_stale_committed_rules_fail_the_missing_rules_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed rows absent from the sweep abort the sync."""
    monkeypatch.setattr(
        sync_components, "_installed_esphome_has_channel_colors_fold", lambda: False
    )
    monkeypatch.setattr(
        sync_components,
        "_committed_channel_colors_platforms",
        lambda: {("light", "esp32_rmt_led_strip")},
    )
    with pytest.raises(SystemExit, match="esp32_rmt_led_strip"):
        sync_components._fail_on_missing_channel_colors_rules([])


def test_fold_free_esphome_passes_the_missing_rules_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sync_components, "_installed_esphome_has_channel_colors_fold", lambda: False
    )
    monkeypatch.setattr(sync_components, "_committed_channel_colors_platforms", set)
    sync_components._fail_on_missing_channel_colors_rules([])


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(b"{not json", "unreadable", id="corrupt"),
        pytest.param(b"[]", "rules", id="wrong-shape"),
        pytest.param(b'{"rules": ["bare"]}', "non-mapping", id="non-mapping-row"),
        pytest.param(
            b'{"rules": [{"kind": "platform_channel_colors", "domain": "light"}]}',
            "missing domain/platform",
            id="missing-field",
        ),
    ],
)
def test_unreadable_committed_artifact_fails_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes, match: str
) -> None:
    """A corrupt, mis-shaped, or field-less artifact aborts instead of reading as empty."""
    bad = tmp_path / "migration_rules.index.json"
    bad.write_bytes(payload)
    monkeypatch.setattr(sync_components, "_MIGRATION_RULES_INDEX_FILE", bad)
    with pytest.raises(SystemExit, match=match):
        sync_components._committed_channel_colors_platforms()


def test_catalog_capable_platform_without_a_rule_fails_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A built catalog entry carrying both fold keys expects an emitted rule."""
    monkeypatch.setattr(sync_components, "_installed_esphome_has_channel_colors_fold", lambda: True)
    monkeypatch.setattr(sync_components, "_committed_channel_colors_platforms", set)
    catalog = [
        {
            "id": "light.esp32_rmt_led_strip",
            "config_entries": [{"key": "rgb_order"}, {"key": "channel_colors"}],
        },
        {"id": "light.fastled_clockless", "config_entries": [{"key": "rgb_order"}]},
        {"id": "ethernet", "config_entries": [{"key": "rgb_order"}, {"key": "channel_colors"}]},
    ]
    with pytest.raises(SystemExit, match="esp32_rmt_led_strip"):
        sync_components._fail_on_missing_channel_colors_rules(catalog)
    _MIGRATION_RULES.add(_LED_STRIP_RULE_ROW)
    sync_components._fail_on_missing_channel_colors_rules(catalog)


def test_acknowledged_fold_platform_does_not_wedge_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bespoke-acknowledged platform's catalog keys must not demand a rule row."""
    monkeypatch.setattr(sync_components, "_installed_esphome_has_channel_colors_fold", lambda: True)
    monkeypatch.setattr(sync_components, "_committed_channel_colors_platforms", set)
    monkeypatch.setattr(sync_components, "_HANDLED_CHANNEL_COLORS", {"weird_strip"})
    catalog = [
        {
            "id": "light.weird_strip",
            "config_entries": [{"key": "rgb_order"}, {"key": "channel_colors"}],
        },
    ]
    sync_components._fail_on_missing_channel_colors_rules(catalog)


def _committed_catalog_fold_platforms() -> list[str]:
    """Platform stems whose committed light catalog carries both fold keys."""
    records = [
        orjson.loads(path.read_bytes())
        for path in sorted((Path(definitions.__file__).parent / "components").glob("light.*.json"))
    ]
    return sorted(
        platform
        for domain, platform in sync_components._catalog_channel_colors_platforms(records)
        if domain == "light"
    )


def test_committed_artifact_platforms_read_the_real_artifact() -> None:
    """The shipped artifact parses; rows are well-formed pairs."""
    pairs = sync_components._committed_channel_colors_platforms()
    if not pairs:
        pytest.skip("shipped artifact predates the channel_colors fold rules")
    for domain, platform in pairs:
        assert domain and platform


@pytest.mark.parametrize("platform", _committed_catalog_fold_platforms())
def test_live_fold_emits_a_rule_for_every_capable_platform(platform: str) -> None:
    """Every committed catalog platform carrying both fold keys emits its rule."""
    pytest.importorskip("esphome.loader")
    if not sync_components._installed_esphome_has_channel_colors_fold():
        pytest.skip("installed esphome predates channel_colors")
    introspect_component(platform)
    row = ("platform_channel_colors", "", "light", platform, "rgb_order", "channel_colors")
    assert row in _MIGRATION_RULES
    assert set() == _UNHANDLED_CHANNEL_COLORS


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
        _fail_on_unhandled_renames()
