"""Dict-level check for a leftover ``ota: encryption: old_key`` the line walker may not see."""

from __future__ import annotations

from esphome.const import CONF_ENCRYPTION

from ._mqtt_block import load_yaml_tolerant
from ._parsing import _ota_esphome_entries

_CONF_OLD_KEY = "old_key"


def yaml_declares_ota_old_key(yaml_content: str) -> bool:
    """Whether the parsed esphome OTA item carries an ``encryption: old_key`` mapping key."""
    config = load_yaml_tolerant(yaml_content)
    return any(
        isinstance(encryption := entry.get(CONF_ENCRYPTION), dict) and _CONF_OLD_KEY in encryption
        for entry in _ota_esphome_entries(config)
    )
