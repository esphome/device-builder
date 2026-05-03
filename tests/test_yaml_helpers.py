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
    generate_component_yaml,
    merge_component_yaml,
    rewrite_esphome_name,
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
    assert rewrite_esphome_name(yaml, "kitchen", "kitchen-2") == (
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
    assert rewrite_esphome_name(yaml, "garage", "garage-2") == yaml


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
    out = rewrite_esphome_name(yaml, "kitchen", "kitchen-2")
    assert "name: kitchen-2" in out
    assert "ssid: kitchen\n" in out  # untouched
    assert "    name: kitchen\n" in out  # sensor untouched


def test_rewrite_esphome_name_preserves_trailing_comment() -> None:
    """Trailing ``# comment`` on the name line survives the rewrite.

    Users sometimes annotate the line; eating their comment on
    every rename would be a noisy regression.
    """
    yaml = "esphome:\n  name: kitchen  # primary device\n"
    out = rewrite_esphome_name(yaml, "kitchen", "kitchen-2")
    assert out == "esphome:\n  name: kitchen-2  # primary device\n"


def test_rewrite_esphome_name_handles_quoted_value() -> None:
    """Both ``"..."`` and ``'...'`` quotes count as a match.

    ESPHome accepts unquoted, single-quoted, and double-quoted
    name values; the rename must recognise all three or the user
    sees "no match" for a name they can clearly read in the file.
    """
    yaml = 'esphome:\n  name: "kitchen"\n'
    out = rewrite_esphome_name(yaml, "kitchen", "kitchen-2")
    assert "name: kitchen-2" in out


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
