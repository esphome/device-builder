"""Unit tests for the schema-author UI-hint passthrough in ``_convert_field``.

Pairs with esphome/esphome#16267, which adds ``advanced`` /
``yaml_only`` kwargs to ``cv.Optional`` / ``cv.Required`` so the
field author can mark a config_var's UI treatment in the schema
itself. The dumper emits those flags onto the per-field dict in the
schema bundle; ``_convert_field`` is the consumer that maps them
onto the catalog's ``advanced`` / ``hidden`` fields.

These tests pin three behavioural invariants:

1. The schema's value wins over the name-based heuristic when the
   key is *present* on the raw dict (even when the schema marks
   the field ``False`` to override a heuristic that would otherwise
   flip it on).
2. The heuristic is the fallback when the schema doesn't carry the
   key at all.
3. ``yaml_only`` maps cleanly onto the catalog's existing
   ``hidden`` field — no rename, no consumer-facing surface change.
"""

from __future__ import annotations

from pathlib import Path

from script.sync_components import _convert_field  # type: ignore[import-not-found]

# ``_convert_field`` only touches ``schema_dir`` for nested
# ``extends`` resolution, which the leaf-field cases below don't
# trigger. ``Path("/")`` is fine for these tests.
_SCHEMA_DIR = Path("/")


def _leaf(**raw: object) -> dict:
    """Build a minimal raw schema dict for a string-typed Optional leaf."""
    return {
        "key": "Optional",
        "type": "string",
        **raw,
    }


def test_schema_advanced_true_wins_over_heuristic_false() -> None:
    """Schema ``advanced=True`` flips a heuristic-False field to advanced.

    ``name`` is in ``_IMPORTANT_KEYS`` so the heuristic returns
    False; the schema flag flips it to True.
    """
    entry = _convert_field("name", _leaf(advanced=True), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True


def test_schema_advanced_false_overrides_heuristic_true() -> None:
    """Schema ``advanced=False`` overrides a heuristic-True field.

    ``setup_priority`` would normally be advanced via the heuristic
    (it's in ``_ADVANCED_BASE_KEYS``); explicit ``advanced=False``
    on the schema must override that.

    Today the upstream dumper only emits the key when ``True``, but
    pinning the override-on-False semantics now keeps the consumer
    forward-compatible with a future dumper that emits both
    polarities.
    """
    entry = _convert_field("setup_priority", _leaf(advanced=False), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False


def test_no_schema_flag_falls_back_to_heuristic() -> None:
    """Without the schema flag, the heuristic decides ``advanced``.

    ``setup_priority`` has the heuristic-derived ``True``;
    ``name`` has ``False``.
    """
    advanced_entry = _convert_field("setup_priority", _leaf(), _SCHEMA_DIR)
    assert advanced_entry is not None
    assert advanced_entry["advanced"] is True

    name_entry = _convert_field("name", _leaf(), _SCHEMA_DIR)
    assert name_entry is not None
    assert name_entry["advanced"] is False


def test_yaml_only_maps_to_hidden() -> None:
    """Schema ``yaml_only=True`` sets the catalog's ``hidden=True``.

    The frontend already filters ``hidden`` entries (via
    ``isEntryVisible``), so the schema concept piggybacks on that
    surface — no consumer rename needed.
    """
    entry = _convert_field("foo", _leaf(yaml_only=True), _SCHEMA_DIR)
    assert entry is not None
    assert entry["hidden"] is True


def test_yaml_only_default_false_when_absent() -> None:
    """A field without ``yaml_only`` on the schema gets ``hidden: False``."""
    entry = _convert_field("foo", _leaf(), _SCHEMA_DIR)
    assert entry is not None
    assert entry["hidden"] is False


def test_advanced_and_yaml_only_are_independent() -> None:
    """Both flags can be set on the same field, independently.

    ``advanced=True`` AND ``hidden=True`` is a legal combination
    (a sensible consumer treats ``hidden`` as "skip" regardless of
    ``advanced``, but the catalog passes both through faithfully).
    """
    entry = _convert_field("foo", _leaf(advanced=True, yaml_only=True), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True
    assert entry["hidden"] is True


def test_advanced_explicit_false_with_yaml_only_true() -> None:
    """``yaml_only`` doesn't imply ``advanced`` — they're distinct dimensions.

    A consumer must not infer ``advanced=True`` from
    ``yaml_only=True``: the schema's explicit ``advanced=False``
    overrides the heuristic for ``setup_priority`` (which would
    otherwise say True).
    """
    entry = _convert_field(
        "setup_priority",
        _leaf(advanced=False, yaml_only=True),
        _SCHEMA_DIR,
    )
    assert entry is not None
    # advanced honours the schema's explicit False even though the
    # heuristic for ``setup_priority`` would say True.
    assert entry["advanced"] is False
    assert entry["hidden"] is True
