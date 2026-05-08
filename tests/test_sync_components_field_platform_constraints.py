"""Tests for schema-derived per-field platform gating in the sync script.

The visual editor needs to know which fields are restricted to a
subset of the platforms their parent component runs on — e.g.
``sensor.debug.psram`` is wrapped in ``cv.only_on_esp32`` upstream
and ``sensor.debug.fragmentation`` accepts ESP8266 *or* ESP32 via
``cv.Any(cv.only_on_esp8266, cv.only_on_esp32)``. Without this
signal the form happily lets a user fill in a field on a board
that will fail validation at compile time three minutes later
(issue #417).

Pin the walker's behaviour against synthetic voluptuous schemas
that mirror the upstream patterns. Synthetic rather than the live
``debug`` manifest keeps the tests independent of upstream schema
changes — a refactor of upstream's gates would surface as a
catalog diff in the next sync, not a test breakage here.
"""

from __future__ import annotations

import esphome.config_validation as cv
import voluptuous as vol

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_field_platform_constraints,
    _collect_field_platform_constraints,
    _platform_set,
)


class _FakeManifest:
    """Minimal manifest stub — only ``config_schema`` is read."""

    def __init__(self, schema: object) -> None:
        self.config_schema = schema


def test_platform_set_recovers_only_on_esp32() -> None:
    """``cv.only_on_esp32`` carries its single-platform list in a closure."""
    assert _platform_set(cv.only_on_esp32) == frozenset({"esp32"})


def test_platform_set_recovers_only_on_with_explicit_list() -> None:
    """``cv.only_on([...])`` carries the list verbatim through the closure."""
    validator = cv.only_on(["bk72xx", "ln882x", "rtl87xx"])
    assert _platform_set(validator) == frozenset({"bk72xx", "ln882x", "rtl87xx"})


def test_platform_set_returns_none_for_unrelated_validator() -> None:
    """A validator that doesn't constrain platform yields ``None``."""
    assert _platform_set(cv.string) is None
    assert _platform_set(cv.boolean) is None
    assert _platform_set(lambda x: x) is None


def test_platform_set_intersects_within_all() -> None:
    """``vol.All`` chains take the intersection of platform constraints.

    A field wrapped in ``cv.All(cv.only_on_esp32, cv.requires_component(...))``
    inherits the ``cv.only_on_esp32`` constraint — the
    ``requires_component`` validator carries no platform gate and
    is treated as universe.
    """
    validator = vol.All(cv.only_on_esp32, cv.string)
    assert _platform_set(validator) == frozenset({"esp32"})


def test_platform_set_unions_within_any() -> None:
    """``vol.Any`` branches take the union of platform constraints.

    Mirrors the ``cv.Any(cv.only_on_esp8266, cv.only_on_esp32)``
    pattern that ``sensor.debug.fragmentation`` uses upstream.
    """
    validator = vol.Any(cv.only_on_esp8266, cv.only_on_esp32)
    assert _platform_set(validator) == frozenset({"esp32", "esp8266"})


def test_platform_set_any_with_unconstrained_branch_is_none() -> None:
    """One unconstrained ``vol.Any`` branch makes the whole Any unconstrained.

    If any branch accepts every platform, the Any accepts every
    platform — so we must not record a constraint.
    """
    validator = vol.Any(cv.only_on_esp32, cv.string)
    assert _platform_set(validator) is None


def test_platform_set_handles_nested_all_inside_any() -> None:
    """The walker recurses through nested All/Any combinators.

    ``cv.Any(cv.All(cv.only_on_esp8266, x), cv.only_on_esp32)``
    is the exact upstream shape for ``sensor.debug.fragmentation``
    (with ``x = cv.require_framework_version(...)``).
    """
    validator = vol.Any(
        vol.All(cv.only_on_esp8266, cv.string),
        cv.only_on_esp32,
    )
    assert _platform_set(validator) == frozenset({"esp32", "esp8266"})


def test_collect_returns_empty_for_unconstrained_schema() -> None:
    """A schema with no ``cv.only_on`` returns an empty constraint dict."""
    schema = {
        cv.Optional("free"): cv.string,
        cv.Optional("loop_time"): cv.string,
    }
    assert _collect_field_platform_constraints(_FakeManifest(schema)) == {}


def test_collect_top_level_only_on_constraint() -> None:
    """``cv.All(cv.only_on_esp32, schema)`` mirrors upstream's psram shape."""
    schema = {
        cv.Optional("free"): cv.string,
        cv.Optional("psram"): cv.All(cv.only_on_esp32, cv.string),
    }
    out = _collect_field_platform_constraints(_FakeManifest(schema))
    assert out == {("psram",): ["esp32"]}


def test_collect_walks_any_branches_for_union() -> None:
    """Unions surface as sorted lists at the field's path.

    Sorted output is what the catalog consumer expects — gives
    deterministic JSON across runs.
    """
    schema = {
        cv.Optional("fragmentation"): cv.All(
            cv.Any(
                cv.All(cv.only_on_esp8266, cv.string),
                cv.only_on_esp32,
            ),
            cv.string,
        ),
    }
    out = _collect_field_platform_constraints(_FakeManifest(schema))
    assert out == {("fragmentation",): ["esp32", "esp8266"]}


def test_collect_walks_explicit_platform_list() -> None:
    """``cv.only_on([...])`` flows through with all listed platforms.

    Mirrors upstream's ``sensor.debug.min_free`` shape:
    ``cv.Any(cv.only_on_esp32, cv.only_on([BK72XX, LN882X, RTL87XX]))``.
    """
    schema = {
        cv.Optional("min_free"): cv.All(
            cv.Any(
                cv.only_on_esp32,
                cv.only_on(["bk72xx", "ln882x", "rtl87xx"]),
            ),
            cv.string,
        ),
    }
    out = _collect_field_platform_constraints(_FakeManifest(schema))
    assert out == {("min_free",): ["bk72xx", "esp32", "ln882x", "rtl87xx"]}


def test_collect_returns_empty_when_manifest_has_no_schema() -> None:
    """Missing ``config_schema`` is handled gracefully."""

    class NoSchemaManifest:
        config_schema = None

    assert _collect_field_platform_constraints(NoSchemaManifest()) == {}


def test_apply_stamps_supported_platforms() -> None:
    """The applier walks catalog entries and copies the constraint in."""
    entries = [
        {"key": "free", "config_entries": []},
        {"key": "psram", "config_entries": []},
        {"key": "loop_time", "config_entries": []},
    ]
    constraints = {("psram",): ["esp32"]}
    _apply_field_platform_constraints(entries, constraints)
    by_key = {e["key"]: e for e in entries}
    assert by_key["psram"]["supported_platforms"] == ["esp32"]
    # Sibling fields without constraints are untouched. The model
    # default is an empty list (``field(default_factory=list)``)
    # which the JSON-stripping pass omits, so absence here is the
    # right signal.
    assert "supported_platforms" not in by_key["free"]
    assert "supported_platforms" not in by_key["loop_time"]


def test_apply_walks_nested_paths() -> None:
    """Nested entries keyed by ``(*parent, child)`` get the constraint.

    Today's upstream gates only target top-level fields, but the
    walker supports nested paths so future deeper gates land
    automatically.
    """
    entries = [
        {
            "key": "outer",
            "config_entries": [
                {"key": "inner", "config_entries": []},
                {"key": "sibling", "config_entries": []},
            ],
        },
    ]
    constraints = {("outer", "inner"): ["rp2040"]}
    _apply_field_platform_constraints(entries, constraints)
    inner = entries[0]["config_entries"][0]
    sibling = entries[0]["config_entries"][1]
    assert inner["supported_platforms"] == ["rp2040"]
    assert "supported_platforms" not in sibling


def test_apply_is_a_no_op_with_empty_constraints() -> None:
    """No constraints → entries are left exactly as they came in."""
    entries = [{"key": "ssid", "config_entries": []}]
    before = [dict(e) for e in entries]
    _apply_field_platform_constraints(entries, {})
    assert entries == before
