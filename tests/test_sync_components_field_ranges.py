"""Tests for schema-derived per-field numeric range bounds.

The schema bundle's ``data_type`` field surfaces fixed-width
integer bounds (``uint8_t`` → ``[0, 255]``) but **drops** the
constraint when the schema author wraps the type in a ``cv.All``
chain alongside an explicit ``cv.Range(min=..., max=...)``. The
canonical case from a real bug report:

    cv.Optional(CONF_CONNECTION_SLOTS, default=3): cv.All(
        cv.positive_int,
        cv.Range(min=1, max=esp32_ble.IDF_MAX_CONNECTIONS),
    )

The bundle records ``data_type=positive_int`` and the
``cv.Range(min=1, max=15)`` is silent. Without live introspection
the visual editor accepts ``connection_slots: 99`` and the user
sees the validation error only after switching to the YAML pane.

Because ``cv.positive_int`` is itself ``cv.All(cv.int_,
cv.Range(min=0))``, the walker has to *intersect* every
``vol.Range`` it finds along the chain — picking the first one
would surface ``(0, None)`` and not solve the bug.

Pin the walker against synthetic ``cv.All`` chains mirroring the
upstream patterns. One integration test runs against the live
``bluetooth_proxy`` manifest to catch regressions where the
algorithm is right against synthetic schemas but breaks against
real upstream shapes.
"""

from __future__ import annotations

import esphome.config_validation as cv
import pytest
import voluptuous as vol

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_field_ranges,
    _collect_field_ranges,
    _numeric_range_bounds,
)


class _FakeManifest:
    """Minimal manifest stub — only ``config_schema`` is read."""

    def __init__(self, schema: object) -> None:
        self.config_schema = schema


def test_numeric_range_bounds_picks_up_explicit_range() -> None:
    """A bare ``cv.Range(min=1, max=15)`` reports its bounds."""
    assert _numeric_range_bounds(vol.Range(min=1, max=15)) == (1, 15)


def test_numeric_range_bounds_returns_none_for_non_validator() -> None:
    """Anything that isn't a Range / All chain reports ``None``."""
    assert _numeric_range_bounds(cv.string) is None
    assert _numeric_range_bounds(cv.boolean) is None


def test_numeric_range_bounds_intersects_within_all_chain() -> None:
    """``cv.positive_int`` (itself an All-with-Range) AND a tighter Range.

    Mirrors the upstream ``bluetooth_proxy.connection_slots`` shape:
    ``cv.All(cv.positive_int, cv.Range(min=1, max=15))`` flattens to
    ``cv.All(cv.int_, cv.Range(min=0), cv.Range(min=1, max=15))``.
    The intersection picks the tighter ``(1, 15)``.
    """
    validator = cv.All(cv.positive_int, cv.Range(min=1, max=15))
    assert _numeric_range_bounds(validator) == (1, 15)


def test_numeric_range_bounds_drops_partially_unbounded_range() -> None:
    """Range with no upper bound (``cv.positive_int`` alone) is dropped.

    The wire format is "fully bounded numeric pair"; partial
    bounds can't be rendered as a clamping numeric input. Falling
    back to None lets the data-type's natural upper bound win
    instead.
    """
    assert _numeric_range_bounds(cv.positive_int) is None


def test_numeric_range_bounds_skips_non_numeric_bounds() -> None:
    """``vol.Range`` with ``TimePeriod`` bounds isn't a numeric input.

    A ``cv.Range(max=TimePeriod(microseconds=4294967295))`` upstream
    bounds a duration, not a plain integer; surfacing it as a
    numeric ``[0, TimePeriod(...)]`` pair would be nonsensical to
    the frontend's number input.
    """

    class _DurationStub:
        """Stand-in for a non-numeric Range bound.

        Mirrors the shape of ``esphome.core.TimePeriod`` for the
        purposes of this test — the only thing the walker checks
        is ``isinstance(node, (int, float))``, so any non-numeric
        class works as the negative case.
        """

    validator = vol.Range(max=_DurationStub())  # type: ignore[arg-type]
    assert _numeric_range_bounds(validator) is None


def test_numeric_range_bounds_treats_booleans_as_non_numeric() -> None:
    """``True`` / ``False`` shouldn't slip through as ``1`` / ``0``.

    ``isinstance(True, int)`` is ``True`` in Python, so a defensive
    ``not isinstance(..., bool)`` guard keeps a buggy
    ``vol.Range(min=False, max=True)`` from emitting ``[0, 1]``.
    Pin the guard.
    """
    assert _numeric_range_bounds(vol.Range(min=False, max=True)) is None


def test_numeric_range_bounds_handles_floats() -> None:
    """Float bounds flow through verbatim (e.g. for percentage fields)."""
    assert _numeric_range_bounds(vol.Range(min=0.0, max=1.0)) == (0.0, 1.0)


def test_numeric_range_bounds_returns_none_for_disjoint_intersection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Disjoint ``vol.Range`` constraints in a ``vol.All`` chain return ``None``.

    ``cv.All(cv.Range(min=10), cv.Range(max=5))`` is an upstream
    schema bug — the field accepts no value. The wire format
    ``[min, max]`` can't represent "accepts nothing," so we log a
    warning (so the upstream bug surfaces) and return ``None``
    rather than serialise an invalid ``[10, 5]`` that would clamp
    wrong on the frontend.

    Pin the logged-warning behaviour so a future refactor can't
    silently drop the diagnostic.
    """
    validator = cv.All(vol.Range(min=10), vol.Range(max=5))
    with caplog.at_level("WARNING"):
        result = _numeric_range_bounds(validator)
    assert result is None
    assert any("collapsed to empty" in rec.message for rec in caplog.records)


def test_numeric_range_bounds_does_not_traverse_vol_any() -> None:
    """``vol.Any`` branches aren't traversed — bounds inside one are dropped.

    A field declared as ``vol.Any(vol.Range(min=1, max=10),
    vol.Range(min=20, max=30))`` would mean "value is in [1, 10] OR
    [20, 30]," which the wire format's single ``[min, max]`` pair
    can't express. Skipping ``vol.Any`` is the conservative choice
    — the field falls through to its ``data_type`` defaults (or no
    bounds), and the user still gets a compile-time validation
    error if they pick a number neither branch accepts.

    Pin the limitation so a future refactor that adds ``vol.Any``
    traversal has to revisit the wire-format question rather than
    silently surfacing a bound that doesn't actually constrain
    what the schema accepts.
    """
    validator = vol.Any(vol.Range(min=1, max=10), vol.Range(min=20, max=30))
    assert _numeric_range_bounds(validator) is None


def test_numeric_range_bounds_returns_none_when_only_any_wraps_a_range() -> None:
    """``cv.All(cv.Any(cv.Range(...)))`` falls through too — the All sees no Range.

    The walker only recurses into ``vol.All``; once it hits a
    ``vol.Any`` child it stops. A range nested behind an ``Any``
    might not constrain the field even when its sibling Any
    branches are non-Range, so reporting it would be misleading.
    """
    validator = vol.All(vol.Any(vol.Range(min=1, max=10), cv.string))
    assert _numeric_range_bounds(validator) is None


def test_collect_returns_empty_for_unbounded_schema() -> None:
    """A schema with only string fields produces no ranges."""
    schema = {cv.Optional("ssid"): cv.string, cv.Optional("password"): cv.string}
    assert _collect_field_ranges(_FakeManifest(schema)) == {}


def test_collect_top_level_range() -> None:
    """The walker stamps the path at the field's depth."""
    schema = {
        cv.Optional("count", default=3): cv.All(cv.positive_int, cv.Range(min=1, max=10)),
    }
    out = _collect_field_ranges(_FakeManifest(schema))
    assert out == {("count",): (1, 10)}


def test_collect_nested_range() -> None:
    """Nested entries' bounds are reachable via dotted paths."""
    schema = {
        cv.Optional("inner"): cv.Schema(
            {
                cv.Optional("value"): vol.Range(min=0, max=100),
            }
        ),
    }
    out = _collect_field_ranges(_FakeManifest(schema))
    assert out == {("inner", "value"): (0, 100)}


def test_collect_returns_empty_when_manifest_has_no_schema() -> None:
    """Missing ``config_schema`` is handled gracefully."""

    class NoSchemaManifest:
        config_schema = None

    assert _collect_field_ranges(NoSchemaManifest()) == {}


def test_apply_overlays_range_onto_matching_entry() -> None:
    """The applier writes the bounds onto the entry's ``range`` key."""
    entries = [
        {"key": "connection_slots", "range": None, "config_entries": []},
        {"key": "active", "range": None, "config_entries": []},
    ]
    _apply_field_ranges(entries, {("connection_slots",): (1, 15)})
    by_key = {e["key"]: e for e in entries}
    assert by_key["connection_slots"]["range"] == [1, 15]
    # Sibling field with no constraint stays untouched.
    assert by_key["active"]["range"] is None


def test_apply_overrides_existing_static_range() -> None:
    """A live-introspected bound is more specific than the data-type default.

    ``data_type=uint8_t`` would surface ``[0, 255]`` from the static
    ``_DATA_TYPE_RANGE`` map; if the schema also wraps it in
    ``cv.Range(min=10, max=200)``, the live bound is what the user
    actually has to satisfy at compile time and should win.
    """
    entries = [
        {"key": "byte_field", "range": [0, 255], "config_entries": []},
    ]
    _apply_field_ranges(entries, {("byte_field",): (10, 200)})
    assert entries[0]["range"] == [10, 200]


def test_apply_walks_nested_paths() -> None:
    """Nested entries' paths route correctly to the constraint dict."""
    entries = [
        {
            "key": "outer",
            "config_entries": [
                {"key": "inner", "range": None, "config_entries": []},
            ],
        },
    ]
    _apply_field_ranges(entries, {("outer", "inner"): (5, 50)})
    assert entries[0]["config_entries"][0]["range"] == [5, 50]


def test_apply_is_a_no_op_with_empty_ranges() -> None:
    """No ranges → entries are left exactly as they came in."""
    entries = [{"key": "a", "range": None, "config_entries": []}]
    before = [dict(e) for e in entries]
    _apply_field_ranges(entries, {})
    assert entries == before


def test_collect_against_live_bluetooth_proxy_manifest() -> None:
    """End-to-end: walker recovers ``connection_slots``' bound.

    Pins the integration so a future upstream refactor that wraps
    ``cv.Range`` in a way the walker doesn't recognise can't slip
    past CI. Asserts structural properties (``connection_slots`` is
    in the dict, lower bound is 1, upper bound is small) rather
    than exact equality so an upstream IDF bump doesn't break us —
    that's a catalog diff worth reviewing in the next nightly sync,
    not a CI failure.
    """
    pytest.importorskip("esphome.components.bluetooth_proxy")
    from esphome import loader  # noqa: PLC0415

    manifest = loader.get_component("bluetooth_proxy")
    out = _collect_field_ranges(manifest)
    assert ("connection_slots",) in out, (
        "bluetooth_proxy.connection_slots lost its Range bound — check whether "
        "upstream replaced cv.All(cv.positive_int, cv.Range(...)) with a shape "
        "the walker doesn't recognise"
    )
    lower, upper = out[("connection_slots",)]
    assert lower == 1
    assert isinstance(upper, int) and 1 < upper <= 64
