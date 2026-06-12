"""
Tests for ``cv.typed_schema`` (discriminated union) field conversion.

The schema bundle encodes a typed_schema field as ``{type: typed,
typed_key, types}``; without a handler the whole union collapses to a bare
string in the visual editor. These pin the nested-discriminator shape the
converter emits, plus the recovery for ``font.file`` whose node a custom
validator hides from the bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _FONT_FILE_NODE,
    _RAW_NODE_OVERRIDES,
    _convert_field,
)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty dir for ``_convert_field`` (only used for ``extends`` lookups)."""
    return tmp_path


def _children(entry: dict) -> dict[str, dict]:
    return {c["key"]: c for c in entry["config_entries"]}


def test_direct_typed_node_becomes_nested_discriminator(schema_dir: Path) -> None:
    """A ``type: typed`` field renders as a nested group with a discriminator select."""
    raw = {
        "key": "Required",
        "type": "typed",
        "typed_key": "type",
        "types": {
            "local": {"config_vars": {"path": {"key": "Required", "type": "string"}}},
            "web": {"config_vars": {"url": {"key": "Required", "type": "string"}}},
            "memory": {"config_vars": {"icon": {"key": "Required", "type": "string"}}},
        },
    }
    entry = _convert_field("source", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "nested"
    children = _children(entry)
    assert children["type"]["type"] == "string"
    assert children["type"]["required"] is True
    assert {o["value"] for o in children["type"]["options"]} == {"local", "web", "memory"}
    # Variant fields are gated by the discriminator.
    assert children["path"]["depends_on"] == "type"
    assert children["path"]["depends_on_value"] == "local"
    assert children["url"]["depends_on_value"] == "web"
    assert children["icon"]["depends_on_value"] == "memory"


def test_shared_field_uses_value_not_when_absent_from_one_type(schema_dir: Path) -> None:
    """A field in every variant but one gates with ``depends_on_value_not``."""
    raw = {
        "key": "Required",
        "type": "typed",
        "typed_key": "type",
        "types": {
            "local": {"config_vars": {"path": {"key": "Required"}}},
            "gfonts": {
                "config_vars": {
                    "family": {"key": "Required"},
                    "weight": {"key": "Optional"},
                }
            },
            "web": {
                "config_vars": {
                    "url": {"key": "Required"},
                    "weight": {"key": "Optional"},
                }
            },
        },
    }
    children = _children(_convert_field("file", raw, schema_dir))
    # ``weight`` lives in gfonts + web (all but ``local``) → single gated entry.
    assert children["weight"]["depends_on"] == "type"
    assert children["weight"]["depends_on_value_not"] == "local"
    assert children["weight"]["depends_on_value"] is None
    assert children["family"]["depends_on_value"] == "gfonts"
    assert children["url"]["depends_on_value"] == "web"


def test_field_in_every_type_is_ungated(schema_dir: Path) -> None:
    """A field present in all variants renders with no ``depends_on``."""
    raw = {
        "key": "Required",
        "type": "typed",
        "typed_key": "type",
        "types": {
            "a": {"config_vars": {"shared": {"key": "Optional"}}},
            "b": {"config_vars": {"shared": {"key": "Optional"}}},
        },
    }
    children = _children(_convert_field("f", raw, schema_dir))
    assert children["shared"]["depends_on"] is None


def test_typed_node_via_extends_ref(schema_dir: Path) -> None:
    """A field that ``extends`` a typed schema is detected through the ref."""
    (schema_dir / "img.json").write_text(
        json.dumps(
            {
                "img": {
                    "schemas": {
                        "TYPED_FILE_SCHEMA": {
                            "type": "typed",
                            "typed_key": "source",
                            "types": {
                                "local": {"config_vars": {"path": {"key": "Required"}}},
                                "web": {"config_vars": {"url": {"key": "Required"}}},
                            },
                        }
                    }
                }
            }
        )
    )
    raw = {"key": "Required", "type": "schema", "schema": {"extends": ["img.TYPED_FILE_SCHEMA"]}}
    entry = _convert_field("file", raw, schema_dir)
    assert entry["type"] == "nested"
    children = _children(entry)
    assert {o["value"] for o in children["source"]["options"]} == {"local", "web"}
    # Two-type union: a single-type field is "all but one", so it gates with
    # ``depends_on_value_not`` (path shows when source != web, i.e. local).
    assert children["path"]["depends_on"] == "source"
    assert children["path"]["depends_on_value_not"] == "web"


def test_self_referential_typed_node_is_depth_bounded(schema_dir: Path) -> None:
    """A typed node nesting itself falls back to string instead of recursing forever."""
    node: dict = {"key": "Required", "type": "typed", "typed_key": "type", "types": {}}
    node["types"] = {
        "leaf": {"config_vars": {"value": {"key": "Required"}}},
        "branch": {"config_vars": {"items": node}},
    }
    entry = _convert_field("items", node, schema_dir)
    assert entry["type"] == "nested"
    children = _children(entry)
    # The outer union expands; the nested ``items`` stays a bare string.
    assert children["items"]["type"] == "string"


# ---------------------------------------------------------------------------
# font.file: the published bundle can't carry the typed node, so a static
# skeleton supplies the local/gfonts/web shape and pulls the shared knobs
# back in from the bundle's surviving EXTERNAL_FONT_SCHEMA via ``extends``.
# ---------------------------------------------------------------------------


def _font_schema_dir(tmp_path: Path) -> Path:
    """Tmp schema dir carrying the bundle's surviving ``font.EXTERNAL_FONT_SCHEMA``."""
    (tmp_path / "font.json").write_text(
        json.dumps(
            {
                "font": {
                    "schemas": {
                        "EXTERNAL_FONT_SCHEMA": {
                            "type": "schema",
                            "schema": {
                                "config_vars": {
                                    "weight": {
                                        "key": "Optional",
                                        "type": "integer",
                                        "default": "regular",
                                    },
                                    "italic": {
                                        "key": "Optional",
                                        "type": "boolean",
                                        "default": "False",
                                    },
                                    "refresh": {
                                        "key": "Optional",
                                        "type": "string",
                                        "default": "1d",
                                    },
                                }
                            },
                        }
                    }
                }
            }
        )
    )
    return tmp_path


def test_font_file_override_is_registered() -> None:
    """``font.file`` is wired to its raw-node override."""
    assert _RAW_NODE_OVERRIDES[("font", "file")] is _FONT_FILE_NODE


def test_font_file_builds_local_gfonts_web_editor(schema_dir: Path) -> None:
    """The static skeleton renders a type local/gfonts/web editor."""
    entry = _convert_field("file", _FONT_FILE_NODE, _font_schema_dir(schema_dir))
    assert entry["type"] == "nested"
    children = _children(entry)

    assert {o["value"] for o in children["type"]["options"]} == {"local", "gfonts", "web"}
    assert children["type"]["required"] is True
    # One required field per source.
    assert children["path"]["depends_on_value"] == "local"
    assert children["family"]["depends_on_value"] == "gfonts"
    assert children["url"]["depends_on_value"] == "web"
    assert all(children[k]["required"] for k in ("path", "family", "url"))


def test_font_file_external_knobs_come_from_the_bundle(schema_dir: Path) -> None:
    """weight/italic/refresh resolve from EXTERNAL_FONT_SCHEMA and gate to gfonts+web."""
    children = _children(_convert_field("file", _FONT_FILE_NODE, _font_schema_dir(schema_dir)))

    for key in ("weight", "italic", "refresh"):
        assert children[key]["depends_on"] == "type"
        assert children[key]["depends_on_value_not"] == "local"

    assert children["italic"]["type"] == "boolean"
    assert children["italic"]["default_value"] is False
    assert children["refresh"]["default_value"] == "1d"
    # weight is the bundle's integer (a gfonts weight like the issue's 300).
    assert children["weight"]["type"] == "integer"
    assert children["weight"]["default_value"] == "regular"


def test_field_in_all_variants_collapses_only_when_identical(schema_dir: Path) -> None:
    """A field shared by every variant is ungated only when its definition matches."""
    raw = {
        "key": "Required",
        "type": "typed",
        "typed_key": "type",
        "types": {
            "a": {"config_vars": {"shared": {"key": "Optional", "type": "string"}}},
            "b": {"config_vars": {"shared": {"key": "Optional", "type": "string"}}},
        },
    }
    shared = [
        c for c in _convert_field("f", raw, schema_dir)["config_entries"] if c["key"] == "shared"
    ]
    assert len(shared) == 1
    assert shared[0]["depends_on"] is None


def test_differing_field_across_variants_stays_per_variant(schema_dir: Path) -> None:
    """A field present in every variant but defined differently gates per variant."""
    raw = {
        "key": "Required",
        "type": "typed",
        "typed_key": "type",
        "types": {
            # ``path`` is required here but optional in ``git`` — the
            # external_components.source case Copilot flagged.
            "local": {"config_vars": {"path": {"key": "Required"}}},
            "git": {"config_vars": {"path": {"key": "Optional"}}},
        },
    }
    paths = {
        c["depends_on_value"]: c
        for c in _convert_field("source", raw, schema_dir)["config_entries"]
        if c["key"] == "path"
    }
    assert set(paths) == {"local", "git"}
    assert paths["local"]["required"] is True
    assert paths["git"]["required"] is False
