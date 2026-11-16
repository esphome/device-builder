"""Check endpoint metadata against the installed core and its exported bundle."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import voluptuous as vol
from esphome.core import CORE

from script.sync_components import (
    _get_esphome_loader,
    _walk_schema_keys,
    build_entries_from_file,
    load_index,
)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("domain", ["sensor", "binary_sensor", "number", "switch"])
def test_exported_endpoint_matches_live_core(domain: str) -> None:
    """Generation preserves the endpoint's bounds, gates and platform capabilities."""
    schema_path = os.environ.get("ESPHOME_TEST_SCHEMA_DIR")
    if not schema_path:
        pytest.skip("Set ESPHOME_TEST_SCHEMA_DIR to a bundle exported by the installed core")
    schema_dir = Path(schema_path)
    entries = build_entries_from_file(
        schema_dir / "template.json", load_index(schema_dir), schema_dir, {}
    )
    component = next(entry for entry in entries if entry["id"] == f"{domain}.template")
    fields = {entry["key"]: entry for entry in component["config_entries"]}
    endpoint = fields["endpoint"]
    assert endpoint["type"] == "integer"
    assert endpoint["range"] == [1, 239]
    assert endpoint["advanced"] is True
    assert endpoint["depends_on_component"] == "zigbee"

    manifest = _get_esphome_loader().get_platform(domain, "template")
    validators = {}
    _walk_schema_keys(
        manifest.config_schema,
        lambda _key, _name, value, path: validators.update({path: value}),
    )
    validator = validators[("endpoint",)]
    previous_integrations = CORE.loaded_integrations
    try:
        accepted = []
        for platform in ["esp32", "nrf52", "esp8266", "rp2040", "host"]:
            CORE.loaded_integrations = {platform, "zigbee"}
            try:
                validator(1)
            except vol.Invalid:
                assert platform not in endpoint["supported_platforms"]
            else:
                accepted.append(platform)
                assert validator(239) == 239
                for value in [0, 240]:
                    with pytest.raises(vol.Invalid):
                        validator(value)
        assert endpoint["supported_platforms"] == sorted(accepted)
        if expected := os.environ.get("ESPHOME_TEST_ENDPOINT_PLATFORMS"):
            assert endpoint["supported_platforms"] == expected.split(",")
        assert "esp32" in accepted
        CORE.loaded_integrations = {"esp32"}
        with pytest.raises(vol.Invalid):
            validator(1)
    finally:
        CORE.loaded_integrations = previous_integrations

    for name in ["cluster", "report", "use_device_type"]:
        assert fields[name]["supported_platforms"] == ["esp32"]
        assert fields[name]["depends_on_component"] == "zigbee"


@pytest.mark.parametrize("domain", ["sensor", "binary_sensor", "number", "switch"])
def test_generated_endpoint_body(domain: str) -> None:
    """Serialized bodies retain the generated endpoint visibility metadata."""
    catalog_dir = os.environ.get("ESPHOME_TEST_CATALOG_DIR")
    expected = os.environ.get("ESPHOME_TEST_ENDPOINT_PLATFORMS")
    if not catalog_dir or not expected:
        pytest.skip("Set ESPHOME_TEST_CATALOG_DIR and ESPHOME_TEST_ENDPOINT_PLATFORMS")
    body = json.loads((Path(catalog_dir) / "components" / f"{domain}.template.json").read_text())
    fields = {entry["key"]: entry for entry in body["config_entries"]}
    endpoint = fields["endpoint"]
    assert endpoint["supported_platforms"] == expected.split(",")
    assert endpoint["type"] == "integer"
    assert endpoint["range"] == [1, 239]
    assert endpoint["advanced"] is True
    assert endpoint["depends_on_component"] == "zigbee"
    for name in ["cluster", "report", "use_device_type"]:
        assert fields[name]["supported_platforms"] == ["esp32"]
        assert fields[name]["depends_on_component"] == "zigbee"
