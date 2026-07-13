"""Unit tests for the schema-author UI-hint passthrough + cascade.

Pairs with esphome/esphome#16267, which adds a ``visibility``
kwarg (``cv.Visibility`` ``StrEnum``) to ``cv.Optional`` /
``cv.Required`` so the field author can mark a config_var's UI
treatment in the schema itself. The dumper emits the str form
(``"advanced"`` / ``"yaml_only"``) onto the per-field dict in
the schema bundle.

This module pins three behavioural invariants:

1. The schema's ``visibility`` value drives the catalog entry's
   ``advanced`` / ``hidden`` flags directly when present; the
   name-based heuristic is the fallback.
2. The cascade rule: a stricter parent forces its descendants
   at-least as strict (``YAML_ONLY`` > ``ADVANCED`` > unset). The
   schema marker is per-field as the author wrote it; the
   catalog generator computes the effective value when it walks
   the tree.
3. ``yaml_only`` maps cleanly onto the catalog's existing
   ``hidden`` field — no rename, no consumer-facing surface change.
"""

from __future__ import annotations

from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_visibility_cascade,
    _convert_field,
)

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


def test_schema_visibility_advanced_wins_over_heuristic_false() -> None:
    """``visibility: "advanced"`` flips a heuristic-False field to advanced.

    ``name`` is in ``_IMPORTANT_KEYS`` so the heuristic returns
    False; the schema flag flips it to True.
    """
    entry = _convert_field("name", _leaf(visibility="advanced"), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True
    assert entry["hidden"] is False


def test_schema_visibility_yaml_only_sets_hidden() -> None:
    """``visibility: "yaml_only"`` sets the catalog's ``hidden`` flag."""
    entry = _convert_field("foo", _leaf(visibility="yaml_only"), _SCHEMA_DIR)
    assert entry is not None
    assert entry["hidden"] is True
    # ``advanced`` from the heuristic isn't relevant when ``hidden``
    # is set — the frontend skips the entry entirely. Don't pin it
    # here; let the heuristic decide.


def test_no_visibility_falls_back_to_heuristic() -> None:
    """Without the schema flag, the heuristic decides ``advanced``.

    ``setup_priority`` has the heuristic-derived ``True``;
    ``name`` has ``False``.
    """
    advanced_entry = _convert_field("setup_priority", _leaf(), _SCHEMA_DIR)
    assert advanced_entry is not None
    assert advanced_entry["advanced"] is True
    assert advanced_entry["hidden"] is False

    name_entry = _convert_field("name", _leaf(), _SCHEMA_DIR)
    assert name_entry is not None
    assert name_entry["advanced"] is False
    assert name_entry["hidden"] is False


def test_schema_visibility_ui_wins_over_heuristic_true() -> None:
    """``visibility: "ui"`` pins a heuristic-True field to the main form.

    Upstream's least-hidden rung (esphome/esphome#17503):
    ``setup_priority`` is heuristic-advanced; the explicit author
    hint overrides it.
    """
    entry = _convert_field("setup_priority", _leaf(visibility="ui"), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False
    assert entry["hidden"] is False


def test_schema_visibility_ui_wins_over_platform_default_rule() -> None:
    """``visibility: "ui"`` beats the platform-defaulted device_class/icon rule."""
    entry = _convert_field("device_class", _leaf(visibility="ui", default="restart"), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False
    assert entry["default_value"] == "restart"


def test_unrecognised_visibility_string_falls_back_to_heuristic() -> None:
    """An unknown ``visibility`` value falls back to the heuristic.

    Future-compatibility: if upstream adds a third visibility level
    (e.g. ``"deprecated"``) the field doesn't silently disappear
    from the form. Neither ``advanced`` nor ``hidden`` flips on
    speculatively; the heuristic still applies.
    """
    entry = _convert_field("name", _leaf(visibility="someday-new-value"), _SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False
    assert entry["hidden"] is False


# ---------------------------------------------------------------------------
# Cascade rule
# ---------------------------------------------------------------------------


def _entry(
    key: str, *, advanced: bool = False, hidden: bool = False, inner: list | None = None
) -> dict:
    """Minimal catalog-entry shape for the cascade pass."""
    e: dict = {"key": key, "advanced": advanced, "hidden": hidden}
    if inner is not None:
        e["config_entries"] = inner
    return e


def test_cascade_parent_advanced_pushes_to_unset_children() -> None:
    """An ``ADVANCED`` parent makes every un-marked descendant ``ADVANCED``.

    Without the cascade an ``advanced`` parent would render under
    a disclosure but its inner fields would surface on the main
    form — leaky disclosure UX.
    """
    entries = [
        _entry(
            "parent",
            advanced=True,
            inner=[
                _entry("child_a"),
                _entry("child_b"),
            ],
        ),
    ]
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    inner = entries[0]["config_entries"]
    assert entries[0]["advanced"] is True
    assert inner[0]["advanced"] is True
    assert inner[1]["advanced"] is True
    # No ``YAML_ONLY`` anywhere → ``hidden`` stays False.
    assert all(e["hidden"] is False for e in [entries[0], *inner])


def test_cascade_yaml_only_parent_hides_all_descendants() -> None:
    """A ``YAML_ONLY`` parent hides every descendant.

    A block the user shouldn't edit through a UI must take its
    children with it — otherwise the form renders an unrooted
    control with no surrounding context to interpret it.
    """
    entries = [
        _entry(
            "parent",
            hidden=True,
            inner=[
                _entry("child_a"),
                _entry("child_b", advanced=True),
            ],
        ),
    ]
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    inner = entries[0]["config_entries"]
    # ``hidden`` cascades into both children regardless of what
    # they originally said.
    assert entries[0]["hidden"] is True
    assert inner[0]["hidden"] is True
    assert inner[1]["hidden"] is True
    # And ``advanced`` is also forced True under a hidden parent —
    # ``YAML_ONLY`` is strictly stronger than ``ADVANCED``.
    assert all(e["advanced"] is True for e in [entries[0], *inner])


def test_cascade_inner_yaml_only_under_advanced_parent() -> None:
    """A child marked ``YAML_ONLY`` keeps its hidden status under an ``ADVANCED`` parent.

    The cascade is monotonically-non-decreasing in strictness:
    children can be stricter than their parent (the child's own
    ``hidden=True`` survives), but never less strict (the parent's
    ``advanced=True`` is pushed onto sibling children).
    """
    entries = [
        _entry(
            "parent",
            advanced=True,
            inner=[
                _entry("child_unmarked"),
                _entry("child_yaml_only", hidden=True),
            ],
        ),
    ]
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    inner = entries[0]["config_entries"]
    # Parent stays ``advanced``.
    assert entries[0]["advanced"] is True
    assert entries[0]["hidden"] is False
    # Sibling without its own setting picks up the parent's
    # ``advanced``.
    assert inner[0]["advanced"] is True
    assert inner[0]["hidden"] is False
    # ``YAML_ONLY`` child stays hidden — and its ``advanced`` is
    # also True (every yaml-only field is also advanced by the
    # strictness ordering).
    assert inner[1]["advanced"] is True
    assert inner[1]["hidden"] is True


def test_cascade_recurses_through_multiple_levels() -> None:
    """The cascade walks all the way down nested groups.

    Three-level structure: parent (advanced) → middle (unset) →
    leaf (unset). The leaf must end up advanced because its
    grandparent is.
    """
    entries = [
        _entry(
            "grandparent",
            advanced=True,
            inner=[
                _entry(
                    "parent",
                    inner=[
                        _entry("leaf"),
                    ],
                ),
            ],
        ),
    ]
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    parent = entries[0]["config_entries"][0]
    leaf = parent["config_entries"][0]
    assert entries[0]["advanced"] is True
    assert parent["advanced"] is True
    assert leaf["advanced"] is True


def test_cascade_no_op_when_no_strict_parent() -> None:
    """A tree with no strict markers stays as the heuristic decided.

    The cascade only ever flips flags from False to True; it
    never flips True to False. A baseline tree should round-trip
    through the cascade unchanged.
    """
    entries = [
        _entry("a", advanced=False),
        _entry("b", advanced=True),
        _entry(
            "c",
            advanced=False,
            inner=[_entry("c1", advanced=True)],
        ),
    ]
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    assert entries[0]["advanced"] is False
    assert entries[1]["advanced"] is True
    assert entries[2]["advanced"] is False
    assert entries[2]["config_entries"][0]["advanced"] is True
    assert all(e["hidden"] is False for e in entries)


def test_cascade_non_list_inner_is_skipped() -> None:
    """``config_entries: None`` is tolerated without traversal.

    Some catalog entry shapes carry an explicit ``None`` for the
    nested list (NESTED-but-empty groups); the cascade must skip
    those rather than crashing on attribute access.
    """
    entries = [
        _entry("a", advanced=True),
    ]
    # Add the ``config_entries`` key as None — the catalog uses
    # this for nested-but-empty groups.
    entries[0]["config_entries"] = None
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    assert entries[0]["advanced"] is True
