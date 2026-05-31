"""Firmware-binary discovery + download endpoints."""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.components.libretiny.const import (
    FAMILY_COMPONENT as _LIBRETINY_FAMILY_COMPONENT,
)
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.storage_path import resolve_storage_path

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


# Platforms whose ``target_platform`` value isn't the component
# module name. ESP32 variants collapse to the umbrella ``esp32``
# component; LibreTiny chip families collapse to ``libretiny``.
# The LibreTiny set is sourced from upstream's
# ``FAMILY_COMPONENT.values()`` so it picks up new chip families
# automatically on the next ``esphome`` dependency bump.
_LIBRETINY_TARGET_PLATFORMS: frozenset[str] = frozenset(_LIBRETINY_FAMILY_COMPONENT.values()) | {
    "libretiny"
}

# Stable ``type`` tag per artifact filename so the frontend can map it to a
# localized label (falling back to the platform-supplied ``title`` for any
# file not listed here).
_ARTIFACT_TYPES: dict[str, str] = {
    "firmware.factory.bin": "factory",
    "firmware.ota.bin": "ota",
    "firmware.bin": "bin",
    "firmware.uf2": "uf2",
    "firmware.elf": "elf",
}


async def get_binaries(controller: FirmwareController, *, configuration: str) -> list[dict]:
    """List on-disk downloadable artifacts as ``[{title, file}]``.

    The platform's ``get_download_types`` entries that exist, plus a
    ``firmware.elf`` entry when present (``get_download_types`` never
    lists it). Empty means nothing is built yet. Each ``file`` is fetched
    over HTTP via ``GET /api/firmware/download`` (see :func:`http_download`).
    """
    # ``resolve_storage_path`` collapses to
    # ``<data_dir>/storage/<Path(configuration).name>.json``; a
    # traversal-shaped *configuration* could still escape to an
    # attacker-controlled basename inside the storage tree, so the
    # validator below is the gate. Do not reorder.
    await controller._validate_configuration_boundary(configuration)
    loop = asyncio.get_running_loop()

    def _get_types() -> list[dict]:
        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None:
            return []
        try:
            component = _resolve_download_component(storage.target_platform)
            module = importlib.import_module(f"esphome.components.{component}")
            types = list(module.get_download_types(storage))
        except Exception:  # noqa: BLE001 — third-party regression: upstream ``get_download_types`` could raise anything
            _LOGGER.warning("Could not determine download types for %s", configuration)
            return []
        # No build dir → can't confirm anything on disk → treat as not built.
        if storage.firmware_bin_path is None:
            return []
        build_dir = storage.firmware_bin_path.parent
        # Filter to files that exist so a cleaned build reads as "compile
        # first" rather than offering a name ``firmware/download`` would 404 on.
        downloads = [dict(t) for t in types if (build_dir / t["file"]).is_file()]
        # firmware.elf sits beside firmware.bin on every platform
        # (remote_build/artifact_platforms/*.py). The `not any` guards against a
        # future get_download_types that lists it, so it can't appear twice.
        if (build_dir / "firmware.elf").is_file() and not any(
            t["file"] == "firmware.elf" for t in downloads
        ):
            downloads.append(
                {
                    "title": "ELF (for debugging)",
                    "description": "Debug symbols for the ESP stack trace decoder.",
                    "file": "firmware.elf",
                }
            )
        for entry in downloads:
            artifact_type = _ARTIFACT_TYPES.get(entry["file"])
            if artifact_type:
                entry["type"] = artifact_type
        return downloads

    return await loop.run_in_executor(None, _get_types)


def _resolve_artifact_path(configuration: str, file: str) -> tuple[Path, str]:
    """Resolve a build artifact to ``(path, download_name)``, traversal-safe.

    Raises ``FileNotFoundError`` when the device isn't built or *file* is
    absent, and ``ValueError`` (from ``relative_to``) when *file* escapes the
    build directory. ``download_name`` is restricted to a filename-safe charset
    so it can't inject into a ``Content-Disposition`` header.
    """
    storage = StorageJSON.load(resolve_storage_path(configuration))
    if storage is None or storage.firmware_bin_path is None:
        msg = "No firmware binary — compile the device first"
        raise FileNotFoundError(msg)

    base_dir = storage.firmware_bin_path.parent.resolve()
    path = (base_dir / file).resolve()
    # Path traversal protection — resolve() collapses ``..`` / absolute
    # ``file`` / symlinks, then relative_to raises if it escaped base_dir.
    path.relative_to(base_dir)

    if not path.is_file():
        msg = f"Binary not found: {file}"
        raise FileNotFoundError(msg)

    download_name = re.sub(r"[^A-Za-z0-9._-]", "_", f"{storage.name}-{path.name}")
    return path, download_name


class DownloadTokens:
    """Single-use, short-TTL capability tokens for HTTP artifact downloads.

    A token is minted over the authenticated WebSocket
    (``firmware/download_token``) and consumed by ``GET /api/firmware/download``.
    The token *is* that route's authorization (the route is in
    ``auth_middleware``'s public allowlist), which lets a plain ``<a href>``
    navigation stream the file straight to disk — no ``Authorization`` header,
    no in-browser buffering, works on mobile. So each token is unguessable
    (:mod:`secrets`), expires fast, is single-use, and is bound to one
    ``(configuration, file)`` pair, so it can't be replayed or repurposed.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._tokens: dict[str, tuple[str, str, float]] = {}

    def create(self, configuration: str, file: str) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (configuration, file, time.monotonic() + self._ttl)
        return token

    def consume(self, token: str) -> tuple[str, str] | None:
        """Pop a token (single-use) and return its ``(configuration, file)``.

        Returns ``None`` for an unknown, already-used, or expired token.
        """
        entry = self._tokens.pop(token, None)
        if entry is None:
            return None
        configuration, file, expiry = entry
        if time.monotonic() > expiry:
            return None
        return configuration, file

    def _purge(self) -> None:
        now = time.monotonic()
        for token in [t for t, (_, _, exp) in self._tokens.items() if now > exp]:
            del self._tokens[token]


async def http_download(request: web.Request) -> web.StreamResponse:
    """``GET /api/firmware/download?token=`` — stream an artifact.

    HTTP (not WebSocket) so a large ``firmware.elf`` isn't capped by a proxy's
    WebSocket ``max_msg_size``, and a navigation streams it straight to disk.
    The ``token`` (see :class:`DownloadTokens`) is the sole authorization and
    carries the ``(configuration, file)`` it was minted for, so query params
    can't point it at a different artifact.
    """
    db = request.app["device_builder"]
    resolved = db.firmware.download_tokens.consume(request.query.get("token", ""))
    if resolved is None:
        raise web.HTTPNotFound
    configuration, file = resolved
    try:
        await db.firmware._validate_configuration_boundary(configuration)
        loop = asyncio.get_running_loop()
        path, download_name = await loop.run_in_executor(
            None, _resolve_artifact_path, configuration, file
        )
    except (CommandError, FileNotFoundError, ValueError) as err:
        # Collapse "not built" / "missing" / "traversal" to a bare 404 for the
        # caller, but log so an operator debugging a failed download has a
        # server-side signal (the token already authenticated the request).
        _LOGGER.debug("Firmware download rejected for %s/%s: %s", configuration, file, err)
        raise web.HTTPNotFound from None
    return web.FileResponse(
        path,
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "Content-Type": "application/octet-stream",
        },
    )


def _resolve_download_component(target_platform: str | None) -> str:
    """Return the ``esphome.components`` module name for *target_platform*.

    ``None`` / empty input collapses to ``""``; the caller's
    ``importlib.import_module`` then fails in its ``try/except``
    and logs a warning.
    """
    platform = (target_platform or "").lower()
    if platform.upper() in ESP32_VARIANTS:
        return "esp32"
    if platform in _LIBRETINY_TARGET_PLATFORMS:
        return "libretiny"
    return platform
