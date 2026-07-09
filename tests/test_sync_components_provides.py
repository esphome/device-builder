"""Provider-interface derivation: ``provides`` from id class + parents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _collect_referenced_classes,
    _implemented_classes,
    _reference_namespace,
    _resolve_provides,
)

_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)
_INDEX_FILE = _BODIES_DIR.parent / "components.index.json"


def _load_body(component_id: str) -> dict:
    """Read one component's split body file off disk."""
    return json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# _reference_namespace
# ---------------------------------------------------------------------------


def test_reference_namespace_maps_qualified_class() -> None:
    """``ns::Class`` collapses to ``ns`` so both reference sides agree."""
    assert _reference_namespace("voltage_sampler::VoltageSampler") == "voltage_sampler"


def test_reference_namespace_applies_cpp_naming_override() -> None:
    """The ``switch_`` C++ namespace maps to the ``switch`` catalog domain."""
    assert _reference_namespace("switch_::Switch") == "switch"


def test_reference_namespace_none_for_unqualified() -> None:
    """A bare base class with no ``::`` is not a referenceable namespace."""
    assert _reference_namespace("PollingComponent") is None


# ---------------------------------------------------------------------------
# _implemented_classes
# ---------------------------------------------------------------------------


def test_implemented_classes_unions_class_parents_and_type_variants() -> None:
    """Own id class + transitive parents + discriminated ``types.*`` ids, all at ``["id"]``."""
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "id": {
                            "id_type": {
                                "class": "adc::ADCSensor",
                                "parents": ["sensor::Sensor", "voltage_sampler::VoltageSampler"],
                            }
                        }
                    }
                },
                "types": {
                    "adc": {
                        "config_vars": {"id": {"id_type": {"class": "x::Y", "parents": ["z::Z"]}}}
                    }
                },
            }
        }
    }
    assert _implemented_classes(section, Path("/unused")) == {
        "adc::ADCSensor": [["id"]],
        "sensor::Sensor": [["id"]],
        "voltage_sampler::VoltageSampler": [["id"]],
        "x::Y": [["id"]],
        "z::Z": [["id"]],
    }


def test_implemented_classes_descends_nested_list_id() -> None:
    """A uart-typed id nested in a list (``usb_uart.channels[].id``) resolves to its path.

    The ``types.*`` discriminator is flattened, so the channel id's path is
    ``["channels", "id"]`` while the variant's own device id stays at ``["id"]``.
    """
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "types": {
                    "CDC_ACM": {
                        "config_vars": {
                            "id": {
                                "id_type": {
                                    "class": "usb_uart::USBUartTypeCdcAcm",
                                    "parents": ["usb_uart::USBUartComponent", "Component"],
                                }
                            },
                            "channels": {
                                "schema": {
                                    "config_vars": {
                                        "id": {
                                            "id_type": {
                                                "class": "usb_uart::USBUartChannel",
                                                "parents": ["uart::UARTComponent"],
                                            }
                                        }
                                    }
                                },
                            },
                        }
                    }
                },
            }
        }
    }
    classes = _implemented_classes(section, Path("/unused"))
    # The channel implements uart via a parent class → advertised at its path.
    assert classes["uart::UARTComponent"] == [["channels", "id"]]
    # A nested non-entity leaf own-class is the sub-block's own identity, not recorded.
    assert "usb_uart::USBUartChannel" not in classes
    # The variant's top-level device id still records its own class.
    assert classes["usb_uart::USBUartComponent"] == [["id"]]


def test_implemented_classes_collects_every_path_for_a_class() -> None:
    """A class declared at several nested fields keeps all its paths."""
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "main_switch": {
                            "schema": {
                                "config_vars": {
                                    "id": {"id_type": {"class": "x::Sw", "parents": ["sw::Switch"]}}
                                }
                            }
                        },
                        "aux_switch": {
                            "schema": {
                                "config_vars": {
                                    "id": {"id_type": {"class": "x::Sw", "parents": ["sw::Switch"]}}
                                }
                            }
                        },
                    }
                }
            }
        }
    }
    assert sorted(_implemented_classes(section, Path("/unused"))["sw::Switch"]) == [
        ["aux_switch", "id"],
        ["main_switch", "id"],
    ]


def test_implemented_classes_skips_use_id_reference() -> None:
    """A cross-reference field (``use_id_type``) is never an implemented id."""
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "uart_id": {
                            "use_id_type": "uart::UARTComponent",
                            "id_type": {"class": "uart::UARTComponent"},
                        }
                    }
                }
            }
        }
    }
    assert _implemented_classes(section, Path("/unused")) == {}


def test_implemented_classes_empty_without_id_type() -> None:
    """No declared id type ⇒ implements nothing referenceable."""
    assert (
        _implemented_classes({"schemas": {"CONFIG_SCHEMA": {"schema": {}}}}, Path("/unused")) == {}
    )


def test_implemented_classes_records_nested_entity_leaf() -> None:
    """A nested entity-class id (a multi-entity sub-sensor) records its path."""
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "id": {"id_type": {"class": "aht10::AHT10Component", "parents": []}},
                        "temperature": {
                            "schema": {
                                "config_vars": {
                                    "id": {
                                        "id_type": {
                                            "class": "sensor::Sensor",
                                            "parents": ["EntityBase"],
                                        }
                                    }
                                }
                            }
                        },
                    }
                }
            }
        }
    }
    classes = _implemented_classes(section, Path("/unused"))
    assert classes["sensor::Sensor"] == [["temperature", "id"]]
    assert classes["aht10::AHT10Component"] == [["id"]]


def test_implemented_classes_resolves_inherited_id_through_extends(tmp_path: Path) -> None:
    """A sub-block whose ``id`` lives only on its ``extends`` base still counts."""
    (tmp_path / "sensor.json").write_text(
        json.dumps(
            {
                "sensor": {
                    "schemas": {
                        "_SENSOR_SCHEMA": {
                            "schema": {
                                "config_vars": {
                                    "id": {
                                        "id_type": {
                                            "class": "sensor::Sensor",
                                            "parents": ["EntityBase"],
                                        }
                                    },
                                    "mqtt_id": {
                                        "id_type": {
                                            "class": "mqtt::MQTTSensorComponent",
                                            "parents": [],
                                        }
                                    },
                                }
                            }
                        }
                    }
                }
            }
        )
    )
    section = {
        "schemas": {
            "CONFIG_SCHEMA": {
                "schema": {
                    "config_vars": {
                        "temperature": {"schema": {"extends": ["sensor._SENSOR_SCHEMA"]}},
                    }
                }
            }
        }
    }
    classes = _implemented_classes(section, tmp_path)
    assert classes["sensor::Sensor"] == [["temperature", "id"]]
    # Only the inherited ``id`` merges; sibling id declarations stay behind.
    assert "mqtt::MQTTSensorComponent" not in classes


# ---------------------------------------------------------------------------
# _resolve_provides
# ---------------------------------------------------------------------------


def test_resolve_provides_keeps_cross_domain_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """A referenced class whose namespace differs from the domain survives."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"voltage_sampler::VoltageSampler", "sensor::Sensor"},
    )
    entries = [
        {
            "id": "sensor.adc",
            "provides": [],
            "_impl_class_paths": {
                "adc::ADCSensor": [["id"]],
                "sensor::Sensor": [["id"]],
                "voltage_sampler::VoltageSampler": [["id"]],
            },
        },
        {
            "id": "sensor.dht",
            "provides": [],
            "_impl_class_paths": {"dht::DHT": [["id"]], "sensor::Sensor": [["id"]]},
        },
    ]
    _resolve_provides(entries, Path("/unused"))
    # adc is a voltage_sampler under the sensor domain → cross-domain, kept.
    assert entries[0]["provides"] == ["voltage_sampler"]
    # Own top-level id → resolved frontend-side via the section id, no path metadata.
    assert entries[0]["provides_id_paths"] == {}
    # dht only implements sensor::Sensor — its own domain, the top-level
    # scan already covers it, so nothing to advertise.
    assert entries[1]["provides"] == []
    # Scratch field is popped before emit.
    assert "_impl_class_paths" not in entries[0]
    assert "_impl_class_paths" not in entries[1]


def test_resolve_provides_records_nested_id_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider whose interface id is nested records the YAML path to descend."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"uart::UARTComponent"},
    )
    entries = [
        {
            "id": "usb_uart",
            "provides": [],
            "_impl_class_paths": {
                "uart::UARTComponent": [["channels", "id"]],
                "usb_uart::USBUartComponent": [["id"]],
            },
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == ["uart"]
    assert entries[0]["provides_id_paths"] == {"uart": [["channels", "id"]]}


def test_resolve_provides_unions_and_sorts_nested_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """All nested paths for an interface are deduped and sorted deterministically."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"switch_::Switch"},
    )
    entries = [
        {
            "id": "sprinkler",
            "provides": [],
            "_impl_class_paths": {
                "switch_::Switch": [
                    ["valves", "valve_switch", "id"],
                    ["auto_advance_switch", "id"],
                    ["auto_advance_switch", "id"],
                ],
            },
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == ["switch"]
    assert entries[0]["provides_id_paths"] == {
        "switch": [["auto_advance_switch", "id"], ["valves", "valve_switch", "id"]]
    }


def test_resolve_provides_omits_path_for_own_id_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """An own-id uart provider (ble_nus) advertises uart but no nested path."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"uart::UARTComponent"},
    )
    entries = [
        {
            "id": "ble_nus",
            "provides": [],
            "_impl_class_paths": {
                "ble_nus::BLENUS": [["id"]],
                "uart::UARTComponent": [["id"]],
            },
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == ["uart"]
    assert entries[0]["provides_id_paths"] == {}


def test_resolve_provides_ignores_unreferenced_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An implemented class that no ``use_id`` targets is never advertised."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: set(),
    )
    entries = [
        {
            "id": "sensor.adc",
            "provides": [],
            "_impl_class_paths": {"voltage_sampler::VoltageSampler": [["id"]]},
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == []
    assert entries[0]["provides_id_paths"] == {}


def test_resolve_provides_same_domain_nested_entity_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-entity platform advertises its own domain at the sub-entity paths."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"sensor::Sensor"},
    )
    entries = [
        {
            "id": "sensor.aht10",
            "provides": [],
            "_impl_class_paths": {
                "aht10::AHT10Component": [["id"]],
                "sensor::Sensor": [["temperature", "id"], ["humidity", "id"]],
            },
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == ["sensor"]
    assert entries[0]["provides_id_paths"] == {
        "sensor": [["humidity", "id"], ["temperature", "id"]]
    }


def test_resolve_provides_same_domain_hybrid_keeps_root_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hybrid platform (root id is the entity, plus a sub-entity) keeps its root path."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: {"sensor::Sensor"},
    )
    entries = [
        {
            "id": "sensor.pulse_counter",
            "provides": [],
            "_impl_class_paths": {"sensor::Sensor": [["id"], ["total", "id"]]},
        }
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == ["sensor"]
    assert entries[0]["provides_id_paths"] == {"sensor": [["id"], ["total", "id"]]}


def test_collect_referenced_classes_walks_nested_use_id(tmp_path: Path) -> None:
    """Every ``use_id_type`` in the bundle, however deeply nested, is gathered."""
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "x.sensor": {
                    "schemas": {
                        "CONFIG_SCHEMA": {
                            "schema": {
                                "config_vars": {
                                    "sensor": {"use_id_type": "voltage_sampler::VoltageSampler"}
                                }
                            }
                        }
                    }
                }
            }
        )
    )
    assert "voltage_sampler::VoltageSampler" in _collect_referenced_classes(tmp_path)


# ---------------------------------------------------------------------------
# Committed catalog contract
# ---------------------------------------------------------------------------


def test_adc_family_advertises_voltage_sampler() -> None:
    """The ADC sensors ct_clamp needs are tagged as voltage_sampler providers."""
    for cid in ("sensor.adc", "sensor.ads1115", "sensor.ads1118"):
        assert _load_body(cid).get("provides") == ["voltage_sampler"], cid


def test_ct_clamp_references_voltage_sampler_but_provides_nothing() -> None:
    """ct_clamp consumes the interface; it is not itself a provider."""
    body = _load_body("sensor.ct_clamp")
    assert body.get("provides", []) == []
    refs = {f["key"]: f.get("references_component") for f in body["config_entries"]}
    assert refs.get("sensor") == "voltage_sampler"


def test_class_match_not_namespace_match() -> None:
    """The touchscreen — not its button binary_sensor — provides ``cst226``.

    Both share the ``cst226`` C++ namespace, but only the touchscreen's id
    is the referenced ``CST226Touchscreen`` class; matching on the full
    class (not the namespace) keeps the button listener out.
    """
    assert _load_body("touchscreen.cst226").get("provides") == ["cst226"]
    assert _load_body("binary_sensor.cst226").get("provides", []) == []


def test_non_provider_sensor_has_no_provides() -> None:
    """A single-entity sensor advertises no interface, not even its own domain."""
    assert _load_body("sensor.ct_clamp").get("provides", []) == []


def test_index_carries_provides_for_providers() -> None:
    """``provides`` reaches the slim index so the frontend can build its map."""
    index = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in index["components"]}
    assert by_id["sensor.adc"].get("provides") == ["voltage_sampler"]
    assert "provides" not in by_id["sensor.ct_clamp"]


def test_multi_entity_platform_advertises_own_domain_sub_ids() -> None:
    """aht10's temperature/humidity sub-sensor ids are offerable sensor references."""
    body = _load_body("sensor.aht10")
    assert body.get("provides") == ["sensor"]
    assert body.get("provides_id_paths") == {"sensor": [["humidity", "id"], ["temperature", "id"]]}
    index = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in index["components"]}
    assert by_id["sensor.aht10"].get("provides") == ["sensor"]
    assert by_id["sensor.aht10"].get("provides_id_paths") == body["provides_id_paths"]


def test_hybrid_platform_keeps_root_entity_path() -> None:
    """pulse_counter's root id is itself the sensor; its path survives beside total's."""
    paths = _load_body("sensor.pulse_counter")["provides_id_paths"]["sensor"]
    assert ["id"] in paths
    assert ["total", "id"] in paths


def test_usb_uart_provides_uart_via_nested_channel_id() -> None:
    """usb_uart's channel id is a uart, advertised with the nested path to it."""
    body = _load_body("usb_uart")
    assert body.get("provides") == ["uart"]
    assert body.get("provides_id_paths") == {"uart": [["channels", "id"]]}


def test_index_carries_usb_uart_nested_provider_path() -> None:
    """The nested-id path reaches the slim index the frontend reads."""
    index = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in index["components"]}
    assert by_id["usb_uart"].get("provides") == ["uart"]
    assert by_id["usb_uart"].get("provides_id_paths") == {"uart": [["channels", "id"]]}


def test_sprinkler_advertises_all_nested_switch_paths() -> None:
    """A component exposing one interface at several nested ids keeps every path."""
    body = _load_body("sprinkler")
    assert body.get("provides") == ["number", "switch"]
    switch_paths = body["provides_id_paths"]["switch"]
    # Not just the first switch: standalone and per-valve switches are all offered.
    assert ["auto_advance_switch", "id"] in switch_paths
    assert ["valves", "valve_switch", "id"] in switch_paths
    assert len(switch_paths) > 1
