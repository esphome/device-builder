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
import re
import sys
from pathlib import Path

from esphome.core import CORE
from esphome.helpers import rmtree as _esphome_rmtree

from ...helpers import remote_build_layout
from ...helpers.async_ import run_in_executor
from ...helpers.subprocess import run_subprocess_capture
from ...helpers.version_compat import is_release_version

_LOGGER = logging.getLogger(__name__)

_VENV_PREFIX = "esphome-"
_VENV_TIMEOUT = 120.0
# ``pip install esphome`` pulls platformio + a large dep tree; allow generously.
_PIP_TIMEOUT = 900.0
# ``esphome version`` just prints a constant, but the interpreter still imports
# esphome; give the health probe margin on a slow host.
_HEALTHCHECK_TIMEOUT = 60.0
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
        return remote_build_layout.venvs_dir(base)

    async def provision(self, version: str) -> list[str]:
        """Return the esphome command for *version*, building its venv on first use.

        Raises :class:`EnvProvisionError` for a non-release version, or a venv
        that won't build or fails its health check; a bad venv is removed so a
        retry starts clean.
        """
        if not is_release_version(version):
            raise EnvProvisionError(f"cannot provision non-release esphome version {version!r}")
        venv = self.venvs_dir / f"{_VENV_PREFIX}{version}"
        async with self._lock_for(version):
            if not await self._is_healthy(venv, version):
                await self._build(version, venv)
                if not await self._is_healthy(venv, version):
                    await run_in_executor(_rmtree, venv)
                    raise EnvProvisionError(
                        f"provisioned esphome venv for {version} failed its health check"
                    )
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

    async def _is_healthy(self, venv: Path, version: str) -> bool:
        """Whether *venv* runs the *version* of esphome it's cached for.

        Verifies esphome both runs (``python -m esphome version`` exits 0) AND
        reports the requested version, so a venv whose contents drifted from its
        directory name (interrupted upgrade, manual pip) is rebuilt rather than
        trusted on liveness alone — version identity is the whole point of the
        feature. Also the readiness check: a missing or crash-partial venv fails
        here, so no separate "finished" marker is needed.
        """
        python = _venv_python(venv)
        if not await run_in_executor(python.is_file):
            return False
        result = await run_subprocess_capture(
            str(python), "-m", "esphome", "version", timeout=_HEALTHCHECK_TIMEOUT
        )
        if result.timed_out or result.returncode != 0:
            return False
        return _version_in_output(version, result.stdout.decode(errors="replace"))

    async def _build(self, version: str, venv: Path) -> None:
        await run_in_executor(_prepare_venv_dir, venv)
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

    async def _run(self, what: str, venv: Path, timeout: float, *args: str) -> None:
        result = await run_subprocess_capture(*args, timeout=timeout)
        if result.timed_out or result.returncode != 0:
            await run_in_executor(_rmtree, venv)
            status = "timed out" if result.timed_out else f"exit {result.returncode}"
            tail = result.stdout[-_ERROR_TAIL_BYTES:].decode(errors="replace")
            raise EnvProvisionError(f"failed to {what} for a remote build ({status}): {tail}")


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_esphome_cmd(venv: Path) -> list[str]:
    """Return the esphome CLI invocation that runs inside *venv*."""
    return [str(_venv_python(venv)), "-m", "esphome"]


def _version_in_output(version: str, output: str) -> bool:
    """Whether *version* appears in *output* as a whole token, not inside a longer number."""
    return re.search(rf"(?<![\w.]){re.escape(version)}(?![\w.])", output) is not None


def _release_key(version: str) -> tuple[int, ...]:
    """Sort key for a plain-release version, trailing ``.0`` normalised out.

    So ``2026.6`` and ``2026.6.0`` order equal rather than the shorter one
    counting as older, which would sweep an effectively-installed venv.
    """
    parts = [int(part) for part in version.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _prepare_venv_dir(venv: Path) -> None:
    """Remove any crash-partial venv and ensure the parent dir exists."""
    _rmtree(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)


def _rmtree(path: Path) -> None:
    """Remove *path* if present, handling Windows read-only files."""
    if path.exists():
        _esphome_rmtree(path)
