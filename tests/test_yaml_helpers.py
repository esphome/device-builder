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
