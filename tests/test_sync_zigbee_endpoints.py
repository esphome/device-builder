"""Targeted endpoint refresh must preserve the rest of the catalog."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import esphome.config_validation as cv
import pytest


@pytest.mark.parametrize("platforms", [["esp32"], ["esp32", "nrf52"]])
def test_endpoint_refresh_uses_schema_and_preserves_other_fields(platforms: list[str]) -> None:
    module = importlib.import_module("script.sync_zigbee_endpoints")
    endpoint = {
        "key": "endpoint",
        "type": "integer",
        "range": [1, 239],
        "advanced": True,
        "depends_on_component": "zigbee",
        "supported_platforms": ["esp32"],
    }
    body = {"config_entries": [endpoint, {"key": "report", "supported_platforms": ["esp32"]}]}
    expected = copy.deepcopy(body)
    expected["config_entries"][0]["supported_platforms"] = platforms
    schema = cv.Schema(
        {
            cv.Optional("endpoint"): cv.All(
                cv.requires_component("zigbee"),
                cv.Any(*(cv.requires_component(platform) for platform in platforms)),
                cv.int_range(1, 239),
            )
        }
    )
    module.refresh_body(body, SimpleNamespace(config_schema=schema))
    assert body == expected


def test_refresh_catalog_uses_live_manifest_and_is_idempotent(tmp_path: Path) -> None:
    from esphome.const import __version__  # noqa: PLC0415

    from script import sync_components, sync_zigbee_endpoints  # noqa: PLC0415

    source = sync_components._OUTPUT_BODIES_DIR / "sensor.template.json"
    bodies = tmp_path / "components"
    bodies.mkdir()
    target = bodies / source.name
    target.write_bytes(source.read_bytes())
    original = json.loads(target.read_bytes())
    expected = copy.deepcopy(original)
    loader = sync_components._get_esphome_loader()
    manifest = loader.get_platform("sensor", "template")
    sync_zigbee_endpoints.refresh_body(expected, manifest)
    sync_zigbee_endpoints.refresh_catalog(tmp_path, __version__)
    assert json.loads(target.read_bytes()) == expected
    first_bytes = target.read_bytes()
    assert sync_zigbee_endpoints.refresh_catalog(tmp_path, __version__) == 0
    assert target.read_bytes() == first_bytes


def test_nested_refresh_ignores_unrelated_endpoint() -> None:
    module = importlib.import_module("script.sync_zigbee_endpoints")
    gated = {"key": "endpoint", "depends_on_component": "zigbee", "supported_platforms": ["esp32"]}
    unrelated = {"key": "endpoint", "supported_platforms": ["esp32"]}
    body = {
        "config_entries": [
            unrelated,
            {"key": "child", "type": "nested", "config_entries": [gated]},
        ]
    }
    schema = cv.Schema(
        {cv.Optional("child"): {cv.Optional("endpoint"): cv.requires_component("nrf52")}}
    )
    module.refresh_body(body, SimpleNamespace(config_schema=schema))
    assert gated["supported_platforms"] == ["nrf52"]
    assert unrelated["supported_platforms"] == ["esp32"]


def test_missing_live_constraint_rejects_refresh() -> None:
    module = importlib.import_module("script.sync_zigbee_endpoints")
    body = {"config_entries": [{"key": "endpoint", "depends_on_component": "zigbee"}]}
    with pytest.raises(ValueError, match="No live platform constraint"):
        module.refresh_body(body, SimpleNamespace(config_schema=cv.Schema({})))


def test_version_mismatch_does_not_write(tmp_path: Path) -> None:
    module = importlib.import_module("script.sync_zigbee_endpoints")
    with pytest.raises(SystemExit, match="needs ESPHome"):
        module.refresh_catalog(tmp_path, "0.0.0")
    assert list(tmp_path.iterdir()) == []


def test_missing_manifest_does_not_partially_write(tmp_path: Path) -> None:
    from esphome.const import __version__  # noqa: PLC0415

    from script import sync_components, sync_zigbee_endpoints  # noqa: PLC0415

    bodies = tmp_path / "components"
    bodies.mkdir()
    body = json.loads((sync_components._OUTPUT_BODIES_DIR / "sensor.template.json").read_bytes())
    endpoint = next(entry for entry in body["config_entries"] if entry["key"] == "endpoint")
    endpoint["supported_platforms"] = []
    first = bodies / "sensor.template.json"
    first.write_text(json.dumps(body))
    before = first.read_bytes()
    body["id"] = "zz_missing_endpoint_fixture"
    (bodies / "zz_missing_endpoint_fixture.json").write_text(json.dumps(body))
    with pytest.raises(ValueError, match="No live platform constraint"):
        sync_zigbee_endpoints.refresh_catalog(tmp_path, __version__)
    assert first.read_bytes() == before


def test_empty_catalog_is_rejected(tmp_path: Path) -> None:
    from esphome.const import __version__  # noqa: PLC0415

    module = importlib.import_module("script.sync_zigbee_endpoints")
    with pytest.raises(ValueError, match="No component bodies"):
        module.refresh_catalog(tmp_path, __version__)
