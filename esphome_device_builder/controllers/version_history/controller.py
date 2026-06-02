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

from ...models import EventType
from .git_repo import GitRepo

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event
    from ...models import DeviceEventData

_LOGGER = logging.getLogger(__name__)

# How long to coalesce scanner-detected disk changes before committing.
# Dashboard mutations commit immediately with a rich message, so this
# window only ends up committing genuinely-external edits (VS Code, the
# HA File Editor) — a dashboard save makes the debounced commit a no-op.
_DEBOUNCE_SECONDS = 2.0

# Catch-all commit message per scanner change kind (external edits).
_EXTERNAL_MESSAGE: dict[EventType, str] = {
    EventType.DEVICE_ADDED: "Add {configuration}",
    EventType.DEVICE_UPDATED: "Edit {configuration}",
    EventType.DEVICE_REMOVED: "Delete {configuration}",
}


class VersionHistoryController:
    """Auto-commit YAML edits and serve their history to the dashboard."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._repo = GitRepo(config_dir=device_builder.settings.config_dir)
        self._lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        # configuration → pending commit message; last write wins.
        self._pending: dict[str, str] = {}
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        """Whether git-backed history is active for this config dir."""
        return self._repo.enabled

    async def start(self) -> None:
        """Probe for git, adopt / init the repo, and watch for disk changes."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._repo.discover_or_init)
        if not self._repo.enabled:
            return
        _LOGGER.info("Version history active (git work tree: %s)", self._repo.toplevel)
        # Catch-all for edits made outside the dashboard: the scanner
        # fires these only on an actual disk cache-key change (mtime /
        # size / inode), so runtime mDNS / ping state ticks don't reach
        # us. Dashboard mutations have already committed by the time the
        # debounced flush runs, so those become no-ops here.
        for event_type in _EXTERNAL_MESSAGE:
            self._unsubs.append(self._db.bus.add_listener(event_type, self._on_disk_change))

    async def stop(self) -> None:
        """Detach listeners and cancel any pending debounced flush."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            self._flush_task = None

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

    # ------------------------------------------------------------------
    # scanner-driven catch-all for external edits
    # ------------------------------------------------------------------

    def _on_disk_change(self, event: Event[DeviceEventData]) -> None:
        """Queue a debounced commit for a scanner-detected disk change."""
        configuration = event.data["device"].configuration
        self._pending[configuration] = _EXTERNAL_MESSAGE[event.event_type].format(
            configuration=configuration
        )
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        """Wait out the debounce window, then commit each pending config once."""
        await asyncio.sleep(_DEBOUNCE_SECONDS)
        pending = self._pending
        self._pending = {}
        for configuration, message in pending.items():
            try:
                await self.record_configuration(configuration, message)
            except Exception:  # noqa: BLE001 — one bad path can't kill the watcher
                _LOGGER.debug("Version-history catch-all failed for %s", configuration)
