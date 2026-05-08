r"""Tests for ``extract_directly_referenced_integrations``.

Backs the issue #422 fix — the device-drawer's "Loaded
Integrations" panel was rendering both user-typed and auto-loaded
integrations together, cluttering the sidebar for any
non-trivial config. Splitting the list needs an authoritative
"the user wrote this" signal; this helper produces that signal
from the resolved YAML config.

The complementary "auto-loaded" set is computed downstream as
``loaded_integrations \ directly_referenced_integrations``, so
correctness here is what determines whether a given chip lands
in the visible "Direct" bucket vs the collapsed "Auto-loaded"
bucket on the frontend.
"""

from __future__ import annotations

from esphome_device_builder.helpers.device_yaml import (
    extract_directly_referenced_integrations,
)


def test_extracts_top_level_keys() -> None:
    """Every top-level key the user wrote counts as direct."""
    config = {
        "esphome": {"name": "x"},
        "esp32": {"board": "nodemcu-32s"},
        "wifi": {"ssid": "x"},
        "api": {},
    }
    assert extract_directly_referenced_integrations(config) == [
        "api",
        "esp32",
        "esphome",
        "wifi",
    ]


def test_extracts_platform_stems_from_list() -> None:
    """``- platform: <name>`` entries under a list-shaped block.

    The common case for ``binary_sensor`` / ``sensor`` / ``switch``
    / ``time`` / ``button`` / etc.: each list item is a dict with a
    ``platform`` key naming the integration to load. All platform
    stems land in the direct set alongside the parent key.
    """
    config = {
        "binary_sensor": [{"platform": "gpio", "name": "Pan Water"}],
        "button": [{"platform": "restart", "name": "Restart"}],
        "time": [
            {"platform": "homeassistant", "id": "ha_time"},
            {"platform": "sntp", "id": "sntp_time"},
        ],
    }
    assert extract_directly_referenced_integrations(config) == [
        "binary_sensor",
        "button",
        "gpio",
        "homeassistant",
        "restart",
        "sntp",
        "time",
    ]


def test_extracts_platform_stem_from_single_dict() -> None:
    """``ota: platform: esphome`` (single-platform dict shape).

    A handful of components — historically ``ota``, ``mqtt`` —
    accept a single platform/transport without the wrapping list.
    The extractor handles both shapes so the user's intent is
    captured the same way regardless of which form their YAML
    uses.
    """
    config = {"ota": {"platform": "esphome"}}
    assert extract_directly_referenced_integrations(config) == ["esphome", "ota"]


def test_real_world_acfloatmonitor32_yaml() -> None:
    """End-to-end shape from a real device YAML (issue #422 example).

    The user pasted a real ``acfloatmonitor32`` config; running
    that through the extractor must produce the exact direct set
    we documented in the issue thread, so the frontend's
    direct/indirect split matches user expectations on this
    canonical case. (The actual ``loaded_integrations`` from a
    compile of this device adds the auto-loaded chain on top —
    ``md5``, ``mdns``, ``network``, ``preferences``, ``safe_mode``,
    ``sha256``, ``socket``, ``watchdog``, etc. — which the
    frontend buckets as indirect.)
    """
    config = {
        "esp32_ble_tracker": {"scan_parameters": {"active": False}},
        "bluetooth_proxy": {"active": True},
        "substitutions": {"devicename": "acfloatmonitor32"},
        "esphome": {"name": "$devicename"},
        "esp32": {"board": "esp32-poe-iso"},
        "ethernet": {"type": "LAN8720"},
        "logger": {"level": "DEBUG"},
        "api": {"id": "api_server", "encryption": {"key": "..."}},
        "ota": {"platform": "esphome"},
        "binary_sensor": [{"platform": "gpio", "name": "Pan Water"}],
        "button": [{"platform": "restart", "name": "Restart"}],
        "time": [
            {"platform": "homeassistant", "id": "homeassistant_time"},
            {"platform": "sntp", "id": "sntp_time"},
        ],
    }
    # ``esphome`` appears as both a top-level key AND as
    # ``ota.platform``, but the extractor dedupes via a set before
    # sorting — landing once in the result.
    assert extract_directly_referenced_integrations(config) == [
        "api",
        "binary_sensor",
        "bluetooth_proxy",
        "button",
        "esp32",
        "esp32_ble_tracker",
        "esphome",
        "ethernet",
        "gpio",
        "homeassistant",
        "logger",
        "ota",
        "restart",
        "sntp",
        "substitutions",
        "time",
    ]


def test_returns_empty_list_for_none_config() -> None:
    """Resolved-config parse failure → empty list (graceful degrade).

    When the YAML can't be parsed (mid-edit drafts, missing
    secrets, malformed YAML), ``load_device_yaml`` returns
    ``None``. The frontend reads an empty
    ``directly_referenced_integrations`` as "I don't know what's
    direct" and falls back to rendering the flat
    ``loaded_integrations`` list, instead of false-bucketing
    everything as indirect.
    """
    assert extract_directly_referenced_integrations(None) == []


def test_returns_empty_list_for_non_dict_config() -> None:
    """Defensive — a malformed loader handing in a list / scalar."""
    assert extract_directly_referenced_integrations([1, 2, 3]) == []  # type: ignore[arg-type]
    assert extract_directly_referenced_integrations("not a config") == []  # type: ignore[arg-type]


def test_skips_non_string_keys_and_platforms() -> None:
    """
    Skip non-string keys and non-string platform values.

    Defensive against weird parses: non-string keys are skipped (a
    YAML mapping with int keys is technically valid but doesn't
    map to any ESPHome integration), and ``platform: <lambda>``
    or other non-string values don't bleed garbage names into the
    direct set.
    """
    config: dict = {
        "sensor": [
            {"platform": "valid_platform"},
            {"platform": 42},  # not a string — skip
            {"platform": ""},  # empty — skip
            {"platform": None},  # none — skip
        ],
        # Single-platform dict with non-string platform — skip
        "weird": {"platform": ["templated"]},
    }
    assert extract_directly_referenced_integrations(config) == [
        "sensor",
        "valid_platform",
        "weird",
    ]


def test_top_level_block_with_no_platform_field() -> None:
    """
    Top-level dicts without a ``platform`` key add only the parent.

    No synthesised platform stem when the value isn't a list of
    platforms or a single-platform dict.
    """
    config = {
        "wifi": {"ssid": "x", "password": "y"},
        "logger": {"level": "DEBUG"},
    }
    assert extract_directly_referenced_integrations(config) == ["logger", "wifi"]


def test_empty_value_blocks_still_count() -> None:
    """``api:`` with no body is still a direct reference.

    Bare ``api:`` is the canonical opt-in to the Native API even
    without an inner ``encryption:`` / ``id:``. The empty-value
    case must not get filtered out.
    """
    config = {"api": None, "logger": None}
    assert extract_directly_referenced_integrations(config) == ["api", "logger"]
