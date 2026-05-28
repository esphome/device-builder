"""
Pick the build path for a firmware job — local or one of the paired remotes.

Pure decision function: takes a snapshot of the offloader's
pairings + per-pairing connection state + queue snapshots and
returns a typed :class:`BuildPathDecision` telling the caller
whether to spawn a local ``FirmwareJob`` or dispatch to a paired
receiver. No controller refs, no I/O — the
``firmware/install`` WS handler gathers the state and threads
it in. :func:`pick_build_path` itself documents the eligibility
filter + two-tier idle / busy pick.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..models.remote_build import (
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)
from .version_compat import major_versions_match

_LOGGER = logging.getLogger(__name__)


class BuildPath(StrEnum):
    """
    Where the bytes for a firmware build come from.

    StrEnum so the value flows through JSON / log strings
    unchanged; mirrors :class:`JobSource`'s wire values
    (``"local"`` / ``"remote"``) so a future migration to a
    single shared enum is a rename, not a value change.
    """

    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True)
class BuildSchedulerInputs:
    """
    Immutable snapshot view :func:`pick_build_path` reads.

    :class:`Mapping` / :class:`frozenset` types so mypy rejects
    mutation; combined with ``frozen=True`` this gives the
    helper an immutable view without forcing the caller to
    deep-copy every nested :class:`StoredPairing`.
    """

    remote_builds_enabled: bool
    pairings: Mapping[str, StoredPairing]
    open_peer_links: frozenset[str]
    peer_queue_status: Mapping[str, PeerQueueStatusSnapshotEntry]
    # Passed in rather than imported so the helper stays pure;
    # empty string disables the gate.
    offloader_esphome_version: str = ""
    # Master toggle for the major-version gate; ``True``
    # bypasses the gate entirely.
    allow_major_version_mismatch: bool = True


@dataclass(frozen=True)
class BuildPathDecision:
    """
    Result of :func:`pick_build_path`.

    ``pin_sha256`` is ``None`` when ``path == BuildPath.LOCAL``
    and the receiver's pin when ``path == BuildPath.REMOTE``.
    Encoded as ``None`` (not ``""``) so consumers must narrow
    before reading the pin — a forgotten guard tripping a pin
    validator surfaces as a clearer error.
    """

    path: BuildPath
    pin_sha256: str | None

    @classmethod
    def local(cls) -> BuildPathDecision:
        """Build :class:`BuildPathDecision` for ``LOCAL`` (no pin)."""
        return cls(path=BuildPath.LOCAL, pin_sha256=None)

    @classmethod
    def remote(cls, pin_sha256: str) -> BuildPathDecision:
        """Build :class:`BuildPathDecision` for ``REMOTE(pin_sha256)``."""
        return cls(path=BuildPath.REMOTE, pin_sha256=pin_sha256)


def pick_build_path(inputs: BuildSchedulerInputs) -> BuildPathDecision:
    """Decide whether a firmware job runs locally or on a paired receiver.

    Eligible pairings are APPROVED + per-pairing-enabled + have
    an open peer-link session + pass the major-version gate
    (matching ``YYYY.MM`` release line, or the master gate is
    off). The pick is two-tier:

    1. First pass picks the oldest idle eligible pairing so
       concurrent installs fan out across idle remotes.
    2. Second pass picks the oldest eligible pairing
       regardless of idle state — the receiver queues the
       dispatch behind its current build rather than the
       scheduler silently falling back to LOCAL (which would
       split the install across two compile contexts and
       confuse the user).

    Sort is on ``(paired_at, pin_sha256)`` so the chosen
    receiver is deterministic regardless of how the caller's
    :class:`Mapping` orders keys.

    Falls back to LOCAL only when no candidate qualifies, or
    when ``remote_builds_enabled`` is ``False`` (the master
    Settings toggle short-circuits before the walk).

    The status gate is ``is PeerStatus.APPROVED`` — any future
    enum member is silent-fallback-LOCAL until the scheduler
    is explicitly taught about it.
    """
    if not inputs.remote_builds_enabled:
        return BuildPathDecision.local()
    ordered = sorted(
        inputs.pairings.items(),
        key=lambda item: (item[1].paired_at, item[0]),
    )
    eligible: list[tuple[str, StoredPairing]] = []
    version_filtered: list[str] = []
    for pin_sha256, pairing in ordered:
        if (
            pairing.status is not PeerStatus.APPROVED
            or not pairing.enabled
            or pin_sha256 not in inputs.open_peer_links
        ):
            continue
        if not inputs.allow_major_version_mismatch and not major_versions_match(
            inputs.offloader_esphome_version, pairing.esphome_version
        ):
            _LOGGER.debug(
                "pick_build_path: filtered %s on version mismatch (peer=%s, offloader=%s)",
                pin_sha256,
                pairing.esphome_version,
                inputs.offloader_esphome_version,
            )
            version_filtered.append(pin_sha256)
            continue
        eligible.append((pin_sha256, pairing))
    for pin_sha256, _pairing in eligible:
        snapshot = inputs.peer_queue_status.get(pin_sha256)
        if snapshot is not None and snapshot["idle"]:
            return BuildPathDecision.remote(pin_sha256)
    if eligible:
        pin_sha256, _pairing = eligible[0]
        return BuildPathDecision.remote(pin_sha256)
    if version_filtered:
        _LOGGER.info(
            "pick_build_path: strict version gate filtered %d peer(s); falling back to LOCAL",
            len(version_filtered),
        )
    return BuildPathDecision.local()
