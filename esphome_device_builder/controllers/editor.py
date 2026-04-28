"""Editor controller — live YAML validation via persistent `esphome vscode --ace` subprocess.

Mirrors the protocol used by the upstream ESPHome dashboard
(esphome/dashboard/src/editor/esphome-editor.ts ←→ esphome/esphome/vscode.py).
The subprocess is spawned lazily on first validate request, kept warm to
avoid per-call interpreter startup cost, and torn down on app stop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..helpers.api import api_command
from .firmware import _find_esphome_cmd

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)
_STARTUP_TIMEOUT = 15.0
_VALIDATE_TIMEOUT = 30.0


class EditorController:
    """Owns a long-lived `esphome vscode --ace` subprocess for structured YAML.

    Single-flights validation requests through an asyncio.Lock so the stateful
    stdin/stdout protocol stays consistent.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._esphome_cmd: list[str] = []

    async def start(self) -> None:
        self._esphome_cmd = _find_esphome_cmd()

    async def stop(self) -> None:
        await self._terminate_subprocess()

    # ------------------------------------------------------------------
    # Subprocess management
    # ------------------------------------------------------------------

    async def _ensure_subprocess(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return

        config_dir = str(self._db.settings.config_dir)
        cmd = [*self._esphome_cmd, "vscode", config_dir, "--ace"]
        _LOGGER.info("Spawning vscode subprocess: %s", " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Drain the initial {"type": "version", ...} line so the next read
        # in validate_yaml lands on a real response.
        assert self._proc.stdout is not None
        try:
            await asyncio.wait_for(self._proc.stdout.readline(), timeout=_STARTUP_TIMEOUT)
        except TimeoutError as err:
            await self._terminate_subprocess()
            raise RuntimeError("esphome vscode subprocess did not start in time") from err

    async def _terminate_subprocess(self) -> None:
        proc = self._proc
        self._proc = None
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
        """Resolve a `read_file` request from the subprocess.

        The main file being edited returns the in-memory content; any other
        file (e.g. `!include`d secrets.yaml) is read from disk so validation
        sees a complete, current view.
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
        """Validate `content` as the YAML for `configuration`.

        Returns ``{"yaml_errors": [...], "validation_errors": [...]}`` —
        the same shape upstream ``vscode.py`` produces. Each error has a
        ``message`` and (for validation errors) a ``range`` with
        ``{start_line, start_col, end_line, end_col}`` (0-indexed).
        """
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._validate_locked(configuration, content),
                    timeout=_VALIDATE_TIMEOUT,
                )
            except (TimeoutError, RuntimeError, BrokenPipeError):
                # Subprocess wedged or died — kill it, surface no errors so the
                # editor stays usable. Next call will respawn.
                await self._terminate_subprocess()
                raise

    async def _validate_locked(self, configuration: str, content: str) -> dict:
        await self._ensure_subprocess()
        proc = self._proc
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
