"""Tests for the ``_extract_default`` resolver helper.

It pairs a field's default value with its ``depends_on_component``
gate from either schema shape (plain ``default`` or
``default_with``).

ESPHome's schema bundle (post esphome/esphome#16276) ships
``cv.OnlyWith`` defaults under a new ``default_with`` field that
bundles the value with the component(s) that gate it; older
schemas ship a plain ``default`` for unconditional defaults.
``_extract_default`` resolves both shapes into the
``(default_value, depends_on_component)`` pair the catalog entry
expects, so downstream consumers don't need to know which schema
shape produced their default.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _extract_default,
)


def test_unconditional_default_returns_value_and_no_gate() -> None:
    """Plain ``default: "true"`` → ``(True, None)``.

    Backwards-compatible path: schemas predating #16276 (and any
    field that uses ``cv.Optional(K, default=X)`` rather than
    ``cv.OnlyWith``) ship the default unconditionally. The gate
    returns ``None`` so the catalog entry's
    ``depends_on_component`` falls through to the curated
    ``_COMPONENT_GATED_KEYS`` mapping (or stays empty).
    """
    assert _extract_default({"default": "true"}) == (True, None)
    assert _extract_default({"default": "False"}) == (False, None)
    assert _extract_default({"default": "5"}) == ("5", None)


def test_no_default_returns_pair_of_nones() -> None:
    """No ``default`` and no ``default_with`` → ``(None, None)``."""
    assert _extract_default({"key": "Optional"}) == (None, None)


def test_default_with_single_component_returns_value_and_gate() -> None:
    """``default_with: {value, components: [<one>]}`` → ``(value, "<one>")``.

    The canonical ``cv.OnlyWith(K, "wifi", default=True)`` shape
    that triggered #16276. ``depends_on_component`` is a
    single-string field, and the typical OnlyWith call site lists
    exactly one gating component, so the catalog entry can apply
    the gate directly.
    """
    raw = {
        "default_with": {"value": "True", "components": ["wifi"]},
    }
    assert _extract_default(raw) == (True, "wifi")


def test_default_with_multi_component_picks_first_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``default_with`` with multiple components → first + log.warning.

    ``cv.OnlyWith`` supports a list of components that ALL must
    be loaded for the default to apply. ``depends_on_component``
    is a single-string field today, so we pick the first and log
    a WARNING. Picking up correct multi-component gating is a
    future extension once a real call site needs it; for now no
    upstream OnlyWith uses a list (verified against ESPHome's
    five existing call sites — all single-component).
    """
    raw = {
        "default_with": {
            "value": "DC_SOURCE",
            "components": ["zigbee", "nrf52"],
        },
    }
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        value, gate = _extract_default(raw, key="power_source")
    assert value == "DC_SOURCE"
    assert gate == "zigbee"
    # One warning, named field surfaces in the message, both
    # components mentioned, and the chosen one is called out.
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "power_source" in msg
    assert "zigbee" in msg
    assert "nrf52" in msg


def test_default_with_empty_components_returns_no_gate() -> None:
    """``default_with`` with empty ``components`` → no gate.

    Defensive against malformed bundles. The default value still
    flows through; the gate just isn't applied.
    """
    raw = {"default_with": {"value": "True", "components": []}}
    assert _extract_default(raw) == (True, None)


def test_default_with_takes_precedence_over_default() -> None:
    """``default_with`` wins when both fields are present.

    Wouldn't normally happen — the upstream schema generator
    populates one or the other, not both — but pin the precedence
    so a future change to either side doesn't accidentally drop
    the gate.
    """
    raw = {
        "default": "False",
        "default_with": {"value": "True", "components": ["wifi"]},
    }
    assert _extract_default(raw) == (True, "wifi")


# ─── End-to-end fixtures from real schema ────────────────────────────
# The fixtures below are the actual ``raw`` schema-bundle entries
# produced by ESPHome's ``script/build_language_schema.py`` after
# esphome/esphome#16276 lands. Captured by running the patched
# script against ESPHome's tree on 2026-05-06; pasted verbatim
# here so the device-builder side has a concrete contract to test
# against without needing the upstream PR merged first. If the
# upstream shape changes (e.g. ``default_with`` becomes
# ``default_when_loaded``), these fixtures are the canary.


_FIXTURE_SOFTWARE_COEXISTENCE: dict = {
    "default_with": {"value": "True", "components": ["wifi"]},
    "key": "Optional",
    "type": "boolean",
}

_FIXTURE_POWER_SOURCE: dict = {
    "default_with": {"value": "DC_SOURCE", "components": ["nrf52"]},
    "key": "Optional",
    "type": "enum",
    "values": {
        "BATTERY": None,
        "DC_SOURCE": None,
        "EMERGENCY_MAINS_CONST": None,
        "EMERGENCY_MAINS_TRANSF": None,
        "MAINS_SINGLE_PHASE": None,
        "MAINS_THREE_PHASE": None,
        "UNKNOWN": None,
    },
}

_FIXTURE_TX_POWER: dict = {
    "default_without": {"value": "3dBm", "components": ["esp32_hosted"]},
    "key": "Optional",
    "type": "enum",
    "values": {
        "-12": None,
        "-3": None,
        "-6": None,
        "-9": None,
        "0": None,
        "3": None,
        "6": None,
        "9": None,
    },
}


def test_extract_default_software_coexistence_fixture() -> None:
    """Real ``esp32_ble_tracker.software_coexistence`` raw entry.

    The motivating case: ``cv.OnlyWith(K, "wifi", default=True)``.
    Pin that the resolver lifts the gated default exactly as the
    catalog generator needs it.
    """
    assert _extract_default(_FIXTURE_SOFTWARE_COEXISTENCE) == (True, "wifi")


def test_extract_default_power_source_fixture() -> None:
    """Real ``zigbee.power_source`` raw entry — string default."""
    assert _extract_default(_FIXTURE_POWER_SOURCE) == ("DC_SOURCE", "nrf52")


def test_extract_default_tx_power_fixture_skipped_for_now() -> None:
    """Real ``esp32_ble_beacon.tx_power`` raw entry — ``default_without``.

    ``cv.OnlyWithout`` has inverse-gate semantics
    (``depends_on_component`` can't model an inverted gate
    today), so the resolver returns ``(None, None)`` for these
    fields — no regression vs. the pre-#16276 era when the
    schema dropped them entirely. Pin that the absence is
    intentional, not a parser bug, so a future contributor who
    extends the catalog model to support inverse gates can
    replace this assertion with the real expected output.
    """
    assert _extract_default(_FIXTURE_TX_POWER) == (None, None)


# ─── End-to-end ``_convert_field`` tests ─────────────────────────────
# Cover the wiring all the way from raw schema entry → catalog
# ConfigEntry dict, so a future change anywhere in the conversion
# pipeline (entry-type resolution, advanced classification, options
# building, …) that quietly drops the gate or default is caught.


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty schema dir for ``_convert_field``.

    ``_convert_field`` only touches it for ``extends`` lookups,
    which our fixtures don't use.
    """
    return tmp_path


def test_convert_field_software_coexistence_carries_gate_and_default(
    schema_dir: Path,
) -> None:
    """OnlyWith boolean field carries the gated default end-to-end.

    ``cv.OnlyWith(K, "wifi", default=True)`` → boolean catalog
    entry with the gated default + ``depends_on_component``.
    The motivating case from esphome/device-builder-frontend#181's
    follow-up: without the gate, the dashboard rendered
    ``software_coexistence`` as a default-OFF toggle even when
    the user had ``wifi:`` configured. With this PR the catalog
    entry pairs the value with the gating component so the
    frontend's ``getEncryptionState``-style fallback chain can
    apply the default conditionally.
    """
    entry = _convert_field("software_coexistence", _FIXTURE_SOFTWARE_COEXISTENCE, schema_dir)
    assert entry is not None
    assert entry["type"] == "boolean"
    assert entry["default_value"] is True
    assert entry["depends_on_component"] == "wifi"
    assert entry["required"] is False


def test_convert_field_power_source_carries_gate_and_string_default(
    schema_dir: Path,
) -> None:
    """OnlyWith enum field with a string default carries through.

    ``cv.OnlyWith(K, "nrf52", default="DC_SOURCE")`` → enum
    catalog entry with the gated string default.
    Verifies the resolver doesn't accidentally coerce the
    ``"DC_SOURCE"`` string through ``_coerce_default``'s
    bool-coercion path.
    """
    entry = _convert_field("power_source", _FIXTURE_POWER_SOURCE, schema_dir)
    assert entry is not None
    assert entry["default_value"] == "DC_SOURCE"
    assert entry["depends_on_component"] == "nrf52"
    # Options come from the ``values`` map; pin a sample to make
    # sure the gate handling didn't disturb the rest of the
    # conversion pipeline.
    option_values = {opt["value"] for opt in entry["options"] or []}
    assert "DC_SOURCE" in option_values
    assert "BATTERY" in option_values


def test_convert_field_tx_power_default_without_no_gate(
    schema_dir: Path,
) -> None:
    """``cv.OnlyWithout`` field → no default, no gate (follow-up).

    The catalog entry still gets built — only the default and
    gate are absent. The frontend continues to render the field
    without a pre-filled value, same as before #16276.
    """
    entry = _convert_field("tx_power", _FIXTURE_TX_POWER, schema_dir)
    assert entry is not None
    assert entry["default_value"] is None
    assert entry["depends_on_component"] is None
    # Rest of the conversion still works.
    option_values = {opt["value"] for opt in entry["options"] or []}
    assert "3" in option_values


def test_convert_field_unconditional_default_unchanged(schema_dir: Path) -> None:
    """Plain ``cv.Optional(K, default=True)`` → unchanged behaviour.

    Schemas predating #16276 (and any field that uses
    ``cv.Optional`` rather than ``cv.OnlyWith``) ship the default
    unconditionally. Pin that those still flow through the
    existing path with no gate applied.
    """
    raw = {"default": "true", "key": "Optional", "type": "boolean"}
    entry = _convert_field("retain", raw, schema_dir)
    assert entry is not None
    assert entry["default_value"] is True
    # ``retain`` is in ``_COMPONENT_GATED_KEYS`` (mqtt) — that
    # curated mapping kicks in via ``_convert_config_vars``, NOT
    # via ``_convert_field`` directly. Bare ``_convert_field``
    # leaves ``depends_on_component`` empty for unconditional
    # defaults; that's the right contract.
    assert entry["depends_on_component"] is None
