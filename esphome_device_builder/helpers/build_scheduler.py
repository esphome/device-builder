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
    """Decide whether a firmware job runs LOCAL or on a paired receiver.

    ``EXACT_REQUIRED`` raises ``NO_COMPATIBLE_PEER`` whenever any
    APPROVED + enabled pairing exists and none make it past the
    filter, for any reason — issue #985 was about silent
    LOCAL-fallback, and gating the hard-fail on a single filter
    would leak the same shape through the others.
    ``remote_builds_enabled=False`` wins over the policy (the
    master toggle is "I don't want remote builds"; the policy is
    "how to filter when I do") so EXACT_REQUIRED never raises in
    that state.
    """
    if not inputs.remote_builds_enabled:
        return BuildPathDecision.local()
    result = _eligible_pairings(inputs)
    for pin_sha256, _pairing in result.eligible:
        snapshot = inputs.peer_queue_status.get(pin_sha256)
        if snapshot is not None and snapshot["idle"]:
            return BuildPathDecision.remote(pin_sha256)
    if result.eligible:
        pin_sha256, _pairing = result.eligible[0]
        return BuildPathDecision.remote(pin_sha256)
    if result.intentional > 0 and inputs.version_match_policy is VersionMatchPolicy.EXACT_REQUIRED:
        # English-only diagnostic for logs / e2e tests; the
        # frontend keys its localised toast on
        # ``ErrorCode.NO_COMPATIBLE_PEER`` and ignores this string.
        # The per-reason breakdown is here for log analysis when
        # the operator's reproducing case isn't the version-skew
        # one (e.g. a transient peer-link drop on Home Assistant
        # Green that surfaces the same code).
        msg = (
            f"version policy 'exact_required' with {result.intentional} intended "
            f"peer(s) but none eligible "
            f"({result.version_filtered} on version mismatch, "
            f"{result.disconnected} on closed peer-link; "
            f"offloader={inputs.offloader_esphome_version!r}); "
            f"refusing to fall back to LOCAL"
        )
        raise CommandError(ErrorCode.NO_COMPATIBLE_PEER, msg)
    return BuildPathDecision.local()


@dataclass(frozen=True)
class _FilterResult:
    """Outcome of the per-peer eligibility filter.

    ``intentional`` counts every APPROVED + enabled pairing — including
    ineligible ones — since that's what drives the ``EXACT_REQUIRED``
    hard-fail. The per-reason counts (``version_filtered``,
    ``disconnected``) are diagnostic only. ``eligible`` is a
    ``tuple`` rather than ``list`` so ``frozen=True`` actually
    freezes the held membership.
    """

    eligible: tuple[tuple[str, StoredPairing], ...]
    intentional: int
    version_filtered: int
    disconnected: int


def _eligible_pairings(inputs: BuildSchedulerInputs) -> _FilterResult:
    """Walk the pairings dict applying the policy filter."""
    ordered = sorted(
        inputs.pairings.items(),
        key=lambda item: (item[1].paired_at, item[0]),
    )
    policy = inputs.version_match_policy
    eligible: list[tuple[str, StoredPairing]] = []
    intentional = 0
    version_filtered = 0
    disconnected = 0
    for pin_sha256, pairing in ordered:
        if pairing.status is not PeerStatus.APPROVED or not pairing.enabled:
            continue
        intentional += 1
        if pin_sha256 not in inputs.open_peer_links:
            disconnected += 1
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
            version_filtered += 1
            continue
        eligible.append((pin_sha256, pairing))
    if not eligible and version_filtered and policy is not VersionMatchPolicy.EXACT_REQUIRED:
        _LOGGER.info(
            "pick_build_path: version policy %s filtered %d peer(s); falling back to LOCAL",
            policy.value,
            version_filtered,
        )
    return _FilterResult(
        eligible=tuple(eligible),
        intentional=intentional,
        version_filtered=version_filtered,
        disconnected=disconnected,
    )
