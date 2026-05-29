"""
Materialise a remote-build artifact tarball into the offloader's local build dir.

After :func:`materialise_remote_artifacts` returns, the offloader's
filesystem looks as if a local compile produced the build:
``<data_dir>/build/<name>/`` carries the per-platform build tree,
``<data_dir>/storage/<basename>.json`` is the rewritten StorageJSON
sidecar, ``<data_dir>/idedata/<name>.json`` is the rewritten idedata
cache (touched so ``_load_idedata``'s mtime gate hits), and -- when
the receiver-side esphome shipped one -- ``<basename>.validated.yaml``
sits next to the JSON sidecar so the next local ``esphome upload`` /
``esphome logs`` skips ``read_config()`` via esphome's fast path.
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import tarfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Any, NamedTuple

from esphome.helpers import rmtree
from esphome.storage_json import StorageJSON
from esphome.writer import storage_should_clean

from ..controllers.remote_build.artifacts_tarball import (
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
    VALIDATED_YAML_MEMBER_NAME,
)
from .cross_os_path import receiver_pure_path_cls
from .json import dumps_indent
from .json import loads as json_loads
from .peer_link_bundle import FIRMWARE_MAX_TOTAL_BYTES
from .storage_path import (
    resolve_compiled_config_path,
    resolve_data_dir,
    resolve_idedata_path,
    resolve_storage_path,
)

_LOGGER = logging.getLogger(__name__)


# Defence-in-depth gate on the receiver-supplied device name.
# Pairing requires explicit operator approval so the wire isn't
# fully untrusted, but the name flows straight into a Path join
# (``<data_dir>/build/<name>/``) and a forged value like ``..``
# would land the build tree outside the data dir.
_SAFE_DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class MaterialiseError(RuntimeError):
    """Raised when a tarball can't be materialised into a usable build tree."""


class _ExtractedTarball(NamedTuple):
    storage_bytes: bytes
    idedata_bytes: bytes
    # ``PurePath``-flavoured per the receiver's OS so the path
    # remap works when receiver and offloader differ.
    receiver_build_path: PurePath
    build_path: Path
    # Optional: present when receiver-side esphome wrote a
    # validated-config cache (>= 2026.6.0).
    validated_yaml_bytes: bytes | None


def materialise_remote_artifacts(tarball: bytes, configuration: str) -> Path:
    """
    Stage *tarball* into offloader-local form.

    The build-dir ``<name>`` segment comes from the shipped
    ``storage.json``'s ``name`` field (not the YAML filename stem)
    so renamed devices key the same as esphome's CORE does.
    Returns the staged build path; callers that just need the
    side-effects (the runner) can discard it.
    """
    extracted = _open_and_extract_build_tree(tarball, configuration)
    cached_idedata_path = _stage_offloader_idedata(
        configuration=configuration,
        idedata_bytes=extracted.idedata_bytes,
        device_name=extracted.build_path.name,
        receiver_build_path=extracted.receiver_build_path,
        offloader_build_path=extracted.build_path,
    )
    _force_idedata_cache_hit(
        platformio_ini=extracted.build_path / PLATFORMIO_INI_MEMBER_NAME,
        cached_idedata=cached_idedata_path,
    )
    if extracted.validated_yaml_bytes is not None:
        _stage_offloader_validated_yaml(
            configuration=configuration,
            payload=extracted.validated_yaml_bytes,
        )
    return extracted.build_path


def _open_and_extract_build_tree(tarball: bytes, configuration: str) -> _ExtractedTarball:
    """Open *tarball*, stage the storage sidecar, conditionally wipe, extract the build tree.

    storage.json + idedata.json are cache-side files; their
    bytes are returned for the caller to rewrite before write
    rather than extracted into the build tree.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            # Thread one running total across every metadata read and
            # the build-tree extract so the global FIRMWARE_MAX_TOTAL_BYTES
            # cap holds for the whole tarball, not per-member.
            total_bytes = 0
            storage_bytes, total_bytes = _read_member_required(
                tar, STORAGE_MEMBER_NAME, total_so_far=total_bytes
            )
            idedata_bytes, total_bytes = _read_member_required(
                tar, IDEDATA_MEMBER_NAME, total_so_far=total_bytes
            )
            validated_yaml_bytes, total_bytes = _read_member_optional(
                tar, VALIDATED_YAML_MEMBER_NAME, total_so_far=total_bytes
            )
            receiver_storage = _parse_storage_json(storage_bytes)
            device_name = _device_name_from_storage(receiver_storage)
            receiver_build_path = _receiver_build_path_from_storage(receiver_storage)

            build_path = resolve_data_dir(configuration) / "build" / device_name
            # Stage the rewritten sidecar before deciding the wipe so
            # esphome's ``storage_should_clean`` sees the offloader-form
            # ``build_path`` instead of the receiver-absolute one it
            # would otherwise flag as a build_path mismatch on every
            # call. Loading the prior first preserves it across the
            # stage write that overwrites the same path.
            prior_storage = StorageJSON.load(resolve_storage_path(configuration))
            new_storage = _stage_offloader_storage(
                configuration=configuration,
                receiver_storage_bytes=storage_bytes,
                receiver_build_path=receiver_build_path,
                offloader_build_path=build_path,
            )
            # rmtree is best-effort: failures log + fall through, and
            # the extract below overwrites every member named in the
            # tarball, but stale files the tarball doesn't mention can
            # survive a failed wipe.
            if storage_should_clean(prior_storage, new_storage):
                try:
                    rmtree(build_path)
                except OSError as exc:
                    _LOGGER.debug("materialise: pre-extract rmtree(%s) failed: %s", build_path, exc)
            build_path.mkdir(parents=True, exist_ok=True)
            pio_path = build_path / PLATFORMIO_INI_MEMBER_NAME
            with _preserve_platformio_ini_mtime_if_unchanged(pio_path):
                _safe_extract_excluding(
                    tar,
                    build_path,
                    exclude={
                        STORAGE_MEMBER_NAME,
                        IDEDATA_MEMBER_NAME,
                        VALIDATED_YAML_MEMBER_NAME,
                    },
                    initial_total_bytes=total_bytes,
                )
    except tarfile.TarError as err:
        raise MaterialiseError(f"tarball is malformed: {err}") from err
    if not (build_path / PLATFORMIO_INI_MEMBER_NAME).is_file():
        raise MaterialiseError(f"tarball missing required {PLATFORMIO_INI_MEMBER_NAME!r} member")
    return _ExtractedTarball(
        storage_bytes=storage_bytes,
        idedata_bytes=idedata_bytes,
        receiver_build_path=receiver_build_path,
        build_path=build_path,
        validated_yaml_bytes=validated_yaml_bytes,
    )


def _device_name_from_storage(receiver_storage: dict[str, Any]) -> str:
    """Pull and validate the device name from the shipped storage.json."""
    device_name = receiver_storage.get("name")
    if not isinstance(device_name, str) or not device_name:
        raise MaterialiseError("tarball storage.json missing required name field")
    if not _SAFE_DEVICE_NAME_RE.fullmatch(device_name):
        raise MaterialiseError(
            f"tarball storage.json name {device_name!r} not safe for a path segment"
        )
    return device_name


def _receiver_build_path_from_storage(receiver_storage: dict[str, Any]) -> PurePath:
    """Pull build_path from the shipped storage.json as a receiver-flavoured PurePath."""
    receiver_build_path_str = receiver_storage.get("build_path")
    if not isinstance(receiver_build_path_str, str):
        raise MaterialiseError("tarball storage.json missing required build_path field")
    return receiver_pure_path_cls(receiver_build_path_str)(receiver_build_path_str)


def _read_member_optional(
    tar: tarfile.TarFile, name: str, *, total_so_far: int = 0
) -> tuple[bytes | None, int]:
    """Read *name* if present. Returns ``(payload-or-None, running total)``."""
    try:
        member = tar.getmember(name)
    except KeyError:
        return None, total_so_far
    if not member.isfile():
        raise MaterialiseError(f"tarball member {name!r} is not a regular file")
    _check_member_size(member, total_so_far=total_so_far)
    payload = tar.extractfile(member)
    if payload is None:
        raise MaterialiseError(f"tarball member {name!r} unreadable")
    return payload.read(), total_so_far + member.size


def _read_member_required(
    tar: tarfile.TarFile, name: str, *, total_so_far: int = 0
) -> tuple[bytes, int]:
    """Read *name* or raise. Returns ``(payload, running total)``.

    Caps the declared member size against
    :data:`FIRMWARE_MAX_TOTAL_BYTES` before reading so a hostile
    peer can't expand a tiny gzipped tarball into multi-GiB
    memory by inflating a metadata-member header. *total_so_far*
    threads the running cumulative-size accounting from the
    caller; the cap is checked against
    ``total_so_far + member.size`` so successive metadata reads
    can't each fit under the cap individually while collectively
    breaching it.
    """
    try:
        member = tar.getmember(name)
    except KeyError as err:
        raise MaterialiseError(f"tarball missing required member: {name!r}") from err
    if not member.isfile():
        raise MaterialiseError(f"tarball member {name!r} is not a regular file")
    _check_member_size(member, total_so_far=total_so_far)
    payload = tar.extractfile(member)
    if payload is None:  # ``isfile()`` already gates this; defence
        raise MaterialiseError(f"tarball member {name!r} unreadable")
    return payload.read(), total_so_far + member.size


def _check_member_size(member: tarfile.TarInfo, *, total_so_far: int) -> None:
    """Reject tar members whose declared size would breach the global cap."""
    if member.size > FIRMWARE_MAX_TOTAL_BYTES:
        raise MaterialiseError(
            f"tarball member {member.name!r} declares size {member.size} "
            f"exceeding FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
    if total_so_far + member.size > FIRMWARE_MAX_TOTAL_BYTES:
        raise MaterialiseError(
            f"tarball cumulative size {total_so_far + member.size} "
            f"exceeds FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )


def _safe_extract_excluding(
    tar: tarfile.TarFile,
    dest: Path,
    *,
    exclude: set[str],
    initial_total_bytes: int = 0,
) -> None:
    """Extract every member except *exclude*; reject any that escapes *dest* or breaches the cap.

    *initial_total_bytes* lets the caller seed the cumulative-size
    counter with bytes already read out of the tarball (the metadata
    members) so the cap applies across the whole archive, not just
    the build-tree members.
    """
    dest_resolved = dest.resolve()
    members_to_extract: list[tarfile.TarInfo] = []
    total_bytes = initial_total_bytes
    for member in tar.getmembers():
        if member.name in exclude:
            continue
        _check_member_size(member, total_so_far=total_bytes)
        total_bytes += member.size
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
        parsed = json_loads(payload)
    except ValueError as err:
        raise MaterialiseError(f"tarball storage.json is not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise MaterialiseError("tarball storage.json is not a JSON object")
    return parsed


def _stage_offloader_storage(
    *,
    configuration: str,
    receiver_storage_bytes: bytes,
    receiver_build_path: PurePath,
    offloader_build_path: Path,
) -> StorageJSON:
    """Stage and return the offloader-form storage sidecar."""
    storage_path = resolve_storage_path(configuration)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(receiver_storage_bytes)
    storage = StorageJSON.load(storage_path)
    if storage is None:
        raise MaterialiseError(
            f"StorageJSON.load returned None for the staged sidecar at {storage_path}"
        )
    if storage.firmware_bin_path is not None:
        # ``str()`` round-trips the receiver-flavour string back out
        # of the (possibly broken-on-this-OS) ``Path`` so
        # ``_remap_to_offloader`` re-parses it with the right flavour.
        storage.firmware_bin_path = _remap_to_offloader(
            str(storage.firmware_bin_path),
            receiver_build_path,
            offloader_build_path,
        )
    storage.build_path = offloader_build_path
    storage.save(storage_path)
    return storage


def _stage_offloader_idedata(
    *,
    configuration: str,
    idedata_bytes: bytes,
    device_name: str,
    receiver_build_path: PurePath,
    offloader_build_path: Path,
) -> Path:
    """Rewrite the receiver's idedata and save at the offloader's cache path."""
    data = _parse_idedata_dict(idedata_bytes)
    _remap_idedata_build_paths(data, receiver_build_path, offloader_build_path)
    _remap_idedata_toolchain_path(data)

    cached_path = resolve_idedata_path(configuration, name=device_name)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(dumps_indent(data) + b"\n")
    return cached_path


def _parse_idedata_dict(payload: bytes) -> dict[str, Any]:
    """Parse the shipped idedata.json or raise MaterialiseError."""
    try:
        data = json_loads(payload)
    except ValueError as err:
        raise MaterialiseError(f"tarball idedata.json is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise MaterialiseError("tarball idedata.json is not a JSON object")
    return data


def _remap_idedata_build_paths(
    data: dict[str, Any],
    receiver_build_path: PurePath,
    offloader_build_path: Path,
) -> None:
    """Rewrite prog_path + extra.flash_images[*].path to the offloader's tree."""

    def _remap(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return str(_remap_to_offloader(value, receiver_build_path, offloader_build_path))

    if (prog := _remap(data.get("prog_path"))) is not None:
        data["prog_path"] = prog
    extra = data.get("extra")
    flash_images = extra.get("flash_images") if isinstance(extra, dict) else None
    for image in flash_images or []:
        if not isinstance(image, dict):
            continue
        if (remapped := _remap(image.get("path"))) is not None:
            image["path"] = remapped


def _remap_idedata_toolchain_path(data: dict[str, Any]) -> None:
    """Swap cc_path's PIO core prefix; drop if unrecognised (picotool falls back to PATH)."""
    cc_path = data.get("cc_path")
    if not isinstance(cc_path, str):
        return
    remapped = _remap_pio_toolchain_path(cc_path)
    if remapped is None:
        data.pop("cc_path", None)
    else:
        data["cc_path"] = remapped


def _stage_offloader_validated_yaml(
    *,
    configuration: str,
    payload: bytes,
) -> None:
    """Stage the receiver's validated-config cache at the offloader's path.

    Written 0600 because the cache resolves !secret references inline.
    mtime is touched to "now" so esphome's fast path (which gates on
    cache mtime >= source YAML mtime) takes the cache instead of
    re-running read_config.
    """
    path = resolve_compiled_config_path(configuration)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Open with 0600 at creation time so the file is never momentarily
    # readable at the process umask between write_bytes() and chmod().
    # O_CREAT honours an existing inode's mode bits, so tighten with
    # an explicit chmod afterwards too (no-op on Windows). O_BINARY
    # only exists on Windows where it disables the CRLF translation
    # that would otherwise corrupt the YAML bytes.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    if sys.platform != "win32":
        path.chmod(0o600)
    now = time.time()
    os.utime(path, (now, now))


@contextmanager
def _preserve_platformio_ini_mtime_if_unchanged(pio_path: Path) -> Iterator[None]:
    """
    Hold *pio_path*'s mtime stable across an enclosed write when the bytes don't change.

    Same bytes → restore the prior mtime so SCons's per-object
    cache survives; different bytes → bump to ``time.time_ns()``
    so SCons unambiguously sees the file as newer than every
    existing ``.pioenvs/<name>/*.o``.
    """
    prior_mtime_ns: int | None = None
    prior_bytes: bytes | None = None
    if pio_path.is_file():
        prior_mtime_ns = pio_path.stat().st_mtime_ns
        prior_bytes = pio_path.read_bytes()
    # No try/finally: a partial extract leaves the file in an
    # indeterminate state and restoring the prior mtime would lie
    # about its contents.
    yield
    if not pio_path.is_file():
        return
    if prior_mtime_ns is not None and pio_path.read_bytes() == prior_bytes:
        os.utime(pio_path, ns=(prior_mtime_ns, prior_mtime_ns))
    else:
        now_ns = time.time_ns()
        os.utime(pio_path, ns=(now_ns, now_ns))


def _force_idedata_cache_hit(*, platformio_ini: Path, cached_idedata: Path) -> None:
    """Push *cached_idedata*'s mtime past *platformio_ini*'s for esphome's _load_idedata gate."""
    if not platformio_ini.is_file() or not cached_idedata.is_file():
        return
    target_ns = max(time.time_ns(), platformio_ini.stat().st_mtime_ns + 1)
    os.utime(cached_idedata, ns=(target_ns, target_ns))


def _remap_to_offloader(
    receiver_abs: str,
    receiver_build_path: PurePath,
    offloader_build_path: Path,
) -> Path:
    """Translate *receiver_abs* under *receiver_build_path* to the offloader's tree.

    *receiver_abs* is parsed with the same ``PurePath`` flavour as
    *receiver_build_path* so cross-OS receiver/offloader pairs
    join correctly. Returns the receiver string as the offloader's
    native ``Path`` when it isn't under *receiver_build_path*.
    Raises :class:`MaterialiseError` on a ``..`` segment in the
    computed relative; ``PurePath.relative_to`` doesn't normalise,
    so a malicious ``firmware_bin_path`` like ``<build>/../evil``
    would otherwise escape the offloader's build tree.
    """
    receiver_cls = type(receiver_build_path)
    try:
        relative = receiver_cls(receiver_abs).relative_to(receiver_build_path)
    except ValueError:
        return Path(receiver_abs)
    if ".." in relative.parts:
        raise MaterialiseError(
            f"receiver path {receiver_abs!r} contains '..' segments relative to "
            f"build_path {str(receiver_build_path)!r}"
        )
    return offloader_build_path.joinpath(*relative.parts)


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
