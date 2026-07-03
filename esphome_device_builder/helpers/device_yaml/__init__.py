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
    NETWORK_PROVIDER_COMPONENT_IDS,
    WIFI_RADIO_PROVIDER_COMPONENT_IDS,
    _has_native_wifi,
    _infer_native_wifi,
    board_provides_network,
    board_requires_wifi,
    generate_device_yaml,
    generate_minimal_stub_yaml,
)
from ._loading import (
    compute_has_pending_changes,
    load_device_from_storage,
    load_device_yaml,
    pending_changes_via_hash,
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
    device_uses_mqtt,
    extract_directly_referenced_integrations,
    extract_esphome_meta_from_config,
    get_api_encryption_block,
    get_api_encryption_key,
    get_api_port,
    get_resolved_api_encryption_key,
    parse_esphome_meta,
    parse_platform_from_yaml,
    resolved_device_name,
    yaml_has_api_encryption,
    yaml_has_top_level_block,
)
from ._resolve import EsphomeConfigUnavailableError, run_esphome_config

__all__ = [
    "DEFAULT_API_PORT",
    "NETWORK_PROVIDER_COMPONENT_IDS",
    "WIFI_RADIO_PROVIDER_COMPONENT_IDS",
    "_UNRESOLVED_SUBSTITUTION_RE",
    "EsphomeConfigUnavailableError",
    "EsphomeMeta",
    "StorageJSON",
    "_extract_resolved_substitutions",
    "_has_native_wifi",
    "_infer_native_wifi",
    "_parse_inline_value",
    "_resolve_substitutions",
    "board_provides_network",
    "board_requires_wifi",
    "compute_has_pending_changes",
    "config_has_top_level_block",
    "configuration_filename",
    "configuration_stem",
    "detect_platform_from_yaml",
    "device_uses_mqtt",
    "extract_directly_referenced_integrations",
    "extract_esphome_meta_from_config",
    "generate_device_yaml",
    "generate_minimal_stub_yaml",
    "get_api_encryption_block",
    "get_api_encryption_key",
    "get_api_port",
    "get_resolved_api_encryption_key",
    "load_device_from_storage",
    "load_device_yaml",
    "parse_esphome_meta",
    "parse_platform_from_yaml",
    "pending_changes_via_hash",
    "resolved_device_name",
    "run_esphome_config",
    "yaml_has_api_encryption",
    "yaml_has_top_level_block",
]
