"""Unit tests for docs-page anchor helpers in ``script/sync_components.py``."""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _attach_docs_anchors,
    _find_component_section,
    _page_anchor_index,
)

_LIGHT_PAGE = """\
---
title: Light Component
---

## Effects

### E1.31 Effect

<span id="e131-light-effect"></span>

```yaml
e131:
```

### Adalight Effect

```yaml
adalight:
```

### Daikin ARC

Some prose without a config example.

## Effects

Duplicate heading to exercise slug dedup.
"""


def test_page_anchor_index_slugs_and_explicit_ids() -> None:
    headings, explicit = _page_anchor_index(_LIGHT_PAGE)
    slugs = [slug for _, slug in headings]
    assert slugs == ["effects", "e1-31-effect", "adalight-effect", "daikin-arc", "effects-1"]
    assert explicit == frozenset({"e131-light-effect"})


def test_find_component_section_bare_key_example() -> None:
    assert _find_component_section(_LIGHT_PAGE, "adalight") == "adalight-effect"
    assert _find_component_section(_LIGHT_PAGE, "e131") == "e1-31-effect"


def test_find_component_section_platform_example() -> None:
    page = "## BME280\n\n```yaml\nsensor:\n  - platform: bme280_i2c\n```\n"
    assert _find_component_section(page, "sensor.bme280_i2c") == "bme280"


def test_find_component_section_heading_slug_fallback() -> None:
    assert _find_component_section(_LIGHT_PAGE, "climate.daikin_arc") == "daikin-arc"


def test_find_component_section_example_above_headings_is_pagewide() -> None:
    assert _find_component_section("```yaml\nwled:\n```\n\n## Later\n", "wled") == ""


def test_find_component_section_no_match() -> None:
    assert _find_component_section(_LIGHT_PAGE, "climate.coolix") is None


def _entry(component_id: str, url: str, **scratch: object) -> dict:
    return {"id": component_id, "docs_url": url, **scratch}


def test_attach_rescue_anchor_when_page_still_matches() -> None:
    entry = _entry(
        "adalight",
        "https://esphome.io/components/light",
        _docs_anchor=("light", "adalight-effect"),
    )
    _attach_docs_anchors([entry], {"light": _LIGHT_PAGE})
    assert entry["docs_url"] == "https://esphome.io/components/light#adalight-effect"
    assert "_docs_anchor" not in entry


def test_attach_rescue_anchor_dropped_when_page_moved() -> None:
    entry = _entry(
        "lvgl",
        "https://esphome.io/components/lvgl",
        _docs_anchor=("light", "adalight-effect"),
    )
    _attach_docs_anchors([entry], {"light": _LIGHT_PAGE, "lvgl": ""})
    assert entry["docs_url"] == "https://esphome.io/components/lvgl"
    assert "_docs_anchor" not in entry


def test_attach_valid_seealso_anchor_on_domain_landing() -> None:
    entry = _entry(
        "e131",
        "https://esphome.io/components/light",
        _docs_anchor_seealso="e131-light-effect",
    )
    _attach_docs_anchors([entry], {"light": _LIGHT_PAGE})
    assert entry["docs_url"] == "https://esphome.io/components/light#e131-light-effect"
    assert "_docs_anchor_seealso" not in entry


def test_attach_repairs_invalid_seealso_anchor_via_section() -> None:
    entry = _entry(
        "e131",
        "https://esphome.io/components/light",
        _docs_anchor_seealso="e131-component",
    )
    _attach_docs_anchors([entry], {"light": _LIGHT_PAGE})
    assert entry["docs_url"] == "https://esphome.io/components/light#e1-31-effect"


def test_attach_leaves_bare_landing_when_repair_fails() -> None:
    entry = _entry(
        "climate.coolix",
        "https://esphome.io/components/light",
        _docs_anchor_seealso="nope",
    )
    _attach_docs_anchors([entry], {"light": _LIGHT_PAGE})
    assert entry["docs_url"] == "https://esphome.io/components/light"


def test_attach_ignores_seealso_anchor_on_non_landing_page() -> None:
    entry = _entry(
        "sensor.bme280",
        "https://esphome.io/components/sensor/bme280",
        _docs_anchor_seealso="configuration-variables",
    )
    _attach_docs_anchors([entry], {"sensor/bme280": "## Configuration variables\n"})
    assert entry["docs_url"] == "https://esphome.io/components/sensor/bme280"
    assert "_docs_anchor_seealso" not in entry


def test_attach_skips_blank_and_unknown_pages() -> None:
    entries = [
        _entry("a", "", _docs_anchor_seealso="x"),
        _entry("b", "https://esphome.io/components/nope", _docs_anchor=("nope", "x")),
    ]
    _attach_docs_anchors(entries, {"light": _LIGHT_PAGE})
    assert entries[0]["docs_url"] == ""
    assert entries[1]["docs_url"] == "https://esphome.io/components/nope"
    assert all("_docs_anchor" not in e and "_docs_anchor_seealso" not in e for e in entries)
