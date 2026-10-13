"""Dict-level check for a leftover ``ota: encryption: old_key`` the line walker may not see."""

from __future__ import annotations

from collections.abc import Iterator

from esphome.const import CONF_ENCRYPTION, CONF_PACKAGES

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
        for entry in _ota_entries_with_inline_packages(config)
    )


def _ota_entries_with_inline_packages(config: dict | None) -> Iterator[dict]:
    """Esphome OTA entries of *config* and of every inline ``packages:`` entry, recursively."""
    yield from _ota_esphome_entries(config)
    packages = config.get(CONF_PACKAGES) if isinstance(config, dict) else None
    if isinstance(packages, dict):
        packages = list(packages.values())
    for package in packages if isinstance(packages, list) else ():
        if isinstance(package, dict):
            yield from _ota_entries_with_inline_packages(package)
