"""Dotted-subdomain ``extends`` refs resolve to their bundle schema."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _lookup_schema_ref,
    _resolve_extends,
)

# A platform sub-namespace keyed whole (``speaker.media_player``) with its
# schema keyed bare (``PIPELINE_SCHEMA``); the plain ``speaker`` entry beside
# it guards against a loose match on the bare name.
_SPEAKER_JSON = {
    "speaker": {
        "schemas": {
            "SPEAKER_SCHEMA": {
                "schema": {"config_vars": {"channel": {"key": "Optional"}}},
            },
        },
    },
    "speaker.media_player": {
        "schemas": {
            "PIPELINE_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "speaker": {"key": "Required", "type": "use_id"},
                        "format": {"key": "Optional", "type": "enum"},
                    },
                },
            },
        },
    },
}


def _schema_dir(tmp_path: Path) -> Path:
    (tmp_path / "speaker.json").write_text(json.dumps(_SPEAKER_JSON), encoding="utf-8")
    return tmp_path


def test_three_segment_dotted_domain_ref_resolves(tmp_path: Path) -> None:
    """``speaker.media_player.PIPELINE_SCHEMA`` finds the bare-keyed schema."""
    body = _lookup_schema_ref("speaker.media_player.PIPELINE_SCHEMA", _schema_dir(tmp_path))
    assert body is not None
    assert set(body["schema"]["config_vars"]) == {"speaker", "format"}


def test_two_segment_ref_still_resolves(tmp_path: Path) -> None:
    """A plain ``<domain>.<schema>`` ref keeps resolving (no regression)."""
    body = _lookup_schema_ref("speaker.SPEAKER_SCHEMA", _schema_dir(tmp_path))
    assert body is not None
    assert set(body["schema"]["config_vars"]) == {"channel"}


def test_resolve_extends_flattens_pipeline_config_vars(tmp_path: Path) -> None:
    """The nested pipeline's required ``speaker`` ref reaches the caller."""
    cvs = _resolve_extends("speaker.media_player.PIPELINE_SCHEMA", _schema_dir(tmp_path))
    assert set(cvs) == {"speaker", "format"}


def test_unresolvable_ref_returns_none(tmp_path: Path) -> None:
    """A ref into a missing file is a clean miss, not a crash."""
    assert _lookup_schema_ref("nope.media_player.PIPELINE_SCHEMA", _schema_dir(tmp_path)) is None


def test_resolve_extends_merges_a_partial_override_over_a_grandparent_field(
    tmp_path: Path,
) -> None:
    """A two-level ancestry keeps the base field's type under a mid-level partial override."""
    schema = {
        "sensor": {
            "schemas": {
                "_SENSOR_SCHEMA": {
                    "schema": {
                        "config_vars": {
                            "device_class": {"key": "Optional", "type": "enum", "values": {}},
                            "name": {"key": "Optional"},
                        }
                    }
                }
            }
        },
        "ina3221.sensor": {
            "schemas": {
                "VOLTAGE_SCHEMA": {
                    "schema": {
                        "config_vars": {"device_class": {"default": "voltage"}},
                        "extends": ["sensor._SENSOR_SCHEMA"],
                    }
                }
            }
        },
    }
    (tmp_path / "sensor.json").write_text(json.dumps({"sensor": schema["sensor"]}))
    (tmp_path / "ina3221.json").write_text(json.dumps({"ina3221.sensor": schema["ina3221.sensor"]}))
    cvs = _resolve_extends("ina3221.sensor.VOLTAGE_SCHEMA", tmp_path)
    assert cvs["device_class"] == {
        "key": "Optional",
        "type": "enum",
        "values": {},
        "default": "voltage",
    }
    assert list(cvs) == ["device_class", "name"]
