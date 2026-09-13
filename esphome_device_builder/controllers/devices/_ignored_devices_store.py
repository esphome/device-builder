"""Codec and store for the dashboard's ignored-devices list."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.json import dumps_indent, loads_mapping_or_warn
from ...helpers.storage import Store

if TYPE_CHECKING:
    from pathlib import Path

    from ...helpers.storage import ShutdownRegister

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY = 1.0


def ignored_devices_store(path: Path, shutdown_register: ShutdownRegister) -> Store[set[str]]:
    """Build the store over *path* in the legacy dashboard's file shape, world-readable."""
    return Store(
        path,
        encoder=_encode,
        decoder=_decode,
        shutdown_register=shutdown_register,
        name="ignored_devices",
        mode=0o644,
    )


def _encode(names: set[str]) -> bytes:
    return dumps_indent({"ignored_devices": sorted(names)})


def _decode(raw: bytes) -> set[str]:
    obj = loads_mapping_or_warn(raw, label="ignored devices store")
    names = [] if obj is None else obj.get("ignored_devices", [])
    if not isinstance(names, list):
        _LOGGER.warning("ignored devices store: non-list ignored_devices field, starting empty")
        return set()
    return {name for name in names if isinstance(name, str)}
