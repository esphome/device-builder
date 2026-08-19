"""
Pure-function helpers for generating, parsing, and reading device YAML.

These utilities are intentionally state-free so they can be reused by
the devices controller, the device builder, and any future tool that
needs to inspect or synthesise an ESPHome config without instantiating
a controller.

Split across three concern modules — ``_generation`` (synthesise new
YAML), ``_parsing`` (inspect raw / resolved config), and ``_loading``
(build :class:`Device` models from disk) — re-exported here so existing
``helpers.device_yaml`` imports keep working.
"""

from __future__ import annotations

from esphome.storage_json import StorageJSON

from ._generation import (
    CAPTIVE_PORTAL_PLATFORMS,
    NETWORK_PROVIDER_COMPONENT_IDS,
    WIFI_RADIO_PROVIDER_COMPONENT_IDS,
    _has_native_wifi,
    _infer_native_wifi,
    board_provides_network,
    board_requires_wifi,
    generate_adoption_yaml,
    generate_device_yaml,
    generate_minimal_stub_yaml,
)
from ._loading import (
    compute_has_pending_changes,
    dotted_loaded_platforms,
    load_device_from_storage,
    load_device_yaml,
    pending_changes_via_hash,
)
from ._mqtt_block import (
    SecretRef,
    build_mqtt_extract,
    extract_mqtt_block,
)
from ._parsing import (
    _UNRESOLVED_SUBSTITUTION_RE,
    DEFAULT_API_PORT,
    EsphomeMeta,
    _extract_resolved_substitutions,
    _parse_inline_value,
    _resolve_substitutions,
    config_has_top_level_block,
    configuration_filename,
    configuration_stem,
    detect_platform_from_yaml,
    device_ap_label,
    device_uses_mqtt,
    extract_component_source_fingerprint,
    extract_directly_referenced_integrations,
    extract_esphome_meta_from_config,
    get_api_encryption_block,
    get_api_encryption_key,
    get_api_port,
    get_resolved_api_encryption_key,
    has_top_level_block,
    parse_esphome_meta,
    parse_platform_from_yaml,
    resolved_device_name,
    retarget_fallback_ap_ssid,
    safe_stat_key,
    yaml_has_api_encryption,
    yaml_has_top_level_block,
)
from ._resolve import EsphomeConfigUnavailableError, run_esphome_config

__all__ = [
    "CAPTIVE_PORTAL_PLATFORMS",
    "DEFAULT_API_PORT",
    "NETWORK_PROVIDER_COMPONENT_IDS",
    "WIFI_RADIO_PROVIDER_COMPONENT_IDS",
    "_UNRESOLVED_SUBSTITUTION_RE",
    "EsphomeConfigUnavailableError",
    "EsphomeMeta",
    "SecretRef",
    "StorageJSON",
    "_extract_resolved_substitutions",
    "_has_native_wifi",
    "_infer_native_wifi",
    "_parse_inline_value",
    "_resolve_substitutions",
    "board_provides_network",
    "board_requires_wifi",
    "build_mqtt_extract",
    "compute_has_pending_changes",
    "config_has_top_level_block",
    "configuration_filename",
    "configuration_stem",
    "detect_platform_from_yaml",
    "device_ap_label",
    "device_uses_mqtt",
    "dotted_loaded_platforms",
    "extract_component_source_fingerprint",
    "extract_directly_referenced_integrations",
    "extract_esphome_meta_from_config",
    "extract_mqtt_block",
    "generate_adoption_yaml",
    "generate_device_yaml",
    "generate_minimal_stub_yaml",
    "get_api_encryption_block",
    "get_api_encryption_key",
    "get_api_port",
    "get_resolved_api_encryption_key",
    "has_top_level_block",
    "load_device_from_storage",
    "load_device_yaml",
    "parse_esphome_meta",
    "parse_platform_from_yaml",
    "pending_changes_via_hash",
    "resolved_device_name",
    "retarget_fallback_ap_ssid",
    "run_esphome_config",
    "safe_stat_key",
    "yaml_has_api_encryption",
    "yaml_has_top_level_block",
]
