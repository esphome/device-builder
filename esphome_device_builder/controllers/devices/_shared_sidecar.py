"""Async wrapper for the shared sidecar's transactional helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..config import (
    clear_volatile_device_metadata,
    get_device_metadata,
    remove_device_metadata,
    rename_device_metadata,
    set_device_metadata,
)


class SharedSidecarClient:
    """Thin async wrapper over ``config_dir/.device-builder.json`` access."""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir

    def get_sync(self, filename: str) -> dict[str, Any]:
        """Read *filename*'s entry; safe from any thread."""
        return get_device_metadata(self._config_dir, filename)

    async def get(self, filename: str) -> dict[str, Any]:
        """Read *filename*'s entry off-loop."""
        return await asyncio.to_thread(get_device_metadata, self._config_dir, filename)

    def update_sync(self, filename: str, **fields: Any) -> None:
        """Apply *fields* synchronously; safe from executor threads, not the loop."""
        set_device_metadata(self._config_dir, filename, **fields)

    async def update(self, filename: str, **fields: Any) -> None:
        """Apply *fields* to *filename* via the transactional setter."""
        await asyncio.to_thread(set_device_metadata, self._config_dir, filename, **fields)

    async def remove(self, filename: str) -> None:
        """Drop *filename*'s entry entirely."""
        await asyncio.to_thread(remove_device_metadata, self._config_dir, filename)

    async def rename(self, old_filename: str, new_filename: str) -> None:
        """Move *old_filename*'s entry to *new_filename* (transactional)."""
        await asyncio.to_thread(
            rename_device_metadata, self._config_dir, old_filename, new_filename
        )

    async def clear_volatile(self, filename: str) -> None:
        """Clear archive-volatile fields (currently ``mac_address``)."""
        await asyncio.to_thread(clear_volatile_device_metadata, self._config_dir, filename)
