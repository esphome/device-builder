"""
Editor controller — supports the in-browser YAML editor.

Exposes live YAML validation and one-click config migration; future
editor utilities (formatting, schema-driven completion, etc.) live here too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fnv_hash_fast import fnv1a_32

from ..helpers.api import api_command
from ..helpers.async_ import drain_tasks, run_in_executor
from ..helpers.device_yaml import extract_component_source_fingerprint
from ..helpers.json import JSONDecodeError, dumps, loads
from ..helpers.migrations import render_migrations
from ..helpers.process import kill_quietly
from ..helpers.sibling_cli import find_esphome_cmd
from ..helpers.subprocess import create_subprocess_exec
from ..models.automations import MigrateConfigResponse

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)
_STARTUP_TIMEOUT = 15.0
_VALIDATE_TIMEOUT = 30.0
# Short budget for adopt: a YAML-syntax error surfaces at parse (sub-second,
# pre-network); the cold ``github://`` package fetch is abandoned, not awaited.
IMPORT_VALIDATE_TIMEOUT = 8.0
# Linter and save both call ``validate_yaml`` on identical
# content (typing-stops → linter at 600 ms → user clicks save).
# Cache covers that hand-off; longer would risk staleness for
# ``!include`` / ``external_components`` files mutated outside
# the editor. ``fnv1a_32`` keys the cache (non-cryptographic;
# collision risk negligible for the ≤dozens of buffers an editor
# session sees inside one TTL window).
_VALIDATE_CACHE_TTL = 60.0
# Idle seconds before a warm vscode subprocess is reaped, respawned on next
# validate. 10 min outlasts a normal mid-edit pause but frees RAM once the
# user leaves.
_IDLE_SUBPROCESS_TIMEOUT = 600.0
_REAP_INTERVAL = 60.0
# ``_EditorSession.source_fingerprint`` sentinel that matches no real
# digest, so the next validate respawns the subprocess.
_STALE_SOURCES = "stale"


class ValidatorUnavailableError(RuntimeError):
    """Validator subprocess couldn't be reached (failed to start / closed its pipe)."""


@dataclass
class _CachedValidation:
    """Snapshot of a validate_yaml result, with the inputs needed to reuse it."""

    content_hash: int
    result: dict[str, Any]
    at: float

    def is_fresh_for(self, content_hash: int) -> bool:
        return (
            self.content_hash == content_hash and time.monotonic() - self.at < _VALIDATE_CACHE_TTL
        )


@dataclass
class _EditorSession:
    """Per-configuration validator state: warm subprocess, lock, and result cache."""

    configuration: str
    proc: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cached: _CachedValidation | None = None
    last_used: float = field(default_factory=time.monotonic)
    # ``extract_component_source_fingerprint`` of the content the subprocess
    # was spawned for.
    source_fingerprint: str = ""
    # Bumped by ``invalidate_cache`` so an in-flight validate can't
    # reinstate a pre-write result into ``cached``.
    invalidation_epoch: int = 0


class EditorController:
    """Backs the WebSocket commands used by the YAML editor in the dashboard.

    Today this means structured YAML validation via the upstream
    `esphome vscode --ace` subprocess: clients send their in-memory YAML and
    receive the same `{yaml_errors, validation_errors}` payload the upstream
    dashboard renders inline. Each configuration keeps its own warm
    subprocess so concurrent edits on different devices do not block each
    other.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._sessions: dict[str, _EditorSession] = {}
        self._esphome_cmd: list[str] = []
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Async initialize the controller."""
        # resolve the `esphome` CLI invocation used to spawn validator subprocesses
        self._esphome_cmd = find_esphome_cmd()
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(), name="editor-subprocess-reaper"
        )

    async def stop(self) -> None:
        """Stop the controller."""
        if self._reaper_task is not None:
            await drain_tasks((self._reaper_task,), log_exceptions=True)
            self._reaper_task = None
        sessions = list(self._sessions.values())
        self._sessions.clear()
        # tear down every warm validator subprocess on app shutdown
        for session in sessions:
            await self._terminate_subprocess(session)

    def invalidate_cache(self) -> None:
        """
        Drop every session's cached validation after a config-dir write.

        Cleared for all sessions, not just the written file: a referenced
        file (secrets, ``!include``, ``packages``) the content-hash key
        can't see affects any open device's validation. Sessions whose
        content declares component sources also lose their warm
        subprocess on the next validate — the write may have touched a
        referenced ``packages:`` / ``!include`` file that carries
        ``external_components:``, which the buffer-derived fingerprint
        can't see. Out-of-band edits to a ``type: local`` component's
        own files fire no config-dir write and stay uncovered until
        the idle reap.
        """
        # Snapshot the values: this is await-free so the dict can't change
        # under us today, but the copy keeps it safe if a future caller adds
        # a suspension point mid-clear.
        for session in tuple(self._sessions.values()):
            session.cached = None
            session.invalidation_epoch += 1
            if session.source_fingerprint:
                session.source_fingerprint = _STALE_SOURCES

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    async def _ensure_subprocess(self, session: _EditorSession, source_fingerprint: str) -> None:
        """Spawn the `esphome vscode --ace` subprocess for `session` unless a matching one runs."""
        if session.proc is not None and session.proc.returncode is None:
            if session.source_fingerprint == source_fingerprint:
                return
            # esphome's loader cache and ``sys.modules`` outlive the
            # subprocess's per-validate ``CORE.reset()``, so changed
            # ``external_components:`` / ``packages:`` need a fresh process.
            _LOGGER.info(
                "Restarting vscode subprocess for %s: component sources changed",
                session.configuration,
            )
            await self._terminate_subprocess(session)
        session.source_fingerprint = source_fingerprint

        config_dir = str(self._db.settings.config_dir)
        cmd = [*self._esphome_cmd, "vscode", config_dir, "--ace"]
        # Include the session's configuration so a fleet-wide log
        # can distinguish "two different files opened" (expected:
        # one warm subprocess per config) from "same file
        # respawned after a timeout / crash". The cmd line itself
        # only carries the config-dir, not the specific file.
        _LOGGER.info(
            "Spawning vscode subprocess for %s: %s",
            session.configuration,
            " ".join(cmd),
        )
        session.proc = await create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Drain the initial {"type": "version", ...} line so the next read
        # in validate_yaml lands on a real response.
        assert session.proc.stdout is not None
        try:
            await asyncio.wait_for(session.proc.stdout.readline(), timeout=_STARTUP_TIMEOUT)
        except TimeoutError as err:
            await self._terminate_subprocess(session)
            raise ValidatorUnavailableError(
                "esphome vscode subprocess did not start in time"
            ) from err

    async def _terminate_subprocess(self, session: _EditorSession) -> None:
        """Terminate the session's subprocess."""
        proc = session.proc
        if proc is None or proc.returncode is not None:
            session.proc = None
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.write(dumps({"type": "exit"}) + b"\n")
                await proc.stdin.drain()
                proc.stdin.close()
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("Error sending exit to vscode subprocess", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                kill_quietly(proc)
                await proc.wait()
        # Cleared only after the process is gone, so a cancel mid-await leaves
        # the handle on the session for stop()'s teardown to reap.
        session.proc = None

    async def _reaper_loop(self) -> None:
        """Periodically reap subprocesses idle past ``_IDLE_SUBPROCESS_TIMEOUT``."""
        while True:
            await asyncio.sleep(_REAP_INTERVAL)
            try:
                await self._reap_idle_subprocesses()
            except Exception:
                # A failed sweep must not kill the reaper.
                _LOGGER.exception("Idle vscode subprocess reaper sweep failed")

    async def _reap_idle_subprocesses(self) -> None:
        """Terminate each session's subprocess after a window of no validation.

        Holds ``session.lock`` across the terminate so it can never interrupt an
        in-flight ``_validate_locked`` round-trip; a busy (locked) session was
        just used, so it's skipped. The session object stays — the next
        ``validate_yaml`` respawns via ``_ensure_subprocess``.
        """
        for session in list(self._sessions.values()):
            proc = session.proc
            if proc is None or proc.returncode is not None:
                continue
            if time.monotonic() - session.last_used < _IDLE_SUBPROCESS_TIMEOUT:
                continue
            if session.lock.locked():
                continue
            async with session.lock:
                # Re-check under the lock: a validate may have run (and stamped
                # last_used) while we waited.
                proc = session.proc
                if (
                    proc is None
                    or proc.returncode is not None
                    or time.monotonic() - session.last_used < _IDLE_SUBPROCESS_TIMEOUT
                ):
                    continue
                _LOGGER.info("Reaping idle vscode subprocess for %s", session.configuration)
                # One stuck terminate must not strand the rest of the sweep.
                try:
                    await self._terminate_subprocess(session)
                except Exception:
                    _LOGGER.exception(
                        "Failed to reap idle vscode subprocess for %s", session.configuration
                    )

    def _resolve_file(self, requested: str, configuration: str, content: str) -> str:
        """
        Answer a `read_file` request from the validator subprocess.

        Returns the in-memory `content` for the file currently being edited
        and falls back to reading from disk for any other path the subprocess
        asks about (e.g. files pulled in via `!include`).

        Synchronous on purpose — performs ``Path.resolve`` (realpath
        syscall) and a blocking ``read_text`` for ``!include`` files.
        Always invoke via ``asyncio.to_thread`` from the event loop;
        the in-line call site in ``_validate_locked`` does that.
        """
        cfg_dir = Path(self._db.settings.config_dir).resolve()
        try:
            req_path = Path(requested).resolve()
        except (OSError, RuntimeError):
            # Unresolvable (broken symlink / EACCES / 3.12 symlink-loop
            # RuntimeError) can't be containment-checked — refuse.
            return ""
        main_path = (cfg_dir / configuration).resolve()
        # Structural equality, not basename: ``/tmp/kitchen.yaml`` must not
        # ride the in-memory shortcut past the containment check below.
        if req_path == main_path or Path(requested) == Path(configuration):
            return content
        # Attacker-controlled ``!include /etc/passwd`` would otherwise echo
        # the file's bytes back as ``yaml_errors`` parse snippets.
        if not req_path.is_relative_to(cfg_dir):
            return ""
        try:
            return req_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # API commands
    # ------------------------------------------------------------------

    @api_command("editor/migrate_config")
    async def migrate_config(
        self,
        *,
        content: str,
        **_kwargs: Any,
    ) -> dict:
        """
        Apply every known migration to *content* in one splice.

        Renamed api and homeassistant spellings plus the ethernet
        ``clk_mode`` -> ``clk`` conversion; ``yaml_diff`` is ``null``
        when nothing needed migrating.
        """
        result = await run_in_executor(render_migrations, content)
        diff = result[1] if result is not None else None
        return MigrateConfigResponse(yaml_diff=diff).to_dict()

    @api_command("editor/validate_yaml")
    async def validate_yaml(
        self,
        *,
        configuration: str,
        content: str,
        timeout: float | None = None,
        client: Any = None,
        message_id: str = "",
        **kwargs: Any,
    ) -> dict:
        """
        Validate `content` as the YAML for `configuration`.

        Returns ``{"yaml_errors": [...], "validation_errors": [...]}`` —
        the same shape upstream ``vscode.py`` produces. Each error has a
        ``message`` and (for validation errors) a ``range`` with
        ``{start_line, start_col, end_line, end_col}`` (0-indexed).

        Results are cached per session by content hash for
        ``_VALIDATE_CACHE_TTL`` seconds; the linter and the
        save-time re-validate hit the same content back-to-back.

        ``timeout`` bounds the round-trip (not subprocess startup) and is
        internal-only; a WS client always gets the default.
        """
        if client is not None:
            # ``timeout`` is internal-only; ignore a WS client's value.
            timeout = None
        if timeout is None:
            timeout = _VALIDATE_TIMEOUT
        session = self._sessions.setdefault(
            configuration, _EditorSession(configuration=configuration)
        )
        # Stamp before any await so the reaper (which re-checks last_used under
        # the lock) never reaps a session with a request in flight.
        session.last_used = time.monotonic()
        content_hash = fnv1a_32(content.encode("utf-8"))
        # Fast path: avoid the lock when the previous result is
        # still fresh for the same content.
        cached = session.cached
        if cached is not None and cached.is_fresh_for(content_hash):
            return cached.result
        async with session.lock:
            # Re-check under the lock so a concurrent linter+save
            # for the same content only spawns one subprocess pass.
            cached = session.cached
            if cached is not None and cached.is_fresh_for(content_hash):
                return cached.result
            ok = False
            epoch = session.invalidation_epoch
            try:
                # Warm the subprocess outside the budget so a cold start
                # (own ``_STARTUP_TIMEOUT``) doesn't eat a short import timeout.
                await self._ensure_subprocess(
                    session, extract_component_source_fingerprint(content)
                )
                result = await asyncio.wait_for(
                    self._validate_locked(session, configuration, content),
                    timeout=timeout,
                )
                ok = True
            finally:
                # Any failure (timeout, subprocess loss, a bug, cancellation)
                # can leave the stateful stdin/stdout protocol mid-message;
                # kill it so the next call respawns clean. The exception (typed
                # for callers) propagates unchanged.
                if not ok:
                    await self._terminate_subprocess(session)
            if session.invalidation_epoch == epoch:
                session.cached = _CachedValidation(
                    content_hash=content_hash, result=result, at=time.monotonic()
                )
            return result

    async def _validate_locked(
        self, session: _EditorSession, configuration: str, content: str
    ) -> dict:
        """
        Run a single validation round-trip against `session`'s subprocess.

        Caller must hold ``session.lock`` and have brought the subprocess up
        via ``_ensure_subprocess``; the stdin/stdout protocol is stateful and
        any interleaving would corrupt subsequent responses.
        """
        proc = session.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        request = {"type": "validate", "file": configuration}
        proc.stdin.write(dumps(request) + b"\n")
        await proc.stdin.drain()

        while True:
            line = await proc.stdout.readline()
            if not line:
                raise ValidatorUnavailableError("esphome vscode subprocess closed stdout")
            try:
                # The subprocess emits one UTF-8 JSON object per line;
                # orjson decodes bytes directly so no .decode() round-trip.
                msg = loads(line)
            except JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "read_file":
                # ``_resolve_file`` does ``Path.resolve`` (realpath
                # syscall) and a blocking ``read_text`` for
                # ``!include`` files. Push to a worker thread so a
                # slow / large include doesn't stall the event loop.
                file_content = await asyncio.to_thread(
                    self._resolve_file, msg.get("path", ""), configuration, content
                )
                response = {"type": "file_response", "content": file_content}
                proc.stdin.write(dumps(response) + b"\n")
                await proc.stdin.drain()
            elif msg_type == "result":
                return {
                    "yaml_errors": msg.get("yaml_errors", []),
                    "validation_errors": msg.get("validation_errors", []),
                }
            # Anything else (stray "version", future events) — ignore and keep reading.
