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
    # Pins of build servers already driving an in-flight job;
    # excluded from ``eligible`` so a re-pick lands on a *free*
    # server. Empty (the default) leaves every connected peer in play.
    busy_build_server_pins: frozenset[str] = frozenset()
    # Advanced opt-in: when set, a compile that would otherwise WAIT for a
    # busy build server runs on the local lane instead, so the local machine
    # shares the build load. Off by default — single builds still offload.
    include_local_in_pool: bool = False
    # Whether the local compile lane is occupied. Late-bound at dispatch like
    # ``busy_build_server_pins``; the submit-time ``pick_build_path`` snapshot
    # leaves it at the default since it never reads it.
    local_compile_busy: bool = False


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


class DispatchOutcome(StrEnum):
    """What the remote-dispatch pool does with a pending compile.

    ``WAIT`` (a compatible server exists but all are busy → hold)
    is the state ``pick_build_path`` can't express.
    """

    REMOTE = "remote"
    WAIT = "wait"
    LOCAL = "local"
    NO_COMPATIBLE_PEER = "no_compatible_peer"


@dataclass(frozen=True)
class DispatchDecision:
    """Result of :func:`pick_dispatch_target`.

    ``pin_sha256`` is set only for ``REMOTE``; ``message`` carries
    the diagnostic only for ``NO_COMPATIBLE_PEER`` (the dispatcher
    stamps it onto ``job.error``).
    """

    outcome: DispatchOutcome
    pin_sha256: str | None = None
    message: str = ""

    @classmethod
    def remote(cls, pin_sha256: str) -> DispatchDecision:
        """Dispatch now to the free server behind *pin_sha256*."""
        return cls(outcome=DispatchOutcome.REMOTE, pin_sha256=pin_sha256)

    @classmethod
    def wait(cls) -> DispatchDecision:
        """Compatible servers exist but all busy — hold the job pending."""
        return cls(outcome=DispatchOutcome.WAIT)

    @classmethod
    def local(cls) -> DispatchDecision:
        """No compatible server — run on the local lane."""
        return cls(outcome=DispatchOutcome.LOCAL)

    @classmethod
    def no_compatible_peer(cls, message: str) -> DispatchDecision:
        """EXACT_REQUIRED with no compatible server — fail the job."""
        return cls(outcome=DispatchOutcome.NO_COMPATIBLE_PEER, message=message)


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
    pin_sha256 = _pick_free_pin(result, inputs.peer_queue_status)
    if pin_sha256 is not None:
        return BuildPathDecision.remote(pin_sha256)
    if result.intentional > 0 and inputs.version_match_policy is VersionMatchPolicy.EXACT_REQUIRED:
        raise CommandError(
            ErrorCode.NO_COMPATIBLE_PEER, _no_compatible_peer_message(result, inputs)
        )
    return BuildPathDecision.local()


def pick_dispatch_target(inputs: BuildSchedulerInputs) -> DispatchDecision:
    """Decide what the remote-dispatch pool does with a pending compile.

    Like :func:`pick_build_path` but late-bound at dispatch with
    ``busy_build_server_pins`` set, and with the extra ``WAIT``
    outcome for "a compatible server exists but every one is
    busy". ``include_local_in_pool`` turns that busy-server WAIT
    into LOCAL when the local lane is free, so the local machine
    shares the load. ``NO_COMPATIBLE_PEER`` is returned (not
    raised) so the dispatcher can finalise the job FAILED off the
    request path.
    """
    if not inputs.remote_builds_enabled:
        return DispatchDecision.local()
    result = _eligible_pairings(inputs)
    pin_sha256 = _pick_free_pin(result, inputs.peer_queue_status)
    if pin_sha256 is not None:
        return DispatchDecision.remote(pin_sha256)
    if result.busy_eligible > 0:
        # Local shares the load only when opted in and its lane is free;
        # otherwise hold for a server to free.
        local_free = inputs.include_local_in_pool and not inputs.local_compile_busy
        return DispatchDecision.local() if local_free else DispatchDecision.wait()
    if result.intentional > 0 and inputs.version_match_policy is VersionMatchPolicy.EXACT_REQUIRED:
        # EXACT_REQUIRED can't fall back to LOCAL, so don't hard-fail on a
        # transient drop: an intended server that's merely offline may reconnect
        # (a peer-link-open event re-wakes the matcher), so WAIT. Only fail when
        # an intended server is genuinely connected-but-incompatible with none
        # left to wait on — reconnecting wouldn't fix a version mismatch.
        if result.disconnected > 0:
            return DispatchDecision.wait()
        return DispatchDecision.no_compatible_peer(_no_compatible_peer_message(result, inputs))
    return DispatchDecision.local()


@dataclass(frozen=True)
class _FilterResult:
    """Outcome of the per-peer eligibility filter.

    ``intentional`` counts every APPROVED + enabled pairing — including
    ineligible ones — since that's what drives the ``EXACT_REQUIRED``
    hard-fail. The per-reason counts (``version_filtered``,
    ``disconnected``) are diagnostic only. ``eligible`` is the
    ``paired_at``-ordered tuple of dispatchable pins (a ``tuple`` so
    ``frozen=True`` freezes the held membership).
    """

    eligible: tuple[str, ...]
    intentional: int
    version_filtered: int
    disconnected: int
    # Fully-eligible servers excluded only by ``busy_build_server_pins``.
    # Drives the dispatcher's WAIT vs NO_COMPATIBLE_PEER split.
    busy_eligible: int


def _pick_free_pin(
    result: _FilterResult,
    peer_queue_status: Mapping[str, PeerQueueStatusSnapshotEntry],
) -> str | None:
    """Two-tier pick: a free idle server first, else the oldest eligible; ``None`` if none.

    Eligible servers are already filtered (APPROVED + connected +
    version-compatible + not busy) and ``paired_at``-ordered, so the
    fallback ``eligible[0]`` is the oldest.
    """
    for pin_sha256 in result.eligible:
        snapshot = peer_queue_status.get(pin_sha256)
        if snapshot is not None and snapshot["idle"]:
            return pin_sha256
    return result.eligible[0] if result.eligible else None


def _no_compatible_peer_message(result: _FilterResult, inputs: BuildSchedulerInputs) -> str:
    """Build the English-only ``NO_COMPATIBLE_PEER`` diagnostic.

    The frontend keys its localised toast on
    ``ErrorCode.NO_COMPATIBLE_PEER`` and ignores this string; the
    per-reason breakdown is for log analysis when the reproducing
    case isn't version skew (e.g. a transient peer-link drop).
    """
    return (
        f"version policy 'exact_required' with {result.intentional} intended "
        f"peer(s) but none eligible "
        f"({result.version_filtered} on version mismatch, "
        f"{result.disconnected} on closed peer-link; "
        f"offloader={inputs.offloader_esphome_version!r}); "
        f"refusing to fall back to LOCAL"
    )


def _eligible_pairings(inputs: BuildSchedulerInputs) -> _FilterResult:
    """Walk the pairings dict applying the policy filter."""
    ordered = sorted(
        inputs.pairings.items(),
        key=lambda item: (item[1].paired_at, item[0]),
    )
    policy = inputs.version_match_policy
    eligible: list[str] = []
    intentional = 0
    version_filtered = 0
    disconnected = 0
    busy_eligible = 0
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
        if pin_sha256 in inputs.busy_build_server_pins:
            # Compatible and connected, but already driving an
            # in-flight job — eligible once it frees, not now.
            busy_eligible += 1
            continue
        eligible.append(pin_sha256)
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
        busy_eligible=busy_eligible,
    )
