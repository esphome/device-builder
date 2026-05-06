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

import pytest

from script.sync_components import _extract_default  # type: ignore[import-not-found]


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
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``default_with`` with multiple components → first + WARNING.

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
    value, gate = _extract_default(raw)
    assert value == "DC_SOURCE"
    assert gate == "zigbee"
    captured = capsys.readouterr()
    assert "default_with with multiple components" in captured.out
    assert "zigbee" in captured.out
    assert "nrf52" in captured.out


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
