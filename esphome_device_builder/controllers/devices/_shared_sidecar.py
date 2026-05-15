"""Async wrapper for the shared sidecar's transactional helpers.

Encapsulates ``config_dir`` so callers don't pass it. Async
methods push the blocking RMW + flock to a thread; ``get_sync``
is the executor-thread shorthand for the same read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..config import (
    clear_volatile_device_metadata,
    get_device_metadata,
    remove_device_metadata,
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
        """Apply *fields* to *filename* synchronously.

        Safe from executor threads (the underlying transactional
        helper takes its own ``threading.Lock`` + ``fcntl.flock``).
        Event-loop callers use :meth:`update` instead.
        """
        set_device_metadata(self._config_dir, filename, **fields)

    async def update(self, filename: str, **fields: Any) -> None:
        """Apply *fields* to *filename* via the transactional setter."""
        await asyncio.to_thread(set_device_metadata, self._config_dir, filename, **fields)

    async def remove(self, filename: str) -> None:
        """Drop *filename*'s entry entirely."""
        await asyncio.to_thread(remove_device_metadata, self._config_dir, filename)

    async def clear_volatile(self, filename: str) -> None:
        """Clear archive-volatile fields (currently ``mac_address``)."""
        await asyncio.to_thread(clear_volatile_device_metadata, self._config_dir, filename)
