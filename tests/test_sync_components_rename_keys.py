"""``cv.rename_key`` alias extraction, pinned at the generator and the committed catalog."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _collect_rename_keys,
    introspect_component,
)

_DEFINITIONS_DIR = Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions"


@pytest.fixture
def cv():
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


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


def test_schema_without_renames_yields_empty(cv) -> None:
    schema = cv.Schema({cv.Optional("name"): cv.string})
    assert _collect_rename_keys(_manifest(schema)) == {}


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


def test_live_api_schema_yields_both_pairs(cv) -> None:
    renamed = introspect_component("api").get("renamed_keys")
    assert renamed == {"services": "actions", "service": "action"}


def test_committed_catalog_pins_api_renamed_keys() -> None:
    expected = {"services": "actions", "service": "action"}
    body = json.loads((_DEFINITIONS_DIR / "components" / "api.json").read_text(encoding="utf-8"))
    assert body["renamed_keys"] == expected
    index = json.loads((_DEFINITIONS_DIR / "components.index.json").read_text(encoding="utf-8"))
    api_row = next(c for c in index["components"] if c["id"] == "api")
    assert api_row["renamed_keys"] == expected
