"""Catalog platform restrictions derived from ESPHome framework metadata."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from esphome import config_validation as cv
from esphome import const
from esphome.const import Framework

from script import sync_components as sync


def _section() -> dict:
    return {
        "schemas": {
            "CONFIG_SCHEMA": {
                "type": "schema",
                "schema": {"config_vars": {"name": {"key": "Optional", "type": "string"}}},
            }
        }
    }


def _component(tmp_path: Path, top_key: str, dependencies: list[str]) -> dict:
    result = sync.build_component_entry(
        top_key,
        _section(),
        sync.SchemaIndex({top_key: {"dependencies": dependencies}}),
        tmp_path,
        {},
    )
    assert result is not None
    return result


def test_framework_dependency_is_implicit_and_limits_platforms(tmp_path: Path) -> None:
    # The live platform auto-loads Zephyr. The output's schema has no framework validator.
    component = _component(tmp_path, "zephyr_pwm.output", ["zephyr"])
    assert component["dependencies"] == []
    assert component["supported_platforms"] == ["nrf52"]


def test_root_framework_validator_limits_platforms(tmp_path: Path) -> None:
    component = _component(tmp_path, "zephyr_ble_server", [])
    assert component["supported_platforms"] == ["nrf52"]


def test_configurable_dependencies_are_kept(tmp_path: Path) -> None:
    component = _component(tmp_path, "zephyr_mcumgr.ota", ["zephyr", "zephyr_ble_server"])
    assert component["dependencies"] == ["zephyr_ble_server"]
    assert component["supported_platforms"] == ["nrf52"]


@pytest.mark.parametrize(
    ("gates", "dependencies"),
    [
        (("zephyr",), ["esp32"]),
        (("zephyr", "arduino"), []),
    ],
    ids=["framework-and-chip-disagree", "frameworks-disagree"],
)
def test_conflicting_framework_constraints_abort_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gates: tuple[str, ...], dependencies: list[str]
) -> None:
    schema = cv.All(*(cv.only_with_framework(framework) for framework in gates))
    monkeypatch.setattr(
        sync,
        "_enumerate_platform_manifests_by_domain",
        lambda *_: [("sensor", SimpleNamespace(config_schema=schema))],
    )
    monkeypatch.setattr(
        sync,
        "load_index",
        lambda _: sync.SchemaIndex({"sensor.template": {"dependencies": dependencies}}),
    )
    monkeypatch.setattr(
        sync, "load_image_map", lambda: dict.fromkeys(sync._IMAGE_MAP_SENTINELS, "image.svg")
    )
    monkeypatch.setattr(
        sync,
        "_fold_component_aliases",
        lambda *a, **kw: pytest.fail("Catalog generation continued"),
    )
    (tmp_path / "template.json").write_text(
        json.dumps({"template.sensor": _section()}), encoding="utf-8"
    )

    with pytest.raises(SystemExit, match=r"sensor\.template: .*constraints are disjoint"):
        sync.build_catalog(schema_dir=tmp_path, limit={"sensor.template"})


@pytest.mark.parametrize("drift", ["renamed-freevar", "empty-cell"])
def test_unreadable_framework_validator_aborts_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    gate = cv.only_with_framework(Framework.ZEPHYR)
    freevars = gate.__code__.co_freevars
    if drift == "renamed-freevar":
        gate.__code__ = gate.__code__.replace(
            co_freevars=tuple(
                "allowed_frameworks" if name == "frameworks" else name for name in freevars
            )
        )
    else:
        assert gate.__closure__ is not None
        del gate.__closure__[freevars.index("frameworks")].cell_contents
    schema = cv.All(cv.Schema({}), gate)
    enumerate_manifests = sync._enumerate_platform_manifests_by_domain
    monkeypatch.setattr(
        sync,
        "_enumerate_platform_manifests_by_domain",
        lambda loader, cid: (
            [("sensor", SimpleNamespace(config_schema=schema))]
            if cid == "template"
            else enumerate_manifests(loader, cid)
        ),
    )
    monkeypatch.setattr(sync, "load_index", lambda _: sync.SchemaIndex({}))
    monkeypatch.setattr(
        sync, "load_image_map", lambda: dict.fromkeys(sync._IMAGE_MAP_SENTINELS, "image.svg")
    )
    monkeypatch.setattr(
        sync,
        "_fold_component_aliases",
        lambda *a, **kw: pytest.fail("Catalog generation continued"),
    )
    (tmp_path / "template.json").write_text(
        json.dumps({"template.sensor": _section()}), encoding="utf-8"
    )

    with pytest.raises(
        SystemExit,
        match=r"sensor\.template: esphome\.config_validation\.only_with_framework.*frameworks",
    ):
        sync.build_catalog(schema_dir=tmp_path, limit={"sensor.template"})


@pytest.mark.parametrize(
    ("framework", "platforms", "expected"),
    [
        (Framework.ARDUINO, [], ["esp32", "esp8266", "rp2"]),
        (Framework.ESP_IDF, [], ["esp32"]),
        (Framework.ARDUINO, ["rp2", "esp32", "nrf52"], ["rp2", "esp32"]),
    ],
)
def test_projects_frameworks_onto_chip_platforms(
    monkeypatch: pytest.MonkeyPatch,
    framework: Framework,
    platforms: list[str],
    expected: list[str],
) -> None:
    pairs = (
        ("esp32", Framework.ARDUINO),
        ("esp32", Framework.ESP_IDF),
        ("esp8266", Framework.ARDUINO),
        ("rp2", Framework.ARDUINO),
        ("nrf52", Framework.ZEPHYR),
    )
    monkeypatch.setattr(sync, "_platform_framework_pairs", lambda: pairs)
    frameworks = sync._root_frameworks(
        cv.All(cv.only_with_framework(framework)), component_id="template"
    )
    assert sync._apply_framework_constraints(
        [], frameworks, platforms, component_id="template"
    ) == (set(), expected)


@pytest.mark.parametrize("location", ["root", "field"])
def test_framework_gates_stay_with_their_manifest_and_schema_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    gate = cv.only_with_framework(Framework.ZEPHYR)
    sensor_schema = (
        cv.All(cv.Schema({}), gate)
        if location == "root"
        else cv.Schema({cv.Optional("name"): cv.All(cv.string, gate)})
    )
    monkeypatch.setattr(
        sync,
        "_enumerate_platform_manifests_by_domain",
        lambda *_: [
            ("sensor", SimpleNamespace(config_schema=sensor_schema)),
            ("text_sensor", SimpleNamespace(config_schema=cv.Schema({}))),
        ],
    )
    assert _component(tmp_path, "template.sensor", [])["supported_platforms"] == (
        ["nrf52"] if location == "root" else []
    )
    assert _component(tmp_path, "template.text_sensor", [])["supported_platforms"] == []


def test_missing_loader_preserves_catalog_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sync, "_get_esphome_loader", lambda: None)
    component = _component(tmp_path, "zephyr_pwm.output", ["zephyr", "nrf52"])
    assert component["dependencies"] == ["zephyr", "nrf52"]
    assert component["supported_platforms"] == ["nrf52"]


def test_missing_platform_framework_import_warns_once_and_preserves_catalog_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    sync._platform_framework_pairs.cache_clear()
    try:
        with monkeypatch.context() as missing:
            missing.delattr(const, "PlatformFramework", raising=False)
            assert sync._platform_framework_pairs() == ()
        component = _component(tmp_path, "zephyr_pwm.output", ["zephyr", "nrf52"])
        assert component["dependencies"] == ["zephyr", "nrf52"]
        assert component["supported_platforms"] == ["nrf52"]
        assert sync._platform_framework_pairs() == ()
        assert sync._platform_framework_pairs.cache_info().misses == 1
        assert [
            level
            for logger, level, message in caplog.record_tuples
            if logger == "sync_components" and "esphome.const.PlatformFramework" in message
        ] == [logging.WARNING]
    finally:
        sync._platform_framework_pairs.cache_clear()
