"""Unit tests for docs_url resolution and validation in ``script/sync_components.py``."""

from __future__ import annotations

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _assert_docs_urls_valid,
    _docs_page_path,
    _repair_help_links,
    _resolve_docs_url,
)

_XIAOMI_PAGE = """\
## LYWSD03MMC

```yaml
sensor:
  - platform: xiaomi_lywsd03mmc
```
"""

_WEIKAI_PAGE = """\
## WK2168

```yaml
weikai:
```
"""


def test_docs_page_path_parses_components_urls() -> None:
    assert _docs_page_path("https://esphome.io/components/light") == "light"
    assert _docs_page_path("https://esphome.io/components/sensor/bme280/") == "sensor/bme280"
    assert _docs_page_path("https://beta.esphome.io/components/light#x") == "light"
    assert _docs_page_path("https://esphome.io/guides/faq") is None
    assert _docs_page_path("") is None


def test_valid_existing_url_is_kept_verbatim() -> None:
    url = "https://esphome.io/components/sensor/bme280"
    assert _resolve_docs_url(url, "sensor.bme280", {"sensor/bme280": ""}) == (url, None)


def test_valid_existing_beta_host_url_is_kept() -> None:
    url = "https://beta.esphome.io/components/sensor/bme280"
    assert _resolve_docs_url(url, "sensor.bme280", {"sensor/bme280": ""}) == (url, None)


def test_stale_existing_url_falls_into_the_chain() -> None:
    url, anchor = _resolve_docs_url(
        "https://esphome.io/components/sensor/ld6002b", "sensor.ld6002b", {"light": ""}
    )
    assert url == ""
    assert anchor is None


def test_derived_page_used_when_it_exists() -> None:
    assert _resolve_docs_url("", "sensor.bme280", {"sensor/bme280": ""}) == (
        "https://esphome.io/components/sensor/bme280",
        None,
    )


def test_variant_suffix_stripped_to_base_page() -> None:
    for cid in ("sensor.bme280_i2c", "sensor.bme280_spi"):
        assert _resolve_docs_url("", cid, {"sensor/bme280": ""}) == (
            "https://esphome.io/components/sensor/bme280",
            None,
        )


def test_bare_stem_page_rescues_dotted_id() -> None:
    assert _resolve_docs_url("", "image.sendspin", {"sendspin": ""}) == (
        "https://esphome.io/components/sendspin",
        None,
    )


def test_unique_cross_domain_page_rescues() -> None:
    assert _resolve_docs_url("", "binary_sensor.dlms_meter", {"sensor/dlms_meter": ""}) == (
        "https://esphome.io/components/sensor/dlms_meter",
        None,
    )


def test_ambiguous_cross_domain_yields_no_link() -> None:
    pages = {"sensor/gpio": "", "switch/gpio": ""}
    assert _resolve_docs_url("", "binary_sensor.gpio", pages) == ("", None)


def test_unique_config_example_match_carries_section_anchor() -> None:
    url, anchor = _resolve_docs_url(
        "", "sensor.xiaomi_lywsd03mmc", {"sensor/xiaomi_ble": _XIAOMI_PAGE}
    )
    assert url == "https://esphome.io/components/sensor/xiaomi_ble"
    assert anchor == ("sensor/xiaomi_ble", "lywsd03mmc")


def test_bare_id_config_example_match() -> None:
    url, anchor = _resolve_docs_url("", "weikai", {"wk": _WEIKAI_PAGE})
    assert url == "https://esphome.io/components/wk"
    assert anchor == ("wk", "wk2168")


def test_ambiguous_config_example_yields_no_link() -> None:
    assert _resolve_docs_url("", "weikai", {"a": _WEIKAI_PAGE, "b": _WEIKAI_PAGE}) == ("", None)


def test_undocumented_component_yields_no_link() -> None:
    assert _resolve_docs_url("", "climate.coolix", {"light": ""}) == ("", None)


def test_assert_accepts_empty_valid_and_anchored_urls() -> None:
    entries = [
        {"id": "a", "docs_url": ""},
        {"id": "b", "docs_url": "https://esphome.io/components/light"},
        {"id": "c", "docs_url": "https://esphome.io/components/light#lywsd03mmc"},
    ]
    _assert_docs_urls_valid(entries, {"light": _XIAOMI_PAGE})


def test_assert_raises_on_unknown_page() -> None:
    entries = [{"id": "a", "docs_url": "https://esphome.io/components/nope"}]
    with pytest.raises(SystemExit, match="no such docs page"):
        _assert_docs_urls_valid(entries, {"light": ""})


def test_assert_raises_on_unknown_anchor() -> None:
    entries = [{"id": "a", "docs_url": "https://esphome.io/components/light#nope"}]
    with pytest.raises(SystemExit, match="no such anchor"):
        _assert_docs_urls_valid(entries, {"light": _XIAOMI_PAGE})


def test_unique_code_span_mention_rescues_without_anchor() -> None:
    pages = {"climate/climate_ir": "## Supported\n\n| Coolix | `coolix` | yes |\n"}
    assert _resolve_docs_url("", "climate.coolix", pages) == (
        "https://esphome.io/components/climate/climate_ir",
        None,
    )


def test_ambiguous_code_span_mention_yields_no_link() -> None:
    pages = {"a": "uses `color` here", "b": "also `color` here"}
    assert _resolve_docs_url("", "color", pages) == ("", None)


def test_repair_help_links_repoints_dead_page_to_component_url() -> None:
    component = {
        "id": "sensor.atc_mithermometer",
        "docs_url": "https://esphome.io/components/sensor/xiaomi_ble#lywsd03mmc",
        "config_entries": [
            {"key": "a", "help_link": "https://esphome.io/components/sensor/atc_mithermometer#x"},
            {"key": "b", "help_link": "https://esphome.io/components/sensor/xiaomi_ble#y"},
            {"key": "c", "help_link": "https://esphome.io/automations/actions#z"},
        ],
    }
    _repair_help_links([component], {"sensor/xiaomi_ble": "## LYWSD03MMC\n\n## Y\n"})
    entries = component["config_entries"]
    assert entries[0]["help_link"] == "https://esphome.io/components/sensor/xiaomi_ble#lywsd03mmc"
    assert entries[1]["help_link"] == "https://esphome.io/components/sensor/xiaomi_ble#y"
    assert entries[2]["help_link"] == "https://esphome.io/automations/actions#z"


def test_repair_help_links_repoint_strips_dead_fallback_anchor() -> None:
    component = {
        "id": "sensor.atc_mithermometer",
        "docs_url": "https://esphome.io/components/sensor/xiaomi_ble#gone",
        "config_entries": [
            {"key": "a", "help_link": "https://esphome.io/components/sensor/atc_mithermometer#x"}
        ],
    }
    _repair_help_links([component], {"sensor/xiaomi_ble": "## Y\n"})
    assert (
        component["config_entries"][0]["help_link"]
        == "https://esphome.io/components/sensor/xiaomi_ble"
    )


def test_repair_help_links_remaps_stale_fragment_spelling() -> None:
    component = {
        "id": "water_heater",
        "docs_url": "",
        "config_entries": [
            {
                "key": "a",
                "help_link": "https://esphome.io/components/water_heater#water_heatercontrol-action",
            }
        ],
    }
    _repair_help_links([component], {"water_heater": "## `water_heater.control` Action\n"})
    assert (
        component["config_entries"][0]["help_link"]
        == "https://esphome.io/components/water_heater#water_heater-control-action"
    )


def test_repair_help_links_strips_dead_anchor_on_live_page() -> None:
    component = {
        "id": "sensor.dht",
        "docs_url": "https://esphome.io/components/sensor/dht",
        "config_entries": [
            {"key": "name", "help_link": "https://esphome.io/components/light#optional-variables"}
        ],
    }
    _repair_help_links([component], {"light": "## Effects\n", "sensor/dht": ""})
    assert component["config_entries"][0]["help_link"] == "https://esphome.io/components/light"


def test_repair_help_links_drops_link_when_component_has_no_page() -> None:
    component = {
        "id": "sensor.ld6002b",
        "docs_url": "",
        "config_entries": [
            {
                "key": "outer",
                "help_link": "https://esphome.io/components/sensor/ld6002b#v",
                "config_entries": [
                    {"key": "inner", "help_link": "https://esphome.io/components/sensor/ld6002b#v"}
                ],
            }
        ],
    }
    _repair_help_links([component], {"light": ""})
    assert "help_link" not in component["config_entries"][0]
    assert "help_link" not in component["config_entries"][0]["config_entries"][0]


def test_assert_raises_on_dead_help_link_page() -> None:
    entries = [
        {
            "id": "a",
            "docs_url": "",
            "config_entries": [{"key": "f", "help_link": "https://esphome.io/components/nope#x"}],
        }
    ]
    with pytest.raises(SystemExit, match="help_link"):
        _assert_docs_urls_valid(entries, {"light": _XIAOMI_PAGE})


def test_assert_raises_on_dead_help_link_anchor() -> None:
    entries = [
        {
            "id": "a",
            "docs_url": "",
            "config_entries": [
                {"key": "f", "help_link": "https://esphome.io/components/light#fabricated-anchor"}
            ],
        }
    ]
    with pytest.raises(SystemExit, match="no such anchor"):
        _assert_docs_urls_valid(entries, {"light": _XIAOMI_PAGE})


def test_assert_ignores_non_component_help_links() -> None:
    entries = [
        {
            "id": "a",
            "docs_url": "",
            "config_entries": [
                {"key": "f", "help_link": "https://esphome.io/components/light#lywsd03mmc"},
                {"key": "g", "help_link": "https://esphome.io/automations/actions#x"},
            ],
        }
    ]
    _assert_docs_urls_valid(entries, {"light": _XIAOMI_PAGE})


def test_blockquote_code_span_mention_does_not_rescue() -> None:
    pages = {"esphome": "> Creators can provide `dashboard_import` URL for end users.\n"}
    assert _resolve_docs_url("", "dashboard_import", pages) == ("", None)
