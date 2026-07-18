"""Read-side primitives shared by the remote-build tarball consumers."""

from __future__ import annotations

import tarfile
from typing import Any

from .json import loads as json_loads
from .peer_link_bundle import FIRMWARE_MAX_TOTAL_BYTES


def check_member_size(
    member: tarfile.TarInfo, *, total_so_far: int, error_cls: type[Exception]
) -> None:
    """
    Reject a tarball member whose declared size would blow the cap.

    Combines a per-member check with a cumulative check against
    ``total_so_far``. Decompression-bomb guard: a hostile stream that
    declares a multi-GiB member (or N just-under-cap members) bails
    here before ``extractfile`` reads a single byte.
    """
    if member.size > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"tarball member {member.name!r} declares size {member.size} "
            f"exceeding FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise error_cls(msg)
    if total_so_far + member.size > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"tarball cumulative size {total_so_far + member.size} "
            f"exceeds FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise error_cls(msg)


def read_member(
    tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    *,
    total_so_far: int,
    error_cls: type[Exception],
) -> tuple[bytes, int]:
    """
    Read *member*'s bytes; returns ``(payload, new running total)``.

    Rejects non-regular members (directories, links, devices) and any
    member :func:`check_member_size` refuses. The ``extractfile`` None
    branch is defence only — ``isfile()`` already gates every member
    type it returns None for.
    """
    if not member.isfile():
        raise error_cls(f"tarball member {member.name!r} is not a regular file")
    check_member_size(member, total_so_far=total_so_far, error_cls=error_cls)
    stream = tar.extractfile(member)
    if stream is None:
        raise error_cls(f"tarball member {member.name!r} unreadable")
    payload = stream.read()
    return payload, total_so_far + len(payload)


def parse_json_object(payload: bytes, *, label: str, error_cls: type[Exception]) -> dict[str, Any]:
    """Parse *payload* as a JSON object; raise *error_cls* on invalid / non-dict."""
    try:
        parsed = json_loads(payload)
    except ValueError as err:
        raise error_cls(f"{label} is not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise error_cls(f"{label} is not a JSON object")
    return parsed
