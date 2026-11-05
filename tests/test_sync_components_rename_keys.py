"""``cv.rename_key`` discovery, routing, and the handled-list canary, pinned at the generator."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import orjson
import pytest
import voluptuous as vol

from esphome_device_builder import definitions
from script import sync_components
from script.sync_components import (  # type: ignore[import-not-found]
    _MIGRATION_RULES,
    _RENAME_SWEEP_COUNT,
    _UNHANDLED_ALIASES,
    _UNHANDLED_CHANNEL_COLORS,
    _UNHANDLED_RENAME_KEYS,
    MigrationRuleRow,
    SchemaHit,
    _classify_rename_pairs,
    _collect_channel_colors_fold,
    _collect_rename_keys,
    _emit_migration_rules_index,
    _fail_on_unhandled_renames,
    _note_unhandled_rename_keys,
    _rename_walk_children,
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


def _renamed_item(cv: ModuleType, new: str = "new_name") -> Any:
    """Return a mapping schema carrying ``old_name`` -> *new* on its own validator chain."""
    return cv.All(
        cv.Schema({cv.Optional(new): cv.string}),
        cv.rename_key("old_name", new),
    )


_COMBINATION_RULE_ROW = MigrationRuleRow(
    "platform_item_list_field",
    "coeffecient",
    "coefficient",
    domain="sensor",
    platform="combination",
    list_key="sources",
)


def test_top_level_rename_is_discovered_direct(cv: ModuleType) -> None:
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_nested_list_item_rename_carries_its_list_key(cv: ModuleType) -> None:
    item = cv.All(
        cv.Schema({cv.Optional("action"): cv.string}),
        cv.rename_key("service", "action"),
    )
    schema = cv.Schema({cv.Optional("actions"): cv.ensure_list(item)})
    assert _collect_rename_keys(_manifest(schema)) == {
        ("service", "action"): SchemaHit(("actions", "[]"))
    }


def test_typed_schema_variant_rename_is_direct(cv: ModuleType) -> None:
    branch = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    schema = cv.typed_schema({"MODELA": branch})
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_typed_schema_variant_list_item_rename_carries_its_list_key(cv: ModuleType) -> None:
    """The combination shape: a rename on the items of one variant's depth-1 list."""
    item = cv.All(
        cv.rename_key("coeffecient", "coefficient", removed_in="2026.12.0"),
        cv.Schema({cv.Required("coefficient"): cv.float_}),
    )
    schema = cv.typed_schema(
        {
            "linear": cv.Schema({cv.Required("sources"): cv.ensure_list(item)}),
            "kalman": cv.Schema({cv.Required("sources"): cv.ensure_list(cv.Schema({}))}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("coeffecient", "coefficient"): SchemaHit(("sources", "[]"), removed_in="2026.12.0")
    }


def test_nested_mapping_rename_keeps_its_key_placement(cv: ModuleType) -> None:
    item = _renamed_item(cv)
    schema = cv.Schema({cv.Optional("inner"): item})
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(("inner",))
    }


def test_legacy_key_declared_beside_its_rename_stays_direct(cv: ModuleType) -> None:
    """The api shape: the legacy key is still declared on the mapping the rename sits on."""
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string, cv.Optional("old_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_sibling_variant_owning_the_legacy_key_is_inexpressible(cv: ModuleType) -> None:
    """A rule can't carry the variant, so a variant still using ``old_name`` blocks it."""
    schema = cv.typed_schema(
        {
            "renamed": _renamed_item(cv),
            "owner": cv.Schema({cv.Optional("old_name"): cv.string}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_owning_the_legacy_list_item_key_is_inexpressible(
    cv: ModuleType,
) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema(
                {cv.Optional("items"): cv.ensure_list({cv.Optional("old_name"): cv.string})}
            ),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_owning_the_legacy_key_as_a_singleton_mapping_is_inexpressible(
    cv: ModuleType,
) -> None:
    """The engine also respells the list's singleton mapping form, so that sibling counts."""
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema(
                {cv.Optional("items"): cv.Schema({cv.Optional("old_name"): cv.string})}
            ),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_open_sibling_variant_covers_a_list_item_rename(cv: ModuleType) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "open": cv.Schema({}, extra=cv.ALLOW_EXTRA),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_with_an_unreadable_list_key_value_is_inexpressible(
    cv: ModuleType,
) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema({cv.Optional("items"): cv.string}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_with_an_opaque_chain_list_key_value_is_inexpressible(
    cv: ModuleType,
) -> None:
    """A chain of plain validators under the list key may accept any mapping."""
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema({cv.Optional("items"): cv.All(lambda value: value)}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_wrapper_yielding_nothing_owns_every_key(cv: ModuleType) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema(
                {cv.Optional("items"): _wrapper_holding("ensure_list.<locals>.validator", 42)}
            ),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_conflicting_target_in_the_singleton_mapping_form_voids_both(cv: ModuleType) -> None:
    """The list rule also respells the singleton form, so a rename there conflicts."""
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "other": cv.Schema({cv.Optional("items"): _renamed_item(cv, "other_name")}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(None),
        ("old_name", "other_name"): SchemaHit(None),
    }


def test_conflicting_target_on_a_demoted_pair_still_voids(cv: ModuleType) -> None:
    """A pair already inexpressible for mixed placement keeps blocking its rival."""
    other = _renamed_item(cv, "other_name")
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "other": cv.Schema(
                {
                    cv.Optional("items"): cv.ensure_list(other),
                    cv.Optional("deeper"): cv.Schema({cv.Optional("items"): cv.ensure_list(other)}),
                }
            ),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(None),
        ("old_name", "other_name"): SchemaHit(None),
    }


def test_any_alternative_declaring_the_legacy_key_is_inexpressible(cv: ModuleType) -> None:
    """A ``cv.Any`` alternative is a sibling the rule can't tell apart either."""
    schema = cv.Any(_renamed_item(cv), cv.Schema({cv.Optional("old_name"): cv.string}))
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_scalar_any_alternative_is_inexpressible(cv: ModuleType) -> None:
    schema = cv.Any(_renamed_item(cv), cv.string)
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_any_alternatives_sharing_one_rename_stay_direct(cv: ModuleType) -> None:
    item = _renamed_item(cv)
    schema = cv.Any(item, cv.All(item))
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_validator_keyed_sibling_variant_is_inexpressible(cv: ModuleType) -> None:
    schema = cv.typed_schema(
        {"renamed": _renamed_item(cv), "open": cv.Schema({cv.string: cv.string})}
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_sibling_variant_with_unreadable_list_items_is_inexpressible(cv: ModuleType) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema({cv.Optional("items"): cv.ensure_list(cv.string)}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


_DEPTH_CAPPED = "<schema past the depth cap>"


def test_depth_capped_sibling_variant_owns_every_key_and_fails_the_sync(cv: ModuleType) -> None:
    """A variant the walk gave up on may hide the legacy key, so the sentinel reaches the canary."""
    deep: Any = cv.Schema({cv.Optional("old_name"): cv.string})
    for _level in range(40):
        deep = cv.All(deep)
    schema = cv.typed_schema({"renamed": _renamed_item(cv), "deep": deep})
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(None),
        (_DEPTH_CAPPED, _DEPTH_CAPPED): SchemaHit(()),
    }


def test_depth_capped_content_beyond_the_rewrite_scope_keeps_the_rule(cv: ModuleType) -> None:
    deep: Any = cv.Schema({cv.Optional("old_name"): cv.string})
    for level in range(40):
        deep = cv.Schema({cv.Optional(f"level{level}"): deep})
    schema = cv.typed_schema({"renamed": _renamed_item(cv), "deep": deep})
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(()),
        (_DEPTH_CAPPED, _DEPTH_CAPPED): SchemaHit(None),
    }


def test_sibling_variant_using_the_legacy_key_elsewhere_stays_expressible(
    cv: ModuleType,
) -> None:
    schema = cv.typed_schema(
        {
            "renamed": cv.Schema({cv.Optional("items"): cv.ensure_list(_renamed_item(cv))}),
            "owner": cv.Schema({cv.Optional("old_name"): cv.string}),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(("items", "[]"))
    }


def test_variants_renaming_one_legacy_key_to_different_targets_are_inexpressible(
    cv: ModuleType,
) -> None:
    """No rule carries the variant, so the first rule would respell the other's items wrongly."""
    schema = cv.typed_schema(
        {
            "renamed": _renamed_item(cv),
            "other": _renamed_item(cv, "other_name"),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(None),
        ("old_name", "other_name"): SchemaHit(None),
    }


def test_bare_rename_sibling_variant_owns_every_key(cv: ModuleType) -> None:
    """A variant that is only a rename validator passes every other key through."""
    schema = cv.typed_schema({"renamed": _renamed_item(cv), "bare": cv.rename_key("x", "y")})
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(None),
        ("x", "y"): SchemaHit(()),
    }


def test_variants_under_an_unconditional_rename_stay_direct(cv: ModuleType) -> None:
    """A rename on the chain above the variants applies to every one of them."""
    schema = cv.All(
        cv.rename_key("old_name", "new_name"),
        cv.typed_schema(
            {
                "both": cv.Schema(
                    {cv.Optional("old_name"): cv.string, cv.Optional("new_name"): cv.string}
                ),
                "open": cv.Schema({}, extra=cv.ALLOW_EXTRA),
            }
        ),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_variants_sharing_one_rename_stay_direct(cv: ModuleType) -> None:
    schema = cv.typed_schema({"a": _renamed_item(cv), "b": _renamed_item(cv)})
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_different_targets_at_different_placements_keep_their_own(cv: ModuleType) -> None:
    schema = cv.typed_schema(
        {
            "renamed": _renamed_item(cv),
            "other": cv.Schema(
                {cv.Optional("items"): cv.ensure_list(_renamed_item(cv, "other_name"))}
            ),
        }
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit(()),
        ("old_name", "other_name"): SchemaHit(("items", "[]")),
    }


def test_deeper_list_item_rename_stays_inexpressible(cv: ModuleType) -> None:
    item = _renamed_item(cv)
    schema = cv.Schema(
        {cv.Optional("outer"): cv.Schema({cv.Optional("inner"): cv.ensure_list(item)})}
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_list_item_rename_under_two_keys_stays_inexpressible(cv: ModuleType) -> None:
    item = _renamed_item(cv)
    schema = cv.Schema(
        {cv.Optional("first"): cv.ensure_list(item), cv.Optional("second"): cv.ensure_list(item)}
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


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
        ("services", "actions"): SchemaHit(()),
        ("service", "action"): SchemaHit(None),
    }
    introspect_component("api")
    assert set() == _UNHANDLED_RENAME_KEYS
    assert _MIGRATION_RULES == {}


def test_platform_manifest_renames_never_reach_the_canary(cv: ModuleType) -> None:
    """sgp4x pairs route to the artifact, never the canary, on every esphome channel."""
    introspect_component("sgp4x")
    assert set() == _UNHANDLED_RENAME_KEYS
    for row in _MIGRATION_RULES:
        assert row.kind == "platform_item_field"
        assert row.domain == "sensor"
        assert row.platform == "sgp4x"


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


def test_live_combination_sources_pair_ships_data_driven(cv: ModuleType) -> None:
    """The combination ``coeffecient`` rename emits a list-item rule, not a canary hit."""
    from esphome.components.combination import sensor as combination_sensor  # noqa: PLC0415

    if "rename_key" not in inspect.getsource(combination_sensor):
        pytest.skip("installed esphome predates the combination coeffecient rename")
    introspect_component("combination")
    assert set() == _UNHANDLED_RENAME_KEYS
    assert {_COMBINATION_RULE_ROW: "2026.12.0"} == _MIGRATION_RULES


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
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): SchemaHit(())})
    assert {
        MigrationRuleRow("component_block_field", "voc", "voc_index", component="sgp4x")
    } == _MIGRATION_RULES.keys()
    assert set() == _UNHANDLED_RENAME_KEYS


def test_direct_platform_pair_routes_to_the_artifact() -> None:
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): SchemaHit(())}, domain="sensor")
    assert {
        MigrationRuleRow(
            "platform_item_field", "voc", "voc_index", domain="sensor", platform="sgp4x"
        )
    } == _MIGRATION_RULES.keys()
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
    assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): SchemaHit(None)}


def test_shared_closure_reachable_both_ways_classifies_nested(cv: ModuleType) -> None:
    """One rename validator object placed direct and nested still votes on both paths."""
    rename = cv.rename_key("service", "action")
    item = cv.All(cv.Schema({cv.Optional("action"): cv.string}), rename)
    schema = cv.All(cv.Schema({cv.Optional("actions"): cv.ensure_list(item)}), rename)
    assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): SchemaHit(None)}


def test_list_item_platform_pair_routes_to_the_artifact() -> None:
    _classify_rename_pairs(
        "combination",
        {("coeffecient", "coefficient"): SchemaHit(("sources", "[]"))},
        domain="sensor",
    )
    assert {_COMBINATION_RULE_ROW} == _MIGRATION_RULES.keys()
    assert set() == _UNHANDLED_RENAME_KEYS


def _unbound_closure(qualname: str) -> Callable[[Any], Any]:
    """Return a closure whose only free variable was never bound, named as a *qualname* wrapper."""

    def outer() -> Callable[[Any], Any]:
        def validator(value: Any) -> Any:
            return schemas

        if value_never_set := False:  # pragma: no cover
            schemas = value_never_set
        return validator

    validator = outer()
    validator.__qualname__ = qualname
    return validator


def test_unreadable_wrapper_closures_yield_no_children(cv: ModuleType) -> None:
    for qualname in ("typed_schema.<locals>.validator", "validate_automation.<locals>.validator"):
        assert list(_rename_walk_children(_unbound_closure(qualname))) == []
    assert (
        sync_components._typed_schema_variants(_unbound_closure("typed_schema.<locals>.validator"))
        is None
    )


def test_unreadable_wrapper_closure_reaches_the_canary(cv: ModuleType) -> None:
    """The pairs beneath an unreadable wrapper can't be seen, so the sync must not pass clean."""
    sentinel = "<unreadable wrapper closure>"
    for qualname in ("ensure_list.<locals>.validator", "typed_schema.<locals>.validator"):
        schema = cv.Schema({cv.Optional("items"): _unbound_closure(qualname)})
        assert _collect_rename_keys(_manifest(schema)) == {
            (sentinel, sentinel): SchemaHit(("items",))
        }, qualname


def test_closure_less_ensure_list_wrapper_yields_no_children() -> None:
    def validator(value: Any) -> Any:
        return value

    validator.__qualname__ = "ensure_list.<locals>.validator"
    assert list(_rename_walk_children(validator)) == []


def _wrapper_holding(qualname: str, held: Any) -> Callable[[Any], Any]:
    """Return a closure named as a *qualname* wrapper whose only freevar is misnamed ``held``."""

    def validator(value: Any) -> Any:
        return held

    validator.__qualname__ = qualname
    return validator


def test_wrapper_with_a_renamed_freevar_still_reaches_the_canary(cv: ModuleType) -> None:
    """A misnamed closure variable degrades to inexpressible instead of vanishing."""
    for qualname in ("ensure_list.<locals>.validator", "typed_schema.<locals>.validator"):
        wrapper = _wrapper_holding(qualname, _renamed_item(cv))
        schema = cv.Schema({cv.Optional("items"): wrapper})
        assert _collect_rename_keys(_manifest(schema)) == {
            ("old_name", "new_name"): SchemaHit(None)
        }, qualname


def test_wrapper_with_a_renamed_freevar_holding_a_bare_rename_reaches_the_canary(
    cv: ModuleType,
) -> None:
    """The generic closure scan must not drop a validator held outside any schema."""
    wrapper = _wrapper_holding(
        "ensure_list.<locals>.validator", cv.rename_key("old_name", "new_name")
    )
    schema = cv.Schema({cv.Optional("items"): wrapper})
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_wrapper_with_a_renamed_freevar_holding_a_bare_fold_is_nested(cv: ModuleType) -> None:
    wrapper = _wrapper_holding("ensure_list.<locals>.validator", _fold_validator())
    schema = cv.Schema({cv.Optional("strips"): wrapper})
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(None, removed_in="2027.3.0")


def test_variant_without_a_mapping_makes_its_siblings_renames_inexpressible(
    cv: ModuleType,
) -> None:
    """A variant whose keys the walk never enumerates may accept the legacy key."""
    for plain in (cv.string, cv.All(cv.string)):
        schema = cv.typed_schema({"renamed": _renamed_item(cv), "plain": plain})
        assert _collect_rename_keys(_manifest(schema)) == {
            ("old_name", "new_name"): SchemaHit(None)
        }


@pytest.mark.parametrize("extra", [vol.ALLOW_EXTRA, vol.REMOVE_EXTRA])
def test_open_sibling_variant_makes_the_rename_inexpressible(cv: ModuleType, extra: int) -> None:
    """A mapping that keeps or drops undeclared keys accepts the legacy one without error."""
    schema = cv.typed_schema({"renamed": _renamed_item(cv), "open": cv.Schema({}, extra=extra)})
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(None)}


def test_open_mapping_carrying_its_own_rename_stays_direct(cv: ModuleType) -> None:
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}, extra=cv.ALLOW_EXTRA),
        cv.rename_key("old_name", "new_name"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {("old_name", "new_name"): SchemaHit(())}


def test_nested_mapping_pair_routes_to_the_canary() -> None:
    _classify_rename_pairs("sgp4x", {("old", "new"): SchemaHit(("inner",))}, domain="sensor")
    assert _MIGRATION_RULES == {}
    assert {("sgp4x", "old", "new")} == _UNHANDLED_RENAME_KEYS


def test_list_item_component_pair_routes_to_the_canary() -> None:
    """The block rule has no list-item leg, so a component block's list pair stays bespoke."""
    _classify_rename_pairs("mycomp", {("old", "new"): SchemaHit(("items", "[]"))})
    assert _MIGRATION_RULES == {}
    assert {("mycomp", "old", "new")} == _UNHANDLED_RENAME_KEYS


def test_platform_domain_pair_routes_to_the_canary() -> None:
    """A platform-domain block's ``- platform:`` items the block rule can't address."""
    _classify_rename_pairs("sensor", {("old", "new"): SchemaHit(())}, platform_domain=True)
    assert _MIGRATION_RULES == {}
    assert {("sensor", "old", "new")} == _UNHANDLED_RENAME_KEYS


def test_multi_conf_component_pair_ships_data_driven() -> None:
    """The block rule handles the list form, so multi_conf pairs emit as rules."""
    _classify_rename_pairs("xiaomi_rtcgq02lm", {("esp32_ble_id", "ble_hub_id"): SchemaHit(())})
    assert {
        MigrationRuleRow(
            "component_block_field", "esp32_ble_id", "ble_hub_id", component="xiaomi_rtcgq02lm"
        )
    } == _MIGRATION_RULES.keys()
    assert set() == _UNHANDLED_RENAME_KEYS


def test_non_direct_pair_routes_to_the_canary() -> None:
    _classify_rename_pairs("sgp4x", {("voc", "voc_index"): SchemaHit(None)})
    assert _MIGRATION_RULES == {}
    assert {("sgp4x", "voc", "voc_index")} == _UNHANDLED_RENAME_KEYS


def test_handled_pair_routes_nowhere_even_when_direct() -> None:
    _classify_rename_pairs("api", {("services", "actions"): SchemaHit(())})
    assert _MIGRATION_RULES == {}
    assert set() == _UNHANDLED_RENAME_KEYS


def test_sentinel_pair_routes_to_the_canary_despite_direct() -> None:
    sentinel = "<unreadable rename_key>"
    _classify_rename_pairs("broken", {(sentinel, sentinel): SchemaHit(())})
    assert _MIGRATION_RULES == {}
    assert {("broken", sentinel, sentinel)} == _UNHANDLED_RENAME_KEYS


def _redirect_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes = b'{"rules": []}'
) -> Path:
    """Point the sync's artifact path at a tmp file seeded with *payload*."""
    out_path = tmp_path / "migration_rules.index.json"
    monkeypatch.setattr(sync_components, "_MIGRATION_RULES_INDEX_FILE", out_path)
    out_path.write_bytes(payload)
    return out_path


def test_emit_writes_sorted_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = _redirect_artifact(tmp_path, monkeypatch)
    _MIGRATION_RULES[
        MigrationRuleRow(
            "platform_item_field", "voc", "voc_index", domain="sensor", platform="sgp4x"
        )
    ] = "2027.2.0"
    _MIGRATION_RULES[
        MigrationRuleRow("component_block_field", "old", "new", component="ethernet")
    ] = None
    _MIGRATION_RULES[MigrationRuleRow("component_key", "rp2040", "rp2")] = "2027.7.0"
    _MIGRATION_RULES[_COMBINATION_RULE_ROW] = "2026.12.0"
    _emit_migration_rules_index("2026.8.0b5")
    payload = orjson.loads(out_path.read_bytes())
    assert payload == {
        "rules": [
            {
                "component": "ethernet",
                "kind": "component_block_field",
                "new": "new",
                "old": "old",
                "removed_in": None,
                "since": "2026.8.0b5",
            },
            {
                "kind": "component_key",
                "new": "rp2",
                "old": "rp2040",
                "removed_in": "2027.7.0",
                "since": "2026.8.0b5",
            },
            {
                "domain": "sensor",
                "kind": "platform_item_field",
                "new": "voc_index",
                "old": "voc",
                "platform": "sgp4x",
                "removed_in": "2027.2.0",
                "since": "2026.8.0b5",
            },
            {
                "domain": "sensor",
                "kind": "platform_item_list_field",
                "list_key": "sources",
                "new": "coefficient",
                "old": "coeffecient",
                "platform": "combination",
                "removed_in": "2026.12.0",
                "since": "2026.8.0b5",
            },
        ]
    }


def test_emit_keeps_the_committed_since_for_a_known_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row the artifact already carries keeps its first-seen esphome; a new row is stamped."""
    out_path = _redirect_artifact(
        tmp_path,
        monkeypatch,
        orjson.dumps(
            {
                "rules": [
                    {
                        "kind": "component_key",
                        "old": "rp2040",
                        "new": "rp2",
                        "since": "2026.7.0",
                        "removed_in": None,
                    },
                    {
                        "kind": "component_key",
                        "old": "gone",
                        "new": "away",
                        "since": "2026.7.0",
                        "removed_in": None,
                    },
                ]
            }
        ),
    )
    _MIGRATION_RULES[MigrationRuleRow("component_key", "rp2040", "rp2")] = "2027.7.0"
    _MIGRATION_RULES[MigrationRuleRow("component_key", "old", "new")] = None
    _emit_migration_rules_index("2026.8.0b5")
    rules = orjson.loads(out_path.read_bytes())["rules"]
    assert [(r["old"], r["since"], r["removed_in"]) for r in rules] == [
        ("old", "2026.8.0b5", None),
        ("rp2040", "2026.7.0", "2027.7.0"),
    ]


def test_emit_writes_the_empty_steady_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_path = _redirect_artifact(tmp_path, monkeypatch)
    _emit_migration_rules_index("2026.8.0b5")
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
    removal_version: str | None = None,
) -> None:
    loader = SimpleNamespace(
        get_alias_metadata=lambda: {
            legacy: SimpleNamespace(canonical=canonical, removal_version=removal_version)
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
    assert _MIGRATION_RULES == {}
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
    assert _MIGRATION_RULES == {}
    assert {("legacy_x", "canon_x")} == _UNHANDLED_ALIASES


def test_alias_rule_carries_the_removal_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_loader(
        monkeypatch,
        canonical="canon_x",
        manifest=SimpleNamespace(is_platform_component=False),
        removal_version="2027.7.0",
    )
    _sweep_component_aliases()
    assert {
        MigrationRuleRow("component_key", "legacy_x", "canon_x"): "2027.7.0"
    } == _MIGRATION_RULES


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
    assert _MIGRATION_RULES == {}
    assert {("esp32", "esp32_new")} == _UNHANDLED_ALIASES


def test_acknowledged_alias_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_loader(
        monkeypatch, canonical="canon_x", manifest=SimpleNamespace(is_platform_component=True)
    )
    monkeypatch.setattr(sync_components, "_HANDLED_ALIASES", {"legacy_x"})
    _sweep_component_aliases()
    assert _MIGRATION_RULES == {}
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


def _fold_validator(removed_in: str | None = "2027.3.0") -> Callable[[Any], Any]:
    """Return a closure whose qualname matches the fold detector and carries *removed_in*."""

    def validator(value: Any) -> Any:
        return value, removed_in

    validator.__qualname__ = "migrate_channel_colors.<locals>.validator"
    return validator


def test_channel_colors_fold_is_discovered_on_the_validator_chain(cv: ModuleType) -> None:
    schema = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit((), removed_in="2027.3.0")
    assert _collect_channel_colors_fold(_manifest(cv.Schema({}))) is None
    assert _collect_channel_colors_fold(SimpleNamespace(config_schema=None)) is None


def test_channel_colors_fold_under_a_wrapper_is_nested(cv: ModuleType) -> None:
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    schema = cv.Schema({cv.Optional("strips"): cv.ensure_list(item)})
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(None, removed_in="2027.3.0")


def test_channel_colors_fold_under_a_typed_variant_is_nested(cv: ModuleType) -> None:
    """The fold has no sibling-variant guard, so a variant-scoped fold stays on the canary."""
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    schema = cv.typed_schema({"strip": item})
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(None, removed_in="2027.3.0")


def test_unreadable_wrapper_closure_makes_the_fold_nested(cv: ModuleType) -> None:
    """A fold beneath a wrapper the walk can't read must reach the canary, not vanish."""
    schema = cv.Schema({cv.Optional("items"): _unbound_closure("ensure_list.<locals>.validator")})
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(None)


def test_channel_colors_fold_shared_both_ways_is_nested(cv: ModuleType) -> None:
    """A shared closure reachable direct and nested still votes on both paths."""
    fold = _fold_validator()
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), fold)
    schema = cv.All(cv.Schema({cv.Optional("strips"): cv.ensure_list(item)}), fold)
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(None, removed_in="2027.3.0")


def test_channel_colors_fold_carrier_reachable_both_ways_is_nested(cv: ModuleType) -> None:
    """A fold-carrying node reachable direct and nested classifies nested in either pop order."""
    fold = _fold_validator()
    carrier = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), fold)
    listed = cv.Schema({cv.Optional("strips"): cv.ensure_list(carrier)})
    for schema in (cv.All(carrier, listed), cv.All(listed, carrier)):
        sync_components._CHANNEL_COLORS_MEMO.clear()
        assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit(
            None, removed_in="2027.3.0"
        )


def test_rename_carrier_reachable_both_ways_is_nested_in_either_order(cv: ModuleType) -> None:
    """A rename-carrying node reachable direct and nested classifies nested in either pop order."""
    rename = cv.rename_key("service", "action")
    carrier = cv.All(cv.Schema({cv.Optional("action"): cv.string}), rename)
    listed = cv.Schema({cv.Optional("actions"): cv.ensure_list(carrier)})
    for schema in (cv.All(carrier, listed), cv.All(listed, carrier)):
        assert _collect_rename_keys(_manifest(schema)) == {("service", "action"): SchemaHit(None)}


def test_nested_platform_fold_routes_to_the_canary(cv: ModuleType) -> None:
    """The depth-1 runtime fold can't apply a nested fold, so no rule is emitted."""
    item = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator())
    platform = _manifest(cv.Schema({cv.Optional("strips"): cv.ensure_list(item)}))
    sync_components._classify_channel_colors_folds(
        "weird_strip", SimpleNamespace(config_schema=None), [("light", platform)]
    )
    assert _MIGRATION_RULES == {}
    assert {"weird_strip"} == _UNHANDLED_CHANNEL_COLORS


_LED_STRIP_RULE_ROW = MigrationRuleRow(
    "platform_channel_colors",
    "rgb_order",
    "channel_colors",
    domain="light",
    platform="esp32_rmt_led_strip",
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
    _MIGRATION_RULES[_LED_STRIP_RULE_ROW] = None
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
    _MIGRATION_RULES[_LED_STRIP_RULE_ROW] = None
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
    _MIGRATION_RULES[_LED_STRIP_RULE_ROW] = None
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
    row = MigrationRuleRow(
        "platform_channel_colors", "rgb_order", "channel_colors", domain="light", platform=platform
    )
    assert row in _MIGRATION_RULES
    # ``removed_in`` is a required argument upstream, so every rule carries it.
    assert _MIGRATION_RULES[row]
    assert set() == _UNHANDLED_CHANNEL_COLORS


def test_rename_key_removed_in_is_read_off_the_closure(cv: ModuleType) -> None:
    if "removed_in" not in inspect.signature(cv.rename_key).parameters:
        pytest.skip("installed esphome predates rename_key(removed_in=)")
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name", removed_in="2027.1.0", component="x"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {
        ("old_name", "new_name"): SchemaHit((), removed_in="2027.1.0")
    }
    _classify_rename_pairs("x", _collect_rename_keys(_manifest(schema)))
    assert {
        MigrationRuleRow("component_block_field", "old_name", "new_name", component="x"): "2027.1.0"
    } == _MIGRATION_RULES


def test_channel_colors_fold_removed_in_is_read_off_the_closure(cv: ModuleType) -> None:
    light = pytest.importorskip("esphome.components.light")
    if not hasattr(light, "migrate_channel_colors"):
        pytest.skip("installed esphome predates channel_colors")
    fold = light.migrate_channel_colors(removed_in="2027.3.0", component="x")
    schema = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), fold)
    assert _collect_channel_colors_fold(_manifest(schema)) == SchemaHit((), removed_in="2027.3.0")


def test_conflicting_removed_in_across_paths_fails_the_sync(cv: ModuleType) -> None:
    if "removed_in" not in inspect.signature(cv.rename_key).parameters:
        pytest.skip("installed esphome predates rename_key(removed_in=)")
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name", removed_in="2027.1.0", component="x"),
        cv.rename_key("old_name", "new_name", removed_in="2027.2.0", component="x"),
    )
    with pytest.raises(SystemExit, match="conflicting removed_in"):
        _collect_rename_keys(_manifest(schema))


def test_malformed_committed_row_fails_the_since_carry_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _redirect_artifact(
        tmp_path,
        monkeypatch,
        orjson.dumps({"rules": [{"kind": "component_key", "old": "a", "new": "b", "since": None}]}),
    )
    with pytest.raises(SystemExit, match="malformed committed row"):
        _emit_migration_rules_index("2026.8.0b5")


def test_fold_without_removed_in_fails_the_sync(cv: ModuleType) -> None:
    schema = cv.All(cv.Schema({cv.Optional("channel_colors"): cv.string}), _fold_validator(None))
    with pytest.raises(SystemExit, match="removed_in"):
        _collect_channel_colors_fold(_manifest(schema))


def test_unreadable_rename_closure_yields_sentinel_pair() -> None:
    from script.sync_components import _rename_key_pair  # noqa: PLC0415

    def validator(value):
        return value

    validator.__qualname__ = "rename_key.<locals>.validator"
    assert _rename_key_pair(validator) == (
        "<unreadable rename_key>",
        "<unreadable rename_key>",
        None,
    )


def test_zero_sweep_fails_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 0
    with pytest.raises(SystemExit, match="walked no schemas"):
        _fail_on_unhandled_renames()
