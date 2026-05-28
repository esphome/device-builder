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

from ..models.api import ErrorCode
from ..models.remote_build import (
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)
from .api import CommandError
from .version_compat import VersionMatchPolicy, version_satisfies_policy

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
    version_match_policy: VersionMatchPolicy = VersionMatchPolicy.ANY


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
    an open peer-link session + survive the version-match policy
    filter against the offloader's own ``esphome_version``. The
    pick is two-tier: idle remotes first (so concurrent installs
    fan out), then the oldest eligible pairing regardless of
    idle state (the receiver queues behind its current build
    rather than splitting the install across two compile
    contexts). Sort key ``(paired_at, pin_sha256)`` keeps the
    pick deterministic across Mapping orderings.

    LOCAL fallback fires when no candidate survives the filter
    OR when ``remote_builds_enabled`` is ``False``. The one
    exception is ``VersionMatchPolicy.EXACT_REQUIRED``: when
    that policy filters every peer out, this helper raises
    ``CommandError(NO_COMPATIBLE_PEER)`` instead of falling
    through, so the install surfaces the policy violation to
    the operator instead of masking it with a slow LOCAL
    compile.

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
    policy = inputs.version_match_policy
    eligible: list[tuple[str, StoredPairing]] = []
    version_filtered: list[str] = []
    for pin_sha256, pairing in ordered:
        if (
            pairing.status is not PeerStatus.APPROVED
            or not pairing.enabled
            or pin_sha256 not in inputs.open_peer_links
        ):
            continue
        if not version_satisfies_policy(
            inputs.offloader_esphome_version, pairing.esphome_version, policy
        ):
            _LOGGER.debug(
                "pick_build_path: filtered %s on version policy %s (peer=%s, offloader=%s)",
                pin_sha256,
                policy.value,
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
    if version_filtered and policy is VersionMatchPolicy.EXACT_REQUIRED:
        msg = (
            f"version policy 'exact_required' filtered every paired peer "
            f"(offloader={inputs.offloader_esphome_version!r}); refusing to "
            f"fall back to LOCAL"
        )
        raise CommandError(ErrorCode.NO_COMPATIBLE_PEER, msg)
    if version_filtered:
        _LOGGER.info(
            "pick_build_path: version policy %s filtered %d peer(s); falling back to LOCAL",
            policy.value,
            len(version_filtered),
        )
    return BuildPathDecision.local()
