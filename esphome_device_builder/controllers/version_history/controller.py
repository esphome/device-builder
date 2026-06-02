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
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError, api_command
from ...models import ErrorCode, EventType
from .git_repo import GitRepo

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event
    from ...models import DeviceEventData

# A commit id from list_versions — full or abbreviated hex. ``sha`` is
# validated against this before reaching git so it can't smuggle extra
# argv into the read commands. The other untrusted input,
# ``configuration``, is guarded separately by ``settings.rel_path`` (it
# raises ``INVALID_ARGS`` for ``..`` / absolute paths that escape the
# config dir, so a client can't read tracked files elsewhere in an
# adopted work tree) and only ever reaches git as a pathspec after ``--``.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

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
        """Detach listeners, cancel the debounce timer, and flush what's queued.

        Draining ``_pending`` on the way out means an external edit that
        landed inside the debounce window isn't silently dropped on
        shutdown (there's no startup re-snapshot for an already-tracked
        file, so it would otherwise be a permanent history gap).
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._flush_pending()

    async def commit(self, paths: list[Path], message: str) -> str | None:
        """Commit *paths* under *message*; best-effort, never raises.

        Returns the new commit sha, or ``None`` when nothing changed
        for those paths or the feature is disabled. Safe to ``await``
        from any mutation site — a git failure is swallowed and logged.
        """
        if not self._repo.enabled or not paths:
            return None
        async with self._lock:
            try:
                return await self._in_executor(self._repo.commit_paths, paths, message)
            except Exception:
                _LOGGER.exception("Version-history commit failed for %s", message)
                return None

    async def record_configuration(self, configuration: str, message: str) -> str | None:
        """Commit a single config YAML by its dashboard *configuration* name."""
        path = self._db.settings.rel_path(configuration)
        return await self.commit([path], message)

    # ------------------------------------------------------------------
    # WS commands
    # ------------------------------------------------------------------

    @api_command("version_history/list_versions")
    async def list_versions(self, *, configuration: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Return the commit history for *configuration*, newest first."""
        if not self._repo.enabled:
            return []
        path = self._db.settings.rel_path(configuration)
        commits = await self._in_executor(self._repo.log_file, path)
        return [
            {
                "sha": c.sha,
                "short_sha": c.short_sha,
                "author": c.author,
                "timestamp": c.timestamp,
                "message": c.message,
            }
            for c in commits
        ]

    @api_command("version_history/get_version")
    async def get_version(self, *, configuration: str, sha: str, **kwargs: Any) -> dict[str, Any]:
        """Return *configuration*'s content at commit *sha*."""
        self._require_enabled()
        self._validate_sha(sha)
        path = self._db.settings.rel_path(configuration)
        content = await self._in_executor(self._repo.file_at, path, sha)
        if content is None:
            raise CommandError(ErrorCode.NOT_FOUND, f"{configuration} not found at {sha}")
        return {"configuration": configuration, "sha": sha, "content": content}

    @api_command("version_history/get_diff")
    async def get_diff(self, *, configuration: str, sha: str, **kwargs: Any) -> dict[str, Any]:
        """Return a unified diff of *configuration* between *sha* and the working copy."""
        self._require_enabled()
        self._validate_sha(sha)
        path = self._db.settings.rel_path(configuration)
        diff = await self._in_executor(self._repo.diff_file, path, sha)
        return {"configuration": configuration, "sha": sha, "diff": diff}

    @api_command("version_history/list_deleted")
    async def list_deleted(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return configs that have history but no working-tree copy (restorable)."""
        if not self._repo.enabled:
            return []
        deleted = await self._in_executor(self._repo.deleted_files)
        return [{"configuration": name} for name in deleted]

    @api_command("version_history/restore")
    async def restore(
        self, *, configuration: str, sha: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Restore *configuration* to commit *sha* (or its latest version if omitted).

        Recreates a deleted file as well as reverting an edit; the
        write goes through the normal persist path so the device row
        updates via events and the restore itself is committed.
        """
        self._require_enabled()
        path = self._db.settings.rel_path(configuration)
        # Commit any queued external edit first, so restoring over it
        # still leaves that just-overwritten version recoverable.
        await self._flush_pending()
        if sha is not None:
            self._validate_sha(sha)
            content = await self._in_executor(self._repo.file_at, path, sha)
            if content is None:
                raise CommandError(ErrorCode.NOT_FOUND, f"{configuration} not found at {sha}")
            restored_from = sha
        else:
            result = await self._in_executor(self._repo.latest_content, path)
            if result is None:
                raise CommandError(ErrorCode.NOT_FOUND, f"no history for {configuration}")
            restored_from, content = result
        devices = self._db.devices
        if devices is None:  # pragma: no cover — devices is always up post-start
            raise CommandError(ErrorCode.INTERNAL_ERROR, "devices controller unavailable")
        await devices.apply_restored_yaml(configuration, content, restored_from=restored_from[:7])
        return {"configuration": configuration, "restored_from": restored_from, "content": content}

    async def _in_executor[T](self, fn: Callable[..., T], *args: Any) -> T:
        """Run a synchronous GitRepo call off the event loop."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn, *args)

    def _require_enabled(self) -> None:
        """Raise if version history isn't available for this config dir."""
        if not self._repo.enabled:
            raise CommandError(
                ErrorCode.NOT_FOUND,
                "version history is not available for this config directory",
            )

    def _validate_sha(self, sha: Any) -> None:
        """Reject anything that isn't a plain hex commit id."""
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise CommandError(ErrorCode.INVALID_ARGS, f"invalid commit id: {sha!r}")

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
            task = asyncio.create_task(self._flush_after_delay())
            # The catch-all is the only recorder for external edits; a
            # done-callback surfaces any failure that escapes the
            # per-config guard so the watcher can't die silently.
            task.add_done_callback(self._on_flush_done)
            self._flush_task = task

    @staticmethod
    def _on_flush_done(task: asyncio.Task[None]) -> None:
        """Log an unexpected flush-task failure (cancellation is normal)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOGGER.warning("Version-history flush task failed unexpectedly", exc_info=exc)

    async def _flush_after_delay(self) -> None:
        """Wait out the debounce window, then flush the queued configs."""
        await asyncio.sleep(_DEBOUNCE_SECONDS)
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        """Commit every queued config; drain in a loop so nothing is stranded.

        An external edit that lands while we're committing (the per-config
        commit awaits) is picked up on the next pass instead of waiting for
        the next scanner event. The final empty-check and the coroutine
        return happen without an await between them, so no event can slip
        in and be stranded — ``_on_disk_change`` then sees the task done
        and schedules a fresh flush.
        """
        while self._pending:
            pending = self._pending
            self._pending = {}
            for configuration, message in pending.items():
                try:
                    await self.record_configuration(configuration, message)
                except Exception:
                    # A git failure inside commit() is already logged there
                    # and returns None; this guard isolates the rarer case of
                    # a bad configuration (rel_path raising) so one bad entry
                    # can't strand the rest of the batch.
                    _LOGGER.warning(
                        "Version-history catch-all failed for %s", configuration, exc_info=True
                    )
