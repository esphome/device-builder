"""
Pick the build path for a firmware job — local or one of the paired remotes.

Phase 7a-1 of issue #106 — the first slice of the transparent
install flow. This module is a pure decision function: it takes
the offloader's current state (paired receivers + per-pairing
peer-link openness + per-pairing queue snapshot) plus the
user's remote-builds toggle, and returns a typed
:class:`BuildPathDecision` telling the caller whether to spawn
a local ``FirmwareJob`` or dispatch to a specific paired
receiver.

The decision function is intentionally side-effect-free: no
controller references, no event-bus interaction, no I/O. The
caller (the ``firmware/install`` WS handler in 7a-3)
gathers the state and threads it in. Two reasons for that shape:

* **Unit-testability.** The candidate-filter rules
  (PENDING vs APPROVED, connected vs not) and the two-tier
  pick (idle preference then queue-on-busy fallback) all
  flow through one function with simple inputs; the test
  suite covers them without standing up the controllers or
  the event bus.
* **Lifetime.** The state the function reads is RAM-canonical
  and lives on :class:`RemoteBuildController` (``_pairings``,
  ``_open_peer_links``, ``_peer_queue_status``). Passing it in
  rather than reaching for the controller keeps the helper
  callable from any future site that's already holding the
  relevant slices, including unit tests that don't have a
  controller wired up.

What this module *does not* do:

* **Version-compat check.** The design doc calls for a
  ``version_compatible(p.esphome_version, local_esphome_version)``
  gate in the candidate filter. ``StoredPairing`` doesn't
  currently carry the receiver's ``esphome_version`` — it's
  available in mDNS TXT for discovered peers but doesn't flow
  through the pair-time handshake into the persisted row. Wiring
  that needs a separate piece of work; the placeholder is
  documented inline at the filter site so the gate's home is
  obvious when the value lands. Until then, every connected
  APPROVED pairing is a candidate regardless of receiver
  version.
* **Load-balancing across multiple candidates.** Pick policy
  is two-tier: first pass walks eligible pairings sorted by
  oldest ``paired_at`` and picks the first one whose
  ``queue_status`` snapshot reports ``idle=True``; second
  pass picks the oldest eligible pairing regardless of idle
  state. Concurrent installs fan out across idle remotes
  (first pass), and when every paired remote is busy the
  oldest queues a job behind whatever's running (second
  pass) rather than silently falling back to LOCAL.
  Round-robin / least-loaded / cache-hot-affinity are
  later-iteration concerns; the design doc explicitly puts
  richer picking policy beyond this first cut.
* **Caller integration.** The ``firmware/install`` WS handler
  route-through lands in 7a-3 alongside the event-stream
  bridge that re-fires ``OFFLOADER_JOB_*`` as local-shaped
  ``JOB_*``. Today this decision is unused — landing it as a
  standalone helper keeps the unit-test layer reviewable
  ahead of the integration's larger blast radius.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..models.remote_build import (
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)


class BuildPath(StrEnum):
    """Where the bytes for a firmware build come from.

    ``LOCAL`` runs the existing in-process ``esphome run``
    pipeline on the offloader. ``REMOTE`` dispatches to a
    paired receiver via ``remote_build/submit_job`` and pulls
    the resulting artifact set back through 6a's
    ``download_artifacts`` round-trip.

    StrEnum (not :class:`enum.Enum`) so the value flows
    through JSON / log strings unchanged — the transparent
    install flow's eventual ``FirmwareJob.source`` field
    carries this discriminator on the wire, and a string
    avoids a custom encoder.
    """

    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True)
class BuildSchedulerInputs:
    """Snapshot view of the scheduler's input state.

    Bundles the four pieces :func:`pick_build_path` reads into
    one immutable value so the helper's signature can't be
    misused with raw controller-owned dicts that another task
    might mutate mid-iteration. The caller — eventually
    :class:`RemoteBuildController` in 7a-3 — is responsible for
    handing in a *snapshot*: today the natural call site is on
    the same event loop as the controller's mutations so a
    shallow ``dict(...)`` / ``frozenset(...)`` of each field
    suffices, but the contract is "consistent-read for the
    lifetime of this call".

    Field types are :class:`Mapping` and :class:`frozenset`
    rather than ``dict`` / ``set`` so mypy rejects mutation
    attempts at the type layer; combined with
    ``@dataclass(frozen=True)`` on the outer wrapper this gives
    the helper an immutable view without forcing the caller to
    deep-copy every nested :class:`StoredPairing`.

    Construction is intentionally explicit (no keyword-passing
    of the four slices through ``pick_build_path``) — making the
    snapshot a discrete step signals to the reader that the
    helper is reading a consistent view rather than poking at
    controller state through indirection.
    """

    remote_builds_enabled: bool
    #: Paired receivers keyed on ``pin_sha256``. Typed as
    #: :class:`Mapping` rather than ``dict`` so mypy rejects
    #: mutation; the scheduler iterates this without relying on
    #: dict insertion order (an explicit ``paired_at`` sort
    #: inside :func:`pick_build_path` produces a deterministic
    #: pick regardless of how the caller's ``Mapping`` impl
    #: orders its keys).
    pairings: Mapping[str, StoredPairing]
    open_peer_links: frozenset[str]
    peer_queue_status: Mapping[str, PeerQueueStatusSnapshotEntry]


@dataclass(frozen=True)
class BuildPathDecision:
    """Result of :func:`pick_build_path`.

    ``path`` is the discriminator. ``pin_sha256`` is set iff
    ``path == BuildPath.REMOTE`` — the pin identifies which
    paired receiver the caller dispatches to. A
    ``BuildPath.LOCAL`` decision sets ``pin_sha256`` to
    ``None`` so the type system requires every consumer to
    narrow before reading the value. An empty-string sentinel
    would have looked cleaner at call sites but would let a
    forgotten ``path == REMOTE`` guard pass a meaningless
    ``""`` to downstream pin validators (`_validate_pin_sha256`
    rejects it as "must be 64 lowercase-hex characters", an
    error that's hard to trace back to the missing guard).
    Forcing narrowing keeps the misuse impossible to
    construct.

    Frozen so callers can stash a decision and reuse it
    across the dispatch + bridge wiring without worrying about
    accidental field mutation downstream.
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

    Pure function — no controller references, no I/O. Designed
    to be called from the ``firmware/install`` WS handler once
    7a-3's integration lands; testable today without standing
    up the controllers.

    The candidate filter walks ``inputs.pairings`` sorted by
    :attr:`StoredPairing.paired_at` ascending (oldest pairing
    first, deterministic on a tie) and picks the first entry
    that satisfies every gate. The explicit sort keeps the
    decision stable regardless of how the caller's
    :class:`Mapping` impl orders keys — relying on
    ``dict``'s insertion-order quirk would let a future
    refactor that swapped in a different mapping type
    silently flip the chosen receiver.

    * **Approved status.** Only ``PeerStatus.APPROVED`` rows are
      eligible. PENDING rows haven't been OOB-confirmed by the
      receiver yet; using them silently would route bytes to a
      not-yet-trusted peer.
    * **Per-pairing enabled toggle.** :attr:`StoredPairing.enabled`
      gates the row independently of status / connection (7b).
      The operator may have a paired receiver they want to keep
      reachable via the Send-builds power-user surface but
      *not* receive transparent install routing — e.g. a build
      server on a flaky link they don't want eating dashboard
      installs every time it briefly looks idle. A disabled
      row is skipped; the peer-link client stays open.
    * **Live peer-link session.** ``pin_sha256`` must be in
      ``inputs.open_peer_links`` — the RAM-canonical set the
      :class:`RemoteBuildController` maintains from
      ``OFFLOADER_PEER_LINK_OPENED`` / ``_CLOSED`` events. An
      APPROVED pairing whose session is reconnecting (or
      orphaned via ``pin_mismatch`` / ``superseded``) doesn't
      qualify and falls through to LOCAL when no other
      eligible pairing exists.

    The pick is **two-tier**:

    1. **First pass — idle preference.** Walk pairings in
       oldest-``paired_at`` order; pick the first APPROVED +
       connected pairing whose ``queue_status`` snapshot
       reports ``idle=True``. This fans new installs out
       across multiple idle remotes (request 1 lands on
       remote A, A's snapshot flips to running, request 2
       lands on the next idle remote B) so the fleet's
       compile capacity is actually used.
    2. **Second pass — busy fallback.** If no idle candidate
       qualified, walk again and pick the first
       APPROVED + connected pairing regardless of queue
       state (busy or missing snapshot). The receiver runs
       its own firmware queue, so the dispatch lands behind
       whatever's currently building and runs when the
       queue drains. Silent fallback to LOCAL here used to
       split the fleet across two compile contexts (warm
       receiver toolchain vs cold local) and re-flash from
       a different build than the first Install — confusing
       and surprising for the user who didn't pick a build
       location.

    Falls back to LOCAL only when **no** APPROVED + connected
    pairing exists. A future per-install "Force local"
    override link in the install dialog is the user-facing
    way to opt out when the scheduler picks REMOTE.

    The status gate uses ``is PeerStatus.APPROVED`` rather than
    a negative match against the current PENDING enum value, so
    any future :class:`PeerStatus` member (e.g. a hypothetical
    ``QUARANTINED`` state) is silent-fallback-LOCAL until the
    scheduler is explicitly taught about it. Fail-closed by
    construction — widening the eligible set is a deliberate
    code change, not an accidental side effect of an enum
    addition.

    ``inputs.remote_builds_enabled`` is the user-facing master
    switch (the 7b Settings toggle that lands alongside the
    transparent install flow). When ``False``, the function
    short-circuits to ``BuildPath.LOCAL`` without walking
    pairings — every install runs locally regardless of how
    many idle receivers are paired. The design doc routes that
    gate through the scheduler rather than the install handler
    so a future "force local for next install only" affordance
    can flip the bit transiently without re-walking every call
    site.

    Returns :class:`BuildPathDecision.local` when no candidate
    qualifies. The integration in 7a-3 treats LOCAL as the
    default (silent-fallback semantic from the design doc);
    callers don't surface a "couldn't find a remote" UI
    message because the user didn't pick "remote" — they
    picked Install, and the scheduler routes transparently.
    """
    if not inputs.remote_builds_enabled:
        return BuildPathDecision.local()
    ordered = sorted(
        inputs.pairings.items(),
        key=lambda item: (item[1].paired_at, item[0]),
    )
    # Collect eligible pairings once so both passes walk the
    # same filtered set. Filtering here (rather than inside
    # each pass) keeps the eligible-set definition in one
    # place — a future PeerStatus addition or peer-link gate
    # added below lands once. ``pairing.enabled`` is the 7b
    # per-pairing toggle: operator wants this receiver paired
    # (Send-builds power-user surface still works) but doesn't
    # want transparent install to route here.
    eligible: list[tuple[str, StoredPairing]] = [
        (pin_sha256, pairing)
        for pin_sha256, pairing in ordered
        if pairing.status is PeerStatus.APPROVED
        and pairing.enabled
        and pin_sha256 in inputs.open_peer_links
    ]
    # First pass: pick the oldest idle pairing so multiple
    # concurrent installs fan out across all idle remotes
    # before any of them queue.
    for pin_sha256, _pairing in eligible:
        snapshot = inputs.peer_queue_status.get(pin_sha256)
        if snapshot is not None and snapshot["idle"]:
            return BuildPathDecision.remote(pin_sha256)
    # Second pass: no idle candidate; queue on the oldest
    # busy receiver rather than falling back to LOCAL.
    if eligible:
        pin_sha256, _pairing = eligible[0]
        return BuildPathDecision.remote(pin_sha256)
    return BuildPathDecision.local()
