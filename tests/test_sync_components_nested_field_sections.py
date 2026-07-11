"""Unit tests for the content-based nested field-description matcher."""

from __future__ import annotations

from typing import Any

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _OUTPUT_BODIES_DIR,
    _apply_nested_field_sections,
    _enumerate_mdx_field_sections,
    _match_section_to_node,
    _slugify_heading,
)


def _leaf(component_id: str, *path: str) -> dict | None:
    """Walk ``path`` (keys) into a component body and return the leaf entry."""
    cur: list[dict] = orjson.loads((_OUTPUT_BODIES_DIR / f"{component_id}.json").read_bytes())[
        "config_entries"
    ]
    node: dict[str, Any] | None = None
    for key in path:
        node = next((e for e in cur if e.get("key") == key), None)
        if node is None:
            return None
        cur = node.get("config_entries", [])
    return node


_MDX = """\
---
title: Example
---

## Configuration variables

- **top_a** (*Optional*, string): A top-level field.

## Networks

### Configuration variables

- **ssid** (*Optional*, string): The network name.
- **bssid** (*Optional*, string): The access-point MAC.
- **priority** (*Optional*, float): The connection priority.

## MQTTMessage

- **topic** (*Optional*, string): The MQTT topic.
- **payload** (*Optional*, string): The payload.
- **qos** (*Optional*, int): The QoS.
- **retain** (*Optional*, boolean): Whether to retain.

## `mqtt.publish` Action

### Configuration variables

- **topic** (*Optional*, string): ACTION topic prose (must not win).
- **payload** (*Optional*, string): ACTION payload prose.
- **qos** (*Optional*, int): ACTION qos prose.
- **retain** (*Optional*, boolean): ACTION retain prose.
"""


def _section(name):
    return next(s for s in _enumerate_mdx_field_sections(_MDX) if s["heading"] == name)


def test_slugify_and_dedup() -> None:
    assert _slugify_heading("Advanced Configuration") == "advanced-configuration"
    assert _slugify_heading("`mqtt.publish` Action") == "mqtt-publish-action"
    slugs = [s["slug"] for s in _enumerate_mdx_field_sections(_MDX)]
    # Three "Configuration variables" headings → deduped in document order.
    assert "configuration-variables" in slugs
    assert "configuration-variables-1" in slugs
    assert "configuration-variables-2" in slugs


def test_automation_section_flagged() -> None:
    # The `### Configuration variables` under the Action heading inherits is_automation.
    action_cvars = next(
        s
        for s in _enumerate_mdx_field_sections(_MDX)
        if s["heading"] == "Configuration variables"
        and "ACTION topic prose" in s["fields"].get("topic", "")
    )
    assert action_cvars["is_automation"] is True
    assert _section("MQTTMessage")["is_automation"] is False


def test_confident_match_applies() -> None:
    sections = _enumerate_mdx_field_sections(_MDX)
    children = {"ssid", "bssid", "priority"}
    sec, apply = _match_section_to_node(children, {"ssid", "priority"}, sections)
    assert sec is not None and sec["slug"] == "configuration-variables-1"
    assert apply == {"ssid": "The network name.", "priority": "The connection priority."}


def test_generic_only_overlap_does_not_match() -> None:
    # esphome.areas[].name shape: {id, name} — all generic, so no section can claim it.
    sections = [
        {
            "heading": "Configuration variables",
            "slug": "configuration-variables",
            "is_automation": False,
            "fields": {"id": "Top id.", "name": "Top name.", "area": "An area.", "x": "Other."},
        }
    ]
    sec, apply = _match_section_to_node({"id", "name"}, {"id", "name"}, sections)
    assert sec is None and apply == {}


def test_automation_twin_excluded_config_section_wins() -> None:
    # MQTTMessage and the mqtt.publish Action share an identical field set; the
    # Action is excluded so the message prose (not the action prose) is applied.
    sections = _enumerate_mdx_field_sections(_MDX)
    sec, apply = _match_section_to_node({"topic", "payload", "qos", "retain"}, {"topic"}, sections)
    assert sec is not None and sec["slug"] == "mqttmessage"
    assert apply == {"topic": "The MQTT topic."}


def test_ambiguous_conflicting_prose_skips() -> None:
    sections = [
        {
            "heading": "A",
            "slug": "a",
            "is_automation": False,
            "fields": {
                "topic": "A topic.",
                "payload": "shared.",
                "qos": "shared.",
                "retain": "shared.",
            },
        },
        {
            "heading": "B",
            "slug": "b",
            "is_automation": False,
            "fields": {
                "topic": "B DIFFERENT topic.",
                "payload": "shared.",
                "qos": "shared.",
                "retain": "shared.",
            },
        },
    ]
    sec, _ = _match_section_to_node({"topic", "payload", "qos", "retain"}, {"topic"}, sections)
    assert sec is None  # winner and runner-up disagree on `topic`


def test_apply_one_section_to_many_nodes() -> None:
    tree = [
        {
            "key": "birth_message",
            "type": "nested",
            "config_entries": [
                {"key": "topic"},
                {"key": "payload"},
                {"key": "qos"},
                {"key": "retain"},
            ],
        },
        {
            "key": "will_message",
            "type": "nested",
            "config_entries": [
                {"key": "topic"},
                {"key": "payload"},
                {"key": "qos"},
                {"key": "retain"},
            ],
        },
    ]
    n = _apply_nested_field_sections(
        tree, _enumerate_mdx_field_sections(_MDX), docs_url="https://x/mqtt"
    )
    assert n == 8
    for node in tree:
        first = node["config_entries"][0]
        assert first["description"] == "The MQTT topic."
        assert first["help_link"] == "https://x/mqtt#mqttmessage"


def test_apply_skips_generic_only_node() -> None:
    tree = [{"key": "areas", "type": "nested", "config_entries": [{"key": "id"}, {"key": "name"}]}]
    n = _apply_nested_field_sections(
        tree, _enumerate_mdx_field_sections(_MDX), docs_url="https://x"
    )
    assert n == 0
    assert "description" not in tree[0]["config_entries"][0]


# --- Catalog pins: the regenerated bodies carry the matched descriptions ---


def test_catalog_wifi_networks_fields_documented() -> None:
    ssid = _leaf("wifi", "networks", "ssid")
    assert ssid is not None
    assert (ssid.get("description") or "").startswith("The SSID or WiFi network name")
    # Networks' `### Configuration variables` de-dupes to the -1 slug.
    assert (ssid.get("help_link") or "").endswith("#configuration-variables-1")
    assert (_leaf("wifi", "networks", "priority").get("description") or "").strip()


def test_catalog_mqtt_message_uses_message_prose_not_action() -> None:
    topic = _leaf("mqtt", "birth_message", "topic")
    assert topic is not None
    # Resolved to the MQTTMessage section (the `#mqttmessage` anchor), not the
    # mqtt.publish Action twin whose section is excluded from matching.
    assert (topic.get("help_link") or "").endswith("#mqttmessage")
    assert (topic.get("description") or "").strip()


def test_catalog_graph_legend_documented() -> None:
    width = _leaf("graph", "legend", "width")
    assert width is not None and (width.get("description") or "").strip()
    assert (width.get("help_link") or "").endswith("#legend-options")


def test_catalog_esphome_generic_name_not_backfilled() -> None:
    # Collision negative: esphome.devices[].name / areas[].name are generic-only,
    # so the matcher must not attach a top-level "name" description to them.
    for group in ("devices", "areas"):
        name = _leaf("esphome", group, "name")
        if name is not None:  # present in the schema
            assert not (name.get("help_link") or "").endswith("#configuration-variables")
