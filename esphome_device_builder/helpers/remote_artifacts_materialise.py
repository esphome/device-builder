"""
Materialise a remote-build artifact tarball into the offloader's local build dir.

After :func:`materialise_remote_artifacts` returns, the offloader's
filesystem looks as if a local compile produced the build:
``<data_dir>/build/<name>/`` carries the per-platform build tree,
``<data_dir>/storage/<basename>.json`` is the rewritten StorageJSON
sidecar, and ``<data_dir>/idedata/<name>.json`` is the rewritten
idedata cache (touched so ``_load_idedata``'s mtime gate hits).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from esphome.storage_json import StorageJSON

from ..controllers.remote_build.artifacts_tarball import (
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
)
from .storage_path import resolve_data_dir, resolve_idedata_path, resolve_storage_path

_LOGGER = logging.getLogger(__name__)


# Defence-in-depth gate on the receiver-supplied device name.
# Pairing requires explicit operator approval so the wire isn't
# fully untrusted, but the name flows straight into a Path join
# (``<data_dir>/build/<name>/``) and a forged value like ``..``
# would land the build tree outside the data dir.
_SAFE_DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class MaterialiseError(RuntimeError):
    """Raised when a tarball can't be materialised into a usable build tree."""


def materialise_remote_artifacts(tarball: bytes, configuration: str) -> Path:
    """
    Stage *tarball* into offloader-local form.

    The build-dir ``<name>`` segment comes from the shipped
    ``storage.json``'s ``name`` field (not the YAML filename stem)
    so renamed devices key the same as esphome's CORE does.
    Returns the staged build path; callers that just need the
    side-effects (the runner) can discard it.
    """
    # storage.json + idedata.json are cache-side files; we
    # rewrite their paths before write so they don't extract
    # into the build tree.
    try:
        tar_io = io.BytesIO(tarball)
        with tarfile.open(fileobj=tar_io, mode="r:gz") as tar:
            storage_bytes = _read_member_required(tar, STORAGE_MEMBER_NAME)
            idedata_bytes = _read_member_required(tar, IDEDATA_MEMBER_NAME)
            receiver_storage = _parse_storage_json(storage_bytes)
            device_name = receiver_storage.get("name")
            if not isinstance(device_name, str) or not device_name:
                raise MaterialiseError("tarball storage.json missing required name field")
            if not _SAFE_DEVICE_NAME_RE.fullmatch(device_name):
                raise MaterialiseError(
                    f"tarball storage.json name {device_name!r} not safe for a path segment"
                )
            receiver_build_path_str = receiver_storage.get("build_path")
            if not isinstance(receiver_build_path_str, str):
                raise MaterialiseError("tarball storage.json missing required build_path field")
            receiver_build_path = Path(receiver_build_path_str)

            data_dir = resolve_data_dir(configuration)
            build_path = data_dir / "build" / device_name
            # Wipe before extract so a board swap on the same YAML
            # (esp32 → bk72xx) doesn't leave stale per-platform
            # artefacts that firmware/download could surface as
            # wrong bytes.
            shutil.rmtree(build_path, ignore_errors=True)
            build_path.mkdir(parents=True, exist_ok=True)

            _safe_extract_excluding(
                tar,
                build_path,
                exclude={STORAGE_MEMBER_NAME, IDEDATA_MEMBER_NAME},
            )
    except tarfile.TarError as err:
        raise MaterialiseError(f"tarball is malformed: {err}") from err

    _stage_offloader_storage(
        configuration=configuration,
        receiver_storage_bytes=storage_bytes,
        receiver_build_path=receiver_build_path,
        offloader_build_path=build_path,
    )
    cached_idedata_path = _stage_offloader_idedata(
        configuration=configuration,
        idedata_bytes=idedata_bytes,
        device_name=device_name,
        receiver_build_path=receiver_build_path,
        offloader_build_path=build_path,
    )
    _force_idedata_cache_hit(
        platformio_ini=build_path / PLATFORMIO_INI_MEMBER_NAME,
        cached_idedata=cached_idedata_path,
    )

    return build_path


def _read_member_required(tar: tarfile.TarFile, name: str) -> bytes:
    """Return *name*'s bytes from *tar* or raise with a clear message."""
    try:
        member = tar.getmember(name)
    except KeyError as err:
        raise MaterialiseError(f"tarball missing required member: {name!r}") from err
    if not member.isfile():
        raise MaterialiseError(f"tarball member {name!r} is not a regular file")
    payload = tar.extractfile(member)
    if payload is None:  # ``isfile()`` already gates this; defence
        raise MaterialiseError(f"tarball member {name!r} unreadable")
    return payload.read()


def _safe_extract_excluding(tar: tarfile.TarFile, dest: Path, *, exclude: set[str]) -> None:
    """Extract every member except *exclude*; reject any that escapes *dest*."""
    dest_resolved = dest.resolve()
    members_to_extract: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        if member.name in exclude:
            continue
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError as err:
            raise MaterialiseError(f"tarball member escapes destination: {member.name!r}") from err
        members_to_extract.append(member)
    # ``filter="data"`` is python 3.14's default-to-be: rejects
    # symlinks / device nodes / setuid bits, mirrors the
    # defensive intent of the per-member relative_to check above.
    tar.extractall(dest, members=members_to_extract, filter="data")


def _parse_storage_json(payload: bytes) -> dict[str, Any]:
    """Parse the shipped storage.json into a dict for the pre-extract lookups."""
    try:
        parsed = json.loads(payload)
    except ValueError as err:
        raise MaterialiseError(f"tarball storage.json is not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise MaterialiseError("tarball storage.json is not a JSON object")
    return parsed


def _stage_offloader_storage(
    *,
    configuration: str,
    receiver_storage_bytes: bytes,
    receiver_build_path: Path,
    offloader_build_path: Path,
) -> None:
    """Write the receiver's storage to the offloader's path, remap paths, save."""
    storage_path = resolve_storage_path(configuration)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(receiver_storage_bytes)
    storage = StorageJSON.load(storage_path)
    if storage is None:
        raise MaterialiseError(
            f"StorageJSON.load returned None for the staged sidecar at {storage_path}"
        )
    if storage.firmware_bin_path is not None:
        storage.firmware_bin_path = _remap_to_offloader(
            Path(storage.firmware_bin_path),
            receiver_build_path,
            offloader_build_path,
        )
    storage.build_path = offloader_build_path
    storage.save(storage_path)


def _stage_offloader_idedata(
    *,
    configuration: str,
    idedata_bytes: bytes,
    device_name: str,
    receiver_build_path: Path,
    offloader_build_path: Path,
) -> Path:
    """Rewrite the receiver's idedata and save at the offloader's cache path."""
    try:
        data = json.loads(idedata_bytes)
    except ValueError as err:
        raise MaterialiseError(f"tarball idedata.json is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise MaterialiseError("tarball idedata.json is not a JSON object")

    prog = data.get("prog_path")
    if isinstance(prog, str):
        data["prog_path"] = str(
            _remap_to_offloader(Path(prog), receiver_build_path, offloader_build_path)
        )
    extra = data.get("extra")
    if isinstance(extra, dict):
        flash_images = extra.get("flash_images")
        if isinstance(flash_images, list):
            for image in flash_images:
                if not isinstance(image, dict):
                    continue
                path_str = image.get("path")
                if isinstance(path_str, str):
                    image["path"] = str(
                        _remap_to_offloader(
                            Path(path_str),
                            receiver_build_path,
                            offloader_build_path,
                        )
                    )

    # Remap cc_path's PIO core prefix so RP2040 BOOTSEL's
    # picotool lookup resolves on the offloader; drop the field
    # if the shape isn't recognisable (picotool falls through to
    # PATH).
    cc_path = data.get("cc_path")
    if isinstance(cc_path, str):
        remapped = _remap_pio_toolchain_path(cc_path)
        if remapped is None:
            data.pop("cc_path", None)
        else:
            data["cc_path"] = remapped

    cached_path = resolve_idedata_path(configuration, name=device_name)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return cached_path


def _force_idedata_cache_hit(*, platformio_ini: Path, cached_idedata: Path) -> None:
    """Make platformio.ini.mtime < cached_idedata.mtime so _load_idedata's gate hits."""
    if not platformio_ini.is_file() or not cached_idedata.is_file():
        return
    now = time.time()
    os.utime(cached_idedata, (now, now))
    os.utime(platformio_ini, (now - 1, now - 1))


def _remap_to_offloader(
    receiver_abs: Path,
    receiver_build_path: Path,
    offloader_build_path: Path,
) -> Path:
    """Translate *receiver_abs* under *receiver_build_path* to the offloader's tree.

    Returns *receiver_abs* unchanged when it isn't actually under
    *receiver_build_path* (cc_path-style absolute paths get
    remapped via :func:`_remap_pio_toolchain_path` instead).
    """
    try:
        relative = receiver_abs.relative_to(receiver_build_path)
    except ValueError:
        return receiver_abs
    return offloader_build_path / relative


def _remap_pio_toolchain_path(cc_path: str) -> str | None:
    """Swap the receiver's ``<pio_core>/packages/...`` prefix for the offloader's.

    Returns None when *cc_path* doesn't carry a ``packages``
    segment; toolchain identifiers themselves are platform-stable
    so the suffix carries verbatim.
    """
    parts = Path(cc_path).parts
    try:
        packages_idx = parts.index("packages")
    except ValueError:
        return None
    offloader_core = Path(os.environ.get("PLATFORMIO_CORE_DIR", str(Path.home() / ".platformio")))
    return str(offloader_core.joinpath(*parts[packages_idx:]))
