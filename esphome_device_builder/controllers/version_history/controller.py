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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError, api_command
from ...helpers.async_ import drain_tasks, run_in_executor
from ...models import ErrorCode, EventType
from .git_repo import GIT_COMMIT_ERRORS, GitIndexLockBusyError, GitRepo

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

# Consecutive commit failures before the feature is flagged ``degraded``
# — enough to tell a persistent breakage (corrupt repo, disk full) from a
# one-off hiccup, which a future History pane can surface to the user.
_DEGRADED_THRESHOLD = 3

# Bounded backoff for a fresh index.lock (a live concurrent writer); the
# wait is awaited on the event loop, never inside the executor thread.
_LOCK_RETRY_ATTEMPTS = 4
_LOCK_RETRY_BACKOFF = 0.2  # seconds; doubled after each attempt

# Catch-all commit message per scanner change kind (external edits).
_EXTERNAL_MESSAGE: dict[EventType, str] = {
    EventType.DEVICE_ADDED: "Add {configuration}",
    EventType.DEVICE_YAML_UPDATED: "Edit {configuration}",
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
        # Consecutive-failure tracking for the degraded signal.
        self._consecutive_failures = 0
        self._degraded = False
        # ``version_history_enabled`` mirror; gates writes only (reads/restore
        # key on the repo being present). Seeded at start, live via set_auto_commit.
        self._auto_commit_enabled = True

    @property
    def enabled(self) -> bool:
        """Whether git-backed history is active for this config dir."""
        return self._repo.enabled

    @property
    def degraded(self) -> bool:
        """True when commits have failed repeatedly — history may be incomplete.

        Distinguishes a persistent breakage from a one-off hiccup so a
        future History pane can surface "version history is failing"
        rather than silently presenting stale versions.
        """
        return self._degraded

    async def start(self) -> None:
        """
        Probe for git and watch for disk changes, honouring the preference.

        Opted in: adopt or init the repo and subscribe. Opted out: discover an
        existing repo read-only so its history stays readable, but create
        nothing and never commit.
        """
        assert self._db.config is not None  # type narrowing — loaded before start()
        self._auto_commit_enabled = self._db.config.prefs.snapshot().version_history_enabled
        if not self._auto_commit_enabled:
            await run_in_executor(self._repo.discover_existing)
            if self._repo.enabled:
                _LOGGER.info(
                    "Version history read-only (auto-commit off; git work tree: %s)",
                    self._repo.toplevel,
                )
            else:
                _LOGGER.info("Version history disabled by preference")
            return
        await self._activate()

    async def _activate(self) -> None:
        """
        Adopt or init the repo and (re)subscribe to external-edit events.

        Always runs ``discover_or_init`` so enabling upgrades a read-only
        discover (from an opted-out start) into a writable adopt; idempotent
        across a re-enable, which finds the repo already set up.
        """
        await run_in_executor(self._repo.discover_or_init)
        if not self._repo.enabled:
            return
        _LOGGER.info("Version history active (git work tree: %s)", self._repo.toplevel)
        # Reached with no listeners attached (start runs once; a re-enable
        # clears them first), so this never double-subscribes.
        # Catch-all for edits made outside the dashboard. DEVICE_YAML_UPDATED
        # fires only when the scanner detects a YAML change on disk (mtime/
        # size/inode), so runtime mDNS / ping ticks and metadata reloads
        # (which ride DEVICE_UPDATED) never reach us. Dashboard mutations have
        # already committed by the debounced flush, so those become no-ops.
        for event_type in _EXTERNAL_MESSAGE:
            self._unsubs.append(self._db.bus.add_listener(event_type, self._on_disk_change))

    async def set_auto_commit(self, *, enabled: bool) -> None:
        """
        Apply a live ``version_history_enabled`` change.

        Off→on activates the repo lazily; on→off stops watching and drops
        any queued commit, leaving an existing repo intact for reads. The
        mirror is restored if activation / teardown raises, so it can't drift
        from the persisted preference (which its caller rolls back in turn).
        """
        if enabled == self._auto_commit_enabled:
            return
        self._auto_commit_enabled = enabled
        try:
            if enabled:
                await self._activate()
            else:
                self._pending.clear()
                await self._teardown()
        except Exception:
            self._auto_commit_enabled = not enabled
            raise

    async def stop(self) -> None:
        """
        Detach listeners, cancel the debounce timer, and flush what's queued.

        The final flush commits an edit caught in the debounce window
        rather than dropping it on shutdown.
        """
        await self._teardown()
        await self._flush_pending()

    async def _teardown(self) -> None:
        """Detach the external-edit listeners and drain the debounced flush task.

        Shared by ``stop`` and the disable path of ``set_auto_commit``;
        the queue is the only thing they treat differently (flush vs drop),
        so it stays with the callers.
        """
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        task = self._flush_task
        self._flush_task = None
        if task is not None:
            await drain_tasks((task,), log_exceptions=True)

    async def commit(self, paths: list[Path], message: str) -> str | None:
        """
        Commit *paths* under *message*; return the sha, ``None`` for a no-op.

        ``None`` means nothing changed (or disabled); a genuine git
        failure **raises** so callers can tell the two apart.
        """
        if not self._repo.enabled or not self._auto_commit_enabled or not paths:
            return None
        async with self._lock:
            backoff = _LOCK_RETRY_BACKOFF
            attempts = 0
            while True:
                try:
                    sha = await run_in_executor(self._repo.commit_paths, paths, message)
                except GitIndexLockBusyError:
                    attempts += 1
                    if attempts > _LOCK_RETRY_ATTEMPTS:
                        self._note_commit_failure()
                        raise
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                except GIT_COMMIT_ERRORS:
                    self._note_commit_failure()
                    raise
                self._note_commit_success()
                return sha

    def _note_commit_failure(self) -> None:
        """Count a git failure; flag degraded once they stop looking one-off."""
        self._consecutive_failures += 1
        if not self._degraded and self._consecutive_failures >= _DEGRADED_THRESHOLD:
            self._degraded = True
            _LOGGER.error(
                "Version history degraded: %d consecutive commit failures — recent "
                "saves may not be recoverable until git is healthy",
                self._consecutive_failures,
            )

    def _note_commit_success(self) -> None:
        """Reset the failure run; log recovery if we were degraded."""
        if self._degraded:
            _LOGGER.info(
                "Version history recovered after %d consecutive failures",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._degraded = False

    async def record_configuration(self, configuration: str, message: str) -> str | None:
        """Commit one config by name; return the sha (``None`` no-op), raise on failure."""
        path = self._db.settings.rel_path(configuration)
        return await self.commit([path], message)

    def discard_pending(self, configuration: str) -> None:
        """Drop a queued catch-all entry; a specific commit supersedes it.

        Lets a dashboard commit's rich message win over the generic
        external-edit message a debounced flush would otherwise apply.
        """
        self._pending.pop(configuration, None)

    # ------------------------------------------------------------------
    # WS commands
    # ------------------------------------------------------------------

    @api_command("version_history/list_versions")
    async def list_versions(self, *, configuration: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Return the commit history for *configuration*, newest first."""
        if not self._repo.enabled:
            return []
        path = self._db.settings.rel_path(configuration)
        commits = await run_in_executor(self._repo.log_file, path)
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
        content = await run_in_executor(self._repo.file_at, path, sha)
        if content is None:
            raise CommandError(ErrorCode.NOT_FOUND, f"{configuration} not found at {sha}")
        return {"configuration": configuration, "sha": sha, "content": content}

    @api_command("version_history/get_diff")
    async def get_diff(self, *, configuration: str, sha: str, **kwargs: Any) -> dict[str, Any]:
        """Return a unified diff of *configuration* between *sha* and the working copy."""
        self._require_enabled()
        self._validate_sha(sha)
        path = self._db.settings.rel_path(configuration)
        diff = await run_in_executor(self._repo.diff_file, path, sha)
        return {"configuration": configuration, "sha": sha, "diff": diff}

    @api_command("version_history/list_deleted")
    async def list_deleted(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Return configs that have history but no working-tree copy (restorable)."""
        if not self._repo.enabled:
            return []
        deleted = await run_in_executor(self._repo.deleted_files)
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
            content = await run_in_executor(self._repo.file_at, path, sha)
            if content is None:
                raise CommandError(ErrorCode.NOT_FOUND, f"{configuration} not found at {sha}")
            restored_from = sha
        else:
            result = await run_in_executor(self._repo.latest_content, path)
            if result is None:
                raise CommandError(ErrorCode.NOT_FOUND, f"no history for {configuration}")
            restored_from, content = result
        devices = self._db.devices
        if devices is None:  # pragma: no cover — devices is always up post-start
            raise CommandError(ErrorCode.INTERNAL_ERROR, "devices controller unavailable")
        await devices.apply_restored_yaml(configuration, content, restored_from=restored_from[:7])
        return {"configuration": configuration, "restored_from": restored_from, "content": content}

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
        """
        Commit every queued config, draining in a loop.

        Load-bearing: the final ``while`` check and the return have no
        await between them, so an event can't slip in and be stranded —
        ``_on_disk_change`` then sees the task done and reschedules.
        """
        while self._pending:
            pending = self._pending
            self._pending = {}
            for configuration, message in pending.items():
                try:
                    await self.record_configuration(configuration, message)
                except GIT_COMMIT_ERRORS:
                    # A genuine git failure for one entry: warn and keep
                    # going so it can't strand the batch. A programming bug
                    # is *not* caught — it propagates to the task's
                    # done-callback rather than being masked here.
                    _LOGGER.warning(
                        "Version-history catch-all failed for %s", configuration, exc_info=True
                    )
