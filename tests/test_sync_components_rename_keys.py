"""``cv.rename_key`` alias extraction, pinned at the generator and the committed catalog."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_renamed_marks,
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


def test_apply_renamed_marks_requires_canonical_sibling_at_level() -> None:
    """A mark lands only where the canonical key exists at the same level."""
    entries = [
        {"key": "services"},
        {
            "key": "actions",
            "config_entries": [{"key": "service"}, {"key": "action"}],
        },
        # The pair's target exists only at the top level, so this
        # nested ``services`` must stay unmarked.
        {"key": "other", "config_entries": [{"key": "services"}]},
    ]
    _apply_renamed_marks(entries, {"services": "actions", "service": "action"})
    assert entries[0]["renamed_to"] == "actions"
    assert entries[1]["config_entries"][0]["renamed_to"] == "action"
    assert "renamed_to" not in entries[1]["config_entries"][1]
    assert "renamed_to" not in entries[2]["config_entries"][0]


def test_committed_catalog_pins_api_renamed_marks() -> None:
    body = json.loads((_DEFINITIONS_DIR / "components" / "api.json").read_text(encoding="utf-8"))
    entries = {e["key"]: e for e in body["config_entries"]}
    assert entries["services"]["renamed_to"] == "actions"
    assert "renamed_to" not in entries["actions"]
    for parent in ("actions", "services"):
        children = {e["key"]: e for e in entries[parent]["config_entries"]}
        assert children["service"]["renamed_to"] == "action"
        assert "renamed_to" not in children["action"]
    index = json.loads((_DEFINITIONS_DIR / "components.index.json").read_text(encoding="utf-8"))
    assert index["renamed_components"] == ["api"]
    api_row = next(c for c in index["components"] if c["id"] == "api")
    assert "renamed_keys" not in api_row
