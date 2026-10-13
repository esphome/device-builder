"""Dict-level check for a leftover ``ota: encryption: old_key`` the line walker may not see."""

from __future__ import annotations

from esphome.const import CONF_ENCRYPTION

from ._parsing import _ota_esphome_entries
from ._tolerant_yaml import load_yaml_tolerant

_CONF_OLD_KEY = "old_key"


def yaml_declares_ota_old_key(yaml_content: str) -> bool | None:
    """Whether the esphome OTA item declares ``encryption: old_key``; ``None`` when unparsable."""
    config = load_yaml_tolerant(yaml_content)
    if config is None:
        return None
    return any(
        isinstance(encryption := entry.get(CONF_ENCRYPTION), dict) and _CONF_OLD_KEY in encryption
        for entry in _ota_esphome_entries(config)
    )
