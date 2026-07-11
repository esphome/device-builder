"""Unit tests for the content-based nested field-description matcher."""

from __future__ import annotations

from typing import Any

import orjson

from script.sync_components import (  # type: ignore[import-not-found]
    _OUTPUT_BODIES_DIR,
    _apply_nested_field_sections,
    _enumerate_mdx_field_sections,
    _extract_mdx_field_descriptions,
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


def test_dedup_counts_bulletless_headings() -> None:
    # A bulletless "## Configuration variables" still consumes the base slug (the
    # docs-site slugger sees every rendered heading), so the following bullet-bearing
    # section anchors at `-1`, not the bare slug.
    mdx = """\
---
title: X
---

## Configuration variables

Intro prose, no config-var bullets here.

## Networks

### Configuration variables

- **ssid** (*Optional*, string): The name.
- **bssid** (*Optional*, string): The MAC.
"""
    sections = _enumerate_mdx_field_sections(mdx)
    assert [s["slug"] for s in sections] == ["configuration-variables-1"]


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


def _sec(fields, *, heading="S", slug="s", is_automation=False):
    return {"heading": heading, "slug": slug, "is_automation": is_automation, "fields": fields}


def test_match_requires_two_nongeneric_shared() -> None:
    # One non-generic overlap (`foo`; `id`/`name` are generic) is below threshold.
    one = [_sec({"id": "I.", "name": "N.", "foo": "F."})]
    sec, _ = _match_section_to_node({"id", "name", "foo"}, {"foo"}, one)
    assert sec is None
    # A second non-generic shared name (`bar`) crosses the threshold.
    two = [_sec({"foo": "F.", "bar": "B."})]
    sec, apply = _match_section_to_node({"foo", "bar"}, {"foo", "bar"}, two)
    assert sec is not None and apply == {"foo": "F.", "bar": "B."}


def test_match_node_coverage_threshold() -> None:
    # Sharing 2 of 5 children is 0.4 coverage — below 0.5, declined.
    below = [_sec({"a": "A.", "b": "B."})]
    sec, _ = _match_section_to_node({"a", "b", "c", "d", "e"}, {"a", "b"}, below)
    assert sec is None
    # Sharing 2 of 4 is exactly 0.5 — accepted.
    at = [_sec({"a": "A.", "b": "B."})]
    sec, _ = _match_section_to_node({"a", "b", "c", "d"}, {"a", "b"}, at)
    assert sec is not None


def test_match_section_coverage_threshold() -> None:
    # The node shares both its fields, but they're only 2 of the section's 6 (0.33).
    wide = [_sec({"a": "A.", "b": "B.", "w": "W.", "x": "X.", "y": "Y.", "z": "Z."})]
    sec, _ = _match_section_to_node({"a", "b", "c"}, {"a", "b", "c"}, wide)
    assert sec is None


def test_ambiguity_guard_ignores_keys_outside_the_node() -> None:
    # Two candidates agree on every shared-in-node key (`a`, `b`) but disagree on
    # `z`, which the node doesn't have — the guard must not trip on it.
    sections = [
        _sec({"a": "A.", "b": "B.", "z": "Z1."}, heading="One", slug="one"),
        _sec({"a": "A.", "b": "B.", "z": "Z2."}, heading="Two", slug="two"),
    ]
    sec, apply = _match_section_to_node({"a", "b"}, {"a", "b"}, sections)
    assert sec is not None
    assert apply == {"a": "A.", "b": "B."}


def test_apply_ignores_nonfield_children_and_preserves_documented() -> None:
    # A divider isn't a field (never counted, never filled); an already-documented
    # child keeps its own description and help_link.
    tree = [
        {
            "key": "networks",
            "type": "nested",
            "config_entries": [
                {"key": "ssid", "description": "Kept.", "help_link": "https://kept#x"},
                {"key": "sep", "type": "divider"},
                {"key": "bssid"},
                {"key": "priority"},
            ],
        }
    ]
    n = _apply_nested_field_sections(
        tree, _enumerate_mdx_field_sections(_MDX), docs_url="https://x/wifi"
    )
    children = tree[0]["config_entries"]
    ssid, sep, bssid = children[0], children[1], children[2]
    assert ssid["description"] == "Kept." and ssid["help_link"] == "https://kept#x"
    assert "description" not in sep  # divider untouched
    assert bssid.get("description") == "The access-point MAC."
    assert n == 2  # bssid + priority, not ssid (documented) or sep (divider)


def test_apply_recurses_into_deeper_nodes() -> None:
    tree = [
        {
            "key": "outer",
            "type": "nested",
            "config_entries": [
                {
                    "key": "networks",
                    "type": "nested",
                    "config_entries": [{"key": "ssid"}, {"key": "bssid"}, {"key": "priority"}],
                }
            ],
        }
    ]
    n = _apply_nested_field_sections(
        tree, _enumerate_mdx_field_sections(_MDX), docs_url="https://x/wifi"
    )
    deep = tree[0]["config_entries"][0]["config_entries"]
    assert n == 3
    assert all((c.get("description") or "").strip() for c in deep)


def test_slugify_heading_edge_cases() -> None:
    assert _slugify_heading("Advanced   Configuration!") == "advanced-configuration"
    assert _slugify_heading("  Leading & trailing --") == "leading-trailing"
    assert _slugify_heading("`code` / mixed.punct") == "code-mixed-punct"


def test_extract_mdx_field_descriptions_reads_top_level_section() -> None:
    # Behaviour pin for the flat top-level extractor (frontmatter stripped, first
    # `## Configuration variables` bullets only).
    fields = _extract_mdx_field_descriptions(_MDX)
    assert fields == {"top_a": "A top-level field."}


# --- Catalog pins: the regenerated bodies carry the matched descriptions ---


def test_catalog_wifi_networks_fields_documented() -> None:
    ssid = _leaf("wifi", "networks", "ssid")
    assert ssid is not None
    assert (ssid.get("description") or "").strip()
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
