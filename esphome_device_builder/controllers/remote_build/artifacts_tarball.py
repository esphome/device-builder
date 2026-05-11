"""
Pack / unpack the receiver's build-artifact tarball.

The remote-build feature ships compiled firmware between two
dashboards by serialising the receiver's flash-artifact set
(``firmware.bin`` plus the platform's auxiliary images +
``idedata.json``) into a single gzipped tarball. This module
owns the pack + unpack helpers — pure data transforms with no
WS / wire-flow knowledge — so the two end-to-end surfaces that
consume the format (the receiver-side
:class:`ArtifactsDownloadSender` streamer and the offloader-side
``download_artifacts`` WS unpacker / source-routed runner) call
into one place instead of re-implementing the format twice.

Tarball layout (flat — no subdirectories):

.. code-block:: text

    idedata.json
    firmware.bin
    bootloader.bin       (ESP32 / native IDF only)
    partitions.bin       (ESP32 / native IDF only)
    ota_data_initial.bin (ESP32 / native IDF only)

The offsets for each image live inside ``idedata.json``'s
``extra.flash_images`` array (and on the receiver-resolved
``firmware_offset`` for ``firmware.bin`` itself, which upstream
tracks separately from the ``extra`` block). The receiver's
absolute build-dir paths in the manifest are rewritten to
basenames on the offloader side — see
:func:`_rewrite_idedata_paths`.
"""

from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ...helpers.build_artifacts import load_build_artifacts
from ...helpers.json import loads as json_loads
from ...helpers.peer_link_bundle import FIRMWARE_MAX_TOTAL_BYTES

if TYPE_CHECKING:
    from .peer_link_client import DownloadArtifactsResult


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
    """Pack the build's flash artifacts for *configuration* into a gzipped tarball.

    Synchronous; meant to run inside an executor. Calls
    :func:`helpers.build_artifacts.load_build_artifacts` to
    discover the flash-image set + idedata bytes, then packs
    every image (flattened to its basename) +
    ``idedata.json`` into a single gzipped tarball.

    Tarball layout matches the module docstring; flat files
    only. The offloader-side install path needs the bytes +
    the offsets from ``idedata.extra.flash_images`` (plus
    ``firmware.bin``'s offset on the start frame because
    upstream tracks the firmware partition separately).

    Raises :class:`FileNotFoundError` from
    :func:`load_build_artifacts` when the StorageJSON sidecar
    or any required artifact is missing. Raises
    :class:`RuntimeError` on duplicate-basename collision
    (defensive — upstream esphome doesn't emit this shape
    today, but we fail loudly rather than ship a silently-
    truncated set) or when the artifact set exceeds
    :data:`FIRMWARE_MAX_TOTAL_BYTES`. Two cap checks fire:
    the uncompressed walking sum (cheap, lets us short-
    circuit before reading huge files), and a final
    compressed-size check on the rendered tarball (the
    offloader's :class:`BundleAssembler` caps on
    ``ArtifactsStartFrameData.total_bytes``, the wire-side
    length, so the receiver-side ceiling needs to match).
    Compression usually shrinks the payload, so the
    uncompressed gate fires first in practice; the
    post-render check exists for the incompressible-data +
    tar-header-overhead corner where ``len(tarball)`` could
    technically exceed the uncompressed total. The caller
    (:meth:`ArtifactsDownloadSender.handle_download_artifacts`)
    catches both and surfaces a structured reject reason.
    """
    artifacts = load_build_artifacts(configuration)
    buf = io.BytesIO()
    total_uncompressed = len(artifacts.idedata_bytes)
    if total_uncompressed > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"idedata.json for {configuration} ({total_uncompressed} bytes) "
            f"already exceeds FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise RuntimeError(msg)
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # ``idedata.json`` first so the offloader can peek at
        # the manifest before chunking the binaries.
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(artifacts.idedata_bytes)
        tar.addfile(info, io.BytesIO(artifacts.idedata_bytes))

        seen_names: set[str] = set()
        for image in artifacts.flash_images:
            name = image.path.name
            if name in seen_names:
                msg = f"duplicate flash image basename {name!r} in idedata"
                raise RuntimeError(msg)
            seen_names.add(name)
            image_bytes = image.path.read_bytes()
            total_uncompressed += len(image_bytes)
            if total_uncompressed > FIRMWARE_MAX_TOTAL_BYTES:
                msg = (
                    f"build artifacts for {configuration} would exceed "
                    f"FIRMWARE_MAX_TOTAL_BYTES uncompressed "
                    f"({total_uncompressed} > {FIRMWARE_MAX_TOTAL_BYTES})"
                )
                raise RuntimeError(msg)
            info = tarfile.TarInfo(name=name)
            info.size = len(image_bytes)
            tar.addfile(info, io.BytesIO(image_bytes))

    # ``flash_images[0]`` is firmware.bin (load_build_artifacts
    # invariant) — its offset is the value the offloader needs
    # for the start frame, since idedata.json's manifest
    # doesn't include the firmware partition itself.
    tarball = buf.getvalue()
    # Final post-render cap on the wire-side length. The
    # uncompressed-walking gate above already short-circuits
    # the common case, but tar adds 512-byte headers per
    # member and gzip can grow incompressible data by a few
    # percent — for an artifact set that lands right at the
    # limit, ``len(tarball)`` could in principle exceed the
    # uncompressed total even though the body fits. The
    # offloader's ``BundleAssembler`` caps on
    # ``ArtifactsStartFrameData.total_bytes`` (the
    # post-render length), so without this match the receiver
    # could deterministically ship a stream the offloader
    # rejects.
    if len(tarball) > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"build artifacts tarball for {configuration} would exceed "
            f"FIRMWARE_MAX_TOTAL_BYTES on the wire "
            f"({len(tarball)} > {FIRMWARE_MAX_TOTAL_BYTES})"
        )
        raise RuntimeError(msg)
    return PackedArtifacts(tarball=tarball, firmware_offset=artifacts.flash_images[0].offset)


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


def extract_firmware_bin(tarball_bytes: bytes) -> bytes:
    """
    Pull ``firmware.bin`` out of the receiver's gzipped tarball.

    Synchronous; meant to run in an executor — the
    :func:`tarfile.open` + member read are blocking
    syscalls. Used by the source-routed runner's UPLOAD /
    INSTALL branch: the runner only needs the firmware
    image to feed into ``esphome upload --file``, not the
    full idedata + multi-image set the WS unpacker
    returns to a Web Serial / esptool consumer.

    Raises :class:`UnpackArtifactsError` on any structural
    problem (missing entry, non-file member, malformed
    tarball) or when ``firmware.bin``'s declared size exceeds
    :data:`FIRMWARE_MAX_TOTAL_BYTES`. The size gate is a
    decompression-bomb guard: gzip can compress huge
    zero-filled / sparse data to a tiny on-the-wire payload,
    so reading without a header-side bound would let a hostile
    peer expand a few-KiB tarball into multi-GiB memory.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            try:
                member = tar.getmember("firmware.bin")
            except KeyError as exc:
                msg = "firmware.bin missing from receiver's tarball"
                raise UnpackArtifactsError(msg) from exc
            # ``isfile()`` rejects symlinks / hardlinks /
            # device nodes / FIFOs / directories — anything
            # that isn't a plain file entry. Load-bearing
            # against a hostile peer: ``tarfile.extractfile()``
            # follows symlinks + hardlinks transparently and
            # returns a readable stream for them, so reading
            # ``firmware.bin`` without this gate would
            # silently flash whatever the link target resolved
            # to on the receiver's filesystem. Matches
            # :func:`_read_tarball_member`'s gate on the
            # general-purpose unpack path.
            if not member.isfile():
                msg = f"firmware.bin in tarball is not a regular file ({member.type!r})"
                raise UnpackArtifactsError(msg)
            _check_member_size(member, total_so_far=0)
            # ``isfile() == True`` is the stdlib contract for
            # ``extractfile()`` returning a readable stream;
            # the cast is safe because we've already gated on
            # ``isfile()`` above.
            payload = cast(io.BufferedReader, tar.extractfile(member))
            bytes_payload: bytes = payload.read()
            return bytes_payload
    except tarfile.TarError as exc:
        msg = f"malformed tarball: {exc}"
        raise UnpackArtifactsError(msg) from exc


def read_artifacts_tarball(tarball: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """
    Read every member of *tarball* into ``(idedata, files-by-basename)``.

    ``idedata`` is the parsed ``idedata.json`` object;
    ``files-by-basename`` excludes ``idedata.json``. Raises
    :class:`UnpackArtifactsError` on any structural problem
    in the tarball, or when the cumulative decompressed
    payload would exceed :data:`FIRMWARE_MAX_TOTAL_BYTES`
    (decompression-bomb guard — see
    :func:`extract_firmware_bin` for the same rationale on the
    per-member size check).
    """
    idedata: dict[str, Any] | None = None
    image_bytes_by_name: dict[str, bytes] = {}
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar:
                _check_member_size(member, total_so_far=total_bytes)
                payload = _read_tarball_member(tar, member)
                total_bytes += len(payload)
                if member.name == "idedata.json":
                    idedata = _parse_idedata(payload)
                else:
                    image_bytes_by_name[member.name] = payload
    except tarfile.TarError as exc:
        msg = f"artifacts tarball is malformed: {exc}"
        raise UnpackArtifactsError(msg) from exc
    if idedata is None:
        msg = "artifacts tarball missing idedata.json"
        raise UnpackArtifactsError(msg)
    return idedata, image_bytes_by_name


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
    ``idedata.extra.flash_images`` in their declared order
    (matches :attr:`BuildArtifacts.flash_images` on the
    receiver side). Mutates *image_bytes_by_name*: every
    image referenced by the manifest is popped; on return
    the dict should be empty, and any leftover entry means
    the tarball carried a file the manifest didn't account
    for.
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
    if image_bytes_by_name:
        msg = (
            f"artifacts tarball contains unexpected files not referenced by idedata: "
            f"{sorted(image_bytes_by_name)}"
        )
        raise UnpackArtifactsError(msg)
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
    return Path(path_str).name, offset


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
        {**entry, "path": Path(entry["path"]).name}
        for entry in flash_images
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    return {**idedata, "extra": {**extra, "flash_images": rewritten}}
