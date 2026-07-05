"""Provision + cache one esphome venv per release version (receiver-side).

A receiver whose installed esphome differs from the offloader's builds the
offloader's version into an isolated venv and compiles from it, instead of
handing back firmware built with the wrong version. Venvs are cached per
release under ``<data_dir>/.remote_builds/venvs/esphome-<version>/`` and reused
across every device/build; a per-version lock serialises concurrent first
builds of the same version while different versions build concurrently.

RELEASE versions only: a dev / prerelease target can't be pinned to a
reproducible ``pip install esphome==<version>`` and is refused.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from esphome.core import CORE

from ...helpers.async_ import run_in_executor
from ...helpers.remote_build_layout import REMOTE_BUILDS_NAME
from ...helpers.subprocess import run_subprocess_capture
from ...helpers.version_compat import is_release_version

_LOGGER = logging.getLogger(__name__)

_VENVS_DIRNAME = "venvs"
_VENV_PREFIX = "esphome-"
# Written only after both venv creation and the install succeed, so a venv
# left half-built by a crash (present dir, no marker) is rebuilt, not reused.
_READY_MARKER = ".provisioned"
_VENV_TIMEOUT = 120.0
# ``pip install esphome`` pulls platformio + a large dep tree; allow generously.
_PIP_TIMEOUT = 900.0
# Cap on the subprocess-output tail folded into an error message.
_ERROR_TAIL_BYTES = 2000


class EnvProvisionError(Exception):
    """A matching esphome venv could not be provisioned."""


class EnvProvisioner:
    """Create + cache one esphome venv per release version, keyed by version."""

    def __init__(self, data_dir: Path | None = None, *, base_python: str | None = None) -> None:
        # ``data_dir`` / ``base_python`` are injectable for tests; production
        # reads ``CORE.data_dir`` lazily and builds from ``sys.executable``.
        self._data_dir = data_dir
        self._base_python = base_python or sys.executable
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def venvs_dir(self) -> Path:
        """Base directory holding every per-version venv."""
        base = self._data_dir if self._data_dir is not None else Path(CORE.data_dir)
        return base / REMOTE_BUILDS_NAME / _VENVS_DIRNAME

    async def provision(self, version: str) -> list[str]:
        """Return the esphome command for *version*, building its venv on first use.

        Raises :class:`EnvProvisionError` for a non-release version or a failed
        venv / pip step; a partial venv is removed so a retry starts clean.
        """
        if not is_release_version(version):
            raise EnvProvisionError(f"cannot provision non-release esphome version {version!r}")
        venv = self.venvs_dir / f"{_VENV_PREFIX}{version}"
        async with self._lock_for(version):
            if not await run_in_executor(_is_ready, venv):
                await self._build(version, venv)
        return _venv_esphome_cmd(venv)

    async def sweep_stale(self, installed_version: str) -> None:
        """Remove cached venvs older than *installed_version* (a startup sweep).

        No-op when *installed_version* isn't a plain release (a dev receiver),
        since older / newer can't be ordered against it.
        """
        if not is_release_version(installed_version):
            return
        installed_key = _release_key(installed_version)
        for venv, version in await run_in_executor(self._list_venvs):
            if _release_key(version) < installed_key:
                _LOGGER.info(
                    "Removing stale esphome venv %s (older than installed %s)",
                    version,
                    installed_version,
                )
                await run_in_executor(_rmtree, venv)

    async def clean_all(self) -> None:
        """Remove every cached venv (the receiver's clean-build-env path)."""
        await run_in_executor(_rmtree, self.venvs_dir)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lock_for(self, version: str) -> asyncio.Lock:
        lock = self._locks.get(version)
        if lock is None:
            lock = self._locks[version] = asyncio.Lock()
        return lock

    def _list_venvs(self) -> list[tuple[Path, str]]:
        """``(dir, version)`` for each ``esphome-<release>`` venv on disk."""
        venvs_dir = self.venvs_dir
        if not venvs_dir.is_dir():
            return []
        found: list[tuple[Path, str]] = []
        for child in venvs_dir.iterdir():
            if not child.is_dir() or not child.name.startswith(_VENV_PREFIX):
                continue
            version = child.name[len(_VENV_PREFIX) :]
            if is_release_version(version):
                found.append((child, version))
        return found

    async def _build(self, version: str, venv: Path) -> None:
        await run_in_executor(_rmtree, venv)  # clear any partial from a prior crash
        await run_in_executor(_mkdirs, venv.parent)
        await self._run(
            "create the venv", venv, _VENV_TIMEOUT, self._base_python, "-m", "venv", str(venv)
        )
        await self._run(
            f"install esphome=={version}",
            venv,
            _PIP_TIMEOUT,
            str(_venv_python(venv)),
            "-m",
            "pip",
            "install",
            f"esphome=={version}",
        )
        await run_in_executor((venv / _READY_MARKER).write_text, version)

    async def _run(self, what: str, venv: Path, timeout: float, *args: str) -> None:
        result = await run_subprocess_capture(*args, timeout=timeout)
        if result.timed_out or result.returncode != 0:
            await run_in_executor(_rmtree, venv)
            tail = result.stdout[-_ERROR_TAIL_BYTES:].decode(errors="replace")
            raise EnvProvisionError(f"failed to {what} for a remote build: {tail}")


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_esphome_cmd(venv: Path) -> list[str]:
    """Return the esphome CLI invocation that runs inside *venv*."""
    return [str(_venv_python(venv)), "-m", "esphome"]


def _release_key(version: str) -> tuple[int, ...]:
    """Sort key for a plain-release version string (sweep ordering)."""
    return tuple(int(part) for part in version.split("."))


def _is_ready(venv: Path) -> bool:
    return (venv / _READY_MARKER).is_file()


def _mkdirs(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
