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
    """Own id class + transitive parents + discriminated ``types.*`` ids."""
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
    assert _implemented_classes(section) == {
        "adc::ADCSensor",
        "sensor::Sensor",
        "voltage_sampler::VoltageSampler",
        "x::Y",
        "z::Z",
    }


def test_implemented_classes_empty_without_id_type() -> None:
    """No declared id type ⇒ implements nothing referenceable."""
    assert _implemented_classes({"schemas": {"CONFIG_SCHEMA": {"schema": {}}}}) == set()


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
            "_impl_classes": {
                "adc::ADCSensor",
                "sensor::Sensor",
                "voltage_sampler::VoltageSampler",
            },
        },
        {
            "id": "sensor.dht",
            "provides": [],
            "_impl_classes": {"dht::DHT", "sensor::Sensor"},
        },
    ]
    _resolve_provides(entries, Path("/unused"))
    # adc is a voltage_sampler under the sensor domain → cross-domain, kept.
    assert entries[0]["provides"] == ["voltage_sampler"]
    # dht only implements sensor::Sensor — its own domain, the top-level
    # scan already covers it, so nothing to advertise.
    assert entries[1]["provides"] == []
    # Scratch field is popped before emit.
    assert "_impl_classes" not in entries[0]
    assert "_impl_classes" not in entries[1]


def test_resolve_provides_ignores_unreferenced_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An implemented class that no ``use_id`` targets is never advertised."""
    monkeypatch.setattr(
        "script.sync_components._collect_referenced_classes",
        lambda _dir: set(),
    )
    entries = [
        {"id": "sensor.adc", "provides": [], "_impl_classes": {"voltage_sampler::VoltageSampler"}}
    ]
    _resolve_provides(entries, Path("/unused"))
    assert entries[0]["provides"] == []


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
    """A plain sensor advertises no interface."""
    assert _load_body("sensor.dht").get("provides", []) == []


def test_index_carries_provides_for_providers() -> None:
    """``provides`` reaches the slim index so the frontend can build its map."""
    index = json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in index["components"]}
    assert by_id["sensor.adc"].get("provides") == ["voltage_sampler"]
    assert "provides" not in by_id["sensor.dht"]
