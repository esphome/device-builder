"""``references_class`` / ``id_classes``: a reference's picker respects the id class it needs."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _prune_automation_reference_classes,
    _resolve_reference_classes,
    _restrictive_references,
    _variant_id_classes,
)

_DEFINITIONS = Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions"
_BODIES_DIR = _DEFINITIONS / "components"


def _body(component_id: str) -> dict:
    return json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))


def _reference(key: str, domain: str, cls: str) -> dict:
    return {"key": key, "references_component": domain, "references_class": cls}


def _declarer(component_id: str, classes: list[str], **extra: object) -> dict:
    return {"id": component_id, "config_entries": [], "_root_id_classes": set(classes), **extra}


_resolve = _resolve_reference_classes


def test_reference_a_candidate_fails_keeps_its_class_and_marks_the_candidate() -> None:
    """A binary-only output gets ``id_classes``; the float-only reference keeps its class."""
    gpio = _declarer("output.gpio", ["output::BinaryOutput", "gpio::GPIOBinaryOutput"])
    ledc = _declarer("output.ledc", ["output::FloatOutput", "output::BinaryOutput"])
    light = {
        "id": "light.monochromatic",
        "config_entries": [_reference("output", "output", "output::FloatOutput")],
    }
    _resolve([gpio, ledc, light])
    assert light["config_entries"][0]["references_class"] == "output::FloatOutput"
    assert gpio["id_classes"] == ["gpio::GPIOBinaryOutput", "output::BinaryOutput"]
    assert "id_classes" not in ledc


def test_reference_every_candidate_satisfies_loses_its_class() -> None:
    """Nothing to filter, so neither side is annotated."""
    bus = _declarer("i2c", ["i2c::I2CBus"])
    sensor = {
        "id": "sensor.bme280",
        "config_entries": [_reference("i2c_id", "i2c", "i2c::I2CBus")],
    }
    _resolve([bus, sensor])
    assert "references_class" not in sensor["config_entries"][0]
    assert "id_classes" not in bus


def test_typed_hub_is_judged_per_variant_and_constrains_the_dependent() -> None:
    """The modbus shape: only ``role: server`` provides the class, and it is not the default."""
    hub = _declarer(
        "modbus",
        ["modbus::ModbusClientHub", "modbus::ModbusServerHub", "modbus::Modbus"],
        _variant_id_classes=(
            "role",
            {
                "client": ["modbus::ModbusClientHub", "modbus::Modbus"],
                "server": ["modbus::ModbusServerHub", "modbus::Modbus"],
            },
        ),
    )
    hub["config_entries"] = [{"key": "role", "default_value": "client"}]
    cover = {
        "id": "hoermann_hcp",
        "config_entries": [_reference("modbus_id", "modbus", "modbus::ModbusServerHub")],
    }
    client = {
        "id": "modbus_controller",
        "config_entries": [_reference("modbus_id", "modbus", "modbus::ModbusClientHub")],
    }
    _resolve([hub, cover, client])
    assert hub["id_classes_by_variant"] == {
        "role": {
            "client": ["modbus::Modbus", "modbus::ModbusClientHub"],
            "server": ["modbus::Modbus", "modbus::ModbusServerHub"],
        }
    }
    # An unset ``role`` declares the default variant's classes.
    assert hub["id_classes"] == ["modbus::Modbus", "modbus::ModbusClientHub"]
    assert cover["bus_constraints"] == {"modbus": {"role": "server"}}
    # The default variant already satisfies it: nothing to seed.
    assert "bus_constraints" not in client


def test_platform_whose_own_domain_ids_are_nested_is_not_a_candidate() -> None:
    """The ``dht`` shape: the picker descends to the nested ids and skips the root."""
    dht = _declarer(
        "sensor.dht", ["dht::DHT"], provides_id_paths={"sensor": [["temperature", "id"]]}
    )
    adc = _declarer("sensor.adc", ["sensor::Sensor"])
    consumer = {
        "id": "climate.thermostat",
        "config_entries": [_reference("sensor", "sensor", "sensor::Sensor")],
    }
    _resolve([dht, adc, consumer])
    assert "id_classes" not in dht
    assert "references_class" not in consumer["config_entries"][0]


def test_hybrid_platform_with_a_root_path_stays_a_candidate() -> None:
    """A root path in ``provides_id_paths`` means the root id is offered, so it is judged."""
    hub = _declarer(
        "sensor.pulse_meter",
        ["pulse_meter::Hub"],
        provides_id_paths={"sensor": [["id"], ["total", "id"]]},
    )
    adc = _declarer("sensor.adc", ["sensor::Sensor"])
    consumer = {
        "id": "climate.thermostat",
        "config_entries": [_reference("sensor", "sensor", "sensor::Sensor")],
    }
    _resolve([hub, adc, consumer])
    assert hub["id_classes"] == ["pulse_meter::Hub"]


def test_variant_id_classes_reads_each_typed_branch() -> None:
    """Unqualified parents (``Component``) are dropped; a non-typed schema yields None."""
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "type": "typed",
                "typed_key": "type",
                "types": {
                    "single": {
                        "config_vars": {
                            "id": {
                                "id_type": {"class": "spi::SPIComponent", "parents": ["Component"]}
                            }
                        }
                    },
                    "quad": {
                        "config_vars": {
                            "id": {
                                "id_type": {
                                    "class": "spi::QuadSPIComponent",
                                    "parents": ["Component", "spi::SPIBase"],
                                }
                            }
                        }
                    },
                },
            }
        }
    }
    assert _variant_id_classes(section) == (
        "type",
        {"single": ["spi::SPIComponent"], "quad": ["spi::QuadSPIComponent", "spi::SPIBase"]},
    )
    assert _variant_id_classes({"schemas": {"CONFIG_SCHEMA": {"schema": {}}}}) is None


def test_reference_no_declarer_can_satisfy_stays_unfiltered() -> None:
    """The ``i2c`` shape: the bundle lost the real class, so nothing may be filtered."""
    bus = _declarer("i2c", ["i2c::I2CBus"])
    camera = {
        "id": "esp32_camera",
        "config_entries": [_reference("i2c_id", "i2c", "i2c::InternalI2CBus")],
    }
    _resolve([bus, camera])
    assert "references_class" not in camera["config_entries"][0]
    assert "id_classes" not in bus


def test_automation_references_use_the_components_restrictive_set() -> None:
    """An already default-stripped action entry loses the key outright, never gains a null."""
    components = [
        _declarer("output.gpio", ["output::BinaryOutput"]),
        _declarer("output.ledc", ["output::FloatOutput", "output::BinaryOutput"]),
        {
            "id": "light.monochromatic",
            "config_entries": [_reference("output", "output", "output::FloatOutput")],
        },
    ]
    _resolve(components)
    restrictive = _restrictive_references(components)
    assert restrictive == {("output", "output::FloatOutput")}
    automations = {
        "actions": [
            {
                "id": "output.set_level",
                "config_entries": [
                    _reference("id", "output", "output::FloatOutput"),
                    _reference("other", "i2c", "i2c::I2CBus"),
                ],
            }
        ],
    }
    _prune_automation_reference_classes(automations, restrictive)
    kept, dropped = automations["actions"][0]["config_entries"]
    assert kept["references_class"] == "output::FloatOutput"
    assert "references_class" not in dropped


# ---------------------------------------------------------------------------
# Shipped catalog
# ---------------------------------------------------------------------------


def test_shipped_float_output_reference_and_binary_only_declarer() -> None:
    light = next(e for e in _body("light.monochromatic")["config_entries"] if e["key"] == "output")
    assert light["references_class"] == "output::FloatOutput"
    assert "output::FloatOutput" not in _body("output.gpio")["id_classes"]


def test_shipped_modbus_variants_and_the_server_dependents() -> None:
    assert _body("modbus")["id_classes_by_variant"]["role"].keys() == {"client", "server"}
    for component_id in ("hoermann_hcp", "modbus_server"):
        assert _body(component_id)["bus_constraints"]["modbus"] == {"role": "server"}
    assert _body("display.qspi_dbi")["bus_constraints"]["spi"]["type"] == "quad"


def test_shipped_i2c_bus_is_never_filtered() -> None:
    """``esp32_camera`` needs an ``InternalI2CBus`` the bundle records as ``I2CBus``."""
    assert "id_classes" not in _body("i2c")
    camera = next(e for e in _body("esp32_camera")["config_entries"] if e["key"] == "i2c_id")
    assert "references_class" not in camera


def test_shipped_index_carries_the_declarer_classes() -> None:
    """The frontend reads them off the slim index, without hydrating a body."""
    index = json.loads((_DEFINITIONS / "components.index.json").read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in index["components"]}
    assert by_id["output.gpio"]["id_classes"] == _body("output.gpio")["id_classes"]
    assert by_id["modbus"]["id_classes_by_variant"] == _body("modbus")["id_classes_by_variant"]
