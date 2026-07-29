"""``cv.rename_key`` discovery and the handled-list canary, pinned at the generator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _RENAME_SWEEP_COUNT,
    _UNHANDLED_RENAME_KEYS,
    _collect_rename_keys,
    _fail_on_unhandled_rename_keys,
    _note_unhandled_rename_keys,
    introspect_component,
)


@pytest.fixture
def cv():
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


@pytest.fixture(autouse=True)
def _clean_unhandled():
    _UNHANDLED_RENAME_KEYS.clear()
    saved_sweeps = _RENAME_SWEEP_COUNT[0]
    yield
    _UNHANDLED_RENAME_KEYS.clear()
    _RENAME_SWEEP_COUNT[0] = saved_sweeps


def _manifest(schema) -> SimpleNamespace:
    return SimpleNamespace(config_schema=schema)


def test_top_level_rename_is_discovered(cv) -> None:
    schema = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    assert _collect_rename_keys(_manifest(schema)) == {"old_name": "new_name"}


def test_nested_list_item_rename_is_discovered(cv) -> None:
    item = cv.All(
        cv.Schema({cv.Optional("action"): cv.string}),
        cv.rename_key("service", "action"),
    )
    schema = cv.Schema({cv.Optional("actions"): cv.ensure_list(item)})
    assert _collect_rename_keys(_manifest(schema)) == {"service": "action"}


def test_typed_schema_branch_rename_is_discovered(cv) -> None:
    branch = cv.All(
        cv.Schema({cv.Optional("new_name"): cv.string}),
        cv.rename_key("old_name", "new_name"),
    )
    schema = cv.typed_schema({"MODELA": branch})
    assert _collect_rename_keys(_manifest(schema)) == {"old_name": "new_name"}


def test_codegen_enum_values_terminate(cv) -> None:
    """A typed_schema whose closure holds codegen MockObj values must not hang."""
    import esphome.codegen as cg  # noqa: PLC0415

    ns = cg.esphome_ns.namespace("rename_test")
    enum = ns.enum("Model", True)
    schema = cv.typed_schema(
        {"MODELA": cv.Schema({cv.Optional("name"): cv.string})},
        enum={"MODELA": enum.MODELA},
    )
    assert _collect_rename_keys(_manifest(schema)) == {}


def test_live_api_pairs_are_discovered_and_handled(cv) -> None:
    """The walk finds the api pairs and the handled list covers them."""
    from script.sync_components import _get_esphome_loader  # noqa: PLC0415

    manifest = _get_esphome_loader().get_component("api")
    assert _collect_rename_keys(manifest) == {"services": "actions", "service": "action"}
    introspect_component("api")
    assert set() == _UNHANDLED_RENAME_KEYS


def test_unhandled_pair_fails_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("sgp4x", {"voc": "voc_index"})
    with pytest.raises(SystemExit, match="sgp4x: voc -> voc_index"):
        _fail_on_unhandled_rename_keys()


def test_handled_pairs_do_not_fail_the_sync() -> None:
    _RENAME_SWEEP_COUNT[0] = 1
    _note_unhandled_rename_keys("api", {"services": "actions", "service": "action"})
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
