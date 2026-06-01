#!/usr/bin/env python3
"""Generate the split component catalog from ESPHome's pre-built schema bundle.

Emits ``definitions/components.index.json`` plus per-id body files
under ``definitions/components/<id>.json``.

The schema repo (https://github.com/esphome/esphome-schema) publishes a
schema.zip per ESPHome release containing one JSON file per component.
That bundle drives VS Code's ESPHome extension and the official Builder
editor — it's the authoritative description of what each component
accepts in YAML. We use it as the primary source of structure, types,
defaults, and field descriptions.

A small amount of ESPHome introspection still happens for things the
schema doesn't capture:

- ``platform_defaults`` (``cv.SplitDefault`` per-target-platform values
  used by the backend to filter inapplicable fields)
- ``multi_conf`` (whether a component can be added more than once)
- ``supported_platforms`` (which target chips the component runs on)

Image URLs come from the docs repo's index page (the only MDX scraping
that survives the rewrite).

Usage
-----

    python script/sync_components.py                # latest stable release
    python script/sync_components.py --version 2026.4.3
    python script/sync_components.py --include-prereleases
"""

from __future__ import annotations

import argparse
import copy
import inspect
import json
import logging
import re
import shutil
import sys
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from io import BytesIO
from pathlib import Path
from typing import Any, NamedTuple

import voluptuous as vol

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOGGER = logging.getLogger("sync_components")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_OUTPUT_INDEX_FILE = _DEFINITIONS_DIR / "components.index.json"
_OUTPUT_BODIES_DIR = _DEFINITIONS_DIR / "components"
_AUTOMATIONS_INDEX_FILE = _DEFINITIONS_DIR / "automations.index.json"
_AUTOMATIONS_BODIES_DIR = _DEFINITIONS_DIR / "automations"
_CACHE_ROOT = _REPO_ROOT / ".cache"

# Fields stripped from index entries — they belong on the per-id body
# files only. Slim-index keeps the catalog UI's list / search /
# filter paths off the per-field tree.
_INDEX_DROP_FIELDS: frozenset[str] = frozenset({"config_entries", "required_groups"})

# Actions with more top-level config entries than this are flagged
# form_editable=False (LVGL *.update sits at 160+, every other action <= 30).
_MAX_FORM_CONFIG_ENTRIES = 80

_RELEASES_API = "https://api.github.com/repos/esphome/esphome-schema/releases"
_SCHEMA_URL_TEMPLATE = "https://schema.esphome.io/{version}/schema.zip"
_DOCS_INDEX_URL = (
    "https://raw.githubusercontent.com/esphome/esphome.io/current/"
    "src/content/docs/components/index.mdx"
)
_DOCS_REPO_URL = "https://github.com/esphome/esphome.io.git"
_DOCS_REPO_BRANCH = "current"
_DOCS_CLONE_DIR = "esphome.io"
_IMAGE_BASE_URL = "https://esphome.io/images/"

# CDN at schema.esphome.io rejects requests without a recognisable
# User-Agent. Use the project name + repo URL so any traffic is easy
# for the ESPHome team to identify.
_USER_AGENT = "esphome-device-builder-backend (https://github.com/esphome/device-builder-dashboard)"

# Re-import the runtime catalog's internal-helper denylist so the
# generator and the runtime loader share one source of truth — see
# ``controllers/components.py`` for the rationale (issue #325).
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _catalog_split import (  # noqa: E402
    emit_body_with_roundtrip,
    prepare_next_bodies_dir,
    swap_split_catalog_in,
)

from esphome_device_builder.controllers.components import (  # noqa: E402
    INTERNAL_COMPONENT_IDS as _INTERNAL_COMPONENT_IDS,
)
from esphome_device_builder.models import (  # noqa: E402
    AutomationAction,
    AutomationActionIndex,
    AutomationCondition,
    AutomationConditionIndex,
    AutomationTrigger,
    AutomationTriggerIndex,
    ComponentCatalogEntry,
    Filter,
    FilterIndex,
    LightEffect,
    LightEffectIndex,
    PinFeature,
)
from script._light_schemas import (  # noqa: E402
    resolve_light_effects_applies_to,
)

# Top-level platform domains in the schema (also keys in our category enum).
# Components keyed as ``<id>.<domain>`` in the schema files — e.g.
# ``dht.sensor`` lives in dht.json under the key ``dht.sensor``.
_PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        "sensor",
        "binary_sensor",
        "switch",
        "light",
        "fan",
        "cover",
        "climate",
        "button",
        "number",
        "select",
        "text",
        "text_sensor",
        "lock",
        "valve",
        "media_player",
        "speaker",
        "microphone",
        "camera",
        "display",
        "touchscreen",
        "output",
        "datetime",
        "event",
        "update",
        "alarm_control_panel",
        "stepper",
        "audio_adc",
        "audio_dac",
        "media_source",
        "one_wire",
        "canbus",
        "infrared",
        "time",
        "water_heater",
        "ota",
        "packet_transport",
    }
)

# Plain top-level keys we don't want to surface as user-facing components.
# ``core`` is the indexing-only metadata block in esphome.json.
_HIDDEN_TOP_LEVEL: frozenset[str] = frozenset({"core"})

# Synthetic umbrella entries for legacy bare-key domains. Both ``ota:``
# and ``time:`` accept a legacy bare-mapping form that predates the
# platform-based shape — bare ``ota:`` implicitly uses the ``esphome``
# OTA platform, bare ``time:`` implicitly uses ``homeassistant``. The
# catalog only ships qualified ``<domain>.<platform>`` entries, so a
# ``get_component("ota")`` lookup or an ``ota`` value in
# ``loaded_integrations`` previously had no exact-id hit. The
# umbrella entries fill that gap with a description that names the
# implicit default platform and lists the platforms available today.
#
# The injector iterates the freshly-built catalog at sync time so
# the platform list in the description stays accurate as platforms
# come and go upstream.
_UMBRELLA_ENTRIES: tuple[dict[str, str], ...] = (
    {
        "id": "ota",
        "name": "OTA Updates",
        "category": "ota",
        "default_platform": "esphome",
        "summary": "Over-the-Air firmware updates",
        "docs_url": "https://esphome.io/components/ota/",
    },
    {
        "id": "time",
        "name": "Time Source",
        "category": "time",
        "default_platform": "homeassistant",
        "summary": "Time source / real-time clock for the device",
        "docs_url": "https://esphome.io/components/time/",
    },
)

# Map prebuilt-schema ``type`` strings to our ConfigEntryType enum.
# Things not in this table fall through to STRING.
_TYPE_MAP: dict[str, str] = {
    "boolean": "boolean",
    "integer": "integer",
    "string": "string",
    "enum": "string",  # SELECT-style; the underlying value is a string
    "pin": "pin",
    "schema": "nested",
    "trigger": "nested",
    "use_id": "id",
}

# ``data_type`` strings narrow the integer range or pick a different
# concrete type. Subset that maps cleanly onto our enum.
_DATA_TYPE_PRIMITIVE: dict[str, str] = {
    "positive_int": "integer",
    "positive_not_null_int": "integer",
    "uint8_t": "integer",
    "uint16_t": "integer",
    "uint32_t": "integer",
    "hex_uint8_t": "integer",
    "hex_uint16_t": "integer",
    "hex_uint32_t": "integer",
    "hex_uint64_t": "integer",
    "positive_float": "float",
    "port": "integer",
}

# Numeric bounds inferred from ``data_type``.
_DATA_TYPE_RANGE: dict[str, tuple[int, int]] = {
    "uint8_t": (0, 255),
    "hex_uint8_t": (0, 255),
    "uint16_t": (0, 65535),
    "hex_uint16_t": (0, 65535),
    "uint32_t": (0, 4294967295),
    "hex_uint32_t": (0, 4294967295),
    "port": (0, 65535),
}

# ``data_type`` strings that signal "this integer should display
# as hexadecimal". Mirrors ESPHome's ``cv.hex_uint*_t`` family
# (and by extension ``cv.i2c_address``, which is just the 8-bit
# variant under a friendlier name). The frontend reads
# ``display_format == "hex"`` to render values as ``0x76`` and
# accept both ``0x76`` and ``118`` on entry — the round-trip
# YAML pretty-prints with hex literals so the file stays
# readable for the hardware-conventional notation.
_DATA_TYPE_HEX: frozenset[str] = frozenset(
    {
        "hex_uint8_t",
        "hex_uint16_t",
        "hex_uint32_t",
        "hex_uint64_t",
    }
)

# ``use_id_type`` is shaped ``"<namespace>::<ClassName>"``. Map the
# namespace to the catalog's component domain. ``switch_`` has a
# trailing underscore (the C++ namespace can't be ``switch``); we strip
# it. Everything else is identity.
_USE_ID_NAMESPACE_OVERRIDES: dict[str, str] = {
    "switch_": "switch",
    "binary_sensor": "binary_sensor",
    "text_sensor": "text_sensor",
}

# Fields whose key appears in this set get auto-detected as a secret
# value (renders masked in the form). Same heuristic as the previous
# sync — schema doesn't tag these explicitly.
_SECRET_KEY_FRAGMENTS = ("password", "passcode", "secret", "token", "api_key", "apikey")

# Schema-time keys we don't expose to the user (build-system / preload).
_SKIP_KEYS: frozenset[str] = frozenset({"mqtt_id", "zigbee_id", "then"})

# Per-component fields we don't surface in the catalog because they're
# deprecated and the dashboard handles the underlying concern itself.
# Keyed by ``(component_id, field_key)``.
#
# - ``esp32.board`` / ``esp8266.board``: the dashboard drives the
#   PlatformIO board ID from the user's board pick (the board catalog
#   is the source of truth). Internally we feed esphome with the
#   ``variant`` only, never ``board``.
_DEPRECATED_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("esp32", "board"),
        ("esp8266", "board"),
        ("rp2040", "board"),
        ("bk72xx", "board"),
        ("rtl87xx", "board"),
        ("ln882x", "board"),
    }
)

# Cross-cutting fields that only make sense when a specific component
# is configured on the same device — qos / retain need an ``mqtt:``
# block, ``zigbee_sensor`` needs a ``zigbee:`` hub, and the
# web_server entity overrides need a ``web_server:`` block. The
# schema lists them on every entity (because they're valid options)
# but most users never configure mqtt/zigbee/web_server, so without
# gating the form is full of fields that quietly do nothing. The
# frontend reads ``depends_on_component`` and hides each entry
# unless the named component appears in the device's YAML.
#
# Keep this list focused on cross-cutting infrastructure. Component-
# specific gates (e.g. "this LED option requires this LED platform")
# belong in the per-component schema via ``depends_on`` /
# ``depends_on_value`` instead.
_COMPONENT_GATED_KEYS: dict[str, str] = {
    # MQTT entity options (apply to every entity when ``mqtt:`` is set)
    "qos": "mqtt",
    "retain": "mqtt",
    "discovery": "mqtt",
    "subscribe_qos": "mqtt",
    "state_topic": "mqtt",
    "command_topic": "mqtt",
    "availability": "mqtt",
    # Zigbee entity options
    "zigbee_sensor": "zigbee",
    "zigbee_binary_sensor": "zigbee",
    "zigbee_switch": "zigbee",
    "zigbee_number": "zigbee",
    # Web server entity overrides
    "web_server": "web_server",
    "web_server_id": "web_server",
    "web_server_base_id": "web_server",
}


# UART ``DEBUG_SCHEMA`` shape — shared between ``uart.debug`` (the
# original) and ``ble_nus.debug`` (which imports ``maybe_empty_debug``
# from uart and reuses the same schema). Defined once here so both
# overrides stay in lockstep when DEBUG_SCHEMA grows a field upstream.
_UART_DEBUG_OVERRIDE: dict[str, Any] = {
    "type": "nested",
    "label": "Debug",
    "description": (
        "Log UART traffic to the ESPHome log for troubleshooting. "
        "Bare `debug:` enables hex logging with sensible defaults."
    ),
    "advanced": False,
    "help_link": "https://esphome.io/components/uart#uart-debugging",
    "config_entries": [
        {
            "key": "direction",
            "type": "string",
            "label": "Direction",
            "description": "Which side of the bus to log. Defaults to `BOTH`.",
            "default_value": "BOTH",
            "options": [
                {"label": "BOTH", "value": "BOTH"},
                {"label": "RX", "value": "RX"},
                {"label": "TX", "value": "TX"},
            ],
            "help_link": "https://esphome.io/components/uart#uart-debugging",
        },
        {
            "key": "debug_prefix",
            "type": "string",
            "label": "Debug Prefix",
            "description": (
                "Prefix prepended to every debug log line. Useful "
                "when multiple UART buses log at the same time."
            ),
            "default_value": "",
            "help_link": "https://esphome.io/components/uart#uart-debugging",
        },
        {
            "key": "dummy_receiver",
            "type": "boolean",
            "label": "Dummy Receiver",
            "description": (
                "Capture incoming bytes even when no UART device "
                "component is bound to the bus. Defaults to `false`."
            ),
            "default_value": False,
            "advanced": True,
            "help_link": "https://esphome.io/components/uart#uart-debugging",
        },
        {
            "key": "after",
            "type": "nested",
            "label": "After",
            "description": "When to flush accumulated bytes to the log.",
            "advanced": True,
            "help_link": "https://esphome.io/components/uart#uart-debugging",
            "config_entries": [
                {
                    "key": "bytes",
                    "type": "integer",
                    "label": "Bytes",
                    "description": (
                        "Flush after this many bytes have been accumulated. Defaults to 150."
                    ),
                    "default_value": 150,
                    "help_link": "https://esphome.io/components/uart#uart-debugging",
                },
                {
                    "key": "timeout",
                    "type": "time_period",
                    "label": "Timeout",
                    "description": (
                        "Flush after no bytes have been seen for this long. Defaults to `100ms`."
                    ),
                    "default_value": "100ms",
                    "help_link": "https://esphome.io/components/uart#uart-debugging",
                },
                {
                    "key": "delimiter",
                    "type": "string",
                    "label": "Delimiter",
                    "description": ("Flush as soon as this byte sequence is seen in the stream."),
                    "help_link": "https://esphome.io/components/uart#uart-debugging",
                },
            ],
        },
    ],
}


# Per-(component, field) entry overrides for cases where the prebuilt
# schema doesn't correctly capture the field's structure. Each value
# is a partial ConfigEntry dict that overrides the schema-derived one.
# Keep this list small and targeted — every entry is a workaround for
# an upstream schema generator gap.
_FIELD_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    # ``api.encryption`` is validated by a custom function in ESPHome
    # so the schema generator emits only ``{key: Optional, docs: ...}``
    # — no inner schema, no type. The actual YAML shape is a small
    # mapping with one optional ``key`` (the pre-shared encryption
    # key). Render as a nested group on the main form so the user can
    # toggle it on and (optionally) supply the key.
    ("api", "encryption"): {
        "type": "nested",
        "advanced": False,
        "config_entries": [
            {
                "key": "key",
                "type": "secure_string",
                "label": "Encryption key",
                "description": (
                    "Pre-shared base64-encoded key for encrypting API traffic. "
                    "Leave empty to let ESPHome generate one — Home Assistant "
                    "will read it back during pairing."
                ),
                "required": False,
                "advanced": False,
                "help_link": ("https://esphome.io/components/api#configuration-variables"),
            },
        ],
    },
    # ``wifi.ap`` is wrapped in a custom validator (``wifi_network_ap``)
    # so the schema bundle drops the inner schema and types it as a
    # bare string. The actual YAML shape is a fallback access point
    # — same fields as a network entry plus ``ap_timeout``. Surface it
    # as a nested group on the main form (it's a feature users
    # actively configure for offline recovery, not an advanced knob)
    # and rename the label away from the schema's bare ``Ap``.
    ("wifi", "ap"): {
        "type": "nested",
        "label": "Fallback Access Point",
        "description": (
            "Bring up an access point on the device when it can't reach "
            "the configured WiFi network. Pair with `captive_portal:` "
            "or `web_server:` so the user can connect to the AP and "
            "reconfigure WiFi from a phone."
        ),
        "advanced": False,
        "help_link": "https://esphome.io/components/wifi#access-point-mode",
        "config_entries": [
            {
                "key": "ssid",
                "type": "string",
                "label": "SSID",
                "description": (
                    "Name of the access point to create. Leave empty to use the device name."
                ),
                "help_link": "https://esphome.io/components/wifi#access-point-mode",
            },
            {
                "key": "password",
                "type": "secure_string",
                "label": "Password",
                "description": ("Password for the access point. Leave empty for an open network."),
                "help_link": "https://esphome.io/components/wifi#access-point-mode",
            },
            {
                "key": "channel",
                "type": "integer",
                "label": "Channel",
                "description": ("2.4GHz channel the AP should operate on (1-14). Defaults to 1."),
                "default_value": 1,
                "range": [1, 14],
                "advanced": True,
                "help_link": "https://esphome.io/components/wifi#access-point-mode",
            },
            {
                "key": "ap_timeout",
                "type": "time_period",
                "label": "AP Timeout",
                "description": (
                    "Time without a station connection before the "
                    "fallback access point comes up. Set to `0s` to "
                    "disable automatic startup. Defaults to `90s`."
                ),
                "default_value": "90s",
                "advanced": True,
                "help_link": "https://esphome.io/components/wifi#access-point-mode",
            },
            {
                "key": "manual_ip",
                "type": "nested",
                "label": "Manual IP",
                "description": (
                    "Manually set the IP options for the AP. Same "
                    "fields as the station-side `manual_ip:`."
                ),
                "advanced": True,
                "help_link": "https://esphome.io/components/wifi#access-point-mode",
                "config_entries": [
                    {
                        "key": "static_ip",
                        "type": "string",
                        "label": "Static IP",
                        "description": "The static IP of the AP.",
                        "required": True,
                        "help_link": "https://esphome.io/components/wifi#access-point-mode",
                    },
                    {
                        "key": "gateway",
                        "type": "string",
                        "label": "Gateway",
                        "description": "The gateway of the AP network.",
                        "required": True,
                        "help_link": "https://esphome.io/components/wifi#access-point-mode",
                    },
                    {
                        "key": "subnet",
                        "type": "string",
                        "label": "Subnet",
                        "description": "The subnet of the AP network.",
                        "required": True,
                        "help_link": "https://esphome.io/components/wifi#access-point-mode",
                    },
                    {
                        "key": "dns1",
                        "type": "string",
                        "label": "DNS 1",
                        "description": "The main DNS server for the AP.",
                        "default_value": "0.0.0.0",
                        "advanced": True,
                        "help_link": "https://esphome.io/components/wifi#access-point-mode",
                    },
                    {
                        "key": "dns2",
                        "type": "string",
                        "label": "DNS 2",
                        "description": "The backup DNS server for the AP.",
                        "default_value": "0.0.0.0",
                        "advanced": True,
                        "help_link": "https://esphome.io/components/wifi#access-point-mode",
                    },
                ],
            },
        ],
    },
    # ``uart.debug`` is wired through ``maybe_empty_debug`` (a custom
    # validator that accepts a bare ``debug:`` and substitutes ``{}``)
    # which hides ``DEBUG_SCHEMA`` from the bundle. The actual YAML is
    # a mapping with direction / prefix / accumulator settings.
    ("uart", "debug"): _UART_DEBUG_OVERRIDE,
    # ``ble_nus.debug`` reuses ``uart.maybe_empty_debug`` for the same
    # ``DEBUG_SCHEMA``. Mirror the override and just retitle the
    # description so it reads about BLE NUS traffic rather than UART.
    ("ble_nus", "debug"): {
        **_UART_DEBUG_OVERRIDE,
        "description": (
            "Log BLE NUS traffic to the ESPHome log for troubleshooting. "
            "Bare `debug:` enables hex logging with sensible defaults."
        ),
    },
}

# Key-name prefixes for automation triggers (``on_press``, ``on_value``,
# ``on_state_change``, ...). These are config-variables in YAML but the
# frontend's form editor isn't where users wire automations — the
# automation editor is. Skip them.
_AUTOMATION_KEY_PREFIXES: tuple[str, ...] = ("on_",)

# Base-schema references that mark a field as a *sub-reading* of a
# multi-sensor platform (DHT exposes ``temperature:`` / ``humidity:``;
# debug exposes ``free:`` / ``block:`` / etc). Sub-readings are
# optional by upstream schema, but they're the *reason* a multi-sensor
# platform exists — keeping them on the main form (not hidden under
# "Show advanced settings") is what users expect (#983).
_SUB_READING_BASE_SCHEMAS: frozenset[str] = frozenset(
    {
        "sensor._SENSOR_SCHEMA",
        "binary_sensor._BINARY_SENSOR_SCHEMA",
        "text_sensor._TEXT_SENSOR_SCHEMA",
    }
)

# Map from the ``**type**`` doc prefix marker to our ConfigEntryType.
# The schema docs lead with bracketed type names (``**[Time](...)**:``)
# or bold scalars (``**boolean**:``); we strip the markup and look up
# the resulting key here.
_DOC_PREFIX_TYPES: dict[str, str] = {
    "Time": "time_period",
    "Time Period": "time_period",
    "MAC Address": "mac_address",
    "MAC": "mac_address",
    "Pin": "pin",
    "Color": "color",
    "Lambda": "lambda",
    "Icon": "icon",
    "boolean": "boolean",
    "float": "float",
    "string": "string",
    "int": "integer",
    "Action": "nested",
    "Automation": "nested",
}

# Time-period default values are short strings like ``"60s"``,
# ``"5min"``, ``"1h30s"``. Each segment is a digit run + a fixed
# unit; the repeating group sticks to the same closed unit
# alternation rather than ``\w+`` so the engine can't backtrack
# exponentially on inputs like ``"9s9" + "00" * N`` (CodeQL
# ReDoS alert). The caller pre-strips whitespace so no ``\s*``
# is needed here either.
_TIME_PERIOD_DEFAULT = re.compile(
    r"^\d+(?:\.\d+)?(?:ms|us|ns|min|s|h|d)"
    r"(?:\d+(?:\.\d+)?(?:ms|us|ns|min|s|h|d))*$"
)


class Visibility(StrEnum):
    """Consumer-side mirror of upstream esphome's ``cv.Visibility``.

    Upstream (esphome/esphome#16267, 2026.5.0b1) models the
    schema-author UI hint as a ``StrEnum`` and dumps the string
    form (``"advanced"`` / ``"yaml_only"``) onto each field. The
    key is absent when the author didn't mark the field. Mirror
    that as a ``StrEnum`` here so the consumer compares against
    a typed value rather than bare string literals; the enum
    member's string value is what the dumper emits, so
    ``raw["visibility"] == Visibility.ADVANCED`` works directly.

    Two-tier strictness ordering: ``YAML_ONLY`` is strictly
    stronger than ``ADVANCED``, which is strictly stronger than
    no setting at all. The cascade pass below relies on that
    ordering.
    """

    ADVANCED = "advanced"
    YAML_ONLY = "yaml_only"


# Base entity / framework fields that always render under "Advanced" by
# default — valid but rarely tweaked. Same set as the previous sync.
_ADVANCED_BASE_KEYS: frozenset[str] = frozenset(
    {
        "internal",
        "disabled_by_default",
        "entity_category",
        "state_class",
        "accuracy_decimals",
        "force_update",
        "setup_priority",
        "expire_after",
        "filters",
        "interlock",
        "interlock_wait_time",
        # MQTT entity options
        "qos",
        "retain",
        "discovery",
        "subscribe_qos",
        "state_topic",
        "command_topic",
        "availability",
        # Zigbee entity options
        "zigbee_sensor",
        "zigbee_switch",
        "zigbee_binary_sensor",
        "zigbee_button",
        "zigbee_cover",
        "zigbee_climate",
        "zigbee_fan",
        "zigbee_light",
        "zigbee_lock",
        "zigbee_number",
        "zigbee_select",
        "zigbee_text",
        "zigbee_text_sensor",
    }
)

# Order in which entries appear in the rendered form. The advanced/
# main-form split is decided separately — this just controls relative
# rank within each section.
_IMPORTANT_KEY_ORDER: tuple[str, ...] = (
    # Discriminators first — they decide which other fields render.
    "platform",
    "type",
    "framework",  # esp32 / esp8266 framework selector (arduino vs esp-idf)
    # Identification
    "name",
    "friendly_name",
    "icon",
    # Credentials / connection
    "ssid",
    "password",
    "broker",
    "username",
    # Hardware
    "pin",
    "address",
    "i2c_id",
    "spi_id",
    "uart_id",
    # Behaviour
    "device_class",
    "unit_of_measurement",
    "restore_mode",
    "update_interval",
    "model",
    "variant",
    "inverted",
    "level",  # logger.level — most users want to see/pick this
    # Common esphome-block metadata
    "area",
    "areas",
    "comment",
    # Important fields that stay flagged advanced — keep their sort
    # priority but render under the "Advanced" section.
    "id",
)
_IMPORTANT_KEYS: frozenset[str] = frozenset(_IMPORTANT_KEY_ORDER)
# Subset of important keys that stay flagged advanced (id keeps its
# sort priority but always lives under the advanced section).
_ADVANCED_IMPORTANT_KEYS: frozenset[str] = frozenset({"id"})

# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point — parse args, fetch schema, generate JSON."""
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    parser = argparse.ArgumentParser(
        description=(
            "Generate components.index.json + per-id body files under "
            "definitions/components/ from ESPHome's pre-built schema bundle."
        ),
    )
    parser.add_argument(
        "--version",
        help="ESPHome release tag to use (e.g. '2026.4.3'). Defaults to the latest GitHub release.",
    )
    parser.add_argument(
        "--include-prereleases",
        action="store_true",
        help="When auto-selecting the latest release, also consider prereleases.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe cached schemas before fetching.",
    )
    parser.add_argument(
        "--limit-component",
        action="append",
        default=[],
        help="If given, only emit catalog entries for the listed component "
        "ids (e.g. ``--limit-component dht --limit-component wifi``). "
        "For local debugging.",
    )
    args = parser.parse_args()

    if args.clean and _CACHE_ROOT.exists():
        for d in _CACHE_ROOT.glob("esphome-schema-*"):
            shutil.rmtree(d)

    version = args.version or resolve_latest_release(
        include_prereleases=args.include_prereleases,
    )
    _LOGGER.info("Using ESPHome schema version: %s", version)

    schema_dir = ensure_schema(version)
    _LOGGER.info("Schema cached at: %s", schema_dir)

    catalog = build_catalog(
        schema_dir=schema_dir,
        limit=set(args.limit_component) or None,
    )
    _LOGGER.info(
        "Built catalog: %d components, %d with config entries",
        len(catalog),
        sum(1 for c in catalog if c.get("config_entries")),
    )

    _audit_catalog_for_unit_mismatches(catalog)

    _emit_split_catalog(catalog, version)

    # Second pass: walk the same schema bundle for action / condition /
    # trigger / effect registries and emit the automation catalog. Runs
    # after ``build_catalog`` so the per-component schema cache (extends
    # resolution, _convert_field's bookkeeping) is already warm. The
    # set of component ids built above is passed in so the automations
    # generator can distinguish a real ``<domain>.<platform>`` pair
    # (``switch.template`` — exists in the catalog) from an
    # organisational namespace in the schema's ``<stem>.<base>`` key
    # (``page.display`` — no ``display.page`` component): the latter
    # flattens to bare ``<domain>`` so the action surfaces whenever
    # a matching base domain is configured.
    component_ids = {c["id"] for c in catalog}
    automations = build_automations(schema_dir=schema_dir, component_ids=component_ids)
    _LOGGER.info(
        "Built automations catalog: %d triggers, %d actions, %d conditions, %d effects",
        len(automations["triggers"]),
        len(automations["actions"]),
        len(automations["conditions"]),
        len(automations["light_effects"]),
    )
    _emit_split_automations_catalog(automations, version)
    return 0


# ---------------------------------------------------------------------------
# Schema fetcher (versioned, cached)
# ---------------------------------------------------------------------------


def resolve_latest_release(*, include_prereleases: bool = False) -> str:
    """Return the latest release tag from the esphome-schema repo."""
    _LOGGER.info("Fetching latest release tag from GitHub...")
    releases = json.loads(_http_get(_RELEASES_API))
    for r in releases:
        if r.get("draft"):
            continue
        if r.get("prerelease") and not include_prereleases:
            continue
        return r["tag_name"]
    msg = "No suitable release found on esphome-schema repo"
    raise RuntimeError(msg)


def ensure_schema(version: str) -> Path:
    """Download and unpack the schema bundle for *version* if not cached."""
    cache_dir = _CACHE_ROOT / f"esphome-schema-{version}"
    schema_dir = cache_dir / "schema"
    if schema_dir.exists() and any(schema_dir.iterdir()):
        return schema_dir

    cache_dir.mkdir(parents=True, exist_ok=True)
    url = _SCHEMA_URL_TEMPLATE.format(version=version)
    _LOGGER.info("Downloading %s", url)
    data = _http_get(url, timeout=120)
    with zipfile.ZipFile(BytesIO(data)) as zf:
        zf.extractall(cache_dir)

    if not schema_dir.exists():
        msg = f"Schema bundle layout unexpected — missing {schema_dir}"
        raise RuntimeError(msg)
    return schema_dir


def _http_get(url: str, *, timeout: int = 30) -> bytes:
    """GET *url* with our identifying User-Agent and return raw bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Schema loader
# ---------------------------------------------------------------------------


@dataclass
class SchemaIndex:
    """Pre-built docs index for every component in the schema bundle.

    Three indexes feed this:

    - ``esphome.json -> core.components[<id>]`` — non-platform components
      (wifi, api, esp32_ble_tracker, ...) carrying ``docs`` and
      ``dependencies`` lists.
    - ``esphome.json -> core.platforms[<domain>]`` — the platform domain
      entries themselves (sensor, switch, ...).
    - ``<domain>.json -> <domain>.components[<id>]`` — every platform-
      providing component (dht under sensor, gpio under switch, ...).
      These carry only ``docs`` (no dependencies — derive from the
      domain).

    All three are merged under one key shape so callers don't need to
    know which index a component lives in.
    """

    # Maps the catalog id (``<domain>.<stem>`` for platform-providing
    # components, bare id otherwise) to its metadata block.
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_index(schema_dir: Path) -> SchemaIndex:
    """Read every index in the bundle into one merged SchemaIndex."""
    metadata: dict[str, dict[str, Any]] = {}

    # 1. esphome.json — core.components and core.platforms.
    try:
        core = (json.loads((schema_dir / "esphome.json").read_text()) or {}).get("core") or {}
    except FileNotFoundError:
        core = {}
    for cid, meta in (core.get("components") or {}).items():
        metadata[cid] = meta or {}
    for pid, meta in (core.get("platforms") or {}).items():
        metadata[pid] = meta or {}

    # 2. Each <domain>.json — domain.components map. Key under both the
    # bare stem and the qualified ``<domain>.<stem>`` form so lookups
    # work regardless of which the caller has on hand. Sorted so
    # ``setdefault`` keeps the same domain's metadata on every run
    # when two domains describe the same stem.
    for domain in sorted(_PLATFORM_DOMAINS):
        domain_file = schema_dir / f"{domain}.json"
        if not domain_file.exists():
            continue
        try:
            domain_raw = json.loads(domain_file.read_text())
        except Exception:  # noqa: S112 — index-only file, broken JSON is non-fatal
            continue
        section = domain_raw.get(domain) or {}
        for cid, meta in (section.get("components") or {}).items():
            qualified = f"{domain}.{cid}"
            metadata.setdefault(qualified, meta or {})
            metadata.setdefault(cid, meta or {})

    return SchemaIndex(metadata=metadata)


def iter_schema_files(schema_dir: Path) -> Iterable[Path]:
    """Yield every <component>.json under *schema_dir*."""
    yield from sorted(schema_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Description cleaner
# ---------------------------------------------------------------------------


# Leading ``**type**:`` prefix pasted in front of every field doc.
_DOCS_TYPE_PREFIX = re.compile(
    r"^\*\*[^*]+\*\*\s*[:\-]\s*",
)

# Trailing ``*See also: [Name](url)*`` link — we extract the URL as
# help_link / docs_url and then drop the footer from the description.
_DOCS_SEE_ALSO = re.compile(
    r"\s*\*See also:\s*\[([^\]]+)\]\(([^)]+)\)\*\s*$",
)


@dataclass
class CleanedDocs:
    text: str
    name: str | None = None  # extracted from "[Name](url)" link
    url: str | None = None


def clean_docs(raw: str | None) -> CleanedDocs:
    """Strip type prefix and ``See also`` footer; surface both as fields."""
    if not raw:
        return CleanedDocs("")
    text = raw.strip()
    name: str | None = None
    url: str | None = None
    m = _DOCS_SEE_ALSO.search(text)
    if m:
        name = m.group(1).strip()
        url = m.group(2).strip()
        text = text[: m.start()].rstrip()
    text = _DOCS_TYPE_PREFIX.sub("", text)
    return CleanedDocs(text=text.strip(), name=name, url=url)


# ---------------------------------------------------------------------------
# Build catalog (top-level)
# ---------------------------------------------------------------------------


def build_catalog(
    *,
    schema_dir: Path,
    limit: set[str] | None = None,
) -> list[dict]:
    """Walk every schema file and produce ConfigCatalogEntry-shaped dicts."""
    index = load_index(schema_dir)
    image_map = load_image_map()
    out: list[dict] = []
    for path in iter_schema_files(schema_dir):
        try:
            entries = build_entries_from_file(path, index, schema_dir, image_map)
        except Exception:
            _LOGGER.exception("Failed to build catalog entries from %s", path.name)
            continue
        for entry in entries:
            if limit and entry["id"] not in limit:
                continue
            # Skip ESPHome internal-helper / auto-load-target
            # components — they're noise in the picker. The runtime
            # ``ComponentCatalog.load`` carries the same filter so
            # this stays a redundant belt-and-braces; the actual
            # bug fix is the runtime side.
            if entry["id"] in _INTERNAL_COMPONENT_IDS:
                continue
            out.append(entry)

    # Workaround for an upstream esphome.io bug: see
    # ``_repair_field_bullet_descriptions``.
    _repair_field_bullet_descriptions(out)

    # Layer MDX-frontmatter descriptions onto components whose
    # schema-supplied description is empty. This patches the upstream
    # gap where the prebuilt schema's component index lists per-platform
    # components with only ``dependencies`` (e.g. ``ota.esphome``,
    # ``ota.http_request``).
    _backfill_descriptions_from_mdx(out)

    # Synthesise umbrella entries for legacy bare-key domains so that
    # ``get_component("ota")`` / ``get_component("time")`` resolve for
    # users still on the pre-platform YAML form. Runs after MDX
    # backfill so the umbrellas live alongside fully-populated
    # platform entries.
    #
    # Skipped under ``--limit-component`` because the umbrella
    # description enumerates every platform in *out* — on a filtered
    # catalog that list would only reflect the surviving subset and
    # mislead the reader. ``--limit-component`` is documented as a
    # local-debugging knob, so debugging individual platform entries
    # doesn't need the umbrellas anyway.
    if not limit:
        _inject_umbrella_entries(out)

    return out


# Matches a description that is actually the first bullet of an MDX
# ``### Configuration variables`` list — ``- **<key>** (*Optional*):`` or
# the ``*Required*`` variant. Used by ``_repair_field_bullet_descriptions``.
_FIELD_BULLET_PATTERN = re.compile(
    r"^-?\s*\*\*[A-Za-z_][\w]*\*\*\s*\(\s*\*(?:Optional|Required)\*\s*\)\s*[:\-]",
)


def _repair_field_bullet_descriptions(entries: list[dict]) -> None:
    """
    Repair descriptions baked from a stray first bullet of an MDX list.

    Workaround for an upstream bug in ``esphome.io``'s
    ``script/schema_doc.py``: when an MDX page documents a platform
    component with ``## <Platform>`` -> ``### Configuration variables``
    (no prose intro between the two headings -- ``debug.mdx`` is the
    canonical example), the generator's ``md_get_paragraph`` skips the
    headings but then accepts the first ``- **field** (*Optional*):``
    bullet as the paragraph, baking that bullet into the platform
    component's ``docs`` field. Affects ``sensor.debug`` /
    ``text_sensor.debug`` at the time of writing.

    For each affected ``<domain>.<stem>`` entry, swap the bullet for the
    catalog's own bare-stem entry's description -- that's the umbrella
    component the user is actually enabling when picking the entry from
    the wizard, and its description is the rich prose intro from the
    same MDX file. Skipped when ``<domain>`` is one of the synthetic-
    umbrella domains (``_UMBRELLA_ENTRIES`` -- currently ``ota``,
    ``time``), because in those cases ``<stem>`` is a platform name
    rather than a sub-component (``ota.esphome``'s stem ``esphome``
    would resolve to the unrelated core ``esphome`` component).
    Entries with no usable umbrella are left cleared so the downstream
    MDX backfill gets a turn.

    Remove this whole function (and the regex above) when the upstream
    fix lands and the schema bundle stops emitting these descriptions.
    """
    umbrella_domains = {spec["id"] for spec in _UMBRELLA_ENTRIES}
    by_id: dict[str, dict] = {e["id"]: e for e in entries}
    repaired = 0
    cleared = 0
    for entry in entries:
        desc = (entry.get("description") or "").strip()
        if not desc or not _FIELD_BULLET_PATTERN.match(desc):
            continue
        cid = entry["id"]
        umbrella_desc = ""
        if "." in cid:
            domain, stem = cid.split(".", 1)
            if domain not in umbrella_domains:
                umbrella = by_id.get(stem)
                if umbrella is not None:
                    umbrella_desc = (umbrella.get("description") or "").strip()
        if umbrella_desc:
            entry["description"] = umbrella_desc
            repaired += 1
        else:
            entry["description"] = ""
            cleared += 1
    if repaired or cleared:
        _LOGGER.info(
            "Repaired %d field-bullet description(s) from umbrella, cleared %d "
            "(upstream esphome.io bug)",
            repaired,
            cleared,
        )


def _backfill_descriptions_from_mdx(entries: list[dict]) -> None:
    """Fill empty names, descriptions and field docs from the docs MDX.

    The prebuilt schema's index sometimes only lists ``dependencies``
    for a component, and the per-field schema entries often omit the
    ``docs`` field entirely (notably the OTA platforms). The MDX docs
    page carries:

    - a curated frontmatter ``title:`` (e.g. "ESPHome OTA Updates" for
      ota.esphome — preferred over the stem-derived "ESPHome" we'd
      otherwise produce when the schema has no See-also link)
    - a frontmatter / intro ``description:``
    - a ``## Configuration variables`` bullet list of per-field docs

    Silently skipped when the docs repo can't be cloned/fetched.
    """
    descriptions = _load_mdx_descriptions()
    field_descriptions = _load_mdx_field_descriptions()
    titles = _load_mdx_titles()
    if not descriptions and not field_descriptions and not titles:
        return

    backfilled_components = 0
    backfilled_names = 0
    backfilled_fields = 0
    for entry in entries:
        cid = entry["id"]
        stem = cid.split(".", 1)[-1]

        # Name: when the schema had no See-also link, ``_resolve_name``
        # fell back to a title-cased stem (e.g. "ESPHome" for
        # ``ota.esphome``). The MDX title is more informative.
        if entry.get("name") == _stem_to_label(stem):
            mdx_title = titles.get(cid) or titles.get(stem)
            if mdx_title:
                entry["name"] = mdx_title
                backfilled_names += 1

        # Component-level description.
        if not (entry.get("description") or "").strip():
            text = descriptions.get(cid) or descriptions.get(stem)
            if text:
                entry["description"] = text
                backfilled_components += 1

        # docs_url: when the schema's See-also link is missing, derive
        # from the catalog id (matches the docs site's URL convention
        # ``/components/<domain>/<stem>/`` for platform-providing
        # components, ``/components/<bare>/`` for non-platform).
        if not entry.get("docs_url"):
            entry["docs_url"] = _derive_docs_url(cid)

        # Per-field descriptions inside config_entries.
        field_map = field_descriptions.get(cid) or field_descriptions.get(stem) or {}
        if field_map:
            backfilled_fields += _apply_field_descriptions(
                entry.get("config_entries") or [],
                field_map,
                docs_url=entry.get("docs_url") or "",
            )

    if backfilled_components or backfilled_fields or backfilled_names:
        _LOGGER.info(
            "Backfilled from docs MDX: %d names, %d descriptions, %d fields",
            backfilled_names,
            backfilled_components,
            backfilled_fields,
        )


def _inject_umbrella_entries(entries: list[dict]) -> None:
    """
    Add synthetic catalog entries for legacy bare-key domains.

    See ``_UMBRELLA_ENTRIES`` for the configured domains and their
    implicit default platforms. The description for each umbrella
    lists every platform present in *entries* under that domain so
    the text stays in sync with the schema as platforms are added or
    removed. Image URL is borrowed from the default platform's entry
    when available so the umbrella renders with the same icon.

    Skips an umbrella whose domain id already exists (defensive) or
    whose configured default platform is missing from the catalog —
    the latter would leave the description claiming a default that
    can't actually be selected.
    """
    by_id: dict[str, dict] = {e["id"]: e for e in entries}
    for spec in _UMBRELLA_ENTRIES:
        domain = spec["id"]
        if domain in by_id:
            continue
        default_qualified = f"{domain}.{spec['default_platform']}"
        default_entry = by_id.get(default_qualified)
        if default_entry is None:
            _LOGGER.warning(
                "Skipping %s umbrella entry: default platform %s not in catalog",
                domain,
                default_qualified,
            )
            continue
        platforms = sorted(cid.split(".", 1)[1] for cid in by_id if cid.startswith(f"{domain}."))
        platforms_csv = ", ".join(f"`{p}`" for p in platforms)
        description = (
            f"{spec['summary']}. When `{domain}:` is configured as a bare "
            f"mapping (no `- platform:` list — the legacy form), ESPHome "
            f"implicitly uses the `{spec['default_platform']}` platform. "
            f"Modern configs select a platform explicitly: available "
            f"platforms are {platforms_csv}."
        )
        umbrella: dict[str, Any] = {
            "id": domain,
            "name": spec["name"],
            "description": description,
            "category": spec["category"],
            "docs_url": spec["docs_url"],
        }
        if default_entry.get("image_url"):
            umbrella["image_url"] = default_entry["image_url"]
        entries.append(umbrella)
        _LOGGER.info(
            "Added umbrella entry %s (default: %s, platforms: %d)",
            domain,
            spec["default_platform"],
            len(platforms),
        )


def _stem_to_label(stem: str) -> str:
    """Recompute ``_resolve_name``'s fallback label for *stem*.

    Used to detect entries whose ``name`` came from the stem rather
    than a curated source — those are the ones we want to override
    with MDX titles.
    """
    name = stem.replace("_", " ").title()
    for k, v in _ACRONYM_NORMALISATIONS.items():
        name = re.sub(rf"\b{re.escape(k)}\b", v, name)
    return name


def _derive_docs_url(component_id: str) -> str:
    """Build the docs site URL for *component_id* using the canonical pattern.

    ESPHome's docs site mirrors the source repo layout:

        ``<domain>.<stem>`` → /components/<domain>/<stem>/
        ``<bare>``          → /components/<bare>/

    Used as a fallback when the schema's per-component ``docs`` field
    has no ``See also`` link (notably the OTA platforms).
    """
    if "." in component_id:
        domain, stem = component_id.split(".", 1)
        return f"https://esphome.io/components/{domain}/{stem}"
    return f"https://esphome.io/components/{component_id}"


def _apply_field_descriptions(
    config_entries: list[dict],
    field_descriptions: dict[str, str],
    *,
    docs_url: str,
    _depth: int = 0,
) -> int:
    """Apply per-field descriptions to entries that lack them.

    Only acts at the top level of the component's config — the MDX's
    ``## Configuration variables`` bullet list is flat, so applying a
    matching key inside a nested entry would mis-attribute prose
    (e.g. ``esphome.name``'s description leaking onto
    ``esphome.areas[].name``). Nested entries can still pick up
    descriptions later via their own component's MDX page when
    relevant (e.g. ``ota.esphome``'s fields), via the per-component
    backfill loop in ``_backfill_descriptions_from_mdx``.
    """
    backfilled = 0
    fragment_url = f"{docs_url}#configuration-variables" if docs_url else ""
    for entry in config_entries:
        if _depth > 0:
            continue
        key = entry["key"]
        if not (entry.get("description") or "").strip():
            text = field_descriptions.get(key)
            if text:
                entry["description"] = text
                backfilled += 1
                if fragment_url and not entry.get("help_link"):
                    entry["help_link"] = fragment_url
        inner = entry.get("config_entries")
        if inner:
            backfilled += _apply_field_descriptions(
                inner, field_descriptions, docs_url=docs_url, _depth=_depth + 1
            )
    return backfilled


def _load_mdx_descriptions() -> dict[str, str]:
    """Walk the cached docs repo, return ``{component_id: description}``.

    Each per-component MDX page lives under
    ``src/content/docs/components/<domain>/<stem>.mdx`` (platform-
    providing components) or ``src/content/docs/components/<bare>.mdx``
    (everything else). The frontmatter ``description:`` field is the
    primary source — short, curated, written for catalog/preview use.
    Falls back to the first prose paragraph when the frontmatter
    description is missing.

    Caches the cloned docs repo in ``.cache/esphome.io/`` so re-runs
    don't refetch.
    """
    docs_dir = _ensure_docs_repo()
    if docs_dir is None:
        return {}

    out: dict[str, str] = {}
    components_root = docs_dir / "src" / "content" / "docs" / "components"
    if not components_root.exists():
        return {}

    for mdx_path in components_root.rglob("*.mdx"):
        rel = mdx_path.relative_to(components_root)
        parts = rel.with_suffix("").parts
        if not parts or parts[-1] == "index":
            continue
        if len(parts) == 1:
            component_id = parts[0]
        elif len(parts) == 2:
            component_id = f"{parts[0]}.{parts[1]}"
        else:
            continue  # Deeper nesting isn't a per-component page.

        text = _extract_mdx_description(mdx_path.read_text(encoding="utf-8"))
        if text:
            out[component_id] = text
            # Also index under the bare stem if it's not already taken,
            # so e.g. ``ota.esphome`` falls back to ``esphome.mdx`` if
            # ever needed (rare, but cheap to support).
            stem = parts[-1]
            out.setdefault(stem, text)
    return out


def _load_mdx_titles() -> dict[str, str]:
    """Walk the cached docs repo, return ``{component_id: title}``.

    Each MDX page has a ``title:`` field in its frontmatter (e.g.
    "ESPHome OTA Updates"). Indexed by both the catalog id
    (``ota.esphome``) and the bare stem (``esphome``).
    """
    docs_dir = _ensure_docs_repo()
    if docs_dir is None:
        return {}

    out: dict[str, str] = {}
    components_root = docs_dir / "src" / "content" / "docs" / "components"
    if not components_root.exists():
        return {}

    for mdx_path in components_root.rglob("*.mdx"):
        rel = mdx_path.relative_to(components_root)
        parts = rel.with_suffix("").parts
        if not parts or parts[-1] == "index":
            continue
        if len(parts) == 1:
            component_id = parts[0]
        elif len(parts) == 2:
            component_id = f"{parts[0]}.{parts[1]}"
        else:
            continue

        title = _extract_mdx_title(mdx_path.read_text(encoding="utf-8"))
        if title:
            out[component_id] = title
            stem = parts[-1]
            out.setdefault(stem, title)
    return out


# Frontmatter title matcher — same shape as the description matcher.
_FRONTMATTER_TITLE = re.compile(
    r'^title:\s*"([^"]+)"|^title:\s*\'([^\']+)\'|^title:\s*([^\n]+)$',
    re.MULTILINE,
)


def _extract_mdx_title(text: str) -> str:
    """Return the curated ``title:`` from an MDX frontmatter block."""
    front_end = text.find("---", 4) if text.startswith("---") else -1
    front = text[:front_end] if front_end > 0 else ""
    m = _FRONTMATTER_TITLE.search(front)
    if not m:
        return ""
    return next(g for g in m.groups() if g).strip()


def _load_mdx_field_descriptions() -> dict[str, dict[str, str]]:
    """Walk the cached docs repo, return ``{component_id: {field: desc}}``.

    Same lookup convention as ``_load_mdx_descriptions`` — keyed by
    catalog id (``ota.esphome``) and bare stem (``esphome``). The
    inner map is ``{field_key: cleaned_description}``.

    Used to fill in per-field descriptions for components whose schema
    entries lack a ``docs`` field — most visibly the OTA platforms.
    """
    docs_dir = _ensure_docs_repo()
    if docs_dir is None:
        return {}

    out: dict[str, dict[str, str]] = {}
    components_root = docs_dir / "src" / "content" / "docs" / "components"
    if not components_root.exists():
        return {}

    for mdx_path in components_root.rglob("*.mdx"):
        rel = mdx_path.relative_to(components_root)
        parts = rel.with_suffix("").parts
        if not parts or parts[-1] == "index":
            continue
        if len(parts) == 1:
            component_id = parts[0]
        elif len(parts) == 2:
            component_id = f"{parts[0]}.{parts[1]}"
        else:
            continue

        fields = _extract_mdx_field_descriptions(mdx_path.read_text(encoding="utf-8"))
        if fields:
            out[component_id] = fields
            stem = parts[-1]
            out.setdefault(stem, fields)
    return out


# Top-level config-variable bullet line:
#   - **field_name** (*Optional*, type): Description text.
_CONFIG_VAR_LINE = re.compile(
    r"^- \*\*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\*\*[^:\n]*?:\s*(?P<desc>.*)$",
)


def _extract_mdx_field_descriptions(text: str) -> dict[str, str]:  # noqa: C901
    """Parse the ``## Configuration variables`` section into a field map.

    Captures one description per top-level bullet — including
    continuation lines from indented prose, but excluding nested
    sub-bullets and stopping at sub-headings (``###`` action /
    trigger sections).
    """
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)

    section_re = re.compile(
        r"^(?:##\s+Configuration variables\s*|Configuration variables:\s*)\n"
        r"(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = section_re.search(body)
    if not match:
        return {}

    descriptions: dict[str, str] = {}
    current_key: str | None = None
    current_parts: list[str] = []

    def commit() -> None:
        nonlocal current_key
        if current_key is None:
            return
        joined = " ".join(p for p in current_parts if p)
        cleaned = _clean_description_text(joined).rstrip(" .,:")
        if cleaned and cleaned[-1] not in ".!?":
            cleaned += "."
        if cleaned:
            descriptions[current_key] = cleaned

    for raw_line in match.group(1).splitlines():
        line = raw_line.rstrip()
        m = _CONFIG_VAR_LINE.match(line)
        if m:
            commit()
            current_key = m.group("name")
            current_parts = [m.group("desc").strip()] if m.group("desc").strip() else []
            continue
        if current_key is None:
            continue
        stripped = line.strip()
        # Block-quotes / GitHub alerts and sub-headings end the field.
        if stripped.startswith((">", "#")):
            commit()
            current_key = None
            current_parts = []
            continue
        # Sub-bullets describe sub-fields — skip.
        if stripped.startswith(("- ", "* ", "+ ")):
            continue
        if stripped:
            current_parts.append(stripped)

    commit()
    return descriptions


def _ensure_docs_repo() -> Path | None:
    """Clone or update the esphome.io repo (shallow). Returns its path."""
    import subprocess

    target = _CACHE_ROOT / _DOCS_CLONE_DIR
    if (target / ".git").exists():
        # Refresh in-place. ``-q`` and ``--ff-only`` keep it quiet and
        # safe; failure here just means we keep using the existing
        # snapshot.
        subprocess.run(
            ["git", "-C", str(target), "pull", "-q", "--ff-only"],
            check=False,
            timeout=60,
        )
        return target
    if target.exists():
        # Pre-existing non-git directory — leave alone, use as-is.
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("Cloning esphome.io (shallow) to %s", target)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth=1",
                "--single-branch",
                f"--branch={_DOCS_REPO_BRANCH}",
                _DOCS_REPO_URL,
                str(target),
            ],
            check=True,
            timeout=120,
        )
    except Exception:
        _LOGGER.warning("Could not clone esphome.io — descriptions stay empty")
        return None
    return target


# Frontmatter description matcher — captures the value of the
# ``description:`` field at the start of the file. Handles both quoted
# and bare values.
_FRONTMATTER_DESCRIPTION = re.compile(
    r'^description:\s*"([^"]+)"|^description:\s*\'([^\']+)\'|^description:\s*([^\n]+)$',
    re.MULTILINE,
)


def _extract_mdx_description(text: str) -> str:  # noqa: C901
    """Return the curated description for a component MDX file.

    Tries the frontmatter ``description:`` field first; falls back to
    the first prose paragraph (after frontmatter, skipping JSX imports
    and HTML anchors) if frontmatter has no description.
    """
    front_end = text.find("---", 4) if text.startswith("---") else -1
    front = text[:front_end] if front_end > 0 else ""
    body = text[front_end + 3 :] if front_end > 0 else text

    m = _FRONTMATTER_DESCRIPTION.search(front)
    if m:
        value = next(g for g in m.groups() if g)
        cleaned = _clean_description_text(value.strip())
        if cleaned:
            return cleaned

    # Fall back to the first prose paragraph.
    paragraphs: list[str] = []
    current: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        if line.startswith(("import ", "<", ":::", "#", "```", "{")):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))

    for p in paragraphs:
        cleaned = _clean_description_text(p)
        if cleaned:
            return cleaned
    return ""


# Markdown link / inline-code stripping for description text.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_ITALIC_RE = re.compile(r"\*{1,3}([^*]+)\*{1,3}")


def _clean_description_text(text: str) -> str:
    """Flatten markdown markup so descriptions read as plain prose."""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_ITALIC_RE.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    # ESPHome docs use a few stock leading phrases that don't add info.
    for phrase in (
        "Instructions for setting up the ",
        "Instructions for setting up ",
        "Instructions for using the ",
        "Instructions for using ",
    ):
        if text.lower().startswith(phrase.lower()):
            text = text[len(phrase) :]
            text = text[:1].upper() + text[1:] if text else text
            break
    return text


def load_image_map() -> dict[str, str]:
    """Parse the docs ``components/index.mdx`` for image URLs.

    The index page renders a tiled list of components where each entry
    is a JSX-array literal:

        ["Name", "/components/<category>/<id>/", "<image>.svg", ...]

    We match those rows and produce a ``component_id -> image_url`` map
    where ``component_id`` matches our catalog ids (qualified with
    ``<domain>.<id>`` for platform-providing components).

    No ImagesMap if the docs file can't be fetched — image_url stays
    empty for every component.
    """
    cache_file = _CACHE_ROOT / "esphome.io-index.mdx"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if not cache_file.exists():
        try:
            cache_file.write_bytes(_http_get(_DOCS_INDEX_URL))
        except Exception:
            _LOGGER.warning(
                "Could not fetch docs index page — image URLs will be empty",
            )
            return {}

    text = cache_file.read_text(errors="ignore")
    pattern = re.compile(
        r'\["([^"]+)",\s*"(/components/[^"]+)",\s*"([^"]+)"',
    )
    out: dict[str, str] = {}
    for _name, path, image in pattern.findall(text):
        # Path examples:
        #   /components/wifi/         -> "wifi"
        #   /components/sensor/dht/   -> "sensor.dht"
        #   /components/sensor/       -> "sensor" (the domain page itself)
        parts = [p for p in path.strip("/").split("/")[1:] if p]
        if not parts:
            continue
        component_id = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else parts[0]
        out.setdefault(component_id, _IMAGE_BASE_URL + image)
        # Also store under the bare stem when only one platform exists,
        # so lookups by either id work.
        if len(parts) >= 2:
            out.setdefault(parts[1], _IMAGE_BASE_URL + image)
    _LOGGER.info("Image map built: %d components", len(out))
    return out


def build_entries_from_file(
    path: Path,
    index: SchemaIndex,
    schema_dir: Path,
    image_map: dict[str, str],
) -> list[dict]:
    """Build one or more catalog entries from a single schema JSON file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for top_key, section in raw.items():
        if top_key in _HIDDEN_TOP_LEVEL:
            continue
        if not isinstance(section, dict):
            continue
        entry = build_component_entry(top_key, section, index, schema_dir, image_map)
        if entry is not None:
            out.append(entry)
    return out


def build_component_entry(
    top_key: str,
    section: dict,
    index: SchemaIndex,
    schema_dir: Path,
    image_map: dict[str, str],
) -> dict | None:
    """Convert one ``<id>.json`` top-level entry to our catalog shape.

    The schema's qualifier order is ``<stem>.<domain>`` (e.g.
    ``dht.sensor``). We surface ids as ``<domain>.<stem>`` to match the
    rest of our codebase.

    Returns None for entries that don't represent a user-facing
    component: bare platform-domain headers (``sensor``, ``switch``)
    and schema-only entries (``bme280_base``, ``as3935`` hub) without
    their own ``CONFIG_SCHEMA``.
    """
    if not _has_config_schema(section):
        return None

    domain, stem = _split_qualified_key(top_key)
    if domain in _PLATFORM_DOMAINS:
        category = domain
        component_id = f"{domain}.{stem}"
    elif top_key in _PLATFORM_DOMAINS:
        # The bare platform domain itself (sensor:, switch:, ...) — not
        # a user-facing component. Skip.
        return None
    else:
        category = _infer_misc_category(top_key)
        component_id = top_key

    config_entries = _extract_config_entries(
        section,
        schema_dir=schema_dir,
        component_id=component_id,
    )

    meta = _lookup_index_meta(component_id, top_key, index)
    docs = clean_docs(meta.get("docs"))
    dependencies = list(meta.get("dependencies") or [])

    # Drop deps the chosen networking transport will auto-load. See
    # ``_implicit_dependencies``.
    implicit = _implicit_dependencies()
    if implicit:
        dependencies = [d for d in dependencies if d not in implicit]

    # Narrow esphome introspection — adds multi_conf, platform_defaults,
    # supported_platforms, and refined types (boolean/float/...) the
    # schema bundle doesn't surface. No-ops when esphome isn't
    # importable.
    introspection = introspect_component(stem if domain else top_key)
    _apply_platform_defaults(config_entries, introspection.get("platform_defaults") or {})
    _apply_platform_constraints(config_entries, introspection.get("platform_constraints") or {})
    _apply_field_ranges(config_entries, introspection.get("field_ranges") or {})
    _apply_refined_types(config_entries, introspection.get("refined_types") or {})
    _apply_inclusive_groups(config_entries, introspection.get("inclusive_groups") or {})
    _apply_unit_of_measurement_options(config_entries)
    _promote_multi_value_keys(config_entries)

    component = {
        "id": component_id,
        "name": _resolve_name(component_id, stem, docs.name),
        "description": docs.text,
        "category": category,
        "docs_url": _strip_anchor(docs.url or ""),
        "image_url": image_map.get(component_id) or image_map.get(stem) or "",
        "dependencies": dependencies,
        "multi_conf": introspection.get("multi_conf", False),
        "supported_platforms": _derive_supported_platforms(
            stem if domain else top_key,
            dependencies,
            introspection,
        ),
        "config_entries": config_entries,
    }
    # Required-groups straddle the component root (path ``()``) and
    # nested ``NESTED`` entries; the applier needs the whole
    # component dict to stamp both locations.
    _apply_required_groups(component, introspection.get("required_groups") or {})
    # Prepend a markdown hint to each constraint-involved field's
    # description so an older frontend (one that doesn't yet
    # consume ``required_groups`` / ``group``) still surfaces the
    # rule to the user as readable prose — issue #924. Drops out
    # naturally once the FE renders the structured fields inline.
    _annotate_constraint_descriptions(component)
    return component


# ---------------------------------------------------------------------------
# Schema → ConfigEntry conversion
# ---------------------------------------------------------------------------


def _scalar_type_for_extends_ref(ref: str) -> str | None:
    """Return the scalar entry type *ref* names, or None for mapping refs.

    Centralises the heuristic that decides whether a schema's
    ``extends: ["core.X"]`` reference resolves to a scalar primitive
    (time period, float, integer, lambda body) or to a sibling mapping
    schema (``sensor.DELTA_SCHEMA`` etc.) — used both at field-level
    (``_convert_field``) and at registry-entry-level
    (``_is_scalar_extends_schema``).
    """
    if "time_period" in ref:
        return "time_period"
    if ref.endswith((".positive_float", ".float_")):
        return "float"
    if "positive_int" in ref or ref.endswith(".int_"):
        return "integer"
    if "returning_lambda" in ref:
        return "lambda"
    return None


def _extract_config_entries(
    section: dict,
    *,
    schema_dir: Path,
    component_id: str = "",
) -> list[dict]:
    """Walk ``schemas.CONFIG_SCHEMA`` and produce our ConfigEntry list.

    Resolves ``extends`` references inline so the entry list reflects
    the merged schema the user will see (e.g. ``dht.sensor.humidity``
    inherits the base ``sensor._SENSOR_SCHEMA`` fields). The
    ``component_id`` is used to filter ``_DEPRECATED_FIELDS`` at the
    top level only — nested fields with the same name are unaffected.
    """
    schemas = section.get("schemas") or {}
    config_schema = schemas.get("CONFIG_SCHEMA") or {}
    schema = config_schema.get("schema") or {}
    if not schema:
        return []
    entries = _convert_config_vars(schema, schema_dir, component_id=component_id)
    # Apply the visibility-cascade rule once at the top of the
    # tree: a stricter parent forces all descendants at-least
    # as strict. ``YAML_ONLY`` > ``ADVANCED`` > no setting. The
    # cascade is at-the-top so nested-NESTED structures get the
    # full chain of ancestors considered without recursive
    # bookkeeping inside ``_convert_field``.
    _apply_visibility_cascade(entries, parent_advanced=False, parent_yaml_only=False)
    return entries


def _apply_visibility_cascade(
    entries: list[dict],
    *,
    parent_advanced: bool,
    parent_yaml_only: bool,
) -> None:
    """In-place push parent strictness onto descendants.

    ``YAML_ONLY`` (mapped to ``hidden=True``) is strictly stronger
    than ``ADVANCED`` (``advanced=True``), which is strictly
    stronger than the un-marked default. A child can declare its
    own setting independently — but the child's *effective*
    setting after this pass is ``max(parent_chain, self)``.

    The rationale is UX: if a parent block is "advanced", every
    field inside it is at-least advanced (otherwise the disclosure
    is leaky — you'd hide the parent header but render a child on
    the main form). Same one level deeper for ``YAML_ONLY`` — a
    block hidden from the editor must hide every descendant or
    the user gets a half-rendered control with no way to set the
    surrounding context.
    """
    for entry in entries:
        own_advanced = entry.get("advanced", False)
        own_hidden = entry.get("hidden", False)
        # Strictness ordering: ``YAML_ONLY`` (``hidden=True``) is
        # strictly stronger than ``ADVANCED`` (``advanced=True``).
        # Apply that locally first — a self-hidden entry is also
        # implicitly advanced — then OR with the parent's
        # strictness so the cascade pushes both flags down.
        entry["advanced"] = own_advanced or own_hidden or parent_advanced or parent_yaml_only
        entry["hidden"] = own_hidden or parent_yaml_only
        # Recurse into NESTED groups, MAP value templates, and any
        # other shape that carries inner ``config_entries``. The
        # child's effective state becomes the parent state for the
        # next level.
        inner = entry.get("config_entries")
        if isinstance(inner, list):
            _apply_visibility_cascade(
                inner,
                parent_advanced=entry["advanced"],
                parent_yaml_only=entry["hidden"],
            )


def _convert_config_vars(  # noqa: C901
    schema_node: dict,
    schema_dir: Path,
    *,
    component_id: str = "",
) -> list[dict]:
    """Convert a ``schema`` node (config_vars + extends) to a list of entries."""
    config_vars = dict(schema_node.get("config_vars") or {})

    # Inline ``extends`` references and merge them with the local
    # config_vars. The schema uses partial overrides — entity
    # sub-readings like ``dht.sensor.humidity.device_class`` only
    # specify ``{"default": "humidity"}`` and inherit the rest
    # (``type: enum``, the 60-value list of accepted device classes,
    # the docs string) from ``sensor._SENSOR_SCHEMA``. A flat
    # ``{**extended, **local}`` would replace the whole field,
    # silently dropping the values list. Deep-merge per-field so
    # local entries override individual keys but inherited metadata
    # survives.
    extended: dict[str, dict] = {}
    for ref in schema_node.get("extends") or []:
        extended.update(_resolve_extends(ref, schema_dir))
    merged: dict[str, dict] = {}
    for key in {*extended, *config_vars}:
        base = extended.get(key)
        local = config_vars.get(key)
        if isinstance(base, dict) and isinstance(local, dict):
            merged[key] = {**base, **local}
        else:
            merged[key] = local if local is not None else base or {}

    out: list[dict] = []
    for key, raw in merged.items():
        if key in _SKIP_KEYS:
            continue
        if (component_id, key) in _DEPRECATED_FIELDS:
            continue
        if any(key.startswith(p) for p in _AUTOMATION_KEY_PREFIXES):
            continue
        entry = _convert_field(key, raw or {}, schema_dir)
        if entry is None:
            continue
        # Per-(component, field) overrides patch up entries the schema
        # generator couldn't model (e.g. ``api.encryption``). Deep-copy
        # so downstream apply-* passes can mutate ``config_entries``
        # in place without leaking the change back into the static
        # ``_FIELD_OVERRIDES`` dict (and across components when two
        # entries share a shape, like ``uart.debug`` / ``ble_nus.debug``).
        override = _FIELD_OVERRIDES.get((component_id, key))
        if override is not None:
            entry = {**entry, **copy.deepcopy(override)}
        # Cross-cutting infrastructure fields are only meaningful when
        # the named component is configured. Tag them so the frontend
        # can hide them by default.
        gate = _COMPONENT_GATED_KEYS.get(key)
        if gate and not entry.get("depends_on_component"):
            entry["depends_on_component"] = gate
        out.append(entry)
    return _sort_entries(out)


def _resolve_extends(ref: str, schema_dir: Path) -> dict[str, dict]:  # noqa: C901
    """Look up an ``extends`` reference and return its config_vars.

    *ref* is shaped ``<file>.<schema_name>`` — e.g.
    ``sensor._SENSOR_SCHEMA``, ``core.positive_time_period_milliseconds``.
    For schemas that themselves carry ``extends`` we recurse so the full
    ancestry is flattened into one config_vars dict.
    """
    parts = ref.split(".")
    if len(parts) < 2:
        return {}
    file_name = parts[0]
    schema_name = ".".join(parts[1:])

    # ``<file_name>.json`` is the obvious lookup, but a few shared
    # scopes are housed in ``esphome.json`` under a top-level key
    # matching their ref prefix — most importantly ``core``, which
    # holds ``ENTITY_BASE_SCHEMA`` (the inheritance source for the
    # entity-level ``name`` / ``icon`` / ``internal`` /
    # ``disabled_by_default`` / ``entity_category`` fields). Without
    # the esphome.json fallback those fields silently disappear from
    # every entity-platform component (binary_sensor.gpio,
    # output.gpio, sensor.aht10, ...).
    candidates: list[dict] = []
    for path in (schema_dir / f"{file_name}.json", schema_dir / "esphome.json"):
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        for top_value in raw.values():
            if not isinstance(top_value, dict):
                continue
            schemas = top_value.get("schemas") or {}
            if schema_name in schemas:
                candidates.append(schemas[schema_name])
        if candidates:
            break

    if not candidates:
        return {}
    target = candidates[0]
    schema_node = target.get("schema") or {}
    inner = dict(schema_node.get("config_vars") or {})
    # Recurse into nested extends so the merged map carries everything.
    for sub in schema_node.get("extends") or []:
        for k, v in _resolve_extends(sub, schema_dir).items():
            inner.setdefault(k, v)
    return inner


def _convert_field(key: str, raw: dict, schema_dir: Path) -> dict | None:  # noqa: PLR0912, PLR0915, C901
    """Build a single ConfigEntry dict from a schema's config_var entry."""
    if not isinstance(raw, dict):
        # Some schemas use bare ``{}``-shaped placeholders for fields
        # whose details live in an extends-referenced base. Treat as
        # plain string optional.
        raw = {}

    # Required vs Optional vs GeneratedID
    schema_key = raw.get("key")
    required = schema_key == "Required"

    schema_type = raw.get("type")
    inner_schema = raw.get("schema")
    data_type = raw.get("data_type")

    # Own-id fields ⇒ always rendered as a free-form id input. The
    # ``key: GeneratedID`` form auto-generates and stays under
    # "Advanced"; the ``key: Required`` + ``id_type`` form is a
    # required user-supplied id (e.g. ``output.gpio.id``).
    if _is_own_id_field(raw):
        return _build_id_entry(key, raw, required=required)

    # Resolve the entry type. Priority: explicit type → data_type
    # → enum shape → extends → docs hints → default-value hints → string.
    docs_text = raw.get("docs") or ""
    extends = (inner_schema or {}).get("extends") if isinstance(inner_schema, dict) else None

    entry_type = _TYPE_MAP.get(schema_type or "")
    if entry_type is None and data_type in _DATA_TYPE_PRIMITIVE:
        entry_type = _DATA_TYPE_PRIMITIVE[data_type]

    # Polymorphic registry list (#941). Two upstream shapes:
    #   1. Lights' ``effects:`` carries ``{filter: [<ids>], key:
    #      Optional}`` with no ``type`` — collapse to the
    #      ``light_effects`` catalog.
    #   2. Sensors' / binary_sensors' / text_sensors' ``filters:``
    #      carries ``{type: registry, registry: <domain>.filter,
    #      is_list: true}`` — collapse to the shared ``filter``
    #      catalog (dedupe across domains).
    # The frontend's REGISTRY_LIST renderer pulls the matching
    # catalog and renders one row per item with a type picker.
    registry_name: str | None = None
    if key == "effects" and isinstance(raw.get("filter"), list) and raw["filter"]:
        entry_type = "registry_list"
        registry_name = "light_effects"
    elif (
        schema_type == "registry"
        and raw.get("is_list")
        and isinstance(raw.get("registry"), str)
        and raw["registry"].endswith(".filter")
    ):
        entry_type = "registry_list"
        registry_name = "filter"

    # An ``enum`` whose values are ``true`` and ``false`` is really a
    # boolean — the schema uses cv.boolean which produces this shape.
    if (entry_type == "string" or entry_type is None) and _looks_like_boolean_enum(raw):
        entry_type = "boolean"
    elif entry_type is None and "values" in raw:
        entry_type = "string"  # enum-shaped (options below)

    # ``extends: ["core.positive_time_period_*"]`` collapses to time_period
    # — even when the schema marked the entry as ``type: schema`` (most
    # _SENSOR_SCHEMA fields like ``expire_after`` come through that way).
    if extends and not (inner_schema or {}).get("config_vars"):
        for ref in extends:
            scalar = _scalar_type_for_extends_ref(ref)
            if scalar is not None:
                entry_type = scalar
                break

    # Docs-prefix hints — fields without explicit type lead with
    # ``**type**:`` markers we can parse out.
    if entry_type is None:
        prefix = _docs_type_marker(docs_text)
        entry_type = _DOC_PREFIX_TYPES.get(prefix)

    # Default-value hints — bare time strings like ``"60s"``, ``"5min"``.
    if entry_type is None and _looks_like_time_period_default(raw.get("default")):
        entry_type = "time_period"

    # Key-name fallback — ``icon`` / ``mac_address`` are usually
    # untyped strings in the schema.
    if entry_type is None and key == "icon":
        entry_type = "icon"

    if entry_type is None and inner_schema and inner_schema.get("config_vars"):
        entry_type = "nested"
    if entry_type is None:
        entry_type = "string"

    # Type promotion: prefer the explicit ``"sensitive"`` flag the upstream
    # esphome dumper emits for fields wrapped in ``cv.sensitive(...)`` (added
    # in esphome/esphome#16673; explicit migrations in #16677 cover api/wifi/
    # ota/mqtt/web_server passwords plus SSIDs); fall back to the local
    # key-name heuristic for older esphome versions that don't carry the
    # flag, or for unmigrated/third-party schemas.
    if entry_type == "string" and (
        raw.get("sensitive") or any(frag in key.lower() for frag in _SECRET_KEY_FRAGMENTS)
    ):
        entry_type = "secure_string"

    # Cleaned docs ⇒ description + help_link/docs_url candidate.
    docs = clean_docs(raw.get("docs"))
    references = _resolve_use_id_reference(raw)

    # Structural fields (wiring + pin selection) are kept on the main
    # form even when optional — users almost always want to see what's
    # wired to what.
    is_structural = entry_type == "pin" or bool(references)
    # Schema-author UI hint from upstream esphome
    # (esphome/esphome#16267): the dumper emits ``"visibility":
    # "advanced" | "yaml_only"`` for fields whose ``cv.Optional`` /
    # ``cv.Required`` set ``visibility=Visibility.ADVANCED`` or
    # ``=Visibility.YAML_ONLY``. Absent → fall back to the name-based
    # heuristic (the long tail of fields the schema doesn't yet
    # annotate; as upstream adoption grows the heuristic rules out
    # of ``_classify_advanced`` can shrink toward zero).
    #
    # The cascade rule (a stricter parent forces its descendants
    # at-least as strict) is applied after leaf conversion by
    # :func:`_apply_visibility_cascade`. This function records the
    # per-field setting as the schema author wrote it; the cascade
    # pass walks the resulting tree and pushes parent strictness
    # down where descendants would otherwise be more visible.
    schema_visibility = raw.get("visibility")
    advanced = schema_visibility == Visibility.ADVANCED or _classify_advanced(
        key, required=required, is_structural=is_structural
    )
    yaml_only = schema_visibility == Visibility.YAML_ONLY
    # Sub-sensor readings on multi-sensor platforms (DHT temperature /
    # humidity, debug.sensor's free / block / loop_time / ..., ADS1115's
    # named ADC reads) extend a base sensor schema; the bundle marks
    # the reference in ``schema.extends``. They're optional, so
    # ``_classify_advanced`` defaults them to advanced — but they're
    # the whole reason a multi-sensor platform exists, so surface them
    # on the main form rather than under "Show advanced settings"
    # (#983).
    if extends and _SUB_READING_BASE_SCHEMAS.intersection(extends):
        advanced = False

    default_value, gated_component = _extract_default(raw, key=key)
    entry: dict[str, Any] = {
        "key": key,
        "type": entry_type,
        "label": _key_to_label(key),
        "description": docs.text or None,
        "required": required,
        "default_value": default_value,
        "options": _build_options(raw),
        "allow_custom_value": False,
        "range": list(_DATA_TYPE_RANGE[data_type]) if data_type in _DATA_TYPE_RANGE else None,
        "display_format": "hex" if data_type in _DATA_TYPE_HEX else None,
        "registry": registry_name,
        # REGISTRY_LIST fields are inherently list-shaped — the
        # upstream ``filter: [...]`` schema doesn't carry an explicit
        # ``is_list`` flag, so the bool conversion of ``None`` would
        # otherwise emit ``multi_value: false`` and the parser /
        # serializer round-trip would miss the array contract.
        "multi_value": (True if entry_type == "registry_list" else bool(raw.get("is_list"))),
        "templatable": bool(raw.get("templatable")),
        "depends_on": None,
        "depends_on_value": None,
        "depends_on_value_not": None,
        "depends_on_component": gated_component,
        "references_component": references,
        "pin_features": _resolve_pin_features(raw) if entry_type == "pin" else [],
        "pin_mode": None,
        "advanced": advanced,
        # ``yaml_only`` from the schema → ``hidden`` on the catalog
        # entry. The frontend already knows how to skip ``hidden``
        # entries; the rename keeps the consumer-facing surface
        # unchanged.
        "hidden": yaml_only,
        "help_link": docs.url,
        "translation_key": None,
        "translation_params": None,
        "platform_type": None,
    }

    # Detect user-keyed maps (``key_type`` set in the raw entry).
    # ``logger.logs``, ``substitutions:`` and similar enumerate every
    # possible *valid* key as a separate config_var with the same
    # value-shape — that's a representation of "any string key,
    # uniform value type", not hundreds of distinct sub-fields.
    # Collapse to a single value template so the frontend can render a
    # dynamic ``add row`` editor instead of a wall of cloned forms.
    if "key_type" in raw and isinstance(inner_schema, dict):
        entry["type"] = "map"
        entry["config_entries"] = _build_map_value_template(inner_schema, schema_dir)
        return entry

    # Recurse into nested schemas for type=nested.
    if entry_type == "nested" and isinstance(inner_schema, dict):
        inner = _convert_config_vars(inner_schema, schema_dir)
        entry["config_entries"] = inner or None
        entry["platform_type"] = _detect_platform_type(inner_schema)
        # When every child would render as advanced anyway, hide the
        # parent's expand affordance under "Advanced" too — no point
        # surfacing an empty group on the main form. We don't pull the
        # parent BACK to non-advanced based on a visible child:
        # required sub-fields like ``framework.components.name`` are
        # only meaningful when the user has chosen to use that group,
        # so leaving the parent's classification to ``_classify_advanced``
        # avoids accidentally exposing deeply technical groups.
        if inner and _all_inner_advanced(inner):
            entry["advanced"] = True
    elif entry_type == "pin":
        # Attach the long-form pin schema (mode flags + inverted) so
        # the editor can render an "Advanced" disclosure under every
        # pin field. Without this the visual editor only supports
        # the short ``pin: GPIO5`` form, which blocks configurations
        # like ``pin: { number: GPIO5, mode: { input: true, pullup:
        # true } }`` (issue #420).
        entry["config_entries"] = list(_pin_long_form_extras(schema_dir)) or None
    else:
        entry["config_entries"] = None

    return entry


@cache
def _pin_long_form_mode_flags(schema_dir: Path) -> tuple[str, ...]:
    """Return the mode-flag keys present in the bundle's pin schema.

    Cached read of ``esp32.json``'s ``pin.schema.config_vars.mode.schema``
    so a sync run with hundreds of pin entries pays the bundle parse
    once. Returns a tuple (immutable) so cache reuse can't leak
    mutations the way a shared dict / list could — the actual
    ``ConfigEntry`` dicts are built fresh on each call to
    ``_pin_long_form_extras``.

    Returns ``()`` for any unexpected bundle shape (file missing,
    non-JSON, JSON that parses to non-dict at any level along the
    ``esp32.pin.schema.config_vars.mode.schema.config_vars`` path).
    The caller treats an empty return as "skip the long-form
    extras", so pin entries fall back to the short-form picker
    rather than crashing the sync.
    """
    try:
        esp32_data = json.loads((schema_dir / "esp32.json").read_text())
    except (FileNotFoundError, ValueError, OSError):
        return ()
    if not isinstance(esp32_data, dict):
        return ()
    node: Any = esp32_data
    for key in ("esp32", "pin", "schema", "config_vars", "mode", "schema", "config_vars"):
        if not isinstance(node, dict):
            return ()
        node = node.get(key)
    if not isinstance(node, dict):
        return ()
    common_modes = ("input", "output", "pullup", "pulldown", "open_drain")
    return tuple(flag for flag in common_modes if flag in node)


@cache
def _pin_long_form_has_inverted(schema_dir: Path) -> bool:
    """Whether the bundle's pin schema declares an ``inverted`` field.

    Cached for the same reason as ``_pin_long_form_mode_flags`` —
    one bundle parse per sync run. Returns ``False`` on any
    unexpected shape so the caller drops the field rather than
    emitting one the bundle didn't claim.
    """
    try:
        esp32_data = json.loads((schema_dir / "esp32.json").read_text())
    except (FileNotFoundError, ValueError, OSError):
        return False
    if not isinstance(esp32_data, dict):
        return False
    node: Any = esp32_data
    for key in ("esp32", "pin", "schema", "config_vars"):
        if not isinstance(node, dict):
            return False
        node = node.get(key)
    return isinstance(node, dict) and "inverted" in node


def _pin_long_form_extras(schema_dir: Path) -> tuple[dict, ...]:
    """Return nested ConfigEntry dicts for the pin schema's long form.

    ESPHome's pin schema accepts both ``pin: GPIO5`` (the short form
    our existing pin picker handles) and ``pin: { number: GPIO5,
    mode: { input: true, pullup: true }, inverted: true }``. Today's
    catalog flat-maps ``type: pin`` to a leaf entry, so the visual
    editor can't drive the long form at all.

    Read the long-form fields from ``esp32.json``'s
    ``pin.schema.config_vars`` — ESP32's schema is the most complete
    of the bundled platforms and includes every common field. The
    common subset (``mode`` + ``inverted``) applies to every
    platform that has a pin schema (esp32, esp8266, rp2040, nrf52,
    host); platform-specific extras (``drive_strength`` on ESP32
    only, ``analog`` mode on esp8266/rp2040/nrf52/host) are
    intentionally excluded for first cut — the catalog entries
    are component-keyed, not platform-keyed, so a per-platform
    field on a pin shared across components can't be resolved
    here.

    The bundle parse is cached one level down (in
    ``_pin_long_form_mode_flags`` / ``_pin_long_form_has_inverted``)
    so the file read happens once per sync. *This* function builds
    the ``ConfigEntry`` dicts fresh on every call — downstream
    sync passes mutate ``config_entries`` in place, so a shared
    cache would let one component's edit leak into every other
    pin field. The cost of rebuilding is six dicts per pin entry,
    which is dwarfed by the rest of the sync work.
    """
    flags = _pin_long_form_mode_flags(schema_dir)
    extras: list[dict] = []
    if flags:
        mode_children = [
            _synthesise_long_form_extra(
                key=flag,
                type_="boolean",
                default_value=False,
                description=f"Set the {_key_to_label(flag).lower()} mode flag.",
            )
            for flag in flags
        ]
        extras.append(
            _synthesise_long_form_extra(
                key="mode",
                type_="nested",
                default_value=None,
                description=(
                    "Pin mode flags (input / output / pullup / pulldown / "
                    "open_drain). Combine flags as needed — e.g. input + "
                    "pullup for a button pulled to VCC."
                ),
                config_entries=mode_children,
            )
        )
    if _pin_long_form_has_inverted(schema_dir):
        extras.append(
            _synthesise_long_form_extra(
                key="inverted",
                type_="boolean",
                default_value=False,
                description=(
                    "Invert the logical level. ``true`` swaps high/low "
                    "in software so an active-low button reads as "
                    "active when grounded."
                ),
            )
        )
    return tuple(extras)


def _synthesise_long_form_extra(
    *,
    key: str,
    type_: str,
    default_value: Any,
    description: str,
    config_entries: list[dict] | None = None,
) -> dict:
    """Build a ConfigEntry-shaped dict for a synthesised long-form pin field.

    Mirrors the shape ``_convert_field`` produces but with the small
    set of fields the long-form pin extras actually need; the rest
    default to safe values so the consumer can treat synthesised and
    schema-derived entries uniformly. Marked ``advanced=True`` —
    the long-form fields are an opt-in disclosure under the pin
    picker, never on the main form.
    """
    return {
        "key": key,
        "type": type_,
        "label": _key_to_label(key),
        "description": description,
        "required": False,
        "default_value": default_value,
        "options": None,
        "allow_custom_value": False,
        "range": None,
        "display_format": None,
        "multi_value": False,
        "templatable": False,
        "depends_on": None,
        "depends_on_value": None,
        "depends_on_value_not": None,
        "depends_on_component": None,
        "references_component": None,
        "pin_features": [],
        "pin_mode": None,
        "advanced": True,
        "hidden": False,
        "help_link": None,
        "translation_key": None,
        "translation_params": None,
        "platform_type": None,
        "config_entries": config_entries,
    }


def _build_map_value_template(
    inner_schema: dict,
    schema_dir: Path,
) -> list[dict] | None:
    """Build a single-entry list describing the value type of a map field.

    The schema's ``key_type`` pattern enumerates every accepted key as
    a config_var carrying the value's shape. Take the first one as a
    template (they're all identical for true maps; any inconsistency
    is upstream noise we can safely flatten). The entry is keyed
    ``"value"`` so the frontend has a stable binding name.
    """
    config_vars = inner_schema.get("config_vars") or {}
    if not config_vars:
        return None
    sample_raw = next(iter(config_vars.values()))
    if not isinstance(sample_raw, dict):
        return None
    template = _convert_field("value", sample_raw, schema_dir)
    return [template] if template else None


def _build_id_entry(key: str, raw: dict, *, required: bool = False) -> dict:
    """Build a ConfigEntry for an own-id field.

    Auto-generated ids (``key: GeneratedID``) stay flagged advanced —
    most users let ESPHome derive them. Required / Optional ids
    (``key: Required`` with ``id_type``) stay on the main form because
    the user has to supply them. ``references_component`` is never set:
    own ids are free-form names, not references to other components.
    """
    docs = clean_docs(raw.get("docs"))
    is_generated = raw.get("key") == "GeneratedID"
    return {
        "key": key,
        "type": "id",
        "label": _key_to_label(key),
        "description": docs.text or None,
        "required": required,
        "default_value": None,
        "options": None,
        "allow_custom_value": False,
        "range": None,
        "multi_value": False,
        "templatable": False,
        "depends_on": None,
        "depends_on_value": None,
        "depends_on_value_not": None,
        "depends_on_component": None,
        "references_component": None,
        "pin_features": [],
        "pin_mode": None,
        "advanced": is_generated and not required,
        "hidden": False,
        "help_link": docs.url,
        "translation_key": None,
        "translation_params": None,
        "config_entries": None,
        "platform_type": None,
    }


def _build_options(raw: dict) -> list[dict] | None:
    """Build a list of ``{label, value}`` dicts from a schema's enum values."""
    values = raw.get("values")
    if not isinstance(values, dict):
        return None
    options: list[dict] = []
    for value, info in values.items():
        label = value or "(none)"
        if isinstance(info, dict) and info.get("docs"):
            label = info["docs"]
        options.append({"label": label, "value": value})
    return options or None


def _coerce_default(value: Any) -> Any:
    """Pass through scalar defaults; coerce schema-string trues/falses."""
    if value is None:
        return None
    if isinstance(value, str):
        if value.lower() == "true":
            return True
        if value.lower() == "false":
            return False
    return value


def _extract_default(raw: dict, key: str = "") -> tuple[Any, str | None]:
    """Resolve ``(default_value, depends_on_component)`` for a field.

    Reads ``default_with`` (``cv.OnlyWith``, esphome/esphome#16276)
    in preference to plain ``default``. ``default_without``
    (``cv.OnlyWithout``) has inverse-gate semantics that
    ``depends_on_component`` can't model — no default surfaces for
    those fields. Multi-component ``default_with`` picks the first
    component and logs a warning (no upstream call site uses a
    list today). *key* is the field name for the log context.
    """
    if (gated := raw.get("default_with")) is not None:
        components = gated.get("components") or []
        if len(components) > 1:
            _LOGGER.warning(
                "%s: default_with with multiple components %s; only "
                "the first (%s) will be used as depends_on_component.",
                key or "<unknown>",
                components,
                components[0],
            )
        return _coerce_default(gated.get("value")), components[0] if components else None
    return _coerce_default(raw.get("default")), None


def _resolve_use_id_reference(raw: dict) -> str | None:
    """Map ``use_id_type: 'ns::Class'`` to a component domain.

    ``use_id_type`` is the schema's marker for *cross-references* —
    fields like ``i2c_id`` that point at another component instance.
    Do NOT confuse with ``id_type``, which describes the type of id
    this field *creates* (its own id) — those are free-form strings,
    not references.
    """
    use_id_type = raw.get("use_id_type")
    if not isinstance(use_id_type, str) or "::" not in use_id_type:
        return None
    namespace = use_id_type.split("::", 1)[0]
    return _USE_ID_NAMESPACE_OVERRIDES.get(namespace, namespace)


def _is_own_id_field(raw: dict) -> bool:
    """Return True iff this field defines the component's own id.

    Two shapes signal an own-id:
      - ``key: "GeneratedID"`` — auto-generated id (rare to set manually)
      - ``id_type: { class: ... }`` without ``use_id_type`` — required
        or optional id field whose type the schema knows but which is
        still the *component's own* identifier, not a reference.
    """
    if raw.get("key") == "GeneratedID":
        return True
    return bool(isinstance(raw.get("id_type"), dict) and "use_id_type" not in raw)


_PIN_FEATURE_VALUES = frozenset(f.value for f in PinFeature)


def _resolve_pin_features(raw: dict) -> list[str]:
    """Translate the schema's ``modes`` list into PinFeature enum keys.

    Drops GPIO mode flags (input / output / pullup / pulldown /
    open_drain) that the schema mixes in; only hardware-capability
    tags (adc, dac, i2c_*, spi_*, ...) belong on
    ``ConfigEntry.pin_features``.
    """
    modes = raw.get("modes") or []
    return [m for m in modes if isinstance(m, str) and m in _PIN_FEATURE_VALUES]


def _detect_platform_type(inner_schema: dict) -> str | None:
    """Infer the platform_type for a NESTED entry from its extends list.

    A nested entry like ``dht.humidity`` extends ``sensor._SENSOR_SCHEMA``
    — that's the signal it represents an entity sub-reading. We surface
    ``"sensor"`` here so the frontend renders it with the sensor base
    fields (name, device_class, ...) on top.
    """
    for ref in inner_schema.get("extends") or []:
        prefix = ref.split(".", 1)[0]
        if prefix in _PLATFORM_DOMAINS:
            return prefix
    return None


# Tokens that should render upper-case in labels rather than the
# Title-case ``str.title()`` produces. Limit this list to widely-
# recognised technical acronyms and signal names — anything more
# component-specific belongs in a per-component override (issue #401).
_LABEL_ACRONYMS = frozenset(
    {
        # Bus / peripheral / pin signals
        "ADC",
        "BCLK",
        "CS",
        "DAC",
        "DIO",
        "DMA",
        "EN",
        "GPIO",
        "HSYNC",
        "I2C",
        "INT",
        "IO",
        "IRQ",
        "JTAG",
        "LNA",
        "LRCLK",
        "MCLK",
        "MISO",
        "MOSI",
        "PA",
        "PCLK",
        "PCNT",
        "PWM",
        "RMT",
        "RST",
        "RX",
        "SCK",
        "SCL",
        "SCLK",
        "SDA",
        "SDO",
        "SPI",
        "TX",
        "UART",
        "USB",
        "VSYNC",
        # Networking
        "AP",
        "API",
        "BSSID",
        "DHCP",
        "DNS",
        "HTTP",
        "HTTPS",
        "IP",
        "MAC",
        "MQTT",
        "NTP",
        "OTA",
        "QOS",
        "SSID",
        "SSL",
        "TCP",
        "TLS",
        "UDP",
        "URI",
        "URL",
        # Wireless / RF
        "BLE",
        "FSK",
        "MSK",
        "NFC",
        "OOK",
        "RF",
        "RFID",
        # Display / colour
        "BGR",
        "LCD",
        "LED",
        "OLED",
        "RGB",
        "RGBW",
        "TFT",
        "WRGB",
        # Identifier / common
        "CRC",
        "CPU",
        "ID",
        "PID",
        "PSRAM",
        "RAM",
        "UID",
        "UUID",
        "VID",
        # Power / electrical
        "AC",
        "DC",
        "GFCI",
        "MPPT",
        "PV",
        "RMS",
        # Sensors / air-quality
        "CO",
        "CO2",
        "IR",
        "NOX",
        "PM",
        "TVOC",
        "UV",
        "VOC",
        # OpenTherm domain (used heavily in the climate catalog)
        "CH",
        "DHW",
    }
)

# Match an alpha prefix followed by a digit cluster (``Dio0``,
# ``Co2``) so we can upper-case acronym + digit tokens together.
_TRAILING_DIGITS_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def _key_to_label(key: str) -> str:
    """
    Turn a config-var key into a human-friendly label for the visual editor.

    Title-cases the key after replacing underscores with spaces, then
    upper-cases any token (or alpha prefix of an alpha+digit token)
    that matches a known technical acronym — so ``cs_pin`` renders
    as ``CS Pin`` and ``dio0_pin`` as ``DIO0 Pin``.
    """
    titled = key.replace("_", " ").title()
    tokens: list[str] = []
    for tok in titled.split(" "):
        if tok.upper() in _LABEL_ACRONYMS:
            tokens.append(tok.upper())
            continue
        match = _TRAILING_DIGITS_RE.match(tok)
        if match and match.group(1).upper() in _LABEL_ACRONYMS:
            tokens.append(match.group(1).upper() + match.group(2))
            continue
        tokens.append(tok)
    return " ".join(tokens)


def _classify_advanced(key: str, *, required: bool, is_structural: bool) -> bool:
    """Decide whether an entry hides behind the "Advanced" toggle.

    Order of precedence:
      1. Structural fields (pins, bus references) — never advanced.
      2. Required fields — never advanced.
      3. ADVANCED_IMPORTANT_KEYS (id, comment) — always advanced.
      4. IMPORTANT_KEYS — never advanced.
      5. ADVANCED_BASE_KEYS — always advanced.
      6. Default: advanced when optional.
    """
    if is_structural:
        return False
    if required:
        return False
    if key in _ADVANCED_IMPORTANT_KEYS:
        return True
    if key in _IMPORTANT_KEYS:
        return False
    if key in _ADVANCED_BASE_KEYS:
        return True
    return True


def _all_inner_advanced(inner: list[dict]) -> bool:
    """Return True iff every inner entry is advanced (else False).

    Empty groups return False so a NESTED parent isn't accidentally
    hidden when its inner entries couldn't be resolved.
    """
    if not inner:
        return False
    return all(e.get("advanced") for e in inner)


def _sort_entries(entries: list[dict]) -> list[dict]:
    """Sort: not-advanced first, then within each group by IMPORTANT_KEY_ORDER."""
    rank = {k: i for i, k in enumerate(_IMPORTANT_KEY_ORDER)}
    fallback = len(_IMPORTANT_KEY_ORDER)

    def sort_key(e: dict) -> tuple[int, int, str]:
        return (
            1 if e.get("advanced") else 0,
            rank.get(e["key"], fallback),
            e["key"],
        )

    return sorted(entries, key=sort_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_qualified_key(top_key: str) -> tuple[str, str]:
    """Split a schema top-level key into ``(domain, stem)``.

    Schema files key multi-platform components as ``<stem>.<domain>``
    (e.g. ``dht.sensor`` — DHT as a sensor platform). For unqualified
    keys we return ``("", top_key)``.
    """
    if "." in top_key:
        stem, domain = top_key.split(".", 1)
        return domain, stem
    return "", top_key


def _lookup_index_meta(component_id: str, top_key: str, index: SchemaIndex) -> dict:
    """Find the merged-index metadata for a catalog entry.

    Tries the catalog id first (``sensor.dht``, ``wifi``), then the bare
    stem (``dht``). Returns an empty dict if neither matches.
    """
    return (
        index.metadata.get(component_id)
        or index.metadata.get(top_key)
        or index.metadata.get(top_key.split(".", 1)[0])
        or {}
    )


def _docs_type_marker(docs: str) -> str | None:
    """Extract the ``**type**:`` marker from a docs string, if any.

    Handles bare bold (``**boolean**:``) and bracketed-link forms
    (``**[Time](...)**:``). Returns the inner text or None when no
    marker is present.
    """
    if not docs:
        return None
    m = re.match(r"^\*\*\[?([^\]*]+)\]?(?:\([^)]+\))?\*\*\s*[:\-]", docs)
    return m.group(1).strip() if m else None


def _looks_like_boolean_enum(raw: dict) -> bool:
    """Return True iff ``raw['values']`` is exactly ``{true, false}``."""
    values = raw.get("values")
    if not isinstance(values, dict):
        return False
    keys = {str(k).lower() for k in values}
    return keys in ({"true", "false"}, {"true", "false", "yes", "no"})


def _looks_like_time_period_default(value: Any) -> bool:
    """Return True iff *value* is a string shaped like a time period."""
    if not isinstance(value, str):
        return False
    return bool(_TIME_PERIOD_DEFAULT.match(value.strip().replace(" ", "")))


def _has_config_schema(section: dict) -> bool:
    """Check whether *section* exposes its own ``CONFIG_SCHEMA``."""
    schemas = section.get("schemas")
    return isinstance(schemas, dict) and "CONFIG_SCHEMA" in schemas


def _strip_anchor(url: str) -> str:
    """Drop ``#anchor`` from a URL to get the bare component-page link."""
    if "#" in url:
        return url.split("#", 1)[0]
    return url


# Hardware acronyms that should retain their canonical capitalisation
# in derived component names. Applied AFTER the default ``str.title()``
# (which gives e.g. "Rc522 Spi") to recover "RC522 SPI".
_ACRONYM_NORMALISATIONS: dict[str, str] = {
    "Adc": "ADC",
    "Dac": "DAC",
    "Bldc": "BLDC",
    "Bme": "BME",
    "I2C": "I²C",
    "I2c": "I²C",
    "Spi": "SPI",
    "Uart": "UART",
    "Ble": "BLE",
    "Pwm": "PWM",
    "Gpio": "GPIO",
    "Rgb": "RGB",
    "Rgbw": "RGBW",
    "Rgbww": "RGBWW",
    "Cwww": "CWWW",
    "Led": "LED",
    "Lcd": "LCD",
    "Oled": "OLED",
    "Tft": "TFT",
    "Usb": "USB",
    "Ota": "OTA",
    "Mqtt": "MQTT",
    "Wifi": "Wi-Fi",
    "Tcp": "TCP",
    "Udp": "UDP",
    "Http": "HTTP",
    "Https": "HTTPS",
    "Url": "URL",
    "Json": "JSON",
    "Pcm": "PCM",
    "Mac": "MAC",
    "Pir": "PIR",
    "Imu": "IMU",
    "Nfc": "NFC",
    "Rfid": "RFID",
    "Pn532": "PN532",
    "Rc522": "RC522",
    "Esp32": "ESP32",
    "Esp8266": "ESP8266",
    "Esphome": "ESPHome",
    "Rp2040": "RP2040",
    "Esp32C3": "ESP32-C3",
    "Esp32S2": "ESP32-S2",
    "Esp32S3": "ESP32-S3",
    "Esp32H2": "ESP32-H2",
    "Esp32C5": "ESP32-C5",
    "Esp32C6": "ESP32-C6",
    "Esp32C61": "ESP32-C61",
    "Esp32P4": "ESP32-P4",
}


def _resolve_name(component_id: str, stem: str, doc_name: str | None) -> str:
    """Produce a human label for the component.

    Preference order:
      1. The link text from the ``See also`` footer (e.g.
         "DHT Temperature+Humidity Sensor").
      2. A title-cased version of the stem with hardware acronyms
         normalised back to their canonical capitalisation.
    """
    if doc_name:
        return doc_name
    name = stem.replace("_", " ").title()
    for k, v in _ACRONYM_NORMALISATIONS.items():
        # Word-boundary replace so "Pwm" -> "PWM" but "Pwms" doesn't
        # accidentally match.
        name = re.sub(rf"\b{re.escape(k)}\b", v, name)
    return name


# Category overrides for non-platform components — matches the legacy
# catalog so existing UI groupings stay stable. The "core" group is
# the union of (a) ESPHome's own infrastructure (api, wifi, logger,
# ...), (b) target-platform components (esp32, esp8266, ...), and
# (c) device-wide config keys that aren't really components but show
# up as top-level YAML blocks (substitutions, packages, globals,
# external_components, ...). The frontend's "Add core configuration"
# dialog is a thin filter on category=core, so this list defines what
# the user sees there.
_CATEGORY_OVERRIDES: dict[str, str] = {
    # Core ESPHome infrastructure
    "esphome": "core",
    "wifi": "core",
    "api": "core",
    "ota": "core",
    "logger": "core",
    "mqtt": "core",
    "web_server": "core",
    "captive_portal": "core",
    "safe_mode": "core",
    "time": "core",
    "network": "core",
    "ethernet": "core",
    "mdns": "core",
    "improv_serial": "core",
    "debug": "core",
    "preferences": "core",
    # Networking / web infrastructure that's auto-pulled by other
    # core components (ota.http_request → http_request,
    # web_server → web_server_base, etc.). Keeping them tagged
    # `core` lets the dependency-satisfaction filter on the core
    # dialog pass the parent OTA / web entries that depend on them.
    "http_request": "core",
    "web_server_base": "core",
    "web_server_idf": "core",
    "socket": "core",
    "async_tcp": "core",
    # Target platform components (esp32, esp8266, ...) — these are
    # also tagged via `is_target_platform` introspection, but listing
    # them explicitly here makes the override authoritative.
    "esp32": "core",
    "esp8266": "core",
    "rp2040": "core",
    "bk72xx": "core",
    "rtl87xx": "core",
    "ln882x": "core",
    "nrf52": "core",
    "host": "core",
    # Device-wide config keys (not really "components" — they don't
    # have C++ implementations — but they live at the top level of
    # YAML alongside real components).
    "substitutions": "core",
    "packages": "core",
    "external_components": "core",
    "dashboard_import": "core",
    "globals": "core",
    # Bus / transport components
    "i2c": "bus",
    "spi": "bus",
    "uart": "bus",
    "one_wire": "bus",
    "modbus": "bus",
    "canbus": "bus",
    # Automation primitives
    "script": "automation",
    "interval": "automation",
}


def _infer_misc_category(top_key: str) -> str:
    """Best-effort category for non-platform components.

    Looks up ``_CATEGORY_OVERRIDES`` first (curated list mirroring the
    legacy catalog), falls through to ``misc`` for everything else.
    """
    return _CATEGORY_OVERRIDES.get(top_key, "misc")


# ---------------------------------------------------------------------------
# Narrow ESPHome introspection
# ---------------------------------------------------------------------------
#
# The pre-built schema bundle doesn't surface three things we still want:
#
#   - multi_conf: whether a component can appear more than once in YAML
#   - platform_defaults: per-target-platform default values (cv.SplitDefault)
#   - supported_platforms: which target chips a component can be used on
#
# We pull these directly from the installed ``esphome`` package. When
# ``esphome`` isn't available (CI without the dep), introspection is a
# no-op and the catalog ships without those fields populated.

# Target-platform component ids — components named after a chip family
# that act as the "platform" entry in YAML.
_TARGET_PLATFORMS: frozenset[str] = frozenset(
    {
        "esp32",
        "esp8266",
        "rp2040",
        "bk72xx",
        "rtl87xx",
        "ln882x",
        "nrf52",
        "host",
    }
)

# Network-transport components — every device picks exactly one. Their
# combined ``AUTO_LOAD`` closure (see ``_implicit_dependencies``)
# determines which "interface" components ESPHome will resolve
# automatically regardless of which transport the user picked.
_NETWORK_TRANSPORTS: frozenset[str] = frozenset({"wifi", "ethernet", "openthread", "host"})


def introspect_component(component_id: str) -> dict[str, Any]:
    """
    Return ``{multi_conf, is_target_platform, platform_defaults, refined_types, auto_load}``.

    Best-effort: returns an empty dict when ``esphome`` isn't importable
    or the component module can't be loaded.

    ``auto_load`` is ESPHome's static list of components pulled in
    whenever this one is configured. When the upstream declaration is
    a callable (config-dependent), we can't resolve it without a
    config and surface an empty list — callers should treat that as
    "unknown" and stay conservative.
    """
    if not component_id:
        return {}
    loader = _get_esphome_loader()
    if loader is None:
        return {}
    try:
        manifest = loader.get_component(component_id)
    except Exception:
        return {}
    if manifest is None:
        return {}
    # ``component_id`` for the platform-style entries the catalog
    # passes us is a bare stem (``mcp3008``) — domain stripping
    # happens at the caller. The bare manifest's ``config_schema``
    # is the parent component's, which doesn't carry the platform-
    # specific fields (``reference_voltage``, etc.). Walk the
    # platform manifests too so unit-coerced validators on those
    # fields get refined like the bus-component ones.
    platform_manifests = _enumerate_platform_manifests(loader, component_id)

    raw_auto_load = manifest.auto_load
    auto_load: list[str] = list(raw_auto_load) if isinstance(raw_auto_load, list) else []

    # Bare manifest results take precedence (``setdefault`` keep-first);
    # platform-manifest results fill in fields that only exist on the
    # platform schema (e.g. ``sensor.debug.psram``'s ``cv.only_on_esp32``
    # gate lives on the ``debug.sensor`` manifest, not the bare
    # ``debug`` one).
    def merge_from_platforms(
        collect: Callable[[Any], dict[tuple[str, ...], Any]],
    ) -> dict[tuple[str, ...], Any]:
        merged = collect(manifest)
        for platform_manifest in platform_manifests:
            for path, value in collect(platform_manifest).items():
                merged.setdefault(path, value)
        return merged

    refined_types = merge_from_platforms(_collect_refined_types)
    platform_constraints = merge_from_platforms(_collect_platform_constraints)
    field_ranges = merge_from_platforms(_collect_field_ranges)
    inclusive_groups = merge_from_platforms(_collect_inclusive_groups)
    required_groups = merge_from_platforms(_collect_required_groups)

    return {
        "multi_conf": bool(getattr(manifest, "multi_conf", False)),
        "is_target_platform": bool(getattr(manifest, "is_target_platform", False)),
        "platform_defaults": _collect_platform_defaults(manifest),
        "platform_constraints": platform_constraints,
        "field_ranges": field_ranges,
        "refined_types": refined_types,
        "inclusive_groups": inclusive_groups,
        "required_groups": required_groups,
        "auto_load": auto_load,
    }


def _audit_catalog_for_unit_mismatches(catalog: list[dict]) -> None:
    """Warn on float/integer entries whose ``default_value`` doesn't parse.

    Runs after the catalog is built. Catches the silent-bug class
    that prompted the ``FLOAT_WITH_UNIT`` work in the first place:
    a ``cv.<unit_coerced>`` validator the live-introspection walker
    didn't recognise (because ESPHome added a new one upstream, or
    the validator was wrapped in a way ``classify`` can't see
    through). The schema-bundle types these entries ``"float"`` /
    ``"integer"`` based on their post-coerce runtime, but their
    ``default_value`` is a unit-suffixed string the frontend's
    number input won't accept.

    Surfacing as a sync-time WARNING gives actionable telemetry to
    add the validator to ``_FLOAT_WITH_UNIT_VALIDATORS`` (or
    ``_UNIT_FALLBACKS``) before users hit the silent-validation
    failure on the affected fields.
    """
    mismatches: list[tuple[str, str, str]] = []
    for component in catalog:
        for path, entry in _walk_entries(component.get("config_entries") or []):
            if entry.get("type") not in ("float", "integer"):
                continue
            default = entry.get("default_value")
            if not isinstance(default, str):
                continue
            try:
                float(default)
            except ValueError:
                mismatches.append(
                    (component["id"], ".".join(path), default),
                )
    if not mismatches:
        return
    _LOGGER.warning(
        "Catalog audit: %d float/integer entries have non-numeric string "
        "defaults — likely a unit-coerced cv.* validator the introspection "
        "walker didn't recognise. The frontend's number input will reject "
        "these defaults as NaN. Add the validator to "
        "_FLOAT_WITH_UNIT_VALIDATORS (or _UNIT_FALLBACKS for hand-rolled "
        "ones) in script/sync_components.py.",
        len(mismatches),
    )
    for component_id, dotted_path, default in mismatches:
        _LOGGER.warning("  %s.%s = %r", component_id, dotted_path, default)


def _walk_entries(
    entries: list[dict],
    parent_path: tuple[str, ...] = (),
) -> Iterable[tuple[tuple[str, ...], dict]]:
    """Yield (dotted-path, entry) for every entry in *entries*.

    Recurses into NESTED groups and MAP value templates so the audit
    covers every entry the catalog actually ships. ``parent_path`` is
    threaded through so leaf yields carry the full path the user
    sees in YAML — e.g. ``("api", "actions", "service")`` rather than
    just ``("service",)`` — which is essential when multiple
    components share a key like ``rate`` or ``size``: the warning
    has to point at the specific instance.
    """
    for entry in entries:
        path = (*parent_path, entry["key"])
        yield path, entry
        # Both NESTED groups and MAP value templates (built via
        # ``_build_map_value_template``) carry their inner schema
        # under ``config_entries``. Walk both so the audit doesn't
        # silently miss unit-coerced defaults inside e.g.
        # ``api.actions.<user_key>.<float-with-string-default>``.
        inner = entry.get("config_entries") if entry.get("type") in ("nested", "map") else None
        if inner:
            yield from _walk_entries(inner, path)


def _enumerate_platform_manifests(loader: Any, stem: str) -> list[Any]:
    """Return platform-specific manifests for *stem*.

    A multi-platform component (e.g. ``mcp3008`` ships a sensor and
    an output) keeps its platform-specific schemas in
    ``esphome.components.<stem>.<domain>``. ``loader.get_platform``
    fetches each one; missing combinations return ``None`` and we
    skip them. Best-effort — exceptions are swallowed so one bad
    platform manifest can't tank the whole sync.

    Iterates ``_PLATFORM_DOMAINS`` (the same set the catalog walk
    already uses for schema-keyed platform entries) so adding a
    domain in one place automatically covers the introspection
    walk too — no parallel list to keep in sync. Sorted so the
    catalog output is deterministic across runs — frozenset
    iteration is hash-randomised per process and would otherwise
    flip refinement results between syncs when two platform
    manifests refine the same path differently (the
    ``setdefault`` keep-first downstream picks whichever domain
    came up first).
    """
    out: list[Any] = []
    for domain in sorted(_PLATFORM_DOMAINS):
        try:
            platform_manifest = loader.get_platform(domain, stem)
        except Exception:  # noqa: S112 — best-effort: missing platform combos are normal
            continue
        if platform_manifest is None:
            continue
        out.append(platform_manifest)
    return out


def _get_esphome_loader() -> Any:
    """Lazy import ``esphome.loader``; cache the (success or failure) result."""
    if _ESPHOME_LOADER_CACHE["resolved"]:
        return _ESPHOME_LOADER_CACHE["module"]
    _ESPHOME_LOADER_CACHE["resolved"] = True
    try:
        from esphome import loader

        _ESPHOME_LOADER_CACHE["module"] = loader
        _LOGGER.info("esphome introspection enabled (esphome.loader importable)")
    except Exception:
        _ESPHOME_LOADER_CACHE["module"] = None
        _LOGGER.warning(
            "esphome.loader not importable — multi_conf, platform_defaults, "
            "supported_platforms will be empty"
        )
    return _ESPHOME_LOADER_CACHE["module"]


_ESPHOME_LOADER_CACHE: dict[str, Any] = {"resolved": False, "module": None}


def _is_json_safe(value: Any) -> bool:
    """Return True iff *value* is a primitive JSON-encodable scalar."""
    return isinstance(value, (str, int, float, bool)) or value is None


# Default values for config-entry fields. Anything matching one of
# these is stripped from the serialized JSON so the output stays close
# to the size of the previous mashumaro-based catalog (which omitted
# defaults via ``serialization_strategy``).
_ENTRY_DEFAULTS: dict[str, Any] = {
    "description": None,
    "required": False,
    "default_value": None,
    "platform_defaults": None,
    "supported_platforms": [],
    "options": None,
    "allow_custom_value": False,
    "range": None,
    "multi_value": False,
    "templatable": False,
    "depends_on": None,
    "depends_on_value": None,
    "depends_on_value_not": None,
    "depends_on_component": None,
    "references_component": None,
    "pin_features": [],
    "pin_mode": None,
    "advanced": False,
    "hidden": False,
    "help_link": None,
    "translation_key": None,
    "translation_params": None,
    "config_entries": None,
    "platform_type": None,
    "group": None,
    "required_groups": [],
    "registry": None,
}

_COMPONENT_DEFAULTS: dict[str, Any] = {
    "docs_url": "",
    "image_url": "",
    "dependencies": [],
    "multi_conf": False,
    "supported_platforms": [],
    "config_entries": [],
    "required_groups": [],
}


def _strip_defaults(component: dict) -> dict:
    """Drop fields equal to their dataclass default to slim the JSON."""
    out: dict[str, Any] = {}
    for k, v in component.items():
        if k in _COMPONENT_DEFAULTS and v == _COMPONENT_DEFAULTS[k]:
            continue
        if k == "config_entries" and v:
            out[k] = [_strip_entry_defaults(e) for e in v]
            continue
        out[k] = v
    return out


def _emit_split_catalog(catalog: list[dict], version: str) -> None:
    """
    Write the catalog as ``components.index.json`` + per-id body files.

    The index carries every field the catalog UI's list / search /
    filter paths reference; the per-id bodies under
    ``definitions/components/<id>.json`` carry the full
    ``config_entries`` tree the detail-view fetches on demand.

    Crash-safety is best-effort; this is a build-time tool. Both
    outputs land at sibling temp paths first so a Ctrl-C
    mid-serialize never overwrites the live catalog. The bodies
    dir swap (rmtree old + rename next) has a sub-millisecond
    window where the dir is absent; the index is written via
    ``os.replace`` so its swap is atomic. Between the bodies swap
    and the index swap, the live index briefly points at the old
    id set against the new bodies; the runtime loader handles
    that gracefully (missing body files log a warning, new ids
    aren't yet listed), so a reader landing in that window
    degrades rather than crashes.
    """
    next_bodies = _OUTPUT_BODIES_DIR.parent / "components.next"
    prepare_next_bodies_dir(next_bodies)

    for component in catalog:
        cid = component["id"]
        stripped = _strip_defaults(component)
        emit_body_with_roundtrip(
            stripped, cid, next_bodies, ComponentCatalogEntry, log_label="Component"
        )

    # Serialize the new index to a sibling temp so a partial
    # write can't leave the live file truncated. orjson keeps the
    # wheel size in check; indented stdlib json was ~39 MB on the
    # monolithic file vs ~19 MB packed here, ~600 KB off the
    # wheel after deflate.
    index_payload = {
        "esphome_schema_version": version,
        "components": [_strip_index_defaults(c) for c in catalog],
    }
    swap_split_catalog_in(
        next_bodies=next_bodies,
        live_bodies=_OUTPUT_BODIES_DIR,
        index_payload=index_payload,
        live_index=_OUTPUT_INDEX_FILE,
    )
    _LOGGER.info("Wrote %d body files to %s", len(catalog), _OUTPUT_BODIES_DIR)
    _LOGGER.info("Wrote %s", _OUTPUT_INDEX_FILE)


# Each automations sub-catalog: (json_key, full_model, slim_model).
# Ordered so the per-type subdir creation runs the same way every run.
_AUTOMATIONS_SUBCATALOGS: list[tuple[str, type, type]] = [
    ("triggers", AutomationTrigger, AutomationTriggerIndex),
    ("actions", AutomationAction, AutomationActionIndex),
    ("conditions", AutomationCondition, AutomationConditionIndex),
    ("light_effects", LightEffect, LightEffectIndex),
    ("filters", Filter, FilterIndex),
]


def _emit_split_automations_catalog(automations: dict[str, Any], version: str) -> None:
    """Write the split automations catalog: index + per-type bodies.

    Layout:

    - ``definitions/automations.index.json`` carrying the slim
      ``AutomationCatalogIndex`` shape (picker fields only).
    - ``definitions/automations/<type>/<id>.json`` for each entry,
      one file per (trigger / action / condition / light_effect /
      filter, id) pair. The per-type subdir avoids id collisions
      across types — the same id can legitimately exist as both
      an action and a trigger in ESPHome.

    Crash-safety + roundtrip-validation + traversal guard all
    mirror ``_emit_split_catalog`` via the shared
    ``emit_body_with_roundtrip`` /
    ``swap_split_catalog_in`` helpers.
    """
    next_bodies = _AUTOMATIONS_BODIES_DIR.parent / "automations.next"
    prepare_next_bodies_dir(next_bodies)

    index_payload: dict[str, Any] = {"esphome_schema_version": version}
    for type_key, full_cls, slim_cls in _AUTOMATIONS_SUBCATALOGS:
        type_subdir = next_bodies / type_key
        type_subdir.mkdir()
        slim_entries: list[dict[str, Any]] = []
        for entry in automations.get(type_key, []):
            emit_body_with_roundtrip(
                entry,
                entry["id"],
                type_subdir,
                full_cls,
                log_label=f"Automation {type_key}",
            )
            # Build the slim index dict by round-tripping through
            # the slim model — drops fields that aren't in the
            # slim picker shape and validates the slim contract
            # against the same source dict.
            slim_src = entry
            if type_key == "actions":
                count = len(entry.get("config_entries") or [])
                form_editable = count <= _MAX_FORM_CONFIG_ENTRIES
                if not form_editable:
                    _LOGGER.info(
                        "Flagging action %s form_editable=False (%d config entries)",
                        entry["id"],
                        count,
                    )
                slim_src = {**entry, "form_editable": form_editable}
            slim_dict = slim_cls.from_dict(slim_src).to_dict()
            # Omit the flag when True so only non-editable actions carry it.
            if type_key == "actions" and slim_dict.get("form_editable", True):
                slim_dict.pop("form_editable", None)
            slim_entries.append(slim_dict)
        index_payload[type_key] = slim_entries

    swap_split_catalog_in(
        next_bodies=next_bodies,
        live_bodies=_AUTOMATIONS_BODIES_DIR,
        index_payload=index_payload,
        live_index=_AUTOMATIONS_INDEX_FILE,
    )
    _LOGGER.info(
        "Wrote %d automation body files to %s",
        sum(len(automations.get(k, [])) for k, *_ in _AUTOMATIONS_SUBCATALOGS),
        _AUTOMATIONS_BODIES_DIR,
    )
    _LOGGER.info("Wrote %s", _AUTOMATIONS_INDEX_FILE)


def _strip_index_defaults(component: dict) -> dict:
    """Slim form of ``_strip_defaults`` for the index file.

    Drops the per-field ``config_entries`` and ``required_groups``
    trees (they live in the body files) and the same default-equal
    fields ``_strip_defaults`` would have dropped.
    """
    out: dict[str, Any] = {}
    for k, v in component.items():
        if k in _INDEX_DROP_FIELDS:
            continue
        if k in _COMPONENT_DEFAULTS and v == _COMPONENT_DEFAULTS[k]:
            continue
        out[k] = v
    return out


def _strip_entry_defaults(entry: dict) -> dict:
    """Recursive variant of ``_strip_defaults`` for ConfigEntry dicts."""
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if k in _ENTRY_DEFAULTS and v == _ENTRY_DEFAULTS[k]:
            continue
        if k == "config_entries" and v:
            out[k] = [_strip_entry_defaults(e) for e in v]
            continue
        out[k] = v
    return out


def _walk_schema_keys(  # noqa: C901
    schema: Any,
    visit: Callable[[Any, str, Any, tuple[str, ...]], None],
) -> None:
    """
    Walk *schema* and call ``visit(key, key_name, val, path)`` per dict entry.

    Common kernel for the introspection collectors that need to
    visit every ``cv.Optional(...)`` / ``cv.Required(...)`` entry
    in a ``CONFIG_SCHEMA`` (and its nested sub-schemas):

    - peels ``vol.Schema`` / ``vol.All`` / ``vol.Any`` wrappers to
      find the underlying ``dict``,
    - dedupes by ``id()`` so cyclic schemas can't loop,
    - bails at depth 6 so a misbehaving recursive schema can't
      blow the stack,
    - swallows exceptions so one bad component manifest can't
      tank the whole sync.

    Each collector hands in a ``visit`` callback that records its
    domain-specific signal (per-platform defaults, refined type,
    platform constraint) on the ``out`` dict it owns.
    """
    visited: set[int] = set()

    def unwrap_to_dict(node: Any) -> dict | None:
        for _ in range(8):
            if isinstance(node, dict):
                return node
            # Compound validators (``vol.All`` / ``vol.Any``) get their
            # ``.schema`` attribute set during voluptuous compilation
            # to the *outer* ``Schema`` being compiled — not the inner
            # sub-schema — via ``_WithSubValidators.__voluptuous_compile__``.
            # Following it on a compiled tree therefore leads back to
            # the outer schema (already in ``visited``) and the walker
            # silently stops descending into nested ``vol.All`` values
            # like ``wifi.eap``. Prefer the ``validators`` tuple — the
            # source of truth on compound validators — and fall back to
            # ``.schema`` for plain ``vol.Schema`` wrappers that have no
            # ``validators`` attribute.
            inner = getattr(node, "validators", None)
            if inner:
                next_node = None
                for v in inner:
                    if isinstance(v, dict) or hasattr(v, "schema"):
                        next_node = v
                        break
                if next_node is None:
                    return None
                node = next_node
                continue
            if hasattr(node, "schema"):
                node = node.schema
                continue
            return None
        return None

    def walk(node: Any, path: tuple[str, ...], depth: int) -> None:
        if depth > 6:
            return
        candidate = unwrap_to_dict(node)
        if candidate is None:
            return
        if id(candidate) in visited:
            return
        visited.add(id(candidate))
        for key, val in candidate.items():
            key_name = key.schema if hasattr(key, "schema") else str(key)
            sub_path = (*path, key_name)
            visit(key, key_name, val, sub_path)
            walk(val, sub_path, depth + 1)

    # Best-effort: don't tank the whole sync if one component
    # manifest's schema is misshapen. Log at debug level so we can
    # tell whether a missing introspection result is "schema didn't
    # constrain that key" or "walker crashed silently."
    try:
        walk(schema, (), 0)
    except Exception:
        _LOGGER.debug("schema walk aborted on %r", schema, exc_info=True)


def _collect_platform_defaults(manifest: Any) -> dict[tuple[str, ...], dict[str, Any]]:
    """Walk the live ``CONFIG_SCHEMA`` for ``cv.SplitDefault`` keys.

    Returns ``{key_path: {platform: default_value}}`` keyed by tuple
    paths so nested fields can be looked up unambiguously. When the
    component has no schema (rare), returns an empty dict.
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}

    out: dict[tuple[str, ...], dict[str, Any]] = {}

    def visit(key: Any, _key_name: str, _val: Any, path: tuple[str, ...]) -> None:
        if not isinstance(key, vol.Optional):
            return
        factories = getattr(key, "_defaults", None)
        if not isinstance(factories, dict):
            return
        per_platform: dict[str, Any] = {}
        for plat, factory in factories.items():
            try:
                value = factory() if callable(factory) else factory
            except Exception:  # noqa: S112 — best-effort default extraction
                continue
            if value is vol.UNDEFINED or not _is_json_safe(value):
                continue
            per_platform[str(plat)] = value
        if per_platform:
            out[path] = per_platform

    _walk_schema_keys(schema, visit)
    return out


class RefinedType(NamedTuple):
    """Type recovered from a runtime validator, with type-specific extras.

    ``unit_options`` is populated only for ``float_with_unit`` entries —
    the unit picker the frontend renders alongside the numeric input.
    Other types ignore it.
    """

    type: str
    unit_options: list[str] | None = None


# ``cv.*`` validators built on ``cv.float_with_unit``. Their unit
# choices come from the validator's compiled regex at runtime —
# never a hand-maintained list — so the catalog stays in sync with
# ESPHome without anyone having to remember to update us.
#
# Metric prefixes the frontend's unit picker offers when the
# validator allows them. ``cv.METRIC_SUFFIXES`` accepts every SI
# prefix from quecto (1e-30) through quetta (1e30) plus a few non-
# standard ones (deca, hecto, deci, centi); a picker exposing all
# 26 entries — half of which describe scales below the noise floor
# of an MCU, the other half above the diameter of the observable
# universe — is unusable. This list is the IoT-relevant subset:
# nano (cap, ns), micro (V, A, F, s), milli, base, kilo, mega, giga.
# Both ``µ`` and ``u`` resolve to 1e-6 in ESPHome — only ``µ`` is
# emitted (the SI canonical form) so the picker doesn't show two
# entries that mean the same thing.
#
# A future per-quantity override list (frequencies don't need ``n``;
# voltages don't need ``G``) is reasonable, but the current list is
# already a strict superset of what every real ESPHome config in the
# wild uses.
# Base unit ("") comes first so the canonical unit (per the model
# docs: "first entry is the canonical unit") is the un-prefixed
# form — `Hz` not `nHz`. The remaining prefixes follow in
# magnitude order.
_COMMON_METRIC_PREFIXES = ["", "n", "µ", "m", "k", "M", "G"]

# Names of the ``cv.*`` validators we know are built on
# ``cv.float_with_unit``. Each comes through the live-introspection
# walker via ``getattr(cv, name)`` so the catalog tracks ESPHome's
# actual surface — adding a validator here only matters when ESPHome
# adds one upstream. ``cv.time_period`` is intentionally absent: its
# grammar (``1h30s``) and unit set are richer than the
# ``float_with_unit`` widget can express, so it keeps its own
# ``time_period`` type.
_FLOAT_WITH_UNIT_VALIDATORS = (
    "frequency",
    "data_size",
    "framerate",
    "voltage",
    "distance",
    "temperature",
    "temperature_delta",
)

# Validators that accept METRIC prefixes on their base unit. Order
# matters: the first entry's compiled value is the canonical unit
# the picker defaults to.
_METRIC_PREFIX_VALIDATORS = frozenset({"frequency", "voltage", "data_size", "distance"})
# Validators whose suffix is a fixed list rather than
# metric-prefix-able (e.g. temperature has only °C / °F / K, not
# m°C). The compiled regex's alternation captures the full set.
_FIXED_UNIT_VALIDATORS = frozenset({"framerate", "temperature", "temperature_delta"})

# Fallback unit lists for validators we can't introspect. Kept as
# small as possible — only validators that fail
# ``_extract_validator_units`` need an entry here. ``cv.validate_bytes``
# uses an inline regex inside the function body (not a closure
# pattern); ``cv.temperature`` / ``cv.temperature_delta`` are hand-
# rolled functions that compose multiple ``float_with_unit`` sub-
# validators sequentially. The unit set for these is stable across
# ESPHome releases — they're physical-unit definitions — so the
# brittleness cost is low. If ESPHome ever changes them, the
# catalog sync produces stale options but the user-visible result
# is just a missing or extra item in the unit picker.
_UNIT_FALLBACKS: dict[str, list[str]] = {
    "data_size": ["B", "kB", "MB", "GB"],
    "temperature": ["°C", "°F", "K"],
    "temperature_delta": ["°C", "°F", "K"],
}


def _extract_validator_units(validator: Any) -> list[str] | None:  # noqa: PLR0911, C901
    """Pull the unit option list out of a ``cv.float_with_unit`` validator.

    Inspects the closure cells produced by ``float_with_unit``: a
    compiled ``re.Pattern`` whose final optional group is the base-unit
    alternation. Combined with ``cv.METRIC_SUFFIXES`` (for prefix-able
    validators) we recover the full picker list — no hand-maintained
    mapping that goes stale on the next ESPHome release.
    """
    try:
        from esphome import config_validation as cv
    except Exception:
        return None
    closure = getattr(validator, "__closure__", None) or ()
    pattern = None
    quantity = None
    for cell in closure:
        contents = cell.cell_contents
        if isinstance(contents, re.Pattern):
            pattern = contents
        elif isinstance(contents, str):
            quantity = contents
    if pattern is None:
        return None
    # The validator's regex ends with a final group capturing the
    # base unit(s) — usually ``(Hz|HZ|hz)?`` for ``cv.frequency``
    # but sometimes ``(m)$`` (no ``?``) for ``cv.distance``. Match
    # the last parenthesized group anchored to ``$``; this avoids
    # false-matching earlier ``(\w*?)`` capture groups in the
    # mantissa-and-prefix prefix.
    match = re.search(r"\(([^)]+)\)\??\$", pattern.pattern)
    if not match:
        return None
    raw_alternatives = [alt for alt in match.group(1).split("|") if alt]
    if not raw_alternatives:
        return None
    # Prefer an alternative containing uppercase letters when one
    # exists — esphome regexes list lowercase first (``v``) for
    # case-insensitive matching, but the user-facing canonical
    # form for SI units uses the uppercase symbol (``V``, ``Hz``).
    # Without this preference we'd populate ``unit_options`` with
    # lowercase ``v`` / ``hz`` etc.
    canonical = next(
        (alt for alt in raw_alternatives if any(c.isupper() for c in alt)),
        raw_alternatives[0],
    )
    raw_alternatives = [canonical, *(a for a in raw_alternatives if a != canonical)]
    if quantity in _FIXED_UNIT_VALIDATORS:
        # Each alternative is a distinct unit (``°C``, ``°F``,
        # ``K``). Deduplicate case variants by lowercasing.
        seen: set[str] = set()
        units: list[str] = []
        for alt in raw_alternatives:
            key = alt.casefold()
            if key in seen:
                continue
            seen.add(key)
            units.append(alt)
        return units
    if quantity in _METRIC_PREFIX_VALIDATORS:
        # Pick the first alternative as the canonical base unit
        # (``Hz`` from ``Hz|HZ|hz``) and combine with metric
        # prefixes. Returns ``["Hz", "mHz", "kHz", "MHz", "GHz"]``.
        base_unit = raw_alternatives[0]
        metric_suffixes = getattr(cv, "METRIC_SUFFIXES", {"": 1.0})
        return [
            f"{prefix}{base_unit}"
            for prefix in _COMMON_METRIC_PREFIXES
            if prefix in metric_suffixes
        ]
    return None


def _collect_refined_types(  # noqa: C901
    manifest: Any,
) -> dict[tuple[str, ...], RefinedType]:
    """Walk the live ``CONFIG_SCHEMA`` to recover types the schema lost.

    The pre-built schema collapses many ``cv.boolean`` / ``cv.float_`` /
    ``cv.icon`` / ``cv.lambda_`` validators into bare strings. By
    inspecting the actual voluptuous validators we can promote those
    fields back to the right type. Returns ``{key_path: RefinedType}``
    where the named tuple carries ``type`` plus per-type extras (e.g.
    ``unit_options`` for ``float_with_unit``).
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}
    try:
        from esphome import config_validation as cv
    except Exception:
        return {}

    # Map runtime validator identities / names to refined types. The
    # schema bundle already gets ``cv.string`` and ``cv.int_`` right via
    # explicit ``type:`` markers; we focus on the cases where the
    # bundle silently emits no type at all. Identity is keyed by
    # ``id()`` because some voluptuous validators (notably _Schema
    # subclasses) override __hash__ to be unhashable.
    by_identity: dict[int, RefinedType] = {}
    by_name: dict[str, RefinedType] = {}

    def add(name: str, refined: RefinedType, *attrs: str) -> None:
        by_name[name] = refined
        for a in attrs:
            obj = getattr(cv, a, None)
            if obj is not None:
                by_identity[id(obj)] = refined

    add("boolean", RefinedType("boolean"), "boolean")
    add("float_", RefinedType("float"), "float_", "positive_float", "negative_float")
    add("float_range", RefinedType("float"), "float_range")
    # Unit-coerced validators — render as a number input + unit
    # picker on the frontend. The validator's runtime type is a
    # float, but the YAML shape is ``"<value><unit>"``. Pull the
    # unit list from the validator's compiled regex at runtime so
    # the catalog stays in sync with ESPHome without a
    # hand-maintained table to forget about.
    for validator_name in _FLOAT_WITH_UNIT_VALIDATORS:
        validator = getattr(cv, validator_name, None)
        if validator is None:
            continue
        units = _extract_validator_units(validator) or _UNIT_FALLBACKS.get(validator_name)
        if not units:
            continue
        add(
            validator_name,
            RefinedType("float_with_unit", unit_options=units),
            validator_name,
        )
    add("icon", RefinedType("icon"), "icon")
    add("lambda_", RefinedType("lambda"), "lambda_")
    add("returning_lambda", RefinedType("lambda"), "returning_lambda")
    add("mac_address", RefinedType("mac_address"), "mac_address")
    add("color_temperature", RefinedType("string"), "color_temperature")

    out: dict[tuple[str, ...], RefinedType] = {}

    def classify(validator: Any) -> RefinedType | None:
        if id(validator) in by_identity:
            return by_identity[id(validator)]
        # Some validators are wrapped (vol.All chains or partials);
        # peel down to find the inner.
        inner = getattr(validator, "validators", None)
        if inner:
            for v in inner:
                t = classify(v)
                if t is not None:
                    return t
        # ``cv.float_with_unit`` returns a closure whose ``__name__``
        # is the generic ``"validator"`` (too noisy to substring-
        # match) but whose ``__qualname__`` carries the factory
        # name. Detect that shape and pull units straight from the
        # closure — handles platform-style entries (``sensor.mcp3008.
        # reference_voltage``, ``esp32_camera.idle_framerate``) the
        # name-by-name registration loop missed because they weren't
        # bound back to a top-level ``cv.<name>`` attribute.
        qualname = getattr(validator, "__qualname__", "") or ""
        if "float_with_unit" in qualname:
            units = _extract_validator_units(validator)
            if units:
                return RefinedType("float_with_unit", unit_options=units)
        # Fall back to name-based matching for closures and partials
        # that lose identity but keep the name.
        name = (getattr(validator, "__name__", None) or qualname).lower()
        for k, t in by_name.items():
            if k in name:
                return t
        return None

    def visit(_key: Any, _key_name: str, val: Any, path: tuple[str, ...]) -> None:
        t = classify(val)
        if t is not None:
            out[path] = t

    _walk_schema_keys(schema, visit)
    return out


def _walk_catalog_entries(
    entries: list[dict],
    visit: Callable[[dict, tuple[str, ...]], None],
) -> None:
    """
    Walk *entries* recursively, calling ``visit(entry, path)`` for each.

    Common kernel for the appliers that layer signals from
    introspection onto the in-progress catalog dict — paths are
    tuples of ``entry["key"]`` values matching the keys returned
    by the schema-side collectors. Used by
    ``_apply_platform_defaults``, ``_apply_refined_types``, and
    ``_apply_platform_constraints``.
    """

    def walk(items: list[dict], path: tuple[str, ...]) -> None:
        for entry in items:
            sub_path = (*path, entry["key"])
            visit(entry, sub_path)
            inner = entry.get("config_entries")
            if inner:
                walk(inner, sub_path)

    walk(entries, ())


def _apply_refined_types(
    entries: list[dict],
    refined: dict[tuple[str, ...], RefinedType],
) -> None:
    """Promote entry types from string → boolean/float/... where known.

    Only acts on entries currently typed ``string`` so we don't
    override the schema's explicit type assignments — EXCEPT for
    ``float_with_unit``, which we always apply because it carries
    extra info (``unit_options``) the schema bundle can't express.
    The schema bundle's ``float`` typing for those entries is
    technically the runtime type after coercion, but the YAML shape
    the user types is a string with a unit suffix; the
    ``float_with_unit`` type captures both halves.
    """
    if not refined:
        return

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        new_type = refined.get(path)
        if new_type is None:
            return
        if new_type.type == "float_with_unit":
            # Always apply — see docstring. Carries unit_options
            # the schema bundle can't represent.
            entry["type"] = new_type.type
            entry["unit_options"] = list(new_type.unit_options or [])
        elif entry.get("type") == "string":
            entry["type"] = new_type.type

    _walk_catalog_entries(entries, visit)


def _apply_unit_of_measurement_options(entries: list[dict]) -> None:
    """Fill ``unit_of_measurement`` options from ``esphome.const.UNIT_*``.

    The schema marks the field with ``data_type:
    validate_unit_of_measurement`` and a custom validator function —
    no enum values, even though ESPHome ships a curated set of common
    units (``W``, ``V``, ``A``, ``°C``, ``%``, ...). Pull them from
    ``esphome.const`` and surface as suggestions with
    ``allow_custom_value=True`` so the frontend renders an
    autocomplete combobox: pick a common unit or type a custom one.

    Walks recursively so entity sub-readings (``sensor.dht.humidity``)
    also get the suggestions.
    """
    options = _UNIT_OF_MEASUREMENT_OPTIONS
    if not options:
        return

    def walk(items: list[dict]) -> None:
        for entry in items:
            if (
                entry.get("key") == "unit_of_measurement"
                and entry.get("type") == "string"
                and not entry.get("options")
            ):
                entry["options"] = options
                entry["allow_custom_value"] = True
            inner = entry.get("config_entries")
            if inner:
                walk(inner)

    walk(entries)


def _promote_multi_value_keys(entries: list[dict]) -> None:
    """
    Promote ``id`` / ``*_id`` children off Advanced for list rows.

    Acts on ``id`` and ``*_id`` children of ``multi_value=True``
    NESTED parents (``esphome.devices``, ``esphome.areas``, …):
    demotes them off the Advanced toggle, and promotes the
    parent's own ``id`` to *required*. ``_id`` references
    (``area_id``) stay optional — a device with no area is a
    valid config; a device with no id is not.

    The upstream schema marks own-id fields advanced because
    ESPHome's id system auto-generates one for top-level
    components like ``sensor.dht``. For repeatable nested
    mappings the id IS the cross-reference primary key — without
    it nothing else can point at the row — so the visual editor
    needs it on the main form, not behind the Advanced toggle.
    Without this fix a fresh row from the renderer's Add button
    looks like it accepts only ``name`` (issue #434).
    """

    def visit(entry: dict, _path: tuple[str, ...]) -> None:
        if not entry.get("multi_value") or entry.get("type") != "nested":
            return
        inner = entry.get("config_entries") or []
        mutated = False
        for child in inner:
            is_own_id = child["key"] == "id" and child.get("type") == "id"
            if not (is_own_id or child["key"].endswith("_id")):
                continue
            # Flip the ``mutated`` flag only when a flag actually
            # changes — if a future upstream-schema release already
            # marks ``id`` non-advanced + required we'd otherwise
            # re-sort a list that's already correct, potentially
            # disturbing an order the schema authors picked
            # deliberately.
            if child.get("advanced"):
                child["advanced"] = False
                mutated = True
            if is_own_id and not child.get("required"):
                child["required"] = True
                mutated = True
        # Demoting from advanced changes ``_sort_entries``' sort key
        # (non-advanced first, then by ``_IMPORTANT_KEY_ORDER``), so
        # an advanced sibling like ``comment`` would otherwise stay
        # ahead of the now-non-advanced ``id``. Re-sort to keep the
        # form's ordering invariant.
        if mutated:
            entry["config_entries"] = _sort_entries(inner)

    _walk_catalog_entries(entries, visit)


def _load_unit_of_measurement_options() -> list[dict[str, str]]:
    """Best-effort: read ``esphome.const`` for ``UNIT_*`` constants.

    Returns a list of ``{label, value}`` dicts sorted alphabetically.
    Empty list when esphome isn't importable.
    """
    try:
        from esphome import const
    except Exception:
        return []
    raw = sorted(
        {
            getattr(const, name)
            for name in dir(const)
            if name.startswith("UNIT_") and isinstance(getattr(const, name), str)
        }
    )
    return [{"label": v, "value": v} for v in raw]


_UNIT_OF_MEASUREMENT_OPTIONS: list[dict[str, str]] = _load_unit_of_measurement_options()


def _apply_platform_defaults(
    entries: list[dict],
    platform_defaults: dict[tuple[str, ...], dict[str, Any]],
) -> None:
    """Layer ``platform_defaults`` from introspection onto matching entries."""
    if not platform_defaults:
        return

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        pd = platform_defaults.get(path)
        if pd:
            entry["platform_defaults"] = pd

    _walk_catalog_entries(entries, visit)


def _platform_set(node: Any) -> frozenset[str] | None:
    """
    Return allowed target platforms for *node*, or ``None`` if unconstrained.

    Walks ``cv.only_on`` / ``cv.only_on_<platform>`` validator
    closures plus the ``vol.All`` / ``vol.Any`` combinators they
    appear inside:

    - closure: ``cv.only_on(platforms)`` returns a closure that
      captures ``platforms`` as a nonlocal. ``inspect.getclosurevars``
      reads it back by name — no qualname / cell-index coupling, so
      we keep working if upstream renames the inner function.
      ``platforms`` is unique to ``only_on`` in
      ``esphome.config_validation`` (the framework variant uses
      ``frameworks``), so a name-only check is unambiguous.
    - ``vol.Any``: union of branch constraints. If any branch is
      unconstrained the whole Any accepts every platform → ``None``.
    - ``vol.All``: intersection of branch constraints, ignoring
      unconstrained children.

    Returns ``None`` (not an empty set) when nothing along the
    chain constrains platform — empty-set would mean "no platform
    accepted," a schema bug we don't want to silently mask.
    """
    if callable(node) and getattr(node, "__closure__", None):
        try:
            nonlocals = inspect.getclosurevars(node).nonlocals
        except (TypeError, ValueError):
            nonlocals = {}
        platforms = nonlocals.get("platforms")
        if isinstance(platforms, list):
            # ``Platform`` is a ``StrEnum``; ``str()`` yields the
            # canonical identifier (``esp32``, ``esp8266``, ...).
            names = [str(p) for p in platforms if isinstance(p, str | StrEnum)]
            if names:
                return frozenset(names)

    if isinstance(node, vol.Any):
        sets = [_platform_set(child) for child in node.validators]
        if not sets or any(s is None for s in sets):
            # Empty Any (no branches) accepts nothing, but that's a
            # schema bug we don't model here; an unconstrained branch
            # makes the whole Any unconstrained.
            return None
        return frozenset().union(*sets)

    if isinstance(node, vol.All):
        constrained = [
            s for s in (_platform_set(child) for child in node.validators) if s is not None
        ]
        if not constrained:
            return None
        result = frozenset.intersection(*constrained)
        if not result:
            # Disjoint ``cv.only_on`` gates in the same ``vol.All``
            # chain (e.g. ``All(only_on_esp32, only_on_esp8266)``)
            # would intersect to the empty set — a field that
            # accepts no platform. That's an upstream schema bug;
            # we can't represent "no platforms" in the wire format
            # (``[]`` already means "no restriction"). Log so the
            # bug surfaces in the next sync run, then fall through
            # to ``return result or None`` below so the empty set
            # doesn't get silently serialised as ``[]`` and the
            # field stays visible — the compile-time validator will
            # catch the actual incompatibility.
            _LOGGER.warning(
                "platform constraint intersection collapsed to empty "
                "(disjoint cv.only_on gates in vol.All chain): %r",
                constrained,
            )
        return result or None

    return None


def _collect_platform_constraints(
    manifest: Any,
) -> dict[tuple[str, ...], list[str]]:
    """
    Walk the live ``CONFIG_SCHEMA`` for per-field ``cv.only_on`` gates.

    Returns ``{key_path: sorted_platforms}`` keyed by tuple paths
    so nested fields can be looked up unambiguously. A path that
    isn't in the returned dict has no platform constraint
    (the common case — fields like ``free``/``loop_time`` on
    ``sensor.debug`` are valid on every platform the parent
    component runs on).

    Empty dict when the component has no schema.
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}

    out: dict[tuple[str, ...], list[str]] = {}

    def visit(_key: Any, _key_name: str, val: Any, path: tuple[str, ...]) -> None:
        constraint = _platform_set(val)
        if constraint:
            out[path] = sorted(constraint)

    _walk_schema_keys(schema, visit)
    return out


def _apply_platform_constraints(
    entries: list[dict],
    constraints: dict[tuple[str, ...], list[str]],
) -> None:
    """Stamp ``supported_platforms`` onto entries whose path is gated."""
    if not constraints:
        return

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        constraint = constraints.get(path)
        if constraint:
            entry["supported_platforms"] = list(constraint)

    _walk_catalog_entries(entries, visit)


# ``cv.has_*_one_key`` closures share the qualname template
# ``has_<kind>_key.<locals>.validate``. We pin against that template
# rather than the ``__name__`` (uniformly ``"validate"``) so a
# legitimate validator from another factory can't masquerade as a
# cardinality constraint. Mirrors the
# :data:`RequiredGroupKind` wire values one-for-one — if upstream
# adds a fifth cardinality validator the mapping needs a new entry
# and the model enum a new member, in lockstep.
_HAS_KEY_QUALNAMES: dict[str, str] = {
    "has_exactly_one_key.<locals>.validate": "exactly_one",
    "has_at_least_one_key.<locals>.validate": "at_least_one",
    "has_at_most_one_key.<locals>.validate": "at_most_one",
    "has_none_or_all_keys.<locals>.validate": "none_or_all",
}


def _required_group_from_validator(node: Any) -> dict[str, Any] | None:
    """Return a ``{kind, keys}`` spec when *node* is a ``cv.has_*_one_key`` closure."""
    qualname = getattr(node, "__qualname__", "") or ""
    kind = _HAS_KEY_QUALNAMES.get(qualname)
    if kind is None:
        return None
    try:
        nonlocals = inspect.getclosurevars(node).nonlocals
    except (TypeError, ValueError):
        return None
    keys = nonlocals.get("keys")
    if not isinstance(keys, tuple | list) or not keys:
        return None
    str_keys = [k for k in keys if isinstance(k, str)]
    if not str_keys:
        return None
    return {"kind": kind, "keys": str_keys}


def _collect_inclusive_groups(
    manifest: Any,
) -> dict[tuple[str, ...], str]:
    """
    Walk the live ``CONFIG_SCHEMA`` for ``cv.Inclusive(...)`` markers.

    Returns ``{key_path: group_name}`` keyed by tuple paths. A
    field wrapped in ``cv.Inclusive(key, "foo")`` upstream surfaces
    as ``{("...", key): "foo"}`` — the frontend pairs this with
    the parent schema's ``required_groups`` to render the full
    "all members of group must be set, or none" rule.

    Empty dict when the component has no schema.
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}

    out: dict[tuple[str, ...], str] = {}

    def visit(key: Any, _key_name: str, _val: Any, path: tuple[str, ...]) -> None:
        if not isinstance(key, vol.Inclusive):
            return
        # voluptuous historically named the attribute
        # ``group_of_exclusion`` (matching the sibling
        # ``Exclusive.group_of_exclusion``); esphome's vendored
        # voluptuous and modern upstream both renamed it to
        # ``group_of_inclusion``. Check both for resilience across
        # voluptuous releases.
        group = getattr(key, "group_of_inclusion", None) or getattr(key, "group_of_exclusion", None)
        if isinstance(group, str) and group:
            out[path] = group

    _walk_schema_keys(schema, visit)
    return out


def _collect_required_groups(  # noqa: C901
    manifest: Any,
) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    """
    Walk the live ``CONFIG_SCHEMA`` for ``cv.has_*_one_key`` constraints.

    Returns ``{schema_path: [{kind, keys}, ...]}`` keyed by tuple
    paths. ``()`` is the component's top-level schema; non-empty
    paths target a nested schema (e.g.
    ``("networks", "eap")`` for ``wifi.networks[].eap``).

    Detection looks at the ``__qualname__`` of each callable
    validator inside the ``vol.All`` chain wrapping a schema.
    ``cv.has_exactly_one_key`` and friends are factory functions
    that return closures named ``"validate"`` — the qualname
    keeps the factory name (e.g.
    ``has_exactly_one_key.<locals>.validate``) so we can both
    detect them and recover their kind. The captured ``keys``
    nonlocal carries the field names the constraint applies to.

    Empty dict when the component has no schema.
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}

    out: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    visited: set[int] = set()

    def collect_at(node: Any, path: tuple[str, ...]) -> None:
        # Walk through ``vol.All`` wrappers at the current level
        # surfacing every ``cv.has_*_one_key`` validator. The cap
        # mirrors the ``unwrap`` budget below — a misbehaving
        # cyclic ``vol.All`` chain can't lock the walker.
        for _ in range(8):
            if not isinstance(node, vol.All):
                return
            for child in node.validators:
                group = _required_group_from_validator(child)
                if group is not None:
                    out.setdefault(path, []).append(group)
            inner = next(
                (v for v in node.validators if isinstance(v, vol.All)),
                None,
            )
            if inner is None:
                return
            node = inner

    def unwrap_to_dict(node: Any) -> dict | None:
        # Same shape (and same vol-compile workaround) as
        # :func:`_walk_schema_keys`'s ``unwrap_to_dict`` — prefer
        # ``validators`` over ``.schema`` on compound validators.
        for _ in range(8):
            if isinstance(node, dict):
                return node
            inner = getattr(node, "validators", None)
            if inner:
                next_node = next(
                    (v for v in inner if isinstance(v, dict) or hasattr(v, "schema")),
                    None,
                )
                if next_node is None:
                    return None
                node = next_node
                continue
            if hasattr(node, "schema"):
                node = node.schema
                continue
            return None
        return None

    def walk(node: Any, path: tuple[str, ...], depth: int) -> None:
        if depth > 6:
            return
        collect_at(node, path)
        target = unwrap_to_dict(node)
        if target is None:
            return
        if id(target) in visited:
            return
        visited.add(id(target))
        for key, val in target.items():
            key_name = key.schema if hasattr(key, "schema") else str(key)
            walk(val, (*path, key_name), depth + 1)

    try:
        walk(schema, (), 0)
    except Exception:
        _LOGGER.debug("required-groups walk aborted on %r", schema, exc_info=True)
    return out


def _apply_inclusive_groups(
    entries: list[dict],
    groups: dict[tuple[str, ...], str],
) -> None:
    """Stamp ``group`` onto entries whose path is a ``cv.Inclusive`` marker."""
    if not groups:
        return

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        group = groups.get(path)
        if group:
            entry["group"] = group

    _walk_catalog_entries(entries, visit)


def _apply_required_groups(
    component: dict,
    groups: dict[tuple[str, ...], list[dict[str, Any]]],
) -> None:
    """
    Stamp ``required_groups`` onto the component root + nested entries.

    Constraints at path ``()`` live on the component itself; deeper
    paths attach to the matching nested ``ConfigEntry`` (only
    meaningful when the target entry is a ``NESTED`` container).
    Paths that don't match any catalog entry are silently dropped —
    schema-only constructs (``cv.ensure_list`` markers, internal
    wrappers) can show up in the schema walk without a catalog
    counterpart.

    Constraint-referenced sibling fields (plus every ``Inclusive``
    group member that shares the same group name) are promoted off
    ``advanced`` so the user can see the choices upstream actually
    requires — issue #924, where ``light.esp32_rmt_led_strip``'s
    required ``chipset`` field sat hidden under Advanced Settings.
    """
    if not groups:
        return
    root = groups.get(())
    if root:
        component["required_groups"] = [dict(g) for g in root]
        component["config_entries"] = _promote_constraint_members(
            component.get("config_entries") or [], root
        )

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        nested = groups.get(path)
        if not nested:
            return
        entry["required_groups"] = [dict(g) for g in nested]
        entry["config_entries"] = _promote_constraint_members(
            entry.get("config_entries") or [], nested
        )

    _walk_catalog_entries(component.get("config_entries") or [], visit)


def _promote_constraint_members(
    entries: list[dict],
    groups: list[dict[str, Any]],
) -> list[dict]:
    """
    Demote constraint-referenced siblings off ``advanced`` and re-sort.

    A field whose key appears in any ``required_group.keys`` — or
    that shares a ``group`` (``cv.Inclusive``) with such a field —
    must be visible on the main form, because the upstream schema
    fails validation without it. Returns the original list when
    nothing changed; otherwise a fresh, re-sorted list (the
    advanced/main split feeds into :func:`_sort_entries`'s key, so
    a demotion changes ordering).
    """
    keys_in_groups = {key for g in groups for key in g.get("keys", [])}
    if not keys_in_groups:
        return entries
    inclusive_groups = {
        e["group"] for e in entries if e["key"] in keys_in_groups and e.get("group")
    }
    mutated = False
    for entry in entries:
        if (
            entry["key"] in keys_in_groups
            or (inclusive_groups and entry.get("group") in inclusive_groups)
        ) and entry.get("advanced"):
            entry["advanced"] = False
            mutated = True
    if not mutated:
        return entries
    return _sort_entries(entries)


# Human-readable prefixes per ``cv.has_*_one_key`` kind. Kept short
# and ``**bold**`` so the markdown renderer makes the rule jump out
# above the schema-author's prose. The keys here mirror
# :data:`_HAS_KEY_QUALNAMES` values one-for-one.
_REQUIRED_GROUP_PREFIX: dict[str, str] = {
    "exactly_one": "**Required — set exactly one of:**",
    "at_least_one": "**Required — set at least one of:**",
    "at_most_one": "**Set at most one of:**",
    "none_or_all": "**Set together — all of these must be set, or all left blank:**",
}


def _annotate_constraint_descriptions(component: dict) -> None:
    """
    Prepend a markdown hint to descriptions of constraint-involved fields.

    The structured ``group`` / ``required_groups`` fields are the
    contract the frontend should eventually consume, but until that
    lands the rule has to reach the user through the description
    prose — otherwise issue #924 lingers (chipset is now visible,
    but the user has no idea they must pick it OR the manual-timing
    fields). The injected prefix is on its own paragraph above the
    original description so a future FE update that drops it can
    pattern-match the leading ``**Required`` / ``**Set`` lines.

    Recurses into nested ``NESTED`` entries' ``config_entries``,
    using each parent's ``required_groups`` for the in-scope hint.
    """

    def visit(entries: list[dict], groups: list[dict[str, Any]]) -> None:
        _annotate_scope(entries, groups)
        for entry in entries:
            inner = entry.get("config_entries")
            if inner:
                visit(inner, entry.get("required_groups") or [])

    visit(
        component.get("config_entries") or [],
        component.get("required_groups") or [],
    )


def _annotate_scope(entries: list[dict], required_groups: list[dict[str, Any]]) -> None:  # noqa: C901
    """Annotate one sibling list with its in-scope required + inclusive hints."""
    if not entries:
        return
    inclusive_members: dict[str, list[str]] = {}
    for entry in entries:
        group_name = entry.get("group")
        if group_name:
            inclusive_members.setdefault(group_name, []).append(entry["key"])

    for entry in entries:
        prefixes: list[str] = []
        for group in required_groups:
            if entry["key"] not in group.get("keys", []):
                continue
            others = [k for k in group["keys"] if k != entry["key"]]
            if not others:
                continue
            prefix = _REQUIRED_GROUP_PREFIX.get(group.get("kind", ""))
            if prefix is None:
                continue
            prefixes.append(f"{prefix} {_format_key_list(group['keys'])}.")
        group_name = entry.get("group")
        if group_name:
            siblings = [k for k in inclusive_members.get(group_name, []) if k != entry["key"]]
            if siblings:
                prefixes.append(
                    f"**Set together with:** {_format_key_list(siblings)} (all-or-none).",
                )
        if not prefixes:
            continue
        # Blank-line separator preserves paragraph breaks in
        # CommonMark — the prefix lands as a standalone paragraph
        # above the schema-author's prose.
        original = entry.get("description") or ""
        entry["description"] = "\n\n".join([*prefixes, original]).strip()


def _format_key_list(keys: list[str]) -> str:
    """Render keys as a comma-separated backticked list (markdown inline code)."""
    return ", ".join(f"`{k}`" for k in keys)


def _numeric_range_bounds(node: Any) -> tuple[int | float, int | float] | None:
    """
    Return ``(min, max)`` for a field's value validator, or ``None``.

    Walks ``vol.All`` chains collecting every ``vol.Range`` along the
    way and intersects them — ``cv.positive_int`` is itself
    ``cv.All(cv.int_, cv.Range(min=0))``, so a field declared as
    ``cv.All(cv.positive_int, cv.Range(min=1, max=15))`` must
    intersect both ranges to recover the tighter bound the user
    actually sees at compile time.

    Only emits when both bounds resolve to numeric (``int`` /
    ``float``) values:

    - ``vol.Range(min=1, max=15)`` → ``(1, 15)`` — surfaced.
    - ``vol.Range(min=0)`` (max unbounded) → ``None``, emit nothing
      so the data-type's natural bounds (or none) win.
    - ``vol.Range(max=TimePeriod(microseconds=4294967295))`` →
      ``None``, the wire format is numeric only.

    Disjoint chains where the intersection collapses to ``min > max``
    (e.g. ``cv.All(cv.Range(min=10), cv.Range(max=5))``) are an
    upstream schema bug — a field that accepts no value. The wire
    format ``[min, max]`` can't represent "accepts nothing," and a
    serialised ``[10, 5]`` would clamp wrong on the frontend. Log a
    warning so the upstream bug surfaces in the next sync run, then
    return ``None`` so the field stays unbounded — the compile-time
    validator catches the actual incompatibility.

    ``vol.Any`` branches aren't traversed: a field declared as
    ``vol.Any(vol.Range(min=1, max=10), vol.Range(min=20, max=30))``
    would mean "value is in [1, 10] OR [20, 30]," which the wire
    format's single ``[min, max]`` pair can't express. Skipping
    ``vol.Any`` entirely is the conservative choice — the field
    falls through to its ``data_type`` defaults (or no bounds), and
    the user still gets a compile-time validation error if they
    pick a number neither branch accepts. None of today's catalog
    components hit this shape; the limitation is documented for
    when a future schema introduces it.

    The schema bundle's ``data_type`` field surfaces this for fixed-
    width integers (``uint8_t`` → ``[0, 255]``) but not for
    ``positive_int`` chained with a ``cv.Range(...)``, which is the
    ``bluetooth_proxy.connection_slots`` case (issue #426 — the
    visual editor accepts any positive integer because the
    ``cv.Range(min=1, max=15)`` is dropped from the bundle).
    """
    mins: list[int | float] = []
    maxes: list[int | float] = []

    def collect(n: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(n, vol.Range):
            if isinstance(n.min, (int, float)) and not isinstance(n.min, bool):
                mins.append(n.min)
            if isinstance(n.max, (int, float)) and not isinstance(n.max, bool):
                maxes.append(n.max)
            return
        if isinstance(n, vol.All):
            for child in n.validators:
                collect(child, depth + 1)
        # ``vol.Any`` deliberately not traversed — see docstring.

    collect(node)
    if not mins or not maxes:
        return None
    lo, hi = max(mins), min(maxes)
    if lo > hi:
        # Disjoint Range constraints in a vol.All chain — schema bug
        # upstream, the field accepts no value. Surface so future
        # syncs catch the upstream bug, then return None so we don't
        # serialise an invalid ``[lo, hi]`` pair.
        _LOGGER.warning(
            "numeric range collapsed to empty (disjoint cv.Range constraints "
            "in vol.All chain): mins=%r maxes=%r",
            mins,
            maxes,
        )
        return None
    return (lo, hi)


def _collect_field_ranges(
    manifest: Any,
) -> dict[tuple[str, ...], tuple[int | float, int | float]]:
    """
    Walk the live ``CONFIG_SCHEMA`` for per-field ``vol.Range`` bounds.

    Returns ``{key_path: (min, max)}`` for fields whose validator
    chain produces a fully-bounded numeric range. Empty dict when
    the component has no schema.
    """
    schema = getattr(manifest, "config_schema", None)
    if schema is None:
        return {}

    out: dict[tuple[str, ...], tuple[int | float, int | float]] = {}

    def visit(_key: Any, _key_name: str, val: Any, path: tuple[str, ...]) -> None:
        bounds = _numeric_range_bounds(val)
        if bounds is not None:
            out[path] = bounds

    _walk_schema_keys(schema, visit)
    return out


def _apply_field_ranges(
    entries: list[dict],
    ranges: dict[tuple[str, ...], tuple[int | float, int | float]],
) -> None:
    """Overlay schema-derived ``range`` bounds onto matching entries.

    Live-introspected bounds are more specific than the static
    ``data_type`` defaults (e.g. ``uint8_t``'s ``[0, 255]``), so
    they override an existing range when present. The frontend's
    numeric input uses the bound to clamp / validate.
    """
    if not ranges:
        return

    def visit(entry: dict, path: tuple[str, ...]) -> None:
        bounds = ranges.get(path)
        if bounds is not None:
            entry["range"] = list(bounds)

    _walk_catalog_entries(entries, visit)


def _derive_supported_platforms(
    component_id: str,
    dependencies: list[str],
    introspection: dict[str, Any],
) -> list[str]:
    """Return the list of target chips this component runs on.

    Target-platform components (``esp32``, ``rp2040``, ...) report
    themselves. Otherwise, dependencies that match ``_TARGET_PLATFORMS``
    are surfaced — ``esp32_ble_tracker`` depends on ``esp32`` so we
    return ``["esp32"]``; most components have no platform-specific
    deps and return ``[]`` (treated as "all platforms").
    """
    if introspection.get("is_target_platform"):
        return [component_id]
    return [d for d in dependencies if d in _TARGET_PLATFORMS]


def _auto_load_closure(component_id: str) -> set[str]:
    """Walk ``AUTO_LOAD`` chains starting from *component_id*."""
    seen: set[str] = set()
    queue = [component_id]
    while queue:
        cid = queue.pop()
        for item in introspect_component(cid).get("auto_load") or []:
            if item not in seen:
                seen.add(item)
                queue.append(item)
    return seen


@cache
def _implicit_dependencies() -> frozenset[str]:
    """
    Return components implicitly satisfied by configuring any transport.

    A device must declare exactly one networking transport (wifi /
    ethernet / openthread / host) and ESPHome auto-loads ``network``
    (plus its own ``AUTO_LOAD`` chain) from whichever transport the
    user picks. Components in the *intersection* of every transport's
    closure are guaranteed to be resolved no matter which one was
    chosen, so we drop them from each component's surface
    ``dependencies``. Without this filter the catalog would prompt
    the frontend to warn about a missing ``network:`` block even
    when ``wifi:`` is already configured.
    """
    if not _NETWORK_TRANSPORTS:
        return frozenset()
    closures = [_auto_load_closure(t) for t in _NETWORK_TRANSPORTS]
    if not all(closures):
        # Introspection failed for at least one transport — fall back
        # to no filtering rather than risk dropping real deps.
        return frozenset()
    return frozenset(set.intersection(*closures))


# ---------------------------------------------------------------------------
# Automation catalog
# ---------------------------------------------------------------------------


# Per-domain catalog of registry entries (action / condition / trigger
# / effect) found in a single schema file.
_AutomationRegistries = dict[str, dict[str, dict]]


# ``then:`` / ``else:`` / ``while.then:`` etc. are placeholders for
# the recursive action list — the frontend renders them as nested
# action lists, not form fields. Same for the boolean-gate keys
# ``condition`` / ``all`` / ``any`` on control-flow actions, which
# the editor renders as a condition tree.
_ACTION_LIST_KEYS: frozenset[str] = frozenset({"then", "else"})
_CONDITION_GATE_KEYS: frozenset[str] = frozenset({"condition", "all", "any"})


# Pretty labels for the small set of esphome.json ``core`` registry
# entries — the schema doesn't carry human names for those. Anything
# not in the table falls back to ``key.replace("_", " ").title()``.
_CORE_AUTOMATION_LABELS: dict[str, str] = {
    "delay": "Delay",
    "if": "If",
    "while": "While",
    "repeat": "Repeat",
    "wait_until": "Wait until",
    "lambda": "Lambda",
    "and": "And",
    "or": "Or",
    "not": "Not",
    "xor": "Xor",
    "all": "All",
    "any": "Any",
    "for": "For",
}


# Default docs URLs for the core automation registry — the schema's
# ``docs`` field is sometimes empty / generic so we point both at
# the canonical Automations page on esphome.io. Component-scoped
# actions / conditions get their docs_url from the schema's
# ``See also`` link via :func:`clean_docs`.
_CORE_AUTOMATION_DOCS = "https://esphome.io/automations/actions"

# Schema-bundle field recording an action/condition's ``maybe_simple_value`` key.
_SCHEMA_MAYBE_FIELD = "maybe"


def build_automations(  # noqa: C901
    *,
    schema_dir: Path,
    component_ids: set[str],
) -> dict[str, list[dict]]:
    """
    Walk every schema file and emit the automation catalog.

    Returns a dict with ``triggers`` / ``actions`` / ``conditions`` /
    ``light_effects`` / ``filters`` lists. Parameter schemas come
    out in the same ``ConfigEntry[]`` shape the component catalog
    uses, so the frontend renders both through one form pipeline.

    *component_ids* is the set of ids in the just-built component
    catalog (``switch.template``, ``display.ssd1306_i2c``, …).
    Used to decide whether a schema's ``<stem>.<base>`` top_key
    encodes a real platform (the flipped ``<base>.<stem>`` exists
    as a component) or just an organisational namespace
    (``page.display`` ⇒ no ``display.page`` component, so actions
    surface against the bare ``display`` domain).
    """
    triggers: list[dict] = []
    actions: list[dict] = []
    conditions: list[dict] = []
    effects: list[dict] = []
    filters: list[dict] = []

    for path in iter_schema_files(schema_dir):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _LOGGER.exception("Failed to read %s", path.name)
            continue
        for top_key, section in raw.items():
            if not isinstance(section, dict):
                continue
            # ``top_key`` is the schema's raw ``<stem>.<base>`` form
            # (e.g. ``template.switch``) — kept verbatim for entry
            # ids so they match ESPHome's wire format
            # (``template.switch.publish``). ``domain`` is the
            # canonical ``<base>.<stem>`` form used for the
            # metadata field that gets matched against the YAML
            # scoping set.
            domain = _automation_domain(top_key, component_ids=component_ids)
            # Component-scoped action / condition registries.
            for name, body in (section.get("action") or {}).items():
                entry = _convert_automation_action(
                    top_key=top_key,
                    domain=domain,
                    name=name,
                    body=body,
                    schema_dir=schema_dir,
                )
                if entry is not None:
                    actions.append(entry)
            for name, body in (section.get("condition") or {}).items():
                entry = _convert_automation_condition(
                    top_key=top_key,
                    domain=domain,
                    name=name,
                    body=body,
                    schema_dir=schema_dir,
                )
                if entry is not None:
                    conditions.append(entry)
            # Light effect registry (only present on light.json).
            for name, body in (section.get("effects") or {}).items():
                entry = _convert_light_effect(
                    name=name,
                    body=body,
                    schema_dir=schema_dir,
                )
                if entry is not None:
                    effects.append(entry)
            # Filter registry — sensor / binary_sensor / text_sensor
            # each carry their own filter registry under a ``filter:``
            # subsection (sensor has 27, binary_sensor 8, text_sensor
            # 7). ``applies_to`` tracks the originating domain so the
            # frontend renderer scopes the per-row picker against the
            # parent component's domain. #941.
            for name, body in (section.get("filter") or {}).items():
                entry = _convert_filter(
                    name=name,
                    body=body,
                    domain=top_key,
                    schema_dir=schema_dir,
                )
                if entry is not None:
                    filters.append(entry)
            # Triggers — surfaced through CONFIG_SCHEMA's config_vars
            # (and any other ``_SCHEMA`` the file declares).
            triggers.extend(
                _extract_triggers_from_section(
                    top_key=top_key,
                    domain=domain,
                    section=section,
                    schema_dir=schema_dir,
                )
            )

    return {
        "triggers": _dedupe_by_id(triggers),
        "actions": _dedupe_by_id(actions),
        "conditions": _dedupe_by_id(conditions),
        "light_effects": _dedupe_by_id(effects),
        "filters": _dedupe_filters(filters),
    }


def _automation_domain(top_key: str, *, component_ids: set[str]) -> str:
    """
    Return the canonical component id for automation entries from *top_key*.

    Flips the schema's raw ``<stem>.<base>`` shape (e.g.
    ``template.switch``) into the codebase-canonical
    ``<base>.<stem>`` form (``switch.template``) that the
    component catalog already uses — but only when the flipped
    id actually exists in *component_ids*. When it doesn't, the
    dotted prefix is an organisational namespace in the schema
    rather than a real platform (``page.display`` has no
    ``display.page`` component; ``date.datetime`` has no
    ``datetime.date`` component — these are sub-feature /
    type-discriminator namespaces) and we flatten to bare
    ``<base>`` so the action surfaces whenever the base domain
    is configured. Bare top_keys pass through unchanged.
    """
    if top_key == "core":
        return "core"
    if "." in top_key:
        stem, base = top_key.split(".", 1)
        canonical = f"{base}.{stem}"
        if canonical in component_ids:
            return canonical
        return base
    return top_key


def _convert_automation_action(
    *,
    top_key: str,
    domain: str,
    name: str,
    body: dict,
    schema_dir: Path,
) -> dict | None:
    """Build one ``AutomationAction`` dict from a schema registry entry."""
    if not isinstance(body, dict):
        return None
    docs = clean_docs(body.get("docs"))
    schema = body.get("schema") if isinstance(body.get("schema"), dict) else None
    config_entries, accepts_action_list, has_condition_gate = _extract_automation_param_schema(
        schema, schema_dir
    )
    # Stabilise the ordering — ``then`` always precedes ``else`` so
    # the wire shape doesn't churn across syncs.
    accepts_action_list = sorted(
        accepts_action_list,
        key=lambda k: (k != "then", k),
    )
    is_control_flow = bool(accepts_action_list) or has_condition_gate
    has_else_branch = "else" in (accepts_action_list or [])
    qualified = f"{top_key}.{name}" if top_key != "core" else name
    config_entries, scalar_shorthand_key = _resolve_automation_lambda(
        top_key=top_key,
        name=name,
        schema=schema,
        docs=docs,
        config_entries=config_entries,
        body=body,
    )
    return {
        "id": qualified,
        "name": _automation_label(domain, name, docs.name),
        "description": docs.text,
        "docs_url": docs.url or _CORE_AUTOMATION_DOCS,
        "domain": domain,
        "config_entries": [_strip_entry_defaults(e) for e in config_entries],
        "is_control_flow": is_control_flow,
        "has_else_branch": has_else_branch,
        "accepts_action_list": accepts_action_list,
        "scalar_shorthand_key": scalar_shorthand_key,
    }


def _convert_automation_condition(
    *,
    top_key: str,
    domain: str,
    name: str,
    body: dict,
    schema_dir: Path,
) -> dict | None:
    """Build one ``AutomationCondition`` dict from a schema registry entry."""
    if not isinstance(body, dict):
        return None
    docs = clean_docs(body.get("docs"))
    schema = body.get("schema") if isinstance(body.get("schema"), dict) else None
    config_entries, _accepts_action_list, _has_condition_gate = _extract_automation_param_schema(
        schema, schema_dir
    )
    # Boolean combinators have ``is_list: true`` + ``registry:
    # condition`` directly on the body, not inside a ``schema``.
    accepts_condition_list = bool(body.get("is_list") and body.get("registry") == "condition")
    qualified = f"{top_key}.{name}" if top_key != "core" else name
    config_entries, scalar_shorthand_key = _resolve_automation_lambda(
        top_key=top_key,
        name=name,
        schema=schema,
        docs=docs,
        config_entries=config_entries,
        body=body,
    )
    return {
        "id": qualified,
        "name": _automation_label(domain, name, docs.name),
        "description": docs.text,
        "docs_url": docs.url or _CORE_AUTOMATION_DOCS,
        "domain": domain,
        "config_entries": [_strip_entry_defaults(e) for e in config_entries],
        "accepts_condition_list": accepts_condition_list,
        "scalar_shorthand_key": scalar_shorthand_key,
    }


def _scalar_shorthand_key(body: dict) -> str | None:
    """
    Return the config key a bare-scalar shorthand maps to.

    The schema bundle records ESPHome's ``maybe_simple_value`` key on the
    registry body's ``maybe`` field (``logger.log`` → ``format``,
    ``light.turn_on`` → ``id``); absent for actions with no scalar shorthand.
    """
    maybe = body.get(_SCHEMA_MAYBE_FIELD)
    return maybe if isinstance(maybe, str) else None


def _is_scalar_extends_schema(schema: dict | None) -> bool:
    """Return True when *schema* extends only scalar primitives (no config_vars)."""
    if not schema or schema.get("config_vars"):
        return False
    extends = schema.get("extends") or []
    return bool(extends) and all(_scalar_type_for_extends_ref(ref) is not None for ref in extends)


# Registry id for the ``lambda`` filter / effect. The schema bundle
# carries no schema for it (just docs), so we recognise it by id and
# tag the catalog entry with ``value_type="lambda"`` so the frontend
# can route to its lambda editor through the same dispatch table as
# the other scalar types.
_LAMBDA_REGISTRY_ID = "lambda"

# Docs anchor for the lambda action / condition help link.
_CORE_LAMBDA_DOCS = "https://esphome.io/automations/templates#config-lambda"


def _resolve_automation_lambda(
    *,
    top_key: str,
    name: str,
    schema: dict | None,
    docs: CleanedDocs,
    config_entries: list[dict],
    body: dict,
) -> tuple[list[dict], str | None]:
    """Resolve an action/condition's ``(config_entries, scalar_shorthand_key)``.

    For the core ``lambda`` action and condition, substitute a synthesized
    ``LAMBDA`` field; for everything else, pass the extracted entries through
    with the body's ordinary scalar-shorthand key.

    ESPHome's ``lambda`` action and condition take a single bare C++ block
    (``- lambda: |- ...``) with no named params, so the schema bundle carries
    no ``schema`` and the param extractor yields no ``config_entries`` —
    leaving the visual editor with only the description and no field to edit
    the body (#1119). Recognise it by id the same way the ``lambda`` filter /
    effect does (see ``_LAMBDA_REGISTRY_ID``) and emit one ``LAMBDA`` entry
    keyed ``lambda`` — the same shape the sensor's ``lambda:`` config var has
    — so the existing form pipeline renders the lambda editor. Route the bare
    scalar through that key via ``scalar_shorthand_key`` so the writer
    collapses it back to ``lambda: |- ...`` on save.
    """
    if top_key == "core" and name == _LAMBDA_REGISTRY_ID and schema is None:
        entry = {
            "key": _LAMBDA_REGISTRY_ID,
            "type": "lambda",
            "label": "Lambda",
            "description": docs.text or None,
            "required": True,
            "help_link": _CORE_LAMBDA_DOCS,
        }
        return [entry], _LAMBDA_REGISTRY_ID
    return config_entries, _scalar_shorthand_key(body)


def _scalar_value_type_for_schema(name: str, schema: dict | None) -> str | None:
    """
    Return the scalar primitive the schema accepts, or None.

    Covers two shapes: a pure scalar (``delayed_on: 50ms``) where the
    schema has only ``extends`` to a scalar primitive, and the
    polymorphic ``cv.Any(scalar, Schema({...}))`` form
    (``delayed_on_off: 50ms`` OR ``delayed_on_off: {time_on, time_off}``)
    where the schema carries both ``extends`` to a scalar primitive
    AND a mapping in ``config_vars``. Both cases signal "the frontend
    should accept the scalar shorthand"; the polymorphic case still
    has ``config_entries`` extracted from the ``config_vars`` (see
    ``_convert_registry_entry``).
    """
    if name == _LAMBDA_REGISTRY_ID and not schema:
        return _LAMBDA_REGISTRY_ID
    if not schema:
        return None
    extends = schema.get("extends") or []
    if not extends:
        return None
    types = [_scalar_type_for_extends_ref(ref) for ref in extends]
    # All extends must resolve to a scalar primitive; a single
    # mapping-shaped extends (sensor.DELTA_SCHEMA etc.) disqualifies.
    if any(t is None for t in types):
        return None
    return types[0]


# Per-filter field overrides for shapes the upstream schema bundle
# can't surface because the validator is a custom callable (e.g.
# ``ntc_process_calibration``) instead of a structural cv.*
# combinator the bundle dumper can introspect. Each entry promotes
# the field to ``multi_value: True`` so the frontend renders an
# add/remove list editor rather than a single text input that loses
# the YAML list on save. Add new entries here as they surface; the
# fix lives upstream when the bundle dumper grows support for the
# custom validators.
_REGISTRY_FIELD_OVERRIDES: dict[tuple[str, str], dict] = {
    ("to_ntc_resistance", "calibration"): {"multi_value": True},
    ("to_ntc_temperature", "calibration"): {"multi_value": True},
}


def _apply_field_overrides(entry_id: str, config_entries: list[dict]) -> list[dict]:
    """Apply ``_REGISTRY_FIELD_OVERRIDES`` to entries keyed by id."""
    return [
        {**e, **_REGISTRY_FIELD_OVERRIDES[(entry_id, e["key"])]}
        if (entry_id, e["key"]) in _REGISTRY_FIELD_OVERRIDES
        else e
        for e in config_entries
    ]


def _convert_registry_entry(
    *,
    name: str,
    body: dict,
    label_domain: str,
    applies_to: list[str],
    schema_dir: Path,
) -> dict | None:
    """Build a registry catalog dict (id, name, config_entries, applies_to, value_type)."""
    if not isinstance(body, dict):
        return None
    docs = clean_docs(body.get("docs"))
    schema = body.get("schema") if isinstance(body.get("schema"), dict) else None
    value_type = _scalar_value_type_for_schema(name, schema)
    has_config_vars = bool(schema and schema.get("config_vars"))
    if value_type is not None and not has_config_vars:
        # Pure scalar shorthand (``delayed_on: 50ms``).
        config_entries: list[dict] = []
    else:
        # Pure mapping OR polymorphic mapping+scalar
        # (``cv.Any(time_period, Schema({...}))`` for delayed_on_off).
        # In the polymorphic case strip ``extends`` before extraction
        # so the scalar primitive's unit-parts
        # (days/hours/minutes/...) don't leak in alongside the
        # ``config_vars`` mapping fields.
        extract_schema: dict | None = schema
        if value_type is not None and has_config_vars and schema is not None:
            extract_schema = {k: v for k, v in schema.items() if k != "extends"}
        config_entries, _alist, _hcg = _extract_automation_param_schema(extract_schema, schema_dir)
        config_entries = _apply_field_overrides(name, config_entries)
    return {
        "id": name,
        "name": _automation_label(label_domain, name, docs.name),
        "config_entries": [_strip_entry_defaults(e) for e in config_entries],
        "applies_to": applies_to,
        "value_type": value_type,
    }


# Shared by `_automation_label` (producer) and `_dedupe_filters`
# (multi-domain prefix stripper).
_AUTOMATION_LABEL_SEPARATOR = " → "


def _convert_filter(
    *,
    name: str,
    body: dict,
    domain: str,
    schema_dir: Path,
) -> dict | None:
    """Build one ``Filter`` dict from a ``<domain>.filter`` registry entry."""
    return _convert_registry_entry(
        name=name,
        body=body,
        label_domain=domain,
        applies_to=[domain],
        schema_dir=schema_dir,
    )


def _dedupe_filters(filters: list[dict]) -> list[dict]:
    """
    Merge filters sharing an ``id`` across domains; union ``applies_to``.

    Multi-domain merges strip the ``"<Domain> → "`` prefix from the
    display name since it would otherwise read wrong in whichever
    domain the user is editing (``lambda`` under ``sensor:`` would
    show "Binary Sensor → Lambda" otherwise).
    """
    by_id: dict[str, dict] = {}
    for f in filters:
        existing = by_id.get(f["id"])
        if existing is None:
            by_id[f["id"]] = f
            continue
        merged_applies_to = sorted({*existing.get("applies_to", []), *f.get("applies_to", [])})
        existing["applies_to"] = merged_applies_to
        # Multi-domain entry: strip the "<Domain> → " prefix so the
        # bare name reads correctly regardless of editing context.
        name = existing.get("name") or ""
        if len(merged_applies_to) > 1 and _AUTOMATION_LABEL_SEPARATOR in name:
            existing["name"] = name.split(_AUTOMATION_LABEL_SEPARATOR, 1)[1]
    return list(by_id.values())


def _convert_light_effect(
    *,
    name: str,
    body: dict,
    schema_dir: Path,
) -> dict | None:
    """Build one ``LightEffect`` dict from a light.effects registry entry."""
    return _convert_registry_entry(
        name=name,
        body=body,
        label_domain="light",
        applies_to=resolve_light_effects_applies_to(name, schema_dir),
        schema_dir=schema_dir,
    )


def _extract_triggers_from_section(
    *,
    top_key: str,
    domain: str,
    section: dict,
    schema_dir: Path,
) -> list[dict]:
    """
    Scan a section's schemas for keys whose ``type == "trigger"``.

    Returns one entry per (schema_file, trigger_key) pair, with the
    trigger's own ``config_vars`` (e.g. ``on_click.min_length``)
    surfaced as :class:`ConfigEntry`-shaped params.
    """
    schemas = section.get("schemas") or {}
    out: list[dict] = []
    is_device_level = top_key == "esphome"
    # ``applies_to`` uses the canonical ``<base>.<stem>`` form so
    # it matches the YAML scoping set assembled in the controller
    # (``binary_sensor`` for bare triggers; ``cover.template`` for
    # template-cover-only triggers).
    applies_to = [] if is_device_level or domain == "core" else [domain]
    for schema_body in schemas.values():
        if not isinstance(schema_body, dict):
            continue
        inner = schema_body.get("schema") if isinstance(schema_body.get("schema"), dict) else None
        cvs = (inner or {}).get("config_vars") or {}
        for key, raw in cvs.items():
            if not isinstance(raw, dict) or raw.get("type") != "trigger":
                continue
            docs = clean_docs(raw.get("docs"))
            param_entries, _accepts, _cond_gate = _extract_automation_param_schema(
                raw.get("schema") if isinstance(raw.get("schema"), dict) else None,
                schema_dir,
            )
            # A trigger's own params (on_time's cron fields,
            # on_value_range's bounds) are its primary config, not
            # advanced knobs the name-based heuristic would hide; surface
            # them on the main form like multi-sensor sub-readings (#983).
            for entry in param_entries:
                entry["advanced"] = False
            trigger_id = key if is_device_level else f"{top_key}.{key}"
            out.append(
                {
                    "id": trigger_id,
                    "name": _automation_label(domain, key, docs.name),
                    "description": docs.text,
                    "docs_url": docs.url or _CORE_AUTOMATION_DOCS,
                    "applies_to": applies_to,
                    "is_device_level": is_device_level,
                    # Per-entry params mark a list-shaped trigger; only
                    # component ones are wizard-stackable by index.
                    "repeatable": bool(param_entries) and not is_device_level,
                    "config_entries": [_strip_entry_defaults(e) for e in param_entries],
                }
            )
    return out


def _extract_automation_param_schema(
    schema: dict | None,
    schema_dir: Path,
) -> tuple[list[dict], list[str], bool]:
    """
    Convert a parameter ``schema`` to ``(config_entries, accepts_action_list, has_condition_gate)``.

    ``accepts_action_list`` and ``has_condition_gate`` are stripped
    from ``config_entries`` so the editor renders them as recursive
    sub-trees instead of plain form fields.
    """
    if not schema:
        return [], [], False
    raw_entries = _convert_config_vars(schema, schema_dir)
    accepts_action_list: list[str] = []
    has_condition_gate = False
    out: list[dict] = []
    # ``_convert_config_vars`` doesn't see registry-typed fields the
    # way it sees normal config_vars; specifically, ``then: { type:
    # trigger }`` and ``then: { type: registry, registry: action }``
    # get coerced into something we'd render. Walk the raw schema
    # directly to detect those, and strip them from the entry list.
    raw_cvs = schema.get("config_vars") or {}
    for key, raw in raw_cvs.items():
        if not isinstance(raw, dict):
            continue
        rtype = raw.get("type")
        registry = raw.get("registry")
        if rtype == "trigger" or (rtype == "registry" and registry == "action"):
            if key in _ACTION_LIST_KEYS:
                accepts_action_list.append(key)
            # Drop the key from the converted entry list — the
            # frontend renders it as a recursive action list, not
            # as a form field.
            raw_entries = [e for e in raw_entries if e.get("key") != key]
            continue
        if key in _CONDITION_GATE_KEYS:
            # ``condition`` / ``all`` / ``any`` on a control-flow
            # action are always boolean gates, never user-typed
            # values. The schema sometimes tags them with
            # ``type: registry, registry: condition`` and sometimes
            # carries only a ``docs`` string (e.g. ``if.any``); the
            # by-name strip catches both shapes uniformly.
            has_condition_gate = True
            raw_entries = [e for e in raw_entries if e.get("key") != key]
            continue
    # The walker uses ``_AUTOMATION_KEY_PREFIXES`` to skip ``on_``
    # config_vars, which is correct for the *component form* path but
    # wrong here — a triggered automation has no ``on_`` keys as
    # parameters anyway, so it's a no-op for us.
    out.extend(raw_entries)
    return out, accepts_action_list, has_condition_gate


def _automation_label(domain: str, name: str, docs_name: str | None) -> str:
    """
    Pretty-print an automation registry entry's human-facing name.

    Precedence: core label table → ``"Domain → Name"`` for
    component-scoped entries → titlecased *name* for device-level
    and core. The ``docs_name`` ("See also" link target) is ignored
    because it's the docs page title, not the entry name.
    """
    del docs_name
    if domain == "core" and name in _CORE_AUTOMATION_LABELS:
        return _CORE_AUTOMATION_LABELS[name]
    pretty_name = name.replace("_", " ").replace(".", " ").title()
    # Device-level triggers (esphome.on_boot / on_loop / on_shutdown)
    # render bare — they're not scoped to a configured component.
    if domain in ("core", "esphome") or not domain:
        return pretty_name
    domain_label = domain.replace("_", " ").title()
    return f"{domain_label}{_AUTOMATION_LABEL_SEPARATOR}{pretty_name}"


def _dedupe_by_id(entries: list[dict]) -> list[dict]:
    """
    Drop duplicate ids; keep the first occurrence; sort by id.

    The same id can land twice when a registry entry surfaces
    through both an ``action`` map and a shared ``schemas`` block.
    Sorting keeps the on-disk JSON diff stable across syncs.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for entry in entries:
        eid = entry.get("id")
        if eid in seen:
            continue
        seen.add(eid)
        out.append(entry)
    return sorted(out, key=lambda e: e["id"])


if __name__ == "__main__":
    sys.exit(main())
