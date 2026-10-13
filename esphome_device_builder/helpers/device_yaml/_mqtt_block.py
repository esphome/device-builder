"""Tolerant ``mqtt:`` block extraction shared by the scanner and the MQTT coordinator."""

from __future__ import annotations

import os
from typing import Any

from ...models.devices import DeviceMqttExtract
from ._parsing import _extract_resolved_substitutions
from ._tolerant_yaml import load_yaml_tolerant


def extract_mqtt_block(yaml_content: str) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """
    Tolerant-parse *yaml_content*; return its ``mqtt:`` dict and substitutions.

    ``!secret`` values stay as ``SecretRef`` markers; other custom
    tags load as ``None``. Returns ``(None, {})`` when the YAML fails to
    parse or has no ``mqtt:`` mapping.
    """
    data = load_yaml_tolerant(yaml_content)
    if not isinstance(data, dict):
        return None, {}
    mqtt = data.get("mqtt")
    if not isinstance(mqtt, dict):
        return None, {}
    return mqtt, _extract_resolved_substitutions(data)


def build_mqtt_extract(
    yaml_content: str,
    resolved_config: dict[str, Any] | None,
    yaml_stat: os.stat_result,
    secrets_key: tuple[int, int],
    resolved_substitutions: dict[str, str],
    *,
    shallow: bool = False,
) -> DeviceMqttExtract:
    """Build the scan-time extraction the MQTT coordinator consumes."""
    main_block, main_subs = extract_mqtt_block(yaml_content)
    resolved_block = resolved_config.get("mqtt") if isinstance(resolved_config, dict) else None
    if not isinstance(resolved_block, dict):
        resolved_block = None
    return DeviceMqttExtract(
        yaml_mtime_ns=yaml_stat.st_mtime_ns,
        yaml_size=yaml_stat.st_size,
        secrets_mtime_ns=secrets_key[0],
        secrets_size=secrets_key[1],
        main_block=main_block,
        main_substitutions=main_subs,
        resolved_block=resolved_block,
        resolved_substitutions=resolved_substitutions,
        from_shallow_load=shallow,
    )
