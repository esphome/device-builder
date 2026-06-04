"""Tests for the pure ``helpers/yaml.py`` functions.

The helper module is purely string transforms — no file I/O, no
event-loop concerns — but it's easy to break in subtle ways
because the YAML it edits is unparsed text. The functions covered
here back the clone, friendly-name editor, and add-component
paths.

What we pin:

* ``rewrite_name_or_substitution`` — clone's hostname rewrite,
  with substitution-aware redirect when ``name: ${var}`` shows up.
* ``upsert_yaml_leaf_under_top_block`` — friendly_name editor's
  rewrite-or-insert path. Three shapes (rewrite existing leaf /
  insert into existing block / prepend a new block) plus the
  YAML-directive anchoring for configs starting with ``%YAML`` /
  ``---``.
* ``merge_component_yaml`` — adding a second sensor / output /
  switch must splice into the existing platform block instead of
  emitting a duplicate top-level key. A non-platform component
  (one without an ``<entity>:`` domain) falls through to plain
  append. The splice algorithm preserves trailing blank lines and
  any blocks that follow.
"""

from __future__ import annotations

import random
import string
from typing import Any

import pytest
import yaml

from esphome_device_builder.helpers.yaml import (
    YamlUpsertNotSupportedError,
    _mapping_body_to_list_item,
    _safe_yaml_scalar,
    _splice_into_domain_block,
    _splice_into_multi_conf_block,
    _strip_yaml_quotes,
    generate_api_encryption_key,
    generate_component_yaml,
    merge_component_yaml,
    parse_substitution_ref,
    read_yaml_scalar,
    rewrite_api_encryption_key,
    rewrite_name_or_substitution,
    rewrite_yaml_scalar,
    upsert_yaml_leaf_under_top_block,
)
from esphome_device_builder.helpers.yaml.scalar import _plain_is_fast_safe, _plain_is_safe
from esphome_device_builder.models.common import ConfigEntry, ConfigEntryType
from esphome_device_builder.models.components import (
    ComponentCatalogEntry,
    ComponentCategory,
)


def _component(
    *,
    component_id: str,
    category: ComponentCategory,
    name: str = "test",
    multi_conf: bool = False,
) -> ComponentCatalogEntry:
    """Minimal ComponentCatalogEntry — fields needed by yaml helpers only.

    The model carries a lot of catalog metadata (description,
    docs_url, config_entries, etc.) that the yaml helpers don't
    touch. Stub them with empty defaults so the test reads without
    boilerplate.
    """
    return ComponentCatalogEntry(
        id=component_id,
        name=name,
        description="",
        category=category,
        multi_conf=multi_conf,
    )


# ---------------------------------------------------------------------------
# parse_substitution_ref / rewrite_name_or_substitution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$devicename", "devicename"),
        ("${devicename}", "devicename"),
        ('"$devicename"', "devicename"),
        ("'${devicename}'", "devicename"),
        ("  ${devicename}  ", "devicename"),
        ("kitchen", None),
        ("$1bad", None),  # name must start with letter / underscore
        ("my-${suffix}", None),  # mixed value, not a pure ref
        ("${a}${b}", None),  # multiple refs
        ("", None),
    ],
)
def test_parse_substitution_ref(value: str, expected: str | None) -> None:
    """Pin the variable-name parser's accept / reject contract.

    Pure references (``$var`` / ``${var}`` optionally quoted)
    return the variable name; anything with extra glue or a
    malformed identifier returns ``None`` so the caller falls
    back to a literal rewrite rather than wreck a partial match.
    """
    assert parse_substitution_ref(value) == expected


def test_rewrite_name_or_substitution_redirects_through_substitution() -> None:
    """The wizard / dashboard_import shape: name lives in substitutions.

    ``esphome.name: ${devicename}`` paired with
    ``substitutions.devicename: kitchen`` is the canonical pattern
    ESPHome's wizard emits. Rewriting the leaf with a literal
    would orphan the substitution and break any other consumer
    (a sensor named ``${devicename}_temp``, etc.). Pin that the
    rewrite walks to the substitution definition instead.
    """
    yaml = (
        "substitutions:\n"
        "  devicename: acfloatmonitor32\n"
        "  friendly_name: AC Float Monitor 32\n"
        "esphome:\n"
        "  name: ${devicename}\n"
        "  friendly_name: ${friendly_name}\n"
    )
    out = rewrite_name_or_substitution(yaml, ("esphome", "name"), "bedroom-bulb")
    # Substitution definition flipped, leaf still references the var.
    assert "  devicename: bedroom-bulb\n" in out
    assert "  name: ${devicename}\n" in out
    # Other substitutions untouched.
    assert "  friendly_name: AC Float Monitor 32\n" in out


def test_rewrite_name_or_substitution_falls_through_to_literal() -> None:
    """A literal value gets rewritten on the leaf line directly."""
    yaml = "esphome:\n  name: kitchen\n"
    out = rewrite_name_or_substitution(yaml, ("esphome", "name"), "bedroom-bulb")
    assert out == "esphome:\n  name: bedroom-bulb\n"


def test_rewrite_name_or_substitution_handles_dollar_form() -> None:
    """Both ``$var`` and ``${var}`` reference shapes redirect."""
    yaml = "substitutions:\n  devicename: kitchen\nesphome:\n  name: $devicename\n"
    out = rewrite_name_or_substitution(yaml, ("esphome", "name"), "bedroom-bulb")
    assert "  devicename: bedroom-bulb\n" in out
    assert "  name: $devicename\n" in out


def test_rewrite_name_or_substitution_falls_through_when_substitution_not_local() -> None:
    """A leaf that references an unresolved variable rewrites the leaf.

    The substitutions block is in a package / ``!include``d file
    we can't see — better to land the literal on the leaf than
    silently no-op. The user can then edit the package definition
    if they want substitution-driven cloning.
    """
    yaml = "esphome:\n  name: ${devicename}\n"
    out = rewrite_name_or_substitution(yaml, ("esphome", "name"), "bedroom-bulb")
    assert "  name: bedroom-bulb\n" in out


def test_rewrite_name_or_substitution_handles_mixed_value_via_leaf() -> None:
    """Partial reference (``${prefix}-suffix``) rewrites the leaf, not the prefix.

    Splitting ``${prefix}-suffix`` into a substitution rewrite +
    suffix preservation isn't possible without changing what
    ``${prefix}`` resolves to elsewhere. Land the new value as a
    literal on the leaf and let the user clean up.
    """
    yaml = "substitutions:\n  prefix: my\nesphome:\n  name: ${prefix}-suffix\n"
    out = rewrite_name_or_substitution(yaml, ("esphome", "name"), "bedroom-bulb")
    assert "  prefix: my\n" in out  # untouched
    assert "  name: bedroom-bulb\n" in out  # leaf flipped to literal


# ---------------------------------------------------------------------------
# upsert_yaml_leaf_under_top_block
# ---------------------------------------------------------------------------


def test_upsert_yaml_leaf_rewrites_existing_leaf_via_substitution_helper() -> None:
    """Existing leaf path → rewrite (substitution-aware).

    Pin that the rewrite path delegates to
    ``rewrite_name_or_substitution`` so the substitution-redirect
    behaviour is preserved when the leaf already exists.
    """
    yaml = "substitutions:\n  friendly_name: Old\nesphome:\n  friendly_name: ${friendly_name}\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "New")
    # Substitution definition flipped, leaf still references the var.
    assert "  friendly_name: New\n" in out
    assert "  friendly_name: ${friendly_name}\n" in out


def test_upsert_yaml_leaf_inserts_into_existing_block() -> None:
    """``esphome:`` exists, no ``friendly_name:`` — insert into the block.

    The new line lands at the end of the block (after any other
    children) so the existing layout — comments above, sibling
    keys in their original order — survives untouched.
    """
    yaml = "esphome:\n  name: kitchen\n  area: Kitchen\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    assert out == (
        "esphome:\n"
        "  name: kitchen\n"
        "  area: Kitchen\n"
        "  friendly_name: Reading Lamp\n"
        "esp32:\n  variant: ESP32\n"
    )


def test_upsert_yaml_leaf_matches_existing_child_indent() -> None:
    """4-space children get a 4-space-indented insert, not a hardcoded 2.

    Hand-edited configs that use 4-space indent shouldn't suddenly
    sprout a 2-space-indented sibling. Detect the indent from any
    existing child and match it.
    """
    yaml = "esphome:\n    name: kitchen\nesp32:\n    variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    assert "    friendly_name: Reading Lamp\n" in out


def test_upsert_yaml_leaf_preserves_tab_indent() -> None:
    """
    Tab-indented children get a tab-indented insert, not column 0.

    PyYAML (and ESPHome's own loader) accept tab-indented YAML, so
    we need to round-trip it; a spaces-only indent-capture step
    collapsed the prefix to ``""`` and emitted the new leaf at
    column 0, producing a sibling top-level key outside the block.
    """
    before = "esphome:\n\tname: kitchen\n"
    after = upsert_yaml_leaf_under_top_block(before, "esphome", "friendly_name", "Kitchen")
    assert after == "esphome:\n\tname: kitchen\n\tfriendly_name: Kitchen\n"


def test_upsert_yaml_leaf_prepends_new_block_when_missing() -> None:
    """No ``esphome:`` block at all — prepend a fresh one with the leaf.

    Package-driven config where ``esphome:`` lives in an
    ``!include``d file. The local override-by-merge means
    inserting our own ``esphome: { friendly_name: ... }`` here
    actually wins on the device.
    """
    yaml = "packages:\n  base: !include common/base.yaml\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    # New block at the top.
    assert out.startswith("esphome:\n  friendly_name: Reading Lamp\n")
    # Pre-existing top-level keys preserved verbatim.
    assert "packages:\n  base: !include common/base.yaml\n" in out
    assert "esp32:\n  variant: ESP32\n" in out


def test_upsert_yaml_leaf_anchors_below_yaml_directives_and_doc_marker() -> None:
    """``%YAML 1.2`` + ``---`` stay at byte 0; new block lands below."""
    yaml = "%YAML 1.2\n---\n\npackages:\n  base: !include common/base.yaml\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    assert out.startswith("%YAML 1.2\n---\n")
    assert "---\n\nesphome:\n  friendly_name: Reading Lamp\n" in out
    assert "packages:\n  base: !include common/base.yaml\n" in out


def test_upsert_yaml_leaf_anchors_below_doc_marker_only() -> None:
    """Bare ``---`` at the top stays at byte 0."""
    yaml = "---\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    assert out.startswith("---\nesphome:\n  friendly_name: Reading Lamp\n")


def test_upsert_yaml_leaf_anchors_when_file_is_only_marker() -> None:
    """File containing only ``---`` + blank still anchors past the marker."""
    out = upsert_yaml_leaf_under_top_block("---\n\n", "esphome", "friendly_name", "X")
    assert out == "---\n\nesphome:\n  friendly_name: X\n"


def test_upsert_yaml_leaf_skips_indented_comments_inside_block() -> None:
    """Indented ``#`` inside the block doesn't end it or steal its indent."""
    yaml = "esphome:\n  name: x\n  # mid-block note\n  area: Y\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Lamp")
    assert "  # mid-block note\n" in out
    assert "  friendly_name: Lamp\n" in out


def test_upsert_yaml_leaf_inserts_before_trailing_blank_lines() -> None:
    """Blank lines between block end and next block aren't part of the block."""
    yaml = "esphome:\n  name: x\n\n\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Lamp")
    # New leaf lands right after ``name:``, before the blank gap.
    assert "  name: x\n  friendly_name: Lamp\n\n\nesp32:" in out


def test_upsert_yaml_leaf_safely_quotes_yaml_specials_on_insert() -> None:
    """``Bedroom #2`` round-trips through ``_safe_yaml_scalar`` quoting on insert.

    The insert path renders the value through the same safe-quote
    helper the rewrite path uses, so a value with `` #`` /
    ``: `` / leading indicator chars / reserved bool/null spelling
    can't quietly truncate or split into a key/value pair.
    """
    yaml = "esphome:\n  name: kitchen\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Bedroom #2")
    assert '  friendly_name: "Bedroom #2"\n' in out


def test_upsert_yaml_leaf_block_with_no_children_uses_default_indent() -> None:
    r"""Block with no children to copy from defaults to two spaces.

    Edge case: a ``esphome:\n`` line with no body (already
    declared but empty). Insert with the ESPHome-canonical
    two-space indent.
    """
    yaml = "esphome:\nesp32:\n  variant: ESP32\n"
    out = upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Reading Lamp")
    assert out.startswith("esphome:\n  friendly_name: Reading Lamp\n")


def test_upsert_yaml_leaf_rejects_flow_style_mapping() -> None:
    """``esphome: { … }`` flow-style raises ``YamlUpsertNotSupportedError``.

    The line-based walker can't safely insert into a single-line
    flow scalar without re-parsing the whole mapping. Rather than
    silently prepending a duplicate ``esphome:`` key (which would
    produce an invalid config that ESPHome rejects with a
    confusing duplicate-key error), reject up-front so callers
    can surface a real "switch to block style" message.
    """
    yaml = "esphome: { name: kitchen }\nesp32:\n  variant: ESP32\n"
    with pytest.raises(YamlUpsertNotSupportedError, match=r"flow-style|block style"):
        upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Lamp")


def test_upsert_yaml_leaf_rejects_tagged_value_at_block_header() -> None:
    """``esphome: !include packaged.yaml`` also raises.

    The block header has a tagged value rather than a nested
    block — the walker has nothing to walk into, and prepending
    a sibling ``esphome:`` would duplicate the key.
    """
    yaml = "esphome: !include packaged.yaml\nesp32:\n  variant: ESP32\n"
    with pytest.raises(YamlUpsertNotSupportedError):
        upsert_yaml_leaf_under_top_block(yaml, "esphome", "friendly_name", "Lamp")


# ---------------------------------------------------------------------------
# read_yaml_scalar
# ---------------------------------------------------------------------------


def test_read_yaml_scalar_returns_raw_value_with_quotes_intact() -> None:
    """``read_yaml_scalar`` returns what the rewrite transform would see."""
    yaml = 'esphome:\n  name: "kitchen"  # primary\n'
    assert read_yaml_scalar(yaml, ("esphome", "name")) == '"kitchen"'


def test_read_yaml_scalar_returns_none_when_path_missing() -> None:
    """Missing path → ``None``."""
    yaml = "esphome:\n  name: kitchen\n"
    assert read_yaml_scalar(yaml, ("api", "encryption", "key")) is None


def test_read_yaml_scalar_returns_empty_string_for_empty_value() -> None:
    """Empty scalar → ``""`` (distinguishable from missing path's ``None``)."""
    yaml = "esphome:\n  name: \n"
    assert read_yaml_scalar(yaml, ("esphome", "name")) == ""


def test_read_yaml_scalar_returns_empty_for_comment_only_leaf() -> None:
    """``name: # placeholder`` reads as empty, not as ``"# placeholder"``."""
    yaml = "esphome:\n  name: # placeholder\n"
    assert read_yaml_scalar(yaml, ("esphome", "name")) == ""


# ---------------------------------------------------------------------------
# rewrite_yaml_scalar (the generic walker)
# ---------------------------------------------------------------------------


def test_rewrite_yaml_scalar_rejects_off_path_nested_match() -> None:
    """A leaf at the right depth but wrong ancestor chain doesn't match.

    Pin the soundness fix for an earlier bug: tracking only on-path
    ancestors meant a YAML like ``api: {something: {encryption:
    {key: ...}}}`` would falsely satisfy the path
    ``("api", "encryption", "key")`` because ``something`` was
    invisible to the ancestor check. The walker now pushes every
    mapping key — on-path or not — so off-path branches show up in
    the ancestor chain and the comparison fails correctly.
    """
    yaml = (
        "api:\n"
        "  something:\n"
        "    encryption:\n"
        '      key: "off-path-value"\n'
        "  encryption:\n"
        '    key: "real-value"\n'
    )
    out = rewrite_yaml_scalar(yaml, ("api", "encryption", "key"), lambda _raw: '"new-key"')
    # Off-path leaf untouched.
    assert '      key: "off-path-value"' in out
    # On-path leaf rewritten.
    assert '    key: "new-key"' in out
    assert "real-value" not in out


def test_rewrite_yaml_scalar_walks_arbitrary_path() -> None:
    """The walker locates a leaf at any nested path the caller supplies.

    Pin that the abstraction isn't hardcoded to the three known
    callers — a future caller (``logger:`` log levels,
    ``substitutions:`` keys, etc.) gets the same machinery for free.
    """
    yaml = "logger:\n  level: INFO\n  logs:\n    api: WARN\n    wifi: VERBOSE\n"
    out = rewrite_yaml_scalar(yaml, ("logger", "logs", "api"), lambda _: "DEBUG")
    assert "    api: DEBUG\n" in out
    # Sibling untouched.
    assert "    wifi: VERBOSE\n" in out


def test_rewrite_yaml_scalar_transform_returning_none_is_a_noop() -> None:
    """A transform that returns ``None`` leaves the file unchanged.

    Callers signal "matched but don't rewrite" (e.g. the encryption
    key path skips ``!secret`` indirections) by returning ``None``;
    the helper must round-trip the input verbatim then.
    """
    yaml = "esphome:\n  name: kitchen\n"
    assert rewrite_yaml_scalar(yaml, ("esphome", "name"), lambda _: None) == yaml


def test_rewrite_yaml_scalar_ignores_lookalike_at_wrong_path() -> None:
    """A leaf key that matches name but lives elsewhere stays put.

    ``name:`` under ``wifi:`` shares the leaf key with
    ``esphome.name`` — the path-aware walker must reject the wrong
    parent chain.
    """
    yaml = "esphome:\n  name: kitchen\nwifi:\n  name: home_network\n"
    out = rewrite_yaml_scalar(yaml, ("esphome", "name"), lambda _: "kitchen-2")
    assert "  name: kitchen-2\n" in out
    assert "  name: home_network\n" in out  # untouched


def test_rewrite_yaml_scalar_only_rewrites_first_match() -> None:
    """Pathological YAMLs with two sibling keys: only the first is touched.

    Well-formed configs declare each path once, but the helper's
    behaviour on duplicates is documented as "first match wins" so
    callers don't have to defend against it.
    """
    yaml = "esphome:\n  name: first\n  name: second\n"
    out = rewrite_yaml_scalar(yaml, ("esphome", "name"), lambda _: "renamed")
    assert "  name: renamed\n" in out
    # Second occurrence stays put (and would still be rejected by
    # ESPHome at compile time — the helper doesn't fix duplicate
    # keys, just doesn't make them worse).
    assert "  name: second\n" in out


def test_rewrite_yaml_scalar_preserves_comment_only_leaf() -> None:
    """A value-less leaf with a trailing comment keeps the comment after rewrite."""
    before = "esphome:\n  name: # placeholder\n"
    after = rewrite_yaml_scalar(before, ("esphome", "name"), lambda _raw: "kitchen")
    assert after == "esphome:\n  name: kitchen # placeholder\n"


def test_rewrite_yaml_scalar_empty_path_is_a_noop() -> None:
    """An empty path is meaningless; helper returns the input unchanged."""
    yaml = "esphome:\n  name: kitchen\n"
    assert rewrite_yaml_scalar(yaml, (), lambda _: "x") == yaml


def test_rewrite_yaml_scalar_skips_list_items() -> None:
    """A ``- key: value`` line under a mapping doesn't satisfy the path.

    The walker only matches plain mapping nesting — the path
    ``("sensor", "name")`` shouldn't accidentally rewrite a sensor
    list-item's ``name:``. (List support would land as a separate
    feature with explicit semantics.)
    """
    yaml = "sensor:\n  - platform: dht\n    name: first\n"
    out = rewrite_yaml_scalar(yaml, ("sensor", "name"), lambda _: "x")
    assert out == yaml


def test_rewrite_yaml_scalar_pops_deeper_frames_at_next_list_item() -> None:
    """A list-item line at indent N pops every frame at indent ≥ N.

    Pin the inner-pop branch in the list-item handling: when the
    walker encounters a second list item at the same indent as the
    first, the first item's contents (which got pushed onto the
    stack as ``(indent, key)`` frames) must be popped so the new
    item starts with a clean ancestor chain. Without this, a leaf
    inside the second item would inherit the previous item's
    keys in its ancestor check.
    """
    # Two ``sensor:`` list items. After processing item 1's
    # ``name: first``, the stack has ``[(0,"sensor"), (2,"-list-"),
    # (4,"name")]``. The next item's ``- platform: bme280`` at
    # indent 2 must pop both ``name`` and the previous list frame
    # before pushing a fresh list frame.
    yaml = "sensor:\n  - platform: dht\n    name: first\n  - platform: bme280\n    name: second\n"
    captured: list[str] = []

    def _capture(raw: str) -> str | None:
        captured.append(raw)
        return None

    # Path that doesn't match anything walks the whole document
    # without an early return — exercises the full pop-then-push
    # sequence at every list item without short-circuiting on the
    # first match.
    rewrite_yaml_scalar(yaml, ("never", "matches"), _capture)
    assert captured == []  # no leaf at the off-path target


def test_rewrite_yaml_scalar_skips_block_scalar_continuation_lines() -> None:
    """Lines inside a ``|`` block scalar don't match the mapping-key regex.

    Pin the ``not m: continue`` branch: a block scalar's
    continuation lines (like ``multi-line content`` here) don't
    satisfy ``_MAPPING_KEY_LINE``'s anchor and aren't list items
    either, so the walker just skips them without touching the
    stack. The leaf at the right path further down should still
    match correctly.
    """
    yaml = (
        "esphome:\n"
        "  name: kitchen\n"
        "  comment: |\n"
        "    multi-line description\n"
        "    spanning two lines\n"
        "wifi:\n"
        "  ssid: home\n"
    )
    out = rewrite_yaml_scalar(yaml, ("wifi", "ssid"), lambda _: "renamed")
    assert "  ssid: renamed\n" in out
    # Block scalar contents are pure text — must not be modified.
    assert "    multi-line description\n" in out
    assert "    spanning two lines\n" in out


def test_rewrite_yaml_scalar_honours_hash_inside_quoted_value() -> None:
    r"""A ``#`` inside a quoted scalar is part of the value, not a comment.

    Earlier draft used ``re.compile(r"^(.*?)(\s+#.*)?$")`` which
    splits at the first ``\s+#`` regardless of quote state.
    Reading ``friendly_name: "Bedroom #2"`` would then yield raw
    value ``"Bedroom`` (truncated) and any subsequent rewrite
    would corrupt the line. Pin that the splitter walks through
    quoted strings without splitting.
    """
    captured: list[str] = []

    def _capture(raw: str) -> str | None:
        captured.append(raw)
        return None

    rewrite_yaml_scalar(
        'esphome:\n  friendly_name: "Bedroom #2"  # the bedroom\n',
        ("esphome", "friendly_name"),
        _capture,
    )
    assert captured == ['"Bedroom #2"']


def test_rewrite_yaml_scalar_honours_double_quoted_backslash_escape() -> None:
    r"""``\"`` inside a double-quoted scalar doesn't end the quote.

    Our own ``_quote`` emits ``\"`` for friendly names that
    contain a literal ``"`` (``Lamp "Bright"`` → ``"Lamp
    \"Bright\""``). On a *re*-clone of such a value, the splitter
    needs to skip the escape body so the inner ``"`` doesn't read
    as the closer and a later ``\s+#`` doesn't get treated as a
    trailing comment.
    """
    captured: list[str] = []

    def _capture(raw: str) -> str | None:
        captured.append(raw)
        return None

    rewrite_yaml_scalar(
        # The full quoted value includes ``\"`` escapes around
        # ``Bright`` and an embedded ``#`` after the closing
        # quote-escape. Without escape handling the splitter would
        # exit quote mode at the first ``\"``, treat ``Bright`` as
        # plain text, and split at `` #`` — corrupting the value.
        'esphome:\n  friendly_name: "Lamp \\"Bright\\" #2"  # tag\n',
        ("esphome", "friendly_name"),
        _capture,
    )
    assert captured == ['"Lamp \\"Bright\\" #2"']


def test_rewrite_yaml_scalar_honours_single_quoted_doubled_quote_escape() -> None:
    """``''`` inside a single-quoted scalar is a literal quote, not the closer.

    YAML's single-quote escape is ``''`` (doubled). The splitter
    must recognise the doubled-quote pattern as "stay in the
    string" rather than treating the first quote as the closer.
    """
    captured: list[str] = []

    def _capture(raw: str) -> str | None:
        captured.append(raw)
        return None

    rewrite_yaml_scalar(
        "esphome:\n  friendly_name: 'Bob''s Lamp #1'  # primary\n",
        ("esphome", "friendly_name"),
        _capture,
    )
    assert captured == ["'Bob''s Lamp #1'"]


def test_rewrite_yaml_scalar_preserves_quoted_value_on_rewrite_with_trailing_comment() -> None:
    """Rewriting a quoted-with-hash value preserves the trailing comment.

    Pin the round-trip: the value ``"Bedroom #2"`` reads back as
    a single quoted scalar, gets rewritten to a new quoted
    scalar, and the trailing ``# the bedroom`` comment survives.
    """
    yaml = 'esphome:\n  friendly_name: "Bedroom #2"  # the bedroom\n'
    out = rewrite_yaml_scalar(
        yaml,
        ("esphome", "friendly_name"),
        lambda _raw: '"Bedroom #3"',
    )
    assert out == 'esphome:\n  friendly_name: "Bedroom #3"  # the bedroom\n'


def test_rewrite_yaml_scalar_passes_raw_value_to_transform() -> None:
    """Transform sees the value with quotes intact, comment stripped.

    ``rewrite_api_encryption_key`` relies on the
    ``!secret`` / ``${`` prefix check working on the raw value;
    if the helper stripped quotes for the transform, ``!secret``
    would still parse but a future caller looking for a quoted
    sentinel would fail. Pin the contract.
    """
    seen: list[str] = []

    def _capture(raw: str) -> str | None:
        seen.append(raw)
        return None

    rewrite_yaml_scalar(
        'esphome:\n  name: "kitchen"  # primary device\n',
        ("esphome", "name"),
        _capture,
    )
    assert seen == ['"kitchen"']


# ---------------------------------------------------------------------------
# _safe_yaml_scalar / _strip_yaml_quotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Plain identifiers / slugs round-trip unquoted.
        ("Kitchen", "Kitchen"),
        ("my-device", "my-device"),
        ("acfloatmonitor32", "acfloatmonitor32"),
        # Embedded sequences that flip a plain scalar into something
        # else MUST be quoted: ``# comment`` (the value would become
        # a comment) and ``: `` (would split into a key/value pair).
        ("Bedroom #2", '"Bedroom #2"'),
        ("Lamp: Bedroom", '"Lamp: Bedroom"'),
        # Leading indicator characters force quoting.
        ("!escaped", '"!escaped"'),
        ("- danger", '"- danger"'),
        ("@host", '"@host"'),
        # Trailing colon would parse as a key with empty value.
        ("Kitchen:", '"Kitchen:"'),
        # Reserved bool / null plain scalars must be quoted to stay strings.
        ("yes", '"yes"'),
        ("Off", '"Off"'),
        ("null", '"null"'),
        ("", '""'),
        # Embedded quotes / backslashes ARE valid in plain scalars
        # (YAML parses them as literal characters). We don't quote
        # for them; only leading indicators / comment markers /
        # key-value splits force quoting.
        ('Lamp "Bright"', 'Lamp "Bright"'),
        ("path\\sub", "path\\sub"),
        # Newlines / tabs become escape sequences inside quotes.
        ("line1\nline2", '"line1\\nline2"'),
    ],
)
def test_safe_yaml_scalar(value: str, expected: str) -> None:
    """Pin the plain-vs-quoted rendering decisions on the user-facing values.

    ``rewrite_friendly_name`` and ``rewrite_name_or_substitution``
    accept arbitrary strings — including ones a YAML parser would
    interpret as comments / structure markers / reserved words.
    Without this normalisation a friendly name like ``Bedroom #2``
    would round-trip as ``Bedroom`` (everything after `` #`` becomes
    a comment) and ``yes`` would parse back as a boolean.
    """
    assert _safe_yaml_scalar(value) == expected


def test_plain_fast_path_is_strict_subset_and_round_trips() -> None:
    """The identifier fast path never disagrees with PyYAML, and output round-trips.

    Fuzzes a deterministic corpus over indicators, control chars,
    digits, whitespace and unicode. ``_plain_is_fast_safe`` returning
    True MUST imply ``_plain_is_safe`` (the PyYAML decision) is also
    True; a violation would emit a value unquoted that the parser
    rejects or rewrites. Every ``_safe_yaml_scalar`` result must also
    survive a re-parse; the round trip is batched into one document per
    chunk so the fuzz stays fast.
    """
    rng = random.Random(20260604)  # noqa: S311, fuzz corpus, not cryptographic
    alphabet = string.ascii_letters + string.digits + "_-./ :#!%@&*,[]{}\"'~+\t\n\r\x00\x0b\x7fé好"

    def _rand() -> str:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))

    for _ in range(40):
        chunk = [_rand() for _ in range(500)]
        lines = []
        for i, value in enumerate(chunk):
            if _plain_is_fast_safe(value):
                assert _plain_is_safe(value), f"fast path kept {value!r} plain but PyYAML quotes it"
            lines.append(f"k{i}: {_safe_yaml_scalar(value)}")
        parsed = yaml.safe_load("\n".join(lines))
        for i, value in enumerate(chunk):
            assert parsed[f"k{i}"] == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("kitchen", "kitchen"),
        ('"kitchen"', "kitchen"),
        ("'kitchen'", "kitchen"),
        ("  kitchen  ", "kitchen"),
        ('  "kitchen"  ', "kitchen"),
        # Mismatched / single quote is not stripped.
        ('"kitchen', '"kitchen'),
        ("'kitchen\"", "'kitchen\""),
    ],
)
def test_strip_yaml_quotes(value: str, expected: str) -> None:
    """Pin the quote-strip helper's accept / reject contract.

    Used by ``parse_substitution_ref`` and the rename gate to
    compare an inline value against an unquoted target without
    crashing on unquoted values or eating partial quotes.
    """
    assert _strip_yaml_quotes(value) == expected


# ---------------------------------------------------------------------------
# rewrite_api_encryption_key
# ---------------------------------------------------------------------------


def test_rewrite_api_encryption_key_swaps_literal_value() -> None:
    """Literal key under ``api: -> encryption:`` gets replaced.

    The replacement is rendered double-quoted so a base64 value
    that happens to start with a YAML special character
    (``!``/``%``/``@``/``-``/``?``/``&``/``*``) parses cleanly.
    """
    yaml = 'api:\n  encryption:\n    key: "OLDKEYBASE64=="\n'
    out = rewrite_api_encryption_key(yaml, "NEWKEYBASE64==")
    assert out == ('api:\n  encryption:\n    key: "NEWKEYBASE64=="\n')


def test_rewrite_api_encryption_key_no_api_block_returns_input() -> None:
    """No ``api:`` block at all → no-op."""
    yaml = "esphome:\n  name: kitchen\nwifi:\n  ssid: home\n"
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_no_encryption_block_returns_input() -> None:
    """``api:`` exists but plaintext (no encryption block) → no-op."""
    yaml = "api:\n  password: hunter2\n"
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_skips_secret_indirection() -> None:
    """``key: !secret api_key`` stays untouched.

    The indirection points at content the clone shares with its
    source on disk. Replacing the indirection name with a literal
    here would silently desync the rendered config from
    ``secrets.yaml``.
    """
    yaml = "api:\n  encryption:\n    key: !secret api_key\n"
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_skips_substitution_indirection() -> None:
    """``key: ${api_key}`` stays untouched, same reasoning as ``!secret``."""
    yaml = "api:\n  encryption:\n    key: ${api_key}\n"
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_skips_quoted_substitution_indirection() -> None:
    """``key: "${api_key}"`` stays untouched.

    Same intent as the unquoted ``${...}`` case — the value points
    at a substitution defined elsewhere, so swapping it for a
    fresh literal would silently desync the rendered config.
    Earlier versions only matched the ``${`` prefix on the raw
    quoted value (``"${api_key}"`` doesn't start with ``${``) and
    would falsely overwrite. Strip quotes before the prefix check.
    """
    yaml = 'api:\n  encryption:\n    key: "${api_key}"\n'
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_skips_quoted_secret_indirection() -> None:
    """``key: "!secret api_key"`` stays untouched.

    Same fix as the substitution case — a quoted ``!secret``
    indirection is unusual but valid YAML, and rewriting it would
    desync from the secrets file the source pointed at.
    """
    yaml = 'api:\n  encryption:\n    key: "!secret api_key"\n'
    assert rewrite_api_encryption_key(yaml, "NEW==") == yaml


def test_rewrite_api_encryption_key_ignores_lookalike_outside_encryption() -> None:
    """A ``key:`` under another block (remote_receiver button code) doesn't flip."""
    yaml = (
        "api:\n"
        "  encryption:\n"
        '    key: "OLDKEYBASE64=="\n'
        "remote_receiver:\n"
        "  - platform: rc_switch\n"
        "    key: 0xABCDEF12\n"
    )
    out = rewrite_api_encryption_key(yaml, "NEW==")
    assert 'key: "NEW=="' in out
    assert "key: 0xABCDEF12" in out


def test_rewrite_api_encryption_key_handles_block_with_comments() -> None:
    """Comment-only / blank lines inside ``api: encryption:`` don't confuse the walker."""
    yaml = (
        "api:\n"
        "  # encryption block — generated by the wizard\n"
        "  encryption:\n"
        "\n"
        '    key: "OLDKEYBASE64=="  # do not share\n'
    )
    out = rewrite_api_encryption_key(yaml, "NEW==")
    assert 'key: "NEW=="  # do not share' in out


def test_generate_api_encryption_key_yields_distinct_base64_values() -> None:
    """Two consecutive calls must return different keys (cryptographic randomness)."""
    a = generate_api_encryption_key()
    b = generate_api_encryption_key()
    assert a != b
    # 32 raw bytes → 44 base64 chars including padding.
    assert len(a) == 44
    assert len(b) == 44


# ---------------------------------------------------------------------------
# merge_component_yaml
# ---------------------------------------------------------------------------


def test_merge_component_yaml_appends_first_platform_block() -> None:
    """First sensor in a YAML with no ``sensor:`` section gets appended.

    The splice helper returns ``None`` when there's no existing
    domain block, and the caller falls through to ``_append_block``.
    """
    component = _component(component_id="sensor.dht", category=ComponentCategory.SENSOR)
    fields: dict[str, Any] = {"pin": "GPIO4", "name": "Temp"}

    result = merge_component_yaml("esphome:\n  name: kitchen\n", component, fields)

    # New top-level ``sensor:`` block follows the existing esphome block.
    assert "esphome:\n  name: kitchen\n" in result
    assert "sensor:\n  - platform: dht\n" in result


def test_merge_component_yaml_splices_second_sensor_into_existing_block() -> None:
    """Two sensors land under one ``sensor:`` block, not two.

    Without the splice, repeated add-component calls on the same
    domain would produce duplicate top-level keys. ESPHome would
    reject the second with a duplicate-key error.
    """
    component = _component(component_id="sensor.dht", category=ComponentCategory.SENSOR)
    fields: dict[str, Any] = {"pin": "GPIO4", "name": "Inside"}

    existing = "esphome:\n  name: kitchen\n\nsensor:\n  - platform: bme280\n    address: 0x76\n"
    result = merge_component_yaml(existing, component, fields)

    # Only one ``sensor:`` block — the new entry is a sibling list item.
    assert result.count("sensor:\n") == 1
    assert "  - platform: bme280" in result
    assert "  - platform: dht" in result
    # New item appears after the existing one (insertion order preserved).
    assert result.index("- platform: bme280") < result.index("- platform: dht")


def test_merge_component_yaml_preserves_following_blocks_after_splice() -> None:
    """Blocks after the spliced ``sensor:`` block survive unmoved.

    The walk-forward logic stops at the first column-zero
    alphabetic line; this test guards against a regression that
    would slurp the trailing block into the splice.
    """
    component = _component(component_id="sensor.dht", category=ComponentCategory.SENSOR)
    fields: dict[str, Any] = {"pin": "GPIO4", "name": "Inside"}

    existing = (
        "esphome:\n  name: kitchen\n\n"
        "sensor:\n  - platform: bme280\n    address: 0x76\n\n"
        "logger:\n  level: DEBUG\n"
    )
    result = merge_component_yaml(existing, component, fields)
    assert result.endswith("logger:\n  level: DEBUG\n")
    assert result.count("sensor:\n") == 1


def test_merge_component_yaml_appends_non_platform_component() -> None:
    """A non-platform component (e.g. ``i2c``) emits as a top-level mapping.

    These don't carry a ``platform`` key and aren't in
    ``_ENTITY_CATEGORIES``, so they always fall through to the
    plain-append path.
    """
    component = _component(component_id="i2c", category=ComponentCategory.BUS)
    fields: dict[str, Any] = {"sda": "GPIO21", "scl": "GPIO22"}

    existing = "esphome:\n  name: kitchen\n"
    result = merge_component_yaml(existing, component, fields)

    assert "i2c:\n  sda: GPIO21\n  scl: GPIO22\n" in result
    # Existing block wasn't touched.
    assert result.startswith("esphome:\n  name: kitchen\n")


def test_merge_component_yaml_splice_handles_trailing_blank_lines() -> None:
    """Splice keeps the trailing blank line(s) before the next block.

    ``_splice_into_domain_block`` trims trailing blank lines from
    the domain block before inserting, then re-emits them. Without
    that, every splice would either eat blank lines or pile new
    ones on, drifting formatting on every add.
    """
    component = _component(component_id="sensor.dht", category=ComponentCategory.SENSOR)
    fields: dict[str, Any] = {"pin": "GPIO4", "name": "Inside"}

    existing = (
        "esphome:\n  name: kitchen\n\n"
        "sensor:\n  - platform: bme280\n    address: 0x76\n\n\n"
        "logger:\n"
    )
    result = merge_component_yaml(existing, component, fields)
    # Both sensors land inside the block, blank lines before the
    # next top-level block are preserved (no run-on into ``logger:``).
    sensor_block_end = result.index("logger:")
    sensor_block = result[:sensor_block_end]
    assert "- platform: bme280" in sensor_block
    assert "- platform: dht" in sensor_block


def test_merge_component_yaml_accepts_incomplete_draft() -> None:
    """A syntactically broken draft is appended-merged, not rejected."""
    component = _component(component_id="i2c", category=ComponentCategory.BUS)
    fields: dict[str, Any] = {"sda": "GPIO21", "scl": "GPIO22"}

    # Unterminated quote + dangling key — invalid YAML mid-edit.
    broken = 'esphome:\n  name: "kitch\nsensor:\n  - platform:\n'
    result = merge_component_yaml(broken, component, fields)

    assert broken in result
    assert "i2c:\n  sda: GPIO21\n  scl: GPIO22\n" in result


@pytest.mark.parametrize("category", [ComponentCategory.OUTPUT, ComponentCategory.SWITCH])
def test_merge_component_yaml_splices_other_platform_categories(
    category: ComponentCategory,
) -> None:
    """Splice works for every category in ``_ENTITY_CATEGORIES``.

    Pin a couple of representative platform domains beyond
    ``sensor`` so a regression that hardcodes the splice to one
    domain shows up in CI. Add new ones if a future bug points at
    a specific category.
    """
    component_id = f"{category.value}.gpio"
    component = _component(component_id=component_id, category=category)
    fields: dict[str, Any] = {"pin": "GPIO4", "id": ""}

    existing = (
        f"esphome:\n  name: kitchen\n\n{category.value}:\n  - platform: ledc\n    pin: GPIO5\n"
    )
    result = merge_component_yaml(existing, component, fields)
    assert result.count(f"{category.value}:\n") == 1
    assert "- platform: ledc" in result
    assert "- platform: gpio" in result


_RTTTL_MAPPING = "rtttl:\n  id: rtttl_1\n  output: buzz\n"
_RTTTL_LIST = "rtttl:\n  - id: rtttl_1\n    output: buzz\n  - id: rtttl_2\n    output: buzz\n"


@pytest.mark.parametrize(
    ("existing", "new_id", "expected_ids", "expected_suffix"),
    [
        pytest.param(
            "esphome:\n  name: kitchen\n",
            "rtttl_1",
            ["rtttl_1"],
            "rtttl:\n  - id: rtttl_1\n    output: buzz\n",
            id="first_add_emits_list",
        ),
        pytest.param(
            f"esphome:\n  name: kitchen\n\n{_RTTTL_MAPPING}",
            "rtttl_2",
            ["rtttl_1", "rtttl_2"],
            None,
            id="second_add_converts_mapping_to_list",
        ),
        pytest.param(
            f"esphome:\n  name: kitchen\n\n{_RTTTL_LIST}",
            "rtttl_3",
            ["rtttl_1", "rtttl_2", "rtttl_3"],
            None,
            id="third_add_splices_into_list",
        ),
        pytest.param(
            f"esphome:\n  name: kitchen\n\n{_RTTTL_MAPPING}\nlogger:\n  level: DEBUG\n",
            "rtttl_2",
            ["rtttl_1", "rtttl_2"],
            "logger:\n  level: DEBUG\n",
            id="trailing_block_survives_conversion",
        ),
    ],
)
def test_merge_component_yaml_multi_conf(
    existing: str,
    new_id: str,
    expected_ids: list[str],
    expected_suffix: str | None,
) -> None:
    """Repeated adds of a ``multi_conf`` component splice under one block."""
    component = _component(component_id="rtttl", category=ComponentCategory.MISC, multi_conf=True)
    result = merge_component_yaml(existing, component, {"id": new_id, "output": "buzz"})

    assert result.count("rtttl:\n") == 1
    positions = [result.index(stem) for stem in expected_ids]
    assert positions == sorted(positions)
    if len(expected_ids) > 1:
        for stem in expected_ids:
            assert f"- id: {stem}\n" in result
    if expected_suffix is not None:
        assert expected_suffix in result


def test_merge_component_yaml_singleton_without_multi_conf_falls_through() -> None:
    """A ``multi_conf=False`` non-platform component skips the splice."""
    component = _component(component_id="wifi", category=ComponentCategory.MISC, multi_conf=False)
    result = merge_component_yaml(
        "esphome:\n  name: kitchen\n\nwifi:\n  ssid: other\n",
        component,
        {"ssid": "home"},
    )

    assert result.count("wifi:\n") == 2


def test_splice_into_multi_conf_block_rejects_mismatched_header() -> None:
    """A *block* whose header doesn't match ``comp_id`` returns ``None``."""
    existing = "rtttl:\n  id: rtttl_1\n  output: buzz\n"
    block = "wifi:\n  ssid: home\n"
    assert _splice_into_multi_conf_block(existing, "rtttl", block) is None


def test_normalize_multi_conf_block_skips_comments_above_list_form() -> None:
    """A leading ``#`` comment above an existing ``- `` item doesn't trigger a rewrite."""
    component = _component(component_id="rtttl", category=ComponentCategory.MISC, multi_conf=True)
    existing = "rtttl:\n  # buzzers for the kitchen\n  - id: rtttl_1\n    output: buzz\n"
    result = merge_component_yaml(existing, component, {"id": "rtttl_2", "output": "buzz"})
    assert "  # buzzers for the kitchen" in result
    assert result.count("rtttl:\n") == 1
    assert "  - id: rtttl_1" in result
    assert "  - id: rtttl_2" in result


def test_normalize_multi_conf_block_treats_bare_dash_as_list_form() -> None:
    """A body whose head line is a bare ``-`` is already list-form."""
    component = _component(component_id="rtttl", category=ComponentCategory.MISC, multi_conf=True)
    existing = "rtttl:\n  -\n    id: rtttl_1\n    output: buzz\n"
    result = merge_component_yaml(existing, component, {"id": "rtttl_2", "output": "buzz"})
    assert result.count("rtttl:\n") == 1
    assert "  -\n    id: rtttl_1" in result
    assert "  - id: rtttl_2\n" in result


def test_merge_component_yaml_multi_conf_preserves_leading_comment() -> None:
    """A leading ``#`` comment in the mapping body keeps its key as the list head."""
    component = _component(component_id="rtttl", category=ComponentCategory.MISC, multi_conf=True)
    existing = "rtttl:\n  # buzzer notes\n  id: rtttl_1\n  output: buzz\n"
    result = merge_component_yaml(existing, component, {"id": "rtttl_2", "output": "buzz"})

    assert "# buzzer notes" in result
    assert "  - # " not in result
    assert "  - id: rtttl_1\n" in result
    assert "  - id: rtttl_2\n" in result


def test_mapping_body_to_list_item_preserves_blank_lines() -> None:
    """Blank lines inside the mapping body survive the list conversion.

    Vertical spacing the user put between fields shouldn't collapse
    when the block flips to list form.
    """
    body = ["  id: rtttl_1", "", "  output: buzz"]
    assert _mapping_body_to_list_item(body) == [
        "  - id: rtttl_1",
        "",
        "    output: buzz",
    ]


def test_generate_component_yaml_multi_conf_emits_list_form() -> None:
    """A ``multi_conf`` component's first entry renders as a ``- `` list item."""
    component = _component(component_id="globals", category=ComponentCategory.MISC, multi_conf=True)
    out = generate_component_yaml(component, {"id": "g1", "type": "int", "initial_value": "0"})
    assert out.startswith("globals:\n  - id: g1\n")
    assert "    type: int" in out
    assert "    initial_value:" in out


def test_generate_component_yaml_singleton_emits_mapping_form() -> None:
    """A non-``multi_conf`` singleton stays a bare mapping (no ``- ``)."""
    component = _component(component_id="wifi", category=ComponentCategory.MISC, multi_conf=False)
    out = generate_component_yaml(component, {"ssid": "home"})
    assert out == "wifi:\n  ssid: home"


# ---------------------------------------------------------------------------
# generate_component_yaml — value formatting
# ---------------------------------------------------------------------------
# These tests pin the YAML literal that each Python value type
# renders to, by driving ``generate_component_yaml`` end-to-end.
# Going through the public function (rather than the private
# ``_format_yaml_value`` helper) anchors the contract on what the
# frontend actually sees in the generated block — a future refactor
# that swaps the internal helper out for orjson / PyYAML / a real
# emitter shouldn't need to rewrite any of these.


def test_generate_component_yaml_emits_bool_true_lowercase() -> None:
    """``True`` renders as bare ``true`` — ESPHome's canonical bool literal.

    YAML 1.2 also accepts ``True`` / ``TRUE``; ESPHome itself only
    emits the lowercase forms, so pin that. The Python ``str(True)``
    repr would write ``True`` and disagree with every other emitter
    in the toolchain.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"enabled": True})
    assert "  enabled: true" in out


def test_generate_component_yaml_emits_bool_false_lowercase() -> None:
    """``False`` renders as bare ``false``, not ``False``.

    Pinned separately from ``True`` because the ``_format_yaml_value``
    branch is a single ternary — a regression that swapped the
    branches (``"false" if value else "true"``) would still pass a
    True-only test.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"enabled": False})
    assert "  enabled: false" in out


@pytest.mark.parametrize("keyword", ["true", "false", "null", "yes", "no", "on", "off", "~", ""])
def test_generate_component_yaml_quotes_yaml_keyword_strings(keyword: str) -> None:
    """Strings whose value is a YAML 1.1 boolean keyword get quoted.

    Without quoting, ``state: on`` parses back as ``True``, not the
    string ``"on"`` — the classic YAML 1.1 footgun. ESPHome accepts
    these as enum values for several components (e.g. light states),
    so the helper has to disambiguate. ``null`` / ``yes`` / ``no``
    follow the same logic; pin every keyword in the helper's
    allowlist so a regression that drops one shows up here. ``~`` and
    the empty string round-trip as YAML null, same class of bug as
    #901 for any field validating as a strict string.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"state": keyword})
    assert f'  state: "{keyword}"' in out


@pytest.mark.parametrize(
    "value",
    ["foo:bar", "foo #bar", "!secret api_key", "%"],
    ids=["colon", "hash", "tag-prefix", "percent"],
)
def test_generate_component_yaml_quotes_strings_with_special_chars(value: str) -> None:
    """Strings PyYAML can't emit plain — ``:`` / `` #`` / leading ``!`` / ``%`` — get quoted.

    ``:`` opens a mapping value, `` #`` opens a comment, ``!``
    introduces a tag. ``%`` is the regression case from issue #675 —
    a humidity sensor's ``unit_of_measurement: "%"`` default was
    being emitted unquoted as ``unit_of_measurement: %`` and
    crashing the downstream ``esphome`` load (``%`` is a YAML
    indicator character reserved for directives).
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": value})
    assert f'  v: "{value}"' in out


def test_generate_component_yaml_emits_plain_string_unquoted() -> None:
    """Plain strings render unquoted — the helper only quotes when needed.

    A regression that "just always quotes" produces correct YAML but
    reads nothing like what a human writes by hand and makes diffs
    against pre-existing files unreviewable. Pin the bare-pin case
    (``GPIO4``) so the quoting decision stays selective.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"pin": "GPIO4"})
    assert "  pin: GPIO4" in out
    assert '"GPIO4"' not in out


@pytest.mark.parametrize("value", [42, 3.14])
def test_generate_component_yaml_emits_numeric_value_via_str(value: float) -> None:
    """Numbers render via ``str()`` — ints stay ints, floats keep the dot.

    A regression that quoted numbers (``priority: "0"``) changes the
    YAML type — ESPHome would either reject the cast or accept the
    string and silently re-parse it, depending on the field. Pin
    both shapes so the unquoted-numeric path is locked in.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": value})
    assert f"  v: {value}" in out
    assert f'"{value}"' not in out


@pytest.mark.parametrize(
    "value",
    ["100", "-5", "+5", "3.14", "0xFF", "0b101", ".inf", ".nan", "2024-01-01"],
    ids=["int", "neg-int", "pos-int", "float", "hex", "binary", "inf", "nan", "date"],
)
def test_generate_component_yaml_quotes_strings_that_reparse_as_non_string(
    value: str,
) -> None:
    """Strings whose YAML re-parse isn't a string get quoted (issue #901)."""
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": value})
    assert f'  v: "{value}"' in out


@pytest.mark.parametrize(
    "value",
    ['"Hello"', "'single'", "with:colon", "has#hash", '"quote\\and\\slash"'],
    ids=[
        "cpp-literal",
        "leading-squote",
        "embedded-colon",
        "embedded-hash",
        "leading-quote-backslash",
    ],
)
def test_generate_component_yaml_quote_bearing_string_round_trips(value: str) -> None:
    r"""Quote/indicator-bearing scalar values survive a full re-parse (#1095).

    A globals ``initial_value`` for a ``std::string`` is a C++ literal
    (``'"Hello"'``); emitting it bare round-trips as plain ``Hello`` so
    ESPHome compiles an undeclared symbol. Embedded ``"`` / ``\`` are
    escaped via ``_quote`` rather than wrapped in invalid ``""..."``.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": value})
    assert yaml.safe_load(out)["myc"]["v"] == value


# ---------------------------------------------------------------------------
# generate_component_yaml — nested fields (_emit_field branches)
# ---------------------------------------------------------------------------


def test_generate_component_yaml_emits_nested_dict_as_indented_mapping() -> None:
    """A dict value renders as ``key:`` plus deeper-indented entries.

    The frontend submits the structure of a NESTED config-entry as a
    dict (e.g. ``scan_parameters: {duration: 90s, active: true}``).
    Pin that the helper recurses with deeper block-style indent
    rather than emitting flow style ``{...}`` — ESPHome accepts
    both, but every other tool in the codebase emits block style and
    flow would diff badly against a hand-written file.
    """
    component = _component(component_id="esp32_ble_tracker", category=ComponentCategory.MISC)
    out = generate_component_yaml(
        component,
        {"scan_parameters": {"duration": "90s", "active": True}},
    )
    assert "  scan_parameters:\n" in out
    assert "    duration: 90s" in out
    assert "    active: true" in out


def test_generate_component_yaml_recurses_through_nested_dict_indent() -> None:
    """Two-deep nesting indents twice — recursion keeps the spacing right.

    ``_emit_field`` calls itself with ``indent + "  "`` per level.
    A regression that hardcoded the indent would land second-level
    keys under the wrong parent and ESPHome would reject the file
    with a confusing column-mismatch error.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"outer": {"inner": {"leaf": "v"}}})
    assert "  outer:\n" in out
    assert "    inner:\n" in out
    assert "      leaf: v" in out


def test_generate_component_yaml_handles_mixed_dict_and_scalar_list_without_raising() -> None:
    """
    A list with a leading dict but a non-dict later element falls through.

    The list-of-dicts branch is gated on every element being a dict;
    a mixed list takes the flow-style fallback instead. Without
    the all-dict gate the second-iteration ``item.items()`` call
    would raise ``AttributeError`` and surface as a generic
    internal error to the user. Pin both the no-raise contract
    and the flow-style fallback shape (valid YAML by accident —
    the dict renders as a flow mapping via ``str(dict)`` and the
    scalar renders bare).
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"items": [{"a": 1}, "loose"]})
    assert "items: [{'a': 1}, loose]" in out


def test_generate_component_yaml_emits_list_of_dicts_as_block_sequence() -> None:
    """A list of dicts renders as ``- mapping`` block-sequence items.

    Used for fields whose value is an array of structured entries
    (e.g. ``on_...:`` automations, ``triggers:`` lists). Each item
    gets a ``-`` prefix on its first key, and continuation keys
    indent under it. Pin all four lines (two items, two keys each)
    so a regression that emits the second item without a fresh
    ``-`` prefix — collapsing the sequence into a single mapping —
    shows up here.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(
        component,
        {"items": [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]},
    )
    assert "  items:\n" in out
    assert "    - a: 1" in out
    assert "      b: x" in out
    assert "    - a: 2" in out
    assert "      b: y" in out


def test_generate_component_yaml_emits_list_of_strings_as_flow_style() -> None:
    """A list of strings renders as ``[a, b]`` flow-style, not Python repr."""
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"modes": ["heat", "cool"]})
    assert "  modes: [heat, cool]" in out


def test_generate_component_yaml_emits_list_of_booleans_lowercased() -> None:
    """
    A list of booleans renders as ``[true, false]``.

    Python ``True`` / ``False`` would be invalid YAML; the flow-style
    branch routes each element through ``_format_yaml_value`` so the
    bool→``true`` / ``false`` lowering applies inside the brackets too.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"levels": [True, False]})
    assert "  levels: [true, false]" in out


def test_generate_component_yaml_emits_list_of_ints_as_flow_style() -> None:
    """A list of ints renders as ``[1, 2, 3]`` flow-style."""
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"ids": [1, 2, 3]})
    assert "  ids: [1, 2, 3]" in out


def test_generate_component_yaml_quotes_flow_string_with_flow_indicator() -> None:
    """
    Strings carrying ``,`` / ``[`` / ``]`` / ``{`` / ``}`` get quoted inside a flow list.

    In flow context those characters are syntactically significant —
    an unquoted ``a,b`` would round-trip as two items rather than one
    string. Pin the quoting so a list whose element contains a comma
    stays one element on parse.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"items": ["a,b", "c"]})
    assert '  items: ["a,b", c]' in out


@pytest.mark.parametrize(
    "value",
    ['a,"b', "x,\\y", '{a},"q"'],
    ids=["embedded-quote", "backslash", "brace-quote"],
)
def test_generate_component_yaml_flow_list_escapes_quote_bearing_element(value: str) -> None:
    """A flow-list element carrying both a flow indicator and a quote/backslash escapes cleanly."""
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"items": [value, "c"]})
    assert yaml.safe_load(out)["myc"]["items"] == [value, "c"]


# ---------------------------------------------------------------------------
# generate_component_yaml — MAP fields with string value templates
# ---------------------------------------------------------------------------


def _component_with_string_map(key: str) -> ComponentCatalogEntry:
    """Component with one MAP entry whose value template is STRING."""
    return ComponentCatalogEntry(
        id="myc",
        name="myc",
        description="",
        category=ComponentCategory.MISC,
        config_entries=[
            ConfigEntry(
                key=key,
                type=ConfigEntryType.MAP,
                label="Map",
                config_entries=[
                    ConfigEntry(key="value", type=ConfigEntryType.STRING, label="Value"),
                ],
            ),
        ],
    )


def test_generate_component_yaml_quotes_map_string_value_that_looks_numeric() -> None:
    """A MAP string-typed value sent as ``"100"`` lands quoted (issue #901)."""
    component = _component_with_string_map("sdkconfig_options")
    out = generate_component_yaml(component, {"sdkconfig_options": {"CONFIG_FOO": "100"}})
    assert '    CONFIG_FOO: "100"' in out


def test_generate_component_yaml_coerces_map_numeric_value_to_quoted_string() -> None:
    """A MAP string-typed value sent as JSON number ``100`` lands as ``"100"``."""
    component = _component_with_string_map("sdkconfig_options")
    out = generate_component_yaml(component, {"sdkconfig_options": {"CONFIG_FOO": 100}})
    assert '    CONFIG_FOO: "100"' in out


def test_generate_component_yaml_leaves_plain_map_string_value_unquoted() -> None:
    """A MAP string value that round-trips as itself stays unquoted."""
    component = _component_with_string_map("sdkconfig_options")
    out = generate_component_yaml(component, {"sdkconfig_options": {"CONFIG_FOO": "y"}})
    assert "    CONFIG_FOO: y" in out
    assert '"y"' not in out


def test_generate_component_yaml_coerce_skips_non_map_entry() -> None:
    """Non-MAP entries pass through the coerce step untouched."""
    component = ComponentCatalogEntry(
        id="myc",
        name="myc",
        description="",
        category=ComponentCategory.MISC,
        config_entries=[
            ConfigEntry(key="label", type=ConfigEntryType.STRING, label="Label"),
        ],
    )
    out = generate_component_yaml(component, {"label": "kitchen"})
    assert "  label: kitchen" in out


def test_generate_component_yaml_coerce_skips_map_without_value_template() -> None:
    """A MAP entry without an inner ``config_entries[0]`` is left alone."""
    component = ComponentCatalogEntry(
        id="myc",
        name="myc",
        description="",
        category=ComponentCategory.MISC,
        config_entries=[
            ConfigEntry(key="opts", type=ConfigEntryType.MAP, label="Opts"),
        ],
    )
    out = generate_component_yaml(component, {"opts": {"k": 1}})
    assert "    k: 1" in out


def test_generate_component_yaml_coerce_skips_map_with_non_string_template() -> None:
    """A MAP whose value template is INTEGER preserves the int value."""
    component = ComponentCatalogEntry(
        id="myc",
        name="myc",
        description="",
        category=ComponentCategory.MISC,
        config_entries=[
            ConfigEntry(
                key="opts",
                type=ConfigEntryType.MAP,
                label="Opts",
                config_entries=[
                    ConfigEntry(key="value", type=ConfigEntryType.INTEGER, label="Value"),
                ],
            ),
        ],
    )
    out = generate_component_yaml(component, {"opts": {"k": 7}})
    assert "    k: 7" in out
    assert '"7"' not in out


def test_generate_component_yaml_coerce_skips_map_value_that_isnt_a_dict() -> None:
    """A MAP field whose value is None emits as ``null`` (coerce no-ops)."""
    component = _component_with_string_map("sdkconfig_options")
    # ``None`` would land here when the frontend submits the field
    # with an empty payload; the coerce step must not blow up and the
    # emitter renders YAML ``null`` rather than Python's ``None``.
    out = generate_component_yaml(component, {"sdkconfig_options": None})
    assert "  sdkconfig_options: null" in out


@pytest.mark.parametrize(
    ("py_value", "expected"),
    [(True, '"true"'), (False, '"false"'), (None, '"null"')],
    ids=["bool-true", "bool-false", "none"],
)
def test_generate_component_yaml_quotes_map_bool_and_none_values(
    py_value: Any, expected: str
) -> None:
    """A MAP string-typed value sent as JSON ``true`` / ``false`` / ``null``.

    Coerce canonicalises to the lowercase YAML keyword, which the
    emitter then quotes — so the round-tripped value stays a string
    rather than landing as a YAML bool / null and tripping
    ``cv.string_strict`` (same class of bug as #901, raised in PR
    review).
    """
    component = _component_with_string_map("sdkconfig_options")
    out = generate_component_yaml(component, {"sdkconfig_options": {"K": py_value}})
    assert f"    K: {expected}" in out


@pytest.mark.parametrize("variant", ["True", "TRUE", "False", "FALSE", "Null", "NULL"])
def test_generate_component_yaml_quotes_case_variant_keyword_strings(variant: str) -> None:
    """``"True"`` / ``"FALSE"`` etc. round-trip as bool/null on YAML 1.1.

    The base allowlist only carried the lowercase forms; PyYAML
    recognises every case variant the spec lists, so a Python string
    ``"True"`` (e.g. ``str(True)``) re-parsed to ``True``. Quote any
    case variant so the value survives as a string.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": variant})
    assert f'  v: "{variant}"' in out


def test_generate_component_yaml_quotes_digit_led_control_char_string() -> None:
    r"""A digit-led control-char value (``"0\t"``) is quoted and round-trips.

    Emitted bare it is invalid YAML; the renderer quotes and escapes
    it so a re-parse recovers the exact value.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"v": "0\t"})
    assert yaml.safe_load(out)["myc"]["v"] == "0\t"


# ---------------------------------------------------------------------------
# _splice_into_domain_block — defensive guards
# ---------------------------------------------------------------------------


def test_splice_rejects_block_without_matching_domain_header() -> None:
    """A block whose first line isn't ``<domain>:`` returns ``None``.

    The guard exists because ``merge_component_yaml`` could grow a
    code path that hands ``_splice_into_domain_block`` a block built
    for a different domain (e.g. category drift between catalog
    sync and the helper). Pin the rejection so the splice always
    falls through to ``_append_block`` instead of welding the wrong
    list under the wrong header.
    """
    assert _splice_into_domain_block("sensor:\n  - platform: dht\n", "switch", "logger:\n") is None


def test_splice_rejects_new_block_with_no_body() -> None:
    r"""A *new_block* that's just a header (one line, no list item) returns ``None``.

    The guard fires on ``len(block_lines) < 2`` — the splice
    needs at least one body line below the header to insert.
    Defensive against a future caller that constructs a
    block via ``"\n".join([header])`` and forgets the body;
    pin the early-return so a header-without-item splice can't
    silently corrupt the file by appending a bare ``sensor:`` to
    an existing block that already has one.
    """
    existing = "sensor:\n  - platform: dht\n"
    # ``new_block`` is just the header — exactly one line after
    # ``splitlines()``, which is the precondition for the guard.
    assert _splice_into_domain_block(existing, "sensor", "sensor:") is None


def test_splice_appends_newline_when_existing_lacks_trailing_lf() -> None:
    r"""When the existing YAML has no trailing newline, the splice adds one.

    Hand-edited YAMLs occasionally arrive without a final newline
    (some editors strip it). Without the guard, the splice would
    concatenate the new list item directly onto the previous line
    (``    address: 0x76  - platform: dht``), producing invalid
    YAML. Pin the inserted ``\n`` so the new item always lands on
    its own line.
    """
    existing = "sensor:\n  - platform: bme280\n    address: 0x76"  # no trailing newline
    out = _splice_into_domain_block(
        existing, "sensor", "sensor:\n  - platform: dht\n    pin: GPIO4"
    )
    assert out is not None
    assert "    address: 0x76\n" in out
    assert "  - platform: dht\n" in out


# ---------------------------------------------------------------------------
# _generate_id — empty-slug fallback (driven via generate_component_yaml)
# ---------------------------------------------------------------------------


def test_generate_component_yaml_id_falls_back_when_name_slug_is_empty() -> None:
    """A name made entirely of punctuation slugifies to ``""`` → bare component id.

    The slug regex collapses every non-``[a-z0-9]`` run to ``_`` then
    strips outer underscores; a name like ``":::"`` collapses to
    nothing. Without the fallback, the emitted id would be
    ``<comp>_`` (trailing underscore) — invalid as a YAML id and
    rejected by ESPHome at compile time. Pin the bare-component-id
    return so the helper degrades gracefully on punctuation-only
    names.
    """
    component = _component(component_id="hlw8012", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"id": "", "name": ":::"})
    # Auto-filled id is just the component stem — no trailing ``_``.
    assert "  id: hlw8012\n" in out
    assert "  id: hlw8012_\n" not in out
