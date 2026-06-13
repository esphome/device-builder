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
from types import SimpleNamespace

import esphome.config_validation as cv
import pytest

import script.sync_components as sc  # type: ignore[import-not-found]
from script.sync_components import (
    _FONT_FILE_NODE,
    _RAW_NODE_OVERRIDES,
    _apply_typed_defaults,
    _collect_typed_defaults,
    _convert_field,
    _extract_config_entries,
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
    assert children["path"]["depends_on_value_any"] == ["local"]
    assert children["url"]["depends_on_value_any"] == ["web"]
    assert children["icon"]["depends_on_value_any"] == ["memory"]


def test_subset_shared_field_gates_on_the_subset(schema_dir: Path) -> None:
    """A field shared by a subset of variants emits once, gated on that subset."""
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
    # ``weight`` lives in gfonts + web → single entry gated on both.
    assert children["weight"]["depends_on"] == "type"
    assert children["weight"]["depends_on_value_any"] == ["gfonts", "web"]
    assert children["weight"]["depends_on_value"] is None
    assert children["family"]["depends_on_value_any"] == ["gfonts"]
    assert children["url"]["depends_on_value_any"] == ["web"]


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
    assert children["path"]["depends_on"] == "source"
    assert children["path"]["depends_on_value_any"] == ["local"]


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
    assert children["path"]["depends_on_value_any"] == ["local"]
    assert children["family"]["depends_on_value_any"] == ["gfonts"]
    assert children["url"]["depends_on_value_any"] == ["web"]
    assert all(children[k]["required"] for k in ("path", "family", "url"))


def test_font_file_external_knobs_come_from_the_bundle(schema_dir: Path) -> None:
    """weight/italic/refresh resolve from EXTERNAL_FONT_SCHEMA and gate to gfonts+web."""
    children = _children(_convert_field("file", _FONT_FILE_NODE, _font_schema_dir(schema_dir)))

    for key in ("weight", "italic", "refresh"):
        assert children[key]["depends_on"] == "type"
        assert children[key]["depends_on_value_any"] == ["gfonts", "web"]

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
        c["depends_on_value_any"][0]: c
        for c in _convert_field("source", raw, schema_dir)["config_entries"]
        if c["key"] == "path"
    }
    assert set(paths) == {"local", "git"}
    assert paths["local"]["required"] is True
    assert paths["git"]["required"] is False


# ---------------------------------------------------------------------------
# Top-level typed CONFIG_SCHEMA (ethernet, spi, template.output, ...): the
# whole component body is the discriminated union, not a nested field.
# ---------------------------------------------------------------------------


def _typed_section(types: dict, typed_key: str = "type") -> dict:
    node = {"type": "typed", "typed_key": typed_key, "types": types}
    return {"schemas": {"CONFIG_SCHEMA": node}}


def _write_schemas(schema_dir: Path, name: str, schemas: dict) -> None:
    (schema_dir / f"{name}.json").write_text(json.dumps({name: {"schemas": schemas}}))


def test_top_level_typed_config_schema_builds_entries(schema_dir: Path) -> None:
    """A typed CONFIG_SCHEMA yields a discriminator-first gated entry list."""
    section = _typed_section(
        {
            "binary": {"config_vars": {"write_action": {"key": "Required"}}},
            "float": {"config_vars": {"zero_means_zero": {"key": "Optional"}}},
        }
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="template")
    by_key = {e["key"]: e for e in entries}
    assert entries[0]["key"] == "type"
    assert entries[0]["required"] is True
    assert {o["value"] for o in entries[0]["options"]} == {"binary", "float"}
    assert by_key["write_action"]["depends_on_value_any"] == ["binary"]
    assert by_key["zero_means_zero"]["depends_on_value_any"] == ["float"]


def test_top_level_typed_with_intermediate_extends(schema_dir: Path) -> None:
    """Variants sharing intermediate base schemas dedupe to one subset-gated entry."""
    _write_schemas(
        schema_dir,
        "eth",
        {
            "BASE_SCHEMA": {
                "type": "schema",
                "schema": {"config_vars": {"domain": {"key": "Optional"}}},
            },
            "RMII_SCHEMA": {
                "type": "schema",
                "schema": {
                    "extends": ["eth.BASE_SCHEMA"],
                    "config_vars": {"mdc_pin": {"key": "Required", "type": "pin"}},
                },
            },
            "SPI_SCHEMA": {
                "type": "schema",
                "schema": {
                    "extends": ["eth.BASE_SCHEMA"],
                    "config_vars": {"cs_pin": {"key": "Required", "type": "pin"}},
                },
            },
        },
    )
    section = _typed_section(
        {
            "LAN8720": {"extends": ["eth.RMII_SCHEMA"]},
            "RTL8201": {"extends": ["eth.RMII_SCHEMA"]},
            "W5500": {"extends": ["eth.SPI_SCHEMA"]},
        }
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="eth")
    (mdc,) = [e for e in entries if e["key"] == "mdc_pin"]
    assert mdc["depends_on_value_any"] == ["LAN8720", "RTL8201"]
    (cs,) = [e for e in entries if e["key"] == "cs_pin"]
    assert cs["depends_on_value_any"] == ["W5500"]
    (domain,) = [e for e in entries if e["key"] == "domain"]
    assert domain["depends_on"] is None
    assert len(entries) == 4


def test_top_level_typed_with_custom_discriminator_key(schema_dir: Path) -> None:
    """A non-``type`` discriminator (i2s_audio's ``dac_type``) still sorts first."""
    _write_schemas(
        schema_dir,
        "i2s",
        {
            "BASE_SCHEMA": {
                "type": "schema",
                "schema": {"config_vars": {"channel": {"key": "Optional"}}},
            }
        },
    )
    section = _typed_section(
        {
            "external": {
                "extends": ["i2s.BASE_SCHEMA"],
                "config_vars": {"i2s_dout_pin": {"key": "Required", "type": "pin"}},
            },
            "internal": {
                "extends": ["i2s.BASE_SCHEMA"],
                "config_vars": {"mode": {"key": "Optional"}},
            },
        },
        typed_key="dac_type",
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="i2s.speaker")
    assert entries[0]["key"] == "dac_type"
    by_key = {e["key"]: e for e in entries}
    assert by_key["i2s_dout_pin"]["depends_on"] == "dac_type"
    assert by_key["i2s_dout_pin"]["depends_on_value_any"] == ["external"]
    assert by_key["mode"]["depends_on_value_any"] == ["internal"]
    assert by_key["channel"]["depends_on"] is None


def test_top_level_typed_uptime_sensor_shape(schema_dir: Path) -> None:
    """Variants sharing an entity base ungate it; per-variant extras gate singly."""
    _write_schemas(
        schema_dir,
        "sensor",
        {
            "_SENSOR_SCHEMA": {
                "type": "schema",
                "schema": {
                    "config_vars": {
                        "icon": {"key": "Optional", "type": "icon"},
                        "accuracy_decimals": {"key": "Optional", "type": "integer"},
                    }
                },
            }
        },
    )
    section = _typed_section(
        {
            "seconds": {
                "extends": ["sensor._SENSOR_SCHEMA"],
                "config_vars": {"update_interval": {"key": "Optional"}},
            },
            "timestamp": {
                "extends": ["sensor._SENSOR_SCHEMA"],
                "config_vars": {"time_id": {"key": "Optional"}},
            },
        }
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="sensor.uptime")
    by_key = {e["key"]: e for e in entries}
    assert by_key["icon"]["depends_on"] is None
    assert by_key["accuracy_decimals"]["depends_on"] is None
    assert by_key["update_interval"]["depends_on_value_any"] == ["seconds"]
    assert by_key["time_id"]["depends_on_value_any"] == ["timestamp"]


def test_variant_config_var_matching_typed_key_is_skipped(schema_dir: Path) -> None:
    """A variant's own ``type`` var (template.datetime) never duplicates the discriminator."""
    section = _typed_section(
        {
            "DATE": {
                "config_vars": {
                    "type": {"key": "Required"},
                    "initial_value": {"key": "Optional"},
                }
            },
            "TIME": {
                "config_vars": {
                    "type": {"key": "Required"},
                    "initial_value": {"key": "Optional"},
                }
            },
        }
    )
    entries = _extract_config_entries(
        section, schema_dir=schema_dir, component_id="datetime.template"
    )
    (type_entry,) = [e for e in entries if e["key"] == "type"]
    assert {o["value"] for o in type_entry["options"]} == {"DATE", "TIME"}
    (initial,) = [e for e in entries if e["key"] == "initial_value"]
    assert initial["depends_on"] is None


def test_ethernet_clk_override_required_on_main_form(schema_dir: Path) -> None:
    """``ethernet.clk`` flips to required + main-form; ``clk_mode`` stays advanced."""
    _write_schemas(
        schema_dir,
        "eth",
        {
            "RMII_SCHEMA": {
                "type": "schema",
                "schema": {
                    "config_vars": {
                        "clk": {
                            "key": "Optional",
                            "type": "schema",
                            "schema": {
                                "config_vars": {
                                    "mode": {
                                        "key": "Required",
                                        "type": "enum",
                                        "values": {"CLK_EXT_IN": None},
                                    },
                                    "pin": {"key": "Required", "type": "pin"},
                                }
                            },
                        },
                        "clk_mode": {
                            "key": "Optional",
                            "type": "enum",
                            "values": {"GPIO0_IN": None},
                        },
                    }
                },
            }
        },
    )
    section = _typed_section(
        {
            "LAN8720": {"extends": ["eth.RMII_SCHEMA"]},
            "W5500": {"config_vars": {"cs_pin": {"key": "Required", "type": "pin"}}},
        }
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="ethernet")
    (clk,) = [e for e in entries if e["key"] == "clk"]
    assert clk["required"] is True
    assert clk["advanced"] is False
    assert clk["depends_on_value_any"] == ["LAN8720"]
    # Cascade no longer buries the group's required children.
    assert all(c["advanced"] is False for c in clk["config_entries"])
    (clk_mode,) = [e for e in entries if e["key"] == "clk_mode"]
    assert clk_mode["advanced"] is True


def test_top_level_typed_applies_component_overrides(
    schema_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deprecated-field filtering and field overrides key on the component id."""
    monkeypatch.setattr(sc, "_DEPRECATED_FIELDS", sc._DEPRECATED_FIELDS | {("widget", "legacy")})
    monkeypatch.setitem(sc._FIELD_OVERRIDES, ("widget", "mode"), {"advanced": True})
    section = _typed_section(
        {
            "a": {
                "config_vars": {
                    "legacy": {"key": "Optional"},
                    "mode": {"key": "Optional"},
                }
            },
            "b": {"config_vars": {"mode": {"key": "Optional"}}},
        }
    )
    entries = _extract_config_entries(section, schema_dir=schema_dir, component_id="widget")
    keys = [e["key"] for e in entries]
    assert "legacy" not in keys
    (mode,) = [e for e in entries if e["key"] == "mode"]
    assert mode["advanced"] is True


# ---------------------------------------------------------------------------
# default_type recovery: the bundle drops a typed_schema's ``default_type``,
# so the discriminator always emits ``Required`` with no default. Live
# introspection reads it back from the validator's closure.
# ---------------------------------------------------------------------------


def _manifest(config_schema: object) -> SimpleNamespace:
    return SimpleNamespace(config_schema=config_schema)


def test_collect_typed_defaults_reads_default_type_through_ensure_list() -> None:
    """``cv.ensure_list(cv.typed_schema(..., default_type=...))`` (the SPI shape)."""
    schema = cv.ensure_list(
        cv.typed_schema(
            {"single": cv.Schema({}), "quad": cv.Schema({}), "octal": cv.Schema({})},
            default_type="single",
        )
    )
    assert _collect_typed_defaults(_manifest(schema)) == {("type",): "single"}


def test_collect_typed_defaults_honours_custom_key() -> None:
    """The discriminator key follows ``cv.typed_schema``'s ``key=`` kwarg."""
    schema = cv.typed_schema({"a": cv.Schema({}), "b": cv.Schema({})}, default_type="a", key="mode")
    assert _collect_typed_defaults(_manifest(schema)) == {("mode",): "a"}


def test_collect_typed_defaults_empty_without_default_type() -> None:
    """A typed_schema with no ``default_type`` yields nothing (stays Required)."""
    schema = cv.typed_schema({"a": cv.Schema({}), "b": cv.Schema({})})
    assert _collect_typed_defaults(_manifest(schema)) == {}


def test_collect_typed_defaults_empty_for_non_typed_schema() -> None:
    """A plain schema has no typed discriminator."""
    schema = cv.Schema({cv.Optional("x"): cv.string})
    assert _collect_typed_defaults(_manifest(schema)) == {}
    assert _collect_typed_defaults(_manifest(None)) == {}


def _disc(value: str, *options: str) -> dict:
    return {
        "key": "type",
        "type": "string",
        "required": True,
        "default_value": None,
        "options": [{"label": o, "value": o} for o in options],
    }


def test_apply_typed_defaults_marks_discriminator_optional() -> None:
    """The discriminator entry gains the default and drops ``required``."""
    entries = [
        _disc("type", "single", "quad", "octal"),
        {"key": "clk_pin", "type": "pin", "required": True, "default_value": None},
    ]
    _apply_typed_defaults(entries, {("type",): "single"})
    by_key = {e["key"]: e for e in entries}
    assert by_key["type"]["required"] is False
    assert by_key["type"]["default_value"] == "single"
    # Sibling fields are untouched.
    assert by_key["clk_pin"]["required"] is True
    assert by_key["clk_pin"]["default_value"] is None


def test_apply_typed_defaults_skips_default_not_in_options() -> None:
    """A default absent from this discriminator's options is dropped (esp32_hosted bleed)."""
    entries = [_disc("type", "embedded", "http")]
    _apply_typed_defaults(entries, {("type",): "sdio"})
    assert entries[0]["required"] is True
    assert entries[0]["default_value"] is None


def test_apply_typed_defaults_noop_when_empty() -> None:
    """No introspected defaults leaves the entries exactly as built."""
    entries = [_disc("type", "single")]
    _apply_typed_defaults(entries, {})
    assert entries[0]["required"] is True
    assert entries[0]["default_value"] is None
