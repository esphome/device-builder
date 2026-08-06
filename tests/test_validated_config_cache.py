"""Tests for the validated-config cache awareness helper."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from esphome_device_builder.helpers.validated_config_cache import (
    JSON_CACHE_MEMBER_NAME,
    LEGACY_YAML_CACHE_MEMBER_NAME,
    find_validated_cache,
    json_cache_path,
    legacy_yaml_cache_path,
    member_name_for,
    parse_validated_cache,
    path_for_member,
    unlink_validated_cache,
)

_CONFIG = {"esphome": {"name": "lamp"}, "ota": [{"platform": "esphome"}]}


def _write_json_cache(envelope: object) -> Path:
    path = json_cache_path("lamp.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def _write_yaml_cache(body: str = "esphome:\n  name: lamp\n") -> Path:
    path = legacy_yaml_cache_path("lamp.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_paths_mirror_esphome_layout() -> None:
    assert json_cache_path("lamp.yaml").name == "lamp.yaml.validated.json"
    assert legacy_yaml_cache_path("lamp.yaml").name == "lamp.yaml.validated.yaml"
    assert json_cache_path("lamp.yaml").parent.name == "storage"


def test_find_returns_none_without_caches() -> None:
    assert find_validated_cache("lamp.yaml") is None


@pytest.mark.parametrize("newer", ["json", "yaml"])
def test_find_prefers_newer_mtime(newer: str) -> None:
    """An up/downgrade's lingering sibling never shadows current compiles."""
    json_path = _write_json_cache({"v": 1, "config": _CONFIG})
    yaml_path = _write_yaml_cache()
    old = time.time() - 3600
    stale = yaml_path if newer == "json" else json_path
    os.utime(stale, (old, old))

    expected = json_path if newer == "json" else yaml_path
    assert find_validated_cache("lamp.yaml") == expected


def test_parse_json_envelope() -> None:
    path = _write_json_cache({"v": 1, "esphome": "2026.8.0", "config": _CONFIG})
    assert parse_validated_cache(path) == _CONFIG


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param({"v": 2, "config": {}}, id="future_version"),
        pytest.param({"config": {}}, id="missing_version"),
        pytest.param({"v": 1, "config": []}, id="non_dict_config"),
        pytest.param({"v": 1}, id="missing_config"),
        pytest.param(["not", "a", "dict"], id="non_dict_envelope"),
    ],
)
def test_parse_json_rejects_foreign_shapes(envelope: object) -> None:
    path = _write_json_cache(envelope)
    assert parse_validated_cache(path) is None


def test_parse_json_rejects_invalid_json() -> None:
    path = json_cache_path("lamp.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"v": 1, "config":', encoding="utf-8")
    assert parse_validated_cache(path) is None


def test_parse_legacy_yaml() -> None:
    path = _write_yaml_cache("ota:\n- platform: esphome\n")
    assert parse_validated_cache(path) == {"ota": [{"platform": "esphome"}]}


def test_parse_legacy_yaml_rejects_invalid() -> None:
    path = _write_yaml_cache("ota: [platform: esphome\nbroken: true")
    assert parse_validated_cache(path) is None


def test_unlink_removes_both_formats() -> None:
    json_path = _write_json_cache({"v": 1, "config": _CONFIG})
    yaml_path = _write_yaml_cache()

    unlink_validated_cache("lamp.yaml")

    assert not json_path.exists()
    assert not yaml_path.exists()


def test_member_name_round_trip() -> None:
    json_path = json_cache_path("lamp.yaml")
    yaml_path = legacy_yaml_cache_path("lamp.yaml")
    assert member_name_for(json_path) == JSON_CACHE_MEMBER_NAME
    assert member_name_for(yaml_path) == LEGACY_YAML_CACHE_MEMBER_NAME
    assert path_for_member(JSON_CACHE_MEMBER_NAME, "lamp.yaml") == json_path
    assert path_for_member(LEGACY_YAML_CACHE_MEMBER_NAME, "lamp.yaml") == yaml_path
    with pytest.raises(ValueError, match="unknown validated-cache member"):
        path_for_member("validated.toml", "lamp.yaml")


def test_find_logs_unreadable_cache(caplog: pytest.LogCaptureFixture) -> None:
    """A stat failure that isn't absence is debug-logged and skipped."""
    storage_parent = json_cache_path("lamp.yaml").parent
    storage_parent.parent.mkdir(parents=True, exist_ok=True)
    # A file where the storage dir should be: stat under it raises
    # NotADirectoryError, an OSError that isn't FileNotFoundError.
    storage_parent.write_text("not a directory", encoding="utf-8")

    with caplog.at_level(
        logging.DEBUG, logger="esphome_device_builder.helpers.validated_config_cache"
    ):
        assert find_validated_cache("lamp.yaml") is None

    assert "unreadable" in caplog.text
