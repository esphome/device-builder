"""
Editor controller — supports the in-browser YAML editor.

Currently exposes live YAML validation; future editor utilities (formatting,
schema-driven completion, etc.) will live here too.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..helpers.api import api_command
from ..helpers.subprocess import create_subprocess_exec
from .firmware import _find_esphome_cmd

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)
_STARTUP_TIMEOUT = 15.0
_VALIDATE_TIMEOUT = 30.0


@dataclass
class _EditorSession:
    """Per-configuration validator state: one warm subprocess plus a serialization lock."""

    proc: asyncio.subprocess.Process | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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

    async def start(self) -> None:
        """Async initialize the controller."""
        # resolve the `esphome` CLI invocation used to spawn validator subprocesses
        self._esphome_cmd = _find_esphome_cmd()

    async def stop(self) -> None:
        """Stop the controller.."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        # tear down every warm validator subprocess on app shutdown
        for session in sessions:
            await self._terminate_subprocess(session)

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    async def _ensure_subprocess(self, session: _EditorSession) -> None:
        """Spawn the `esphome vscode --ace` subprocess for `session` if not already running."""
        if session.proc is not None and session.proc.returncode is None:
            return

        config_dir = str(self._db.settings.config_dir)
        cmd = [*self._esphome_cmd, "vscode", config_dir, "--ace"]
        _LOGGER.info("Spawning vscode subprocess: %s", " ".join(cmd))
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
            raise RuntimeError("esphome vscode subprocess did not start in time") from err

    async def _terminate_subprocess(self, session: _EditorSession) -> None:
        """Terminate the session's subprocess."""
        proc = session.proc
        session.proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.write(json.dumps({"type": "exit"}).encode() + b"\n")
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
                proc.kill()
                await proc.wait()

    def _resolve_file(self, requested: str, configuration: str, content: str) -> str:
        """
        Answer a `read_file` request from the validator subprocess.

        Returns the in-memory `content` for the file currently being edited
        and falls back to reading from disk for any other path the subprocess
        asks about (e.g. files pulled in via `!include`).
        """
        cfg_dir = Path(self._db.settings.config_dir).resolve()
        try:
            req_path = Path(requested).resolve()
        except OSError:
            req_path = Path(requested)
        main_path = (cfg_dir / configuration).resolve()
        if req_path == main_path or Path(requested).name == configuration:
            return content
        try:
            return req_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # API commands
    # ------------------------------------------------------------------

    @api_command("editor/validate_yaml")
    async def validate_yaml(
        self,
        *,
        configuration: str,
        content: str,
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
        """
        session = self._sessions.setdefault(configuration, _EditorSession())
        async with session.lock:
            try:
                return await asyncio.wait_for(
                    self._validate_locked(session, configuration, content),
                    timeout=_VALIDATE_TIMEOUT,
                )
            except (TimeoutError, RuntimeError, BrokenPipeError):
                # Subprocess wedged or died — kill it so the next call respawns.
                await self._terminate_subprocess(session)
                raise

    async def _validate_locked(
        self, session: _EditorSession, configuration: str, content: str
    ) -> dict:
        """
        Run a single validation round-trip against `session`'s subprocess.

        Caller must hold ``session.lock``; the stdin/stdout protocol is stateful
        and any interleaving would corrupt subsequent responses.
        """
        await self._ensure_subprocess(session)
        proc = session.proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None

        request = {"type": "validate", "file": configuration}
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        await proc.stdin.drain()

        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("esphome vscode subprocess closed stdout")
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")
            if msg_type == "read_file":
                file_content = self._resolve_file(msg.get("path", ""), configuration, content)
                response = {"type": "file_response", "content": file_content}
                proc.stdin.write(json.dumps(response).encode() + b"\n")
                await proc.stdin.drain()
            elif msg_type == "result":
                return {
                    "yaml_errors": msg.get("yaml_errors", []),
                    "validation_errors": msg.get("validation_errors", []),
                }
            # Anything else (stray "version", future events) — ignore and keep reading.
