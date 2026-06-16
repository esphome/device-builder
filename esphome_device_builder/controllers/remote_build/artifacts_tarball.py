"""
Pack / unpack the receiver's build-artifact tarball.

The remote-build feature ships compiled firmware between two
dashboards by serialising the receiver's build tree into a
single gzipped tarball. This module owns the pack + unpack
helpers — pure data transforms with no WS / wire-flow
knowledge — so the two end-to-end surfaces that consume the
format (the receiver-side :class:`ArtifactsDownloadSender`
streamer and the offloader-side ``download_artifacts`` WS
unpacker / source-routed runner) call into one place instead
of re-implementing the format twice.

Tarball layout (materialise-locally wire format):

.. code-block:: text

    storage.json     # receiver's <data_dir>/storage/<basename>.json
    idedata.json     # receiver's <data_dir>/idedata/<name>.json (esphome's cache copy)
    platformio.ini   # receiver's <build_path>/platformio.ini
    build_info.json  # receiver's <build_path>/build_info.json (optional; #654)
    <per-platform build-tree files>  # see artifact_platforms/*.py

The metadata members at the top of the tarball are
platform-independent — every build ships them
(``build_info.json`` is optional: skipped when the receiver's
build tree predates ESPHome's ``build_info.json`` write
hook). The :mod:`controllers.remote_build.artifact_platforms`
registry drives which build-tree files travel alongside them;
see the per-platform modules for the exact paths each platform
ships.

The offloader-side materialiser
(:func:`helpers.remote_artifacts_materialise.materialise_remote_artifacts`)
reads ``storage.json`` + ``idedata.json``, rewrites their
receiver-absolute path fields to offloader-absolute, and stages
them at the offloader's canonical cache locations:
``<data_dir>/storage/<basename>.json`` and
``<data_dir>/idedata/<name>.json``. The build tree extracts as-is
under ``<data_dir>/build/<name>/``.

The :func:`unpack_artifacts_response` adapter is a separate
consumer — it serves the multi-image set to the browser-side
Web Serial flasher via the offloader's
``remote_build/download_artifacts`` WS command, keyed by image
basename (the frontend doesn't care about the build-tree
layout). See :func:`_rewrite_idedata_paths` for the basename
rewrite the frontend's lookup relies on.
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from esphome.storage_json import StorageJSON

from ...helpers.build_artifacts import _firmware_offset_for_platform
from ...helpers.cross_os_path import cross_os_basename
from ...helpers.json import loads as json_loads
from ...helpers.peer_link_bundle import FIRMWARE_MAX_TOTAL_BYTES
from ...helpers.storage_path import (
    resolve_compiled_config_path,
    resolve_idedata_path,
    resolve_storage_path,
)
from .artifact_platforms import build_files_for_platform

if TYPE_CHECKING:
    from .peer_link_client import DownloadArtifactsResult

_LOGGER = logging.getLogger(__name__)

# Tarball member names that ride alongside the build tree. The
# offloader-side materialiser pulls these out of the tarball and
# stages them at the offloader's canonical cache locations; the
# WS-adapter (:func:`unpack_artifacts_response`) ignores
# ``storage.json`` / ``platformio.ini`` (they're not flash images)
# and reads ``idedata.json`` to recover the upstream-canonical
# flash-image manifest.
STORAGE_MEMBER_NAME = "storage.json"
IDEDATA_MEMBER_NAME = "idedata.json"
PLATFORMIO_INI_MEMBER_NAME = "platformio.ini"
# Read by the offloader's ``read_build_info_hash`` to populate
# ``expected_config_hash`` post-build (see #654).
BUILD_INFO_MEMBER_NAME = "build_info.json"
# Receiver-side esphome >= 2026.6.0 dumps the validated config alongside
# the StorageJSON sidecar; reusing it on the offloader lets `esphome
# upload` / `esphome logs` skip the full `read_config()` pipeline. Optional
# member: skipped when the receiver predates the cache.
VALIDATED_YAML_MEMBER_NAME = "validated.yaml"
_METADATA_MEMBERS: frozenset[str] = frozenset(
    {
        STORAGE_MEMBER_NAME,
        IDEDATA_MEMBER_NAME,
        PLATFORMIO_INI_MEMBER_NAME,
        BUILD_INFO_MEMBER_NAME,
        VALIDATED_YAML_MEMBER_NAME,
    }
)

# esphome's `update_storage_json` writes the validated-config cache and
# the StorageJSON sidecar inside the same call, so a same-compile pair
# of mtimes lands within milliseconds. A downgrade to an esphome version
# that doesn't write the cache leaves the previous cache lingering with
# its mtime stuck on the older compile while the storage.json gets
# rewritten by every new compile. Reject anything older than the
# sidecar by more than this many seconds -- generous enough to cover
# `clean_build` + `clean_cmake_cache` running between the two writes,
# tight enough to never accept a cache from a prior compile cycle.
_VALIDATED_YAML_STALE_THRESHOLD_S = 60.0


# ---------------------------------------------------------------------------
# Pack (receiver side)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackedArtifacts:
    """Output of :func:`pack_build_artifacts` — tarball bytes + start-frame fields.

    ``firmware_offset`` rides alongside the tarball so the
    sender can populate
    :attr:`ArtifactsStartFrameData.firmware_offset` without
    re-running
    :func:`helpers.build_artifacts.load_build_artifacts`.
    Same string form ``idedata.extra.flash_images`` uses
    (lowercase hex, ``0x`` prefix) — keeps the wire shape
    uniform across the firmware partition and the extras.
    """

    tarball: bytes
    firmware_offset: str


def pack_build_artifacts(configuration: str) -> PackedArtifacts:
    """Pack the build for *configuration* into the materialise-locally tarball.

    Synchronous; meant to run inside an executor. Raises
    :class:`FileNotFoundError` when the StorageJSON sidecar /
    cached idedata / platformio.ini aren't on disk;
    :class:`RuntimeError` on unknown ``target_platform`` or
    when the artifact set exceeds :data:`FIRMWARE_MAX_TOTAL_BYTES`
    (per-member walking sum + post-render check, the latter
    matching the offloader-side BundleAssembler cap).
    """
    storage_path, storage = _load_storage_for_pack(configuration)
    build_path = Path(storage.build_path)
    members = _collect_pack_members(
        configuration=configuration,
        storage=storage,
        storage_path=storage_path,
        build_path=build_path,
    )
    firmware_offset = _firmware_offset_for_platform(storage.target_platform or "")
    tarball = _render_tarball(members, configuration=configuration)
    return PackedArtifacts(tarball=tarball, firmware_offset=firmware_offset)


def _load_storage_for_pack(configuration: str) -> tuple[Path, StorageJSON]:
    """Load + validate the StorageJSON sidecar for the receiver-side pack."""
    storage_path = resolve_storage_path(configuration)
    storage = StorageJSON.load(storage_path)
    if storage is None:
        msg = f"StorageJSON sidecar missing for {configuration}: {storage_path}"
        raise FileNotFoundError(msg)
    if storage.firmware_bin_path is None:
        msg = f"firmware_bin_path unset in StorageJSON for {configuration}"
        raise FileNotFoundError(msg)
    if storage.build_path is None:
        msg = f"build_path unset in StorageJSON for {configuration}"
        raise FileNotFoundError(msg)
    if not isinstance(storage.name, str) or not storage.name:
        msg = f"StorageJSON name unset / non-string for {configuration}: {storage.name!r}"
        raise FileNotFoundError(msg)
    return storage_path, storage


def _collect_pack_members(  # noqa: C901
    *,
    configuration: str,
    storage: StorageJSON,
    storage_path: Path,
    build_path: Path,
) -> list[tuple[str, Path]]:
    """Return ``(arcname, src_path)`` pairs for the tarball, in write order."""
    target_platform = (storage.target_platform or "").lower()
    build_files = build_files_for_platform(target_platform)
    if not build_files:
        msg = (
            f"no artifact_platforms module for target_platform="
            f"{storage.target_platform!r} (configuration={configuration!r})"
        )
        raise RuntimeError(msg)

    idedata_cache_path = resolve_idedata_path(configuration, name=storage.name)
    platformio_ini = build_path / PLATFORMIO_INI_MEMBER_NAME
    # A native ESP-IDF build (CMake/ninja) emits neither platformio.ini nor
    # an idedata cache; a PlatformIO build always emits both, so their
    # absence on a PIO build is a real failure that must surface here rather
    # than ship a silently-incomplete tarball. Detect native-IDF positively
    # off the build toolchain, never off file absence.
    if getattr(storage, "toolchain", None) != "esp-idf":
        if not platformio_ini.is_file():
            msg = f"platformio.ini missing for {configuration}: {platformio_ini}"
            raise FileNotFoundError(msg)
        if not idedata_cache_path.is_file():
            msg = f"idedata cache missing for {configuration}: {idedata_cache_path}"
            raise FileNotFoundError(msg)

    build_info_path = build_path / BUILD_INFO_MEMBER_NAME
    validated_yaml_path = resolve_compiled_config_path(configuration)
    members: list[tuple[str, Path]] = [(STORAGE_MEMBER_NAME, storage_path)]
    if idedata_cache_path.is_file():
        members.append((IDEDATA_MEMBER_NAME, idedata_cache_path))
    if platformio_ini.is_file():
        members.append((PLATFORMIO_INI_MEMBER_NAME, platformio_ini))
    if build_info_path.is_file():
        members.append((BUILD_INFO_MEMBER_NAME, build_info_path))
    if _validated_yaml_is_fresh(validated_yaml_path, storage_path):
        members.append((VALIDATED_YAML_MEMBER_NAME, validated_yaml_path))
    # Dedupe by basename, not full path: the WS-unpack adapter
    # keys flash images by basename, so a build emitting both
    # ``.pioenvs/<name>/firmware.factory.bin`` and
    # ``build/firmware.factory.bin`` would otherwise produce a
    # tarball the offloader's adapter rejects. First-wins
    # ordering (BUILD_FILES list order) picks the canonical copy.
    seen_basenames: set[str] = set()

    def _maybe_add(rel: str) -> None:
        abs_path = build_path / rel
        if not abs_path.is_file():
            return
        basename = Path(rel).name
        if basename in seen_basenames:
            return
        members.append((rel, abs_path))
        seen_basenames.add(basename)

    for template in build_files:
        _maybe_add(template.format(name=storage.name))

    # Every file ``get_download_types`` lists for this platform
    # must travel too so the offloader's ``firmware/get_binaries``
    # + ``firmware/download`` surface matches the legacy
    # esphome.dashboard set.
    firmware_bin = Path(storage.firmware_bin_path)
    pioenvs_rel = _relative_or_raise(firmware_bin.parent, build_path, configuration=configuration)
    for download_file in _download_type_files(storage, storage_path):
        _maybe_add(f"{pioenvs_rel}/{download_file}")

    # firmware_bin_path MUST be in the tarball — otherwise the
    # offloader stages a tree where firmware/download misses.
    firmware_bin_rel = _relative_or_raise(firmware_bin, build_path, configuration=configuration)
    if firmware_bin.name not in seen_basenames:
        msg = (
            f"firmware_bin_path {firmware_bin_rel!r} not covered by BUILD_FILES "
            f"for target_platform={storage.target_platform!r}"
        )
        raise RuntimeError(msg)
    return members


def _validated_yaml_is_fresh(validated_yaml: Path, storage_json: Path) -> bool:
    """Test that *validated_yaml* was written by the same compile as *storage_json*.

    esphome >= 2026.6 writes both inside one ``update_storage_json``
    call. If the receiver later downgrades to an esphome that doesn't
    write the cache, the old cache stays on disk while the sidecar
    gets rewritten by every new compile -- detected here by the
    sidecar pulling ahead of the cache.
    """
    try:
        storage_mtime = storage_json.stat().st_mtime
        validated_mtime = validated_yaml.stat().st_mtime
    except OSError:
        return False
    return storage_mtime - validated_mtime <= _VALIDATED_YAML_STALE_THRESHOLD_S


def _download_type_files(storage: StorageJSON, storage_path: Path) -> list[str]:
    """Return the build-relative files ``get_download_types`` lists for *storage*.

    Shares download.py's resolution: precomputed catalog for the static
    platforms, device-builder-helper subprocess for libretiny / nrf52, so the
    receiver process never imports ``esphome.components.*``.
    """
    # Function-local: a top-level import pulls firmware/__init__ -> controller ->
    # remote_runner -> helpers.remote_artifacts_materialise, which imports this
    # module back (circular). The import cost is gone but the cycle remains.
    from ..firmware.download import _download_types_for  # noqa: PLC0415

    entries = _download_types_for(storage, storage_path, label=storage.name)
    return [entry["file"] for entry in entries]


def _render_tarball(members: list[tuple[str, Path]], *, configuration: str) -> bytes:
    """Write *members* to a gzipped tarball, capping declared + rendered bytes."""
    buf = io.BytesIO()
    total_uncompressed = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, src in members:
            # Stat before read so a runaway build artefact trips
            # the cap before we allocate its bytes into memory.
            file_size = src.stat().st_size
            if total_uncompressed + file_size > FIRMWARE_MAX_TOTAL_BYTES:
                msg = (
                    f"build artifacts for {configuration} would exceed "
                    f"FIRMWARE_MAX_TOTAL_BYTES uncompressed "
                    f"({total_uncompressed + file_size} > {FIRMWARE_MAX_TOTAL_BYTES})"
                )
                raise RuntimeError(msg)
            payload = src.read_bytes()
            total_uncompressed += len(payload)
            info = tarfile.TarInfo(name=arcname)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    tarball = buf.getvalue()
    # Final post-render cap on the wire-side length so the
    # receiver-side ceiling matches the offloader's
    # BundleAssembler check on ArtifactsStartFrameData.total_bytes.
    if len(tarball) > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"build artifacts tarball for {configuration} would exceed "
            f"FIRMWARE_MAX_TOTAL_BYTES on the wire "
            f"({len(tarball)} > {FIRMWARE_MAX_TOTAL_BYTES})"
        )
        raise RuntimeError(msg)
    return tarball


# ---------------------------------------------------------------------------
# Unpack (offloader side)
# ---------------------------------------------------------------------------


class UnpackArtifactsError(RuntimeError):
    """Raised on a malformed receiver tarball.

    The receiver-side packer (:func:`pack_build_artifacts`) is
    the only thing that should be writing this stream, so a
    structural failure here means an in-flight bug or a
    misbehaving peer — surfaced as ``INVALID_ARGS`` at the WS
    layer rather than ``INTERNAL_ERROR`` so the user sees a
    clear "the receiver sent a tarball we can't parse"
    message rather than a generic backend-stack-trace toast.
    """


def unpack_artifacts_response(packed: DownloadArtifactsResult, job_id: str) -> dict[str, Any]:
    """
    Unpack the receiver's artifact tarball into the WS response shape.

    Synchronous; meant to run in an executor (``tarfile.open``
    + per-image ``read()`` are blocking syscalls). Reads
    ``idedata.json`` to recover the upstream-canonical
    flash-image manifest, then walks the tarball's remaining
    members to build the ``images`` list. The ``firmware.bin``
    partition's offset comes from *packed*'s
    ``firmware_offset`` field — the receiver populated it
    from ``StorageJSON.target_platform`` via
    :func:`helpers.build_artifacts._firmware_offset_for_platform`.
    The remaining offsets ride inside
    ``idedata.extra.flash_images``. Rewrites every
    ``extra.flash_images[].path`` from the receiver's
    absolute build-dir paths to bare basenames (the only
    thing the offloader's install path can resolve against
    the in-tarball entries).

    Raises :class:`UnpackArtifactsError` on:

    * Missing ``idedata.json``.
    * ``idedata.json`` not parseable as JSON, or not a dict.
    * A flash image declared in ``idedata.extra.flash_images``
      whose tarball member is missing.
    * Missing ``firmware.bin`` in the tarball.
    * A directory entry in the tarball (the receiver-side
      packer is flat by design; a directory means the wire
      format drifted).
    """
    idedata, image_bytes_by_name = read_artifacts_tarball(packed.tarball)
    images = _build_images_response(packed.firmware_offset, idedata, image_bytes_by_name)
    rewritten_idedata = _rewrite_idedata_paths(idedata)
    total_bytes = sum(int(image["size"]) for image in images)
    return {
        "job_id": job_id,
        "idedata": rewritten_idedata,
        "images": images,
        "total_bytes": total_bytes,
    }


def read_artifacts_tarball(tarball: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """
    Read every member of *tarball* into ``(idedata, files-by-basename)``.

    ``idedata`` is the parsed ``idedata.json`` object;
    ``files-by-basename`` carries every non-metadata tarball
    member keyed on its basename (``Path(member.name).name``)
    so the WS-adapter consumer's lookup ignores the
    build-tree nesting the receiver shipped them under.
    ``storage.json`` / ``platformio.ini`` are filtered out:
    they belong to the materialiser, not the flash-image set.

    Raises :class:`UnpackArtifactsError` on any structural
    problem in the tarball (missing idedata, duplicate
    basename across different build-tree members, malformed
    gzip / tar framing) or when the cumulative decompressed
    payload would exceed :data:`FIRMWARE_MAX_TOTAL_BYTES`.
    The size gate is a decompression-bomb guard: gzip can
    compress huge zero-filled / sparse data to a tiny
    on-the-wire payload, so reading without a header-side
    bound would let a hostile peer expand a few-KiB tarball
    into multi-GiB memory.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            idedata, image_bytes_by_name = _walk_artifacts_members(tar)
    except tarfile.TarError as exc:
        msg = f"artifacts tarball is malformed: {exc}"
        raise UnpackArtifactsError(msg) from exc
    if idedata is None:
        msg = "artifacts tarball missing idedata.json"
        raise UnpackArtifactsError(msg)
    return idedata, image_bytes_by_name


def _walk_artifacts_members(
    tar: tarfile.TarFile,
) -> tuple[dict[str, Any] | None, dict[str, bytes]]:
    """Walk *tar*'s members; return (idedata-or-None, basename → bytes)."""
    idedata: dict[str, Any] | None = None
    image_bytes_by_name: dict[str, bytes] = {}
    # Origin path per basename so the duplicate-basename error
    # message can name both members.
    basename_origin: dict[str, str] = {}
    total_bytes = 0
    for member in tar:
        _check_member_size(member, total_so_far=total_bytes)
        payload = _read_tarball_member(tar, member)
        total_bytes += len(payload)
        if member.name == IDEDATA_MEMBER_NAME:
            idedata = _parse_idedata(payload)
            continue
        if member.name in _METADATA_MEMBERS:
            # storage.json + platformio.ini are materialiser-only.
            continue
        basename = Path(member.name).name
        if basename in image_bytes_by_name:
            msg = (
                f"duplicate basename {basename!r} in artifacts tarball: "
                f"{basename_origin[basename]!r} and {member.name!r}"
            )
            raise UnpackArtifactsError(msg)
        image_bytes_by_name[basename] = payload
        basename_origin[basename] = member.name
    return idedata, image_bytes_by_name


def _relative_or_raise(path: Path, base: Path, *, configuration: str) -> str:
    """Return *path* relative to *base* as a posix string, or raise."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError as err:
        msg = (
            f"firmware_bin_path {path} for {configuration} not under "
            f"build_path {base}; can't include in tarball"
        )
        raise RuntimeError(msg) from err


def _check_member_size(member: tarfile.TarInfo, *, total_so_far: int) -> None:
    """
    Reject a tarball member whose decompressed size would blow the cap.

    Combines a per-member check (``member.size`` exceeds the
    cap on its own) with a cumulative check
    (``member.size + total_so_far`` would push the running
    total past the cap). The receiver-side packer
    (:func:`pack_build_artifacts`) enforces the same ceiling
    on the way out, so a well-formed tarball never trips
    this gate; a peer-controlled / malformed stream that
    declares a multi-GiB member in the tar header bails
    here before :meth:`tarfile.TarFile.extractfile` reads
    a single byte.
    """
    if member.size > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"tarball member {member.name!r} declares size {member.size} "
            f"exceeding FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise UnpackArtifactsError(msg)
    if total_so_far + member.size > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"tarball cumulative size {total_so_far + member.size} "
            f"exceeds FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise UnpackArtifactsError(msg)


def _read_tarball_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    """Read *member*'s bytes.

    Raises :class:`UnpackArtifactsError` on directory entries
    or any other non-regular tarball member type. Stdlib
    ``tarfile`` guarantees ``extractfile()`` returns a
    readable stream iff ``isfile()`` returns ``True`` —
    ``extractfile`` only returns ``None`` for link / device /
    FIFO members, every one of which ``isfile()`` already
    rejects.
    """
    if not member.isfile():
        msg = f"unexpected non-file tarball entry: {member.name!r}"
        raise UnpackArtifactsError(msg)
    return cast(io.BufferedReader, tar.extractfile(member)).read()


def _parse_idedata(payload: bytes) -> dict[str, Any]:
    """Parse *payload* as ``idedata.json``; raise on non-dict / invalid JSON."""
    try:
        parsed = json_loads(payload)
    except ValueError as exc:
        msg = f"idedata.json is not valid JSON: {exc}"
        raise UnpackArtifactsError(msg) from exc
    if not isinstance(parsed, dict):
        msg = "idedata.json is not a JSON object"
        raise UnpackArtifactsError(msg)
    return parsed


def _build_images_response(
    firmware_offset: str,
    idedata: dict[str, Any],
    image_bytes_by_name: dict[str, bytes],
) -> list[dict[str, Any]]:
    """
    Pop bytes from *image_bytes_by_name* in canonical order; base64-encode.

    Order is ``firmware.bin`` first, then every entry from
    ``idedata.extra.flash_images`` in their declared order.
    Mutates *image_bytes_by_name*: only the images the
    manifest names are popped; leftover entries (the
    materialise-locally tarball legitimately ships per-platform
    aux files like ``firmware.elf`` for picotool symbol
    resolution and ``firmware.uf2`` for libretiny/RP2040
    ltchiptool flashing, neither of which is in
    ``idedata.extra.flash_images``) are ignored. The size cap
    in :func:`_check_member_size` already gates total payload,
    so we don't need a "no leftovers" check to reject a
    bloated tarball.
    """
    images: list[dict[str, Any]] = []
    firmware_bytes = image_bytes_by_name.pop("firmware.bin", None)
    if firmware_bytes is None:
        msg = "artifacts tarball missing firmware.bin"
        raise UnpackArtifactsError(msg)
    images.append(_image_entry("firmware.bin", firmware_offset, firmware_bytes))
    # Guard the chained ``.get`` — a non-dict ``extra`` field
    # (``null`` / list / scalar) on a corrupt-but-parseable
    # idedata would otherwise blow up on the second ``.get``
    # with ``AttributeError`` and bypass the
    # :class:`UnpackArtifactsError` mapping. Mirror the
    # :func:`helpers.build_artifacts.load_build_artifacts`
    # stance: treat non-dict as "no extras."
    extra = idedata.get("extra")
    extras_list = extra.get("flash_images") or [] if isinstance(extra, dict) else []
    for entry in extras_list:
        basename, offset = _flash_image_basename_offset(entry)
        image_bytes = image_bytes_by_name.pop(basename, None)
        if image_bytes is None:
            msg = f"artifacts tarball missing flash image {basename!r}"
            raise UnpackArtifactsError(msg)
        images.append(_image_entry(basename, offset, image_bytes))
    return images


def _image_entry(name: str, offset: str, payload: bytes) -> dict[str, Any]:
    """Build one ``images`` list entry: ``{name, offset, size, data_b64}``."""
    return {
        "name": name,
        "offset": offset,
        "size": len(payload),
        "data_b64": base64.b64encode(payload).decode("ascii"),
    }


def _flash_image_basename_offset(entry: object) -> tuple[str, str]:
    """Validate one ``idedata.extra.flash_images`` entry and return ``(basename, offset)``."""
    if not isinstance(entry, dict):
        msg = "idedata.extra.flash_images entry is not an object"
        raise UnpackArtifactsError(msg)
    path_str = entry.get("path")
    offset = entry.get("offset")
    if not isinstance(path_str, str) or not isinstance(offset, str):
        msg = "idedata.extra.flash_images entry missing path/offset"
        raise UnpackArtifactsError(msg)
    return cross_os_basename(path_str), offset


def _rewrite_idedata_paths(idedata: dict[str, Any]) -> dict[str, Any]:
    """
    Return *idedata* with ``extra.flash_images[].path`` replaced by basenames.

    The receiver writes absolute build-dir paths into
    ``idedata.json`` at compile time; those paths are
    meaningless on the offloader. The offloader-side
    consumers look up bytes by basename in the unpacked
    ``images`` list, so the wire-rendered idedata mirrors
    that with basenames in the same field. Returns a
    shallow-copied dict; the caller's input isn't mutated.
    """
    extra = idedata.get("extra")
    if not isinstance(extra, dict):
        return idedata
    flash_images = extra.get("flash_images") or []
    rewritten = [
        {**entry, "path": cross_os_basename(entry["path"])}
        for entry in flash_images
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    return {**idedata, "extra": {**extra, "flash_images": rewritten}}
