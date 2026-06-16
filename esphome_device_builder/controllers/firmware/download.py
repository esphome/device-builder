"""Firmware-binary discovery + download endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from esphome.storage_json import StorageJSON

from ...definitions import (
    PlatformCapabilities,
    coerce_download_entries,
    load_platform_capabilities_index,
)
from ...helpers.api import CommandError
from ...helpers.json import JSONDecodeError
from ...helpers.json import loads as json_loads
from ...helpers.storage_path import resolve_storage_path
from .helpers import _find_sibling_cli

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

# Generous: the child pays a full esphome import before answering.
_HELPER_TIMEOUT_S = 60


def _capabilities() -> PlatformCapabilities:
    """Seam over the (cached) platform-capabilities index loader."""
    return load_platform_capabilities_index()


@dataclass(frozen=True, slots=True)
class _DownloadRouting:
    """Sets that map a ``target_platform`` to an ``esphome.components`` module."""

    esp32_variants: frozenset[str]
    libretiny_targets: frozenset[str]


@cache
def _platform_sets() -> _DownloadRouting:
    """Return the download-routing sets, derived from the generated index.

    Read from the index rather than ``esphome.components.esp32`` to keep espidf /
    requests / esphome.config off cold start. LibreTiny chip families collapse to
    the ``libretiny`` component, so the umbrella name joins that set.
    """
    caps = _capabilities()
    return _DownloadRouting(
        esp32_variants=frozenset(caps.esp32_variants),
        libretiny_targets=frozenset(caps.libretiny_families) | {"libretiny"},
    )


# Prime the cached index read at import so the first download request doesn't
# pay the (small, esphome-free) file read inside the event loop.
_platform_sets()


def _helper_cmd() -> tuple[str, ...]:
    """Argv prefix for the device-builder-helper child (cached by _find_sibling_cli)."""
    return _find_sibling_cli("device-builder-helper", "esphome_device_builder.helper_cli")


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
        storage_path = resolve_storage_path(configuration)
        storage = StorageJSON.load(storage_path)
        if storage is None:
            return []
        return collect_download_entries(storage, storage_path, label=configuration)

    return await loop.run_in_executor(None, _get_types)


def collect_download_entries(
    storage: StorageJSON, storage_path: Path, *, label: str | None = None
) -> list[dict]:
    """Return the downloadable artifacts on disk for *storage* as ``[{title, file, ...}]``.

    The platform's ``get_download_types`` entries that exist under
    ``firmware_bin_path.parent`` (the build dir, ``.pioenvs/<name>/`` or
    native-IDF ``build/``), plus ``firmware.elf`` when present. Empty
    when nothing is built. The single source of truth for what a build
    offers; ``get_binaries`` is its async wrapper. *label* identifies the
    build in the failure log -- the caller's configuration filename when it
    has one (more specific than ``storage.name`` across colliding device
    names); defaults to ``storage.name``. *storage_path* is required to resolve
    the build-dir-dependent platforms (libretiny / nrf52) through the helper
    subprocess; the static platforms are answered from the catalog regardless.
    """
    # No build dir → can't confirm anything on disk → treat as not built.
    # Checked before resolving types so an unbuilt libretiny / nrf52 device
    # doesn't spawn the helper subprocess just to discard the result.
    if storage.firmware_bin_path is None:
        return []
    types = _download_types_for(storage, storage_path, label=label)
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


def _download_types_for(
    storage: StorageJSON, storage_path: Path | None, *, label: str | None
) -> list[dict]:
    """Return ``get_download_types`` entries for *storage*'s platform.

    Static platforms (esp32 / esp8266 / rp2040) come straight from the
    precomputed catalog index. Build-dir-dependent platforms (libretiny / nrf52)
    are answered by the device-builder-helper subprocess, so the long-lived
    process never imports ``esphome.components.*``. A missing *storage_path* or a
    failing helper yields ``[]`` -- the same "treat as not built" fall-through
    the in-process import used to take on error.
    """
    component = _resolve_download_component(storage.target_platform)
    if not component:
        return []
    precomputed = _capabilities().download_types.get(component)
    if precomputed is not None:
        # Copy so a caller that mutates the result can't corrupt the @cache'd
        # index (the helper path likewise returns fresh dicts).
        return [dict(entry) for entry in precomputed]
    if storage_path is None:
        _LOGGER.warning(
            "No storage path given to resolve %s download types for %s",
            component,
            label or storage.name,
        )
        return []
    cmd = [*_helper_cmd(), "download-types", str(storage_path), component]
    try:
        # ``close_fds=False`` mirrors helpers.subprocess's policy (skip the
        # fork-time /proc/self/fd close walk; we inherit nothing the child needs shut).
        result = subprocess.run(  # noqa: S603 — argv is internally built, no shell
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_S,
            close_fds=False,
        )
    except Exception as err:  # spawn / nonzero exit / timeout / esphome regression
        # An infrastructure failure (helper not installed, timeout, import error)
        # is distinct from a built device with no artifacts; surface the child's
        # stderr so it's diagnosable, not an unbuilt-looking empty row. Still
        # degrade to [] (the listing must keep rendering for other devices).
        _LOGGER.warning(
            "download-types helper failed for %s: %s",
            label or storage.name,
            getattr(err, "stderr", None) or err,
            exc_info=True,
        )
        return []
    try:
        payload = json_loads(result.stdout)
    except JSONDecodeError:  # non-JSON stdout (rare: the helper isolates its stdout)
        _LOGGER.warning(
            "download-types helper returned non-JSON for %s: stdout=%r stderr=%r",
            label or storage.name,
            result.stdout[:200],
            result.stderr[:200],
            exc_info=True,
        )
        return []
    # Coerce at the boundary so a malformed reply can't reach a downstream
    # ``entry["file"]``; same validation the index payload goes through.
    return coerce_download_entries(payload)


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

    ``None`` / empty input collapses to ``""``; ``_download_types_for`` then
    short-circuits to ``[]`` (no helper spawn, no log) and the build reads as not
    built. A real but unknown platform routes to the helper, whose import failure
    is the branch that logs.
    """
    platform = (target_platform or "").lower()
    routing = _platform_sets()
    if platform.upper() in routing.esp32_variants:
        return "esp32"
    if platform in routing.libretiny_targets:
        return "libretiny"
    # Every esp32 variant is the umbrella ``esp32`` component, so fold by prefix
    # even when the index is degraded (empty variants) — a missing index then
    # makes an ESP32-S3/C3/... download slow (helper spawn) rather than broken
    # (helper importing a nonexistent ``esphome.components.esp32s3``).
    if platform.startswith("esp32"):
        return "esp32"
    return platform
