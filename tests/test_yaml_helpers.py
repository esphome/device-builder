"""Tests for the pure ``helpers/yaml.py`` functions.

The helper module is purely string transforms — no file I/O, no
event-loop concerns — but it's easy to break in subtle ways
because the YAML it edits is unparsed text. The functions covered
here are the ones the active codepaths in
``DevicesController._manual_rename`` and ``add_component`` lean on.

What we pin:

* ``rewrite_esphome_name`` — the rename CLI's fallback path. Walks
  YAML line-by-line looking for ``name:`` *only* under the
  top-level ``esphome:`` block. Indentation and trailing comments
  must round-trip; lookalikes in other blocks (sensor names,
  Wi-Fi SSIDs) must not flip.
* ``merge_component_yaml`` — adding a second sensor / output /
  switch must splice into the existing platform block instead of
  emitting a duplicate top-level key. A non-platform component
  (one without an ``<entity>:`` domain) falls through to plain
  append. The splice algorithm preserves trailing blank lines and
  any blocks that follow.
"""

from __future__ import annotations

from typing import Any

import pytest

from esphome_device_builder.helpers.yaml import (
    _safe_yaml_scalar,
    _splice_into_domain_block,
    _strip_yaml_quotes,
    generate_api_encryption_key,
    generate_component_yaml,
    merge_component_yaml,
    parse_substitution_ref,
    read_yaml_scalar,
    rewrite_api_encryption_key,
    rewrite_esphome_name,
    rewrite_name_or_substitution,
    rewrite_yaml_scalar,
)
from esphome_device_builder.models.components import (
    ComponentCatalogEntry,
    ComponentCategory,
)


def _component(
    *,
    component_id: str,
    category: ComponentCategory,
    name: str = "test",
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
    )


# ---------------------------------------------------------------------------
# rewrite_esphome_name
# ---------------------------------------------------------------------------


def test_rewrite_esphome_name_swaps_value_under_esphome_block() -> None:
    """The basic happy path: rename inside ``esphome:`` updates the value.

    Indentation must survive; we use two spaces because that's
    what the wizard emits.
    """
    yaml = "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n"
    assert rewrite_esphome_name(yaml, "kitchen-2", only_if_current="kitchen") == (
        "esphome:\n  name: kitchen-2\n  friendly_name: Kitchen\n"
    )


def test_rewrite_esphome_name_returns_original_when_no_match() -> None:
    """No-op rename: the input string is returned unchanged.

    The function's documented contract: returns the original
    text when nothing matches. Use equality (not identity) here
    because the docstring only promises content equivalence —
    pinning identity would over-constrain the implementation
    against changes that re-allocate the string. ``_manual_rename``'s
    handling of the no-op case (whether to skip the write or not)
    is its own concern; this test stays focused on the helper.
    """
    yaml = "esphome:\n  name: kitchen\n"
    assert rewrite_esphome_name(yaml, "garage-2", only_if_current="garage") == yaml


def test_rewrite_esphome_name_ignores_lookalike_in_other_block() -> None:
    """``name:`` in another block (sensor, Wi-Fi) must not flip.

    Without the in-esphome-only gate, a device named ``kitchen``
    with a sensor also named ``kitchen`` would have its sensor
    renamed too, and ESPHome would reject the YAML at compile
    time with a confusing duplicate-id error.
    """
    yaml = (
        "esphome:\n  name: kitchen\n"
        "wifi:\n  ssid: kitchen\n"
        "sensor:\n  - platform: dht\n    name: kitchen\n"
    )
    out = rewrite_esphome_name(yaml, "kitchen-2", only_if_current="kitchen")
    assert "name: kitchen-2" in out
    assert "ssid: kitchen\n" in out  # untouched
    assert "    name: kitchen\n" in out  # sensor untouched


def test_rewrite_esphome_name_preserves_trailing_comment() -> None:
    """Trailing ``# comment`` on the name line survives the rewrite.

    Users sometimes annotate the line; eating their comment on
    every rename would be a noisy regression.
    """
    yaml = "esphome:\n  name: kitchen  # primary device\n"
    out = rewrite_esphome_name(yaml, "kitchen-2", only_if_current="kitchen")
    assert out == "esphome:\n  name: kitchen-2  # primary device\n"


def test_rewrite_esphome_name_no_match_walks_past_sibling_top_level_blocks() -> None:
    """A no-op rename still walks the whole file without crashing.

    Pin the two post-esphome branches that only fire when the
    function runs to EOF without finding a match (no early
    ``break`` after the first rewrite):

    - sibling top-level header (``wifi:``) flips ``in_esphome``
      back off (the block-exit branch);
    - the indented child line that follows then short-circuits via
      the ``not in_esphome`` guard.

    Without these branches the walker would either keep
    ``in_esphome=True`` after leaving the esphome block (and
    rewrite a stray ``name:`` under ``wifi:``) or attempt the
    ``name:`` regex on every line forever.
    """
    yaml = "esphome:\n  name: kitchen\nwifi:\n  ssid: home\n"
    # ``other`` doesn't match ``kitchen`` → no rewrite, walker runs to EOF.
    assert rewrite_esphome_name(yaml, "renamed", only_if_current="other") == yaml


def test_rewrite_esphome_name_walks_past_non_name_lines_and_other_blocks() -> None:
    """The walker tolerates non-``name:`` esphome lines and exits the block on a sibling.

    Drives the three early-continue branches in one trace:

    - ``friendly_name:`` is inside the esphome block but doesn't match
      the ``name:`` regex, so the loop falls through to ``continue``.
    - ``wifi:`` is a sibling top-level key and flips ``in_esphome``
      back off — without that, a stray ``name:`` under ``wifi:`` would
      get rewritten too.
    - ``  ssid:`` lands after the block flipped off and skips the
      regex check entirely (the ``not in_esphome`` guard).

    Asserts the function still finds and rewrites the real ``name:``
    further down rather than bailing on the friendly_name detour.
    """
    yaml = (
        "esphome:\n"
        "  friendly_name: Kitchen\n"
        "  name: kitchen\n"
        "wifi:\n"
        "  ssid: home\n"
        "  name: kitchen\n"  # would-be lookalike under wifi block
    )
    out = rewrite_esphome_name(yaml, "kitchen-2", only_if_current="kitchen")
    assert "  name: kitchen-2\n" in out
    # Wi-Fi's ``name:`` lookalike survives — the block-exit branch did its job.
    assert out.count("name: kitchen\n") == 1
    assert out.endswith("  name: kitchen\n")


def test_rewrite_esphome_name_handles_quoted_value() -> None:
    """Both ``"..."`` and ``'...'`` quotes count as a match.

    ESPHome accepts unquoted, single-quoted, and double-quoted
    name values; the rename must recognise all three or the user
    sees "no match" for a name they can clearly read in the file.
    """
    yaml = 'esphome:\n  name: "kitchen"\n'
    out = rewrite_esphome_name(yaml, "kitchen-2", only_if_current="kitchen")
    assert "name: kitchen-2" in out


def test_rewrite_esphome_name_unconditional_replaces_regardless_of_value() -> None:
    """Default mode (no ``only_if_current``) replaces whatever's there.

    Pin the clone-path's behaviour: a YAML whose ``esphome.name``
    has drifted from its filename (hand-edited config, or a
    ``name: $hostname`` substitution where the literal in the YAML
    is ``$hostname``) still gets the new name landed on the line.
    The gated mode used by ``_manual_rename`` is opt-in via the
    keyword arg.
    """
    yaml = "esphome:\n  name: my-kitchen-bulb\n  friendly_name: Kitchen\n"
    out = rewrite_esphome_name(yaml, "bedroom-bulb")
    assert "  name: bedroom-bulb\n" in out
    assert "my-kitchen-bulb" not in out


def test_rewrite_esphome_name_unconditional_replaces_substituted_name() -> None:
    """Unconditional replace works when the source uses ``$var`` substitutions.

    The literal value on the ``name:`` line is ``$hostname``, not
    the resolved string. A gated rewrite keyed on the filename
    would no-op; the unconditional path lands the new name and
    drops the substitution dependency for the cloned device.
    """
    yaml = "esphome:\n  name: $hostname\n"
    out = rewrite_esphome_name(yaml, "bedroom-bulb")
    assert out == "esphome:\n  name: bedroom-bulb\n"


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


@pytest.mark.parametrize("keyword", ["true", "false", "null", "yes", "no", "on", "off"])
def test_generate_component_yaml_quotes_yaml_keyword_strings(keyword: str) -> None:
    """Strings whose value is a YAML 1.1 boolean keyword get quoted.

    Without quoting, ``state: on`` parses back as ``True``, not the
    string ``"on"`` — the classic YAML 1.1 footgun. ESPHome accepts
    these as enum values for several components (e.g. light states),
    so the helper has to disambiguate. ``null`` / ``yes`` / ``no``
    follow the same logic; pin every keyword in the helper's
    allowlist so a regression that drops one shows up here.
    """
    component = _component(component_id="myc", category=ComponentCategory.MISC)
    out = generate_component_yaml(component, {"state": keyword})
    assert f'  state: "{keyword}"' in out


@pytest.mark.parametrize(
    "value",
    ["foo:bar", "foo#bar", "!secret api_key"],
    ids=["colon", "hash", "tag-prefix"],
)
def test_generate_component_yaml_quotes_strings_with_special_chars(value: str) -> None:
    """Strings containing ``:`` / ``#`` or starting with ``!`` get quoted.

    Each of these is YAML structural punctuation: ``:`` opens a
    mapping value, ``#`` opens a comment, and ``!`` introduces a tag.
    Emitting any of them unquoted either breaks the parse or
    silently changes the meaning (``key: foo#bar`` becomes ``key:
    foo`` with a trailing comment). Pin all three so a refactor that
    drops one of the disjuncts surfaces here.
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
def test_generate_component_yaml_emits_numeric_value_via_str(value: int | float) -> None:
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
