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
    _has_native_wifi,
    _infer_native_wifi,
    generate_device_yaml,
    generate_minimal_stub_yaml,
)
from ._loading import (
    compute_has_pending_changes,
    load_device_from_storage,
    load_device_yaml,
)
from ._parsing import (
    DEFAULT_API_PORT,
    EsphomeMeta,
    _parse_inline_value,
    config_has_top_level_block,
    configuration_stem,
    detect_platform_from_yaml,
    device_uses_mqtt,
    extract_directly_referenced_integrations,
    extract_esphome_meta_from_config,
    get_api_encryption_block,
    get_api_encryption_key,
    get_api_port,
    parse_esphome_meta,
    parse_platform_from_yaml,
    yaml_has_api_encryption,
    yaml_has_top_level_block,
)

__all__ = [
    "DEFAULT_API_PORT",
    "NETWORK_PROVIDER_COMPONENT_IDS",
    "EsphomeMeta",
    "StorageJSON",
    "_has_native_wifi",
    "_infer_native_wifi",
    "_parse_inline_value",
    "compute_has_pending_changes",
    "config_has_top_level_block",
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
    "load_device_from_storage",
    "load_device_yaml",
    "parse_esphome_meta",
    "parse_platform_from_yaml",
    "yaml_has_api_encryption",
    "yaml_has_top_level_block",
]
