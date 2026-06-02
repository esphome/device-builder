"""
Version-history controller — git-backed history of the config dir.

Owns a :class:`GitRepo` over ``settings.config_dir`` and exposes an
async, lock-serialised commit API plus the read/restore WS commands.
The git index is not concurrency-safe, so every mutating op runs in an
executor behind :attr:`_lock`; the whole feature is best-effort and
self-disabling (no git binary, or repo setup failed → every method a
quiet no-op), so a git hiccup never breaks a user's save.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .git_repo import GitRepo

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


class VersionHistoryController:
    """Auto-commit YAML edits and serve their history to the dashboard."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._repo = GitRepo(config_dir=device_builder.settings.config_dir)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Whether git-backed history is active for this config dir."""
        return self._repo.enabled

    async def start(self) -> None:
        """Probe for git and adopt / initialise the config-dir repo."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._repo.discover_or_init)
        if self._repo.enabled:
            _LOGGER.info("Version history active (git work tree: %s)", self._repo.toplevel)

    async def stop(self) -> None:
        """No persistent resources to release (commits are synchronous)."""

    async def commit(self, paths: list[Path], message: str) -> str | None:
        """Commit *paths* under *message*; best-effort, never raises.

        Returns the new commit sha, or ``None`` when nothing changed
        for those paths or the feature is disabled. Safe to ``await``
        from any mutation site — a git failure is swallowed and logged.
        """
        if not self._repo.enabled or not paths:
            return None
        loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                return await loop.run_in_executor(None, self._repo.commit_paths, paths, message)
            except Exception:
                _LOGGER.exception("Version-history commit failed for %s", message)
                return None

    async def record_configuration(self, configuration: str, message: str) -> str | None:
        """Commit a single config YAML by its dashboard *configuration* name."""
        path = self._db.settings.rel_path(configuration)
        return await self.commit([path], message)
