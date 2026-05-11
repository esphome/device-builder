"""
Tests for :mod:`helpers.build_scheduler`'s :func:`pick_build_path` decision.

Phase 7a-1 of issue #106 — the first slice of the transparent
install flow. The function is pure; tests pin the candidate-
filter rules (master-switch / APPROVED / open-peer-link / idle)
without standing up the remote-build controller.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.build_scheduler import (
    BuildPath,
    BuildPathDecision,
    BuildSchedulerInputs,
    pick_build_path,
)
from esphome_device_builder.models.remote_build import (
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)


def _stub_pairing(
    *,
    pin_sha256: str = "a" * 64,
    receiver_hostname: str = "build.local",
    receiver_port: int = 6055,
    label: str = "desktop",
    paired_at: float = 1.0,
    status: PeerStatus = PeerStatus.APPROVED,
    enabled: bool = True,
) -> StoredPairing:
    """Build a :class:`StoredPairing` with defaults aimed at the scheduler tests.

    Defaults to APPROVED + enabled because the scheduler's
    interesting cases all start from "this pairing would be
    eligible if it cleared the rest of the filter";
    PENDING-rejection / disabled are one test each, not the
    baseline.
    """
    return StoredPairing(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        pin_sha256=pin_sha256,
        static_x25519_pub=b"\x00" * 32,
        label=label,
        paired_at=paired_at,
        status=status,
        enabled=enabled,
    )


def _stub_queue_status(
    *,
    pin_sha256: str,
    idle: bool = True,
    running: bool = False,
    queue_depth: int = 0,
    receiver_hostname: str = "build.local",
    receiver_port: int = 6055,
) -> PeerQueueStatusSnapshotEntry:
    """Build a :class:`PeerQueueStatusSnapshotEntry` for the scheduler tests."""
    return PeerQueueStatusSnapshotEntry(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        pin_sha256=pin_sha256,
        idle=idle,
        running=running,
        queue_depth=queue_depth,
    )


def _inputs(
    *,
    remote_builds_enabled: bool = True,
    pairings: dict[str, StoredPairing] | None = None,
    open_peer_links: set[str] | None = None,
    peer_queue_status: dict[str, PeerQueueStatusSnapshotEntry] | None = None,
) -> BuildSchedulerInputs:
    """Build :class:`BuildSchedulerInputs` with the test's slices.

    Wraps the four-field construction so each test reads as
    "set up some state, call pick_build_path, assert the
    decision" rather than re-typing the snapshot-view dance.
    Converts ``set`` to ``frozenset`` and ``dict`` to a
    read-through ``Mapping`` at the boundary so tests don't
    have to think about the immutability discipline.
    """
    return BuildSchedulerInputs(
        remote_builds_enabled=remote_builds_enabled,
        pairings=pairings or {},
        open_peer_links=frozenset(open_peer_links or set()),
        peer_queue_status=peer_queue_status or {},
    )


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------


def test_master_switch_off_returns_local_even_with_idle_remote() -> None:
    """``remote_builds_enabled=False`` short-circuits to LOCAL.

    Pins the user-toggle gate that the future 7b Settings UI
    exposes. With the switch off, every install routes locally
    regardless of how many idle receivers are connected — the
    scheduler doesn't even walk the pairings dict.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            remote_builds_enabled=False,
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            open_peer_links={pin},
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.local()


# ---------------------------------------------------------------------------
# No candidates → LOCAL
# ---------------------------------------------------------------------------


def test_empty_pairings_returns_local() -> None:
    """No paired receivers at all → silent fallback to LOCAL."""
    decision = pick_build_path(_inputs())
    assert decision.path is BuildPath.LOCAL
    assert decision.pin_sha256 is None


def test_pending_pairing_skipped() -> None:
    """A PENDING pairing is not eligible — only APPROVED rows route remote."""
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin, status=PeerStatus.PENDING)},
            open_peer_links={pin},
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.local()


@pytest.mark.parametrize(
    "status",
    [s for s in PeerStatus if s is not PeerStatus.APPROVED],
)
def test_every_non_approved_status_is_ineligible(status: PeerStatus) -> None:
    """Every non-APPROVED :class:`PeerStatus` member is skipped.

    Fail-closed-by-construction contract: the scheduler gates on
    ``is PeerStatus.APPROVED`` rather than blocklisting known
    not-trusted values. A future enum addition (e.g.
    a hypothetical ``QUARANTINED`` state) is silent-fallback-
    LOCAL until the scheduler is explicitly taught about it.
    Iterating the enum here means adding a new member without
    revisiting the scheduler trips this test rather than
    silently routing bytes to a freshly-defined-and-untested
    peer state.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin, status=status)},
            open_peer_links={pin},
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.local()


def test_approved_but_session_not_open_skipped() -> None:
    """An APPROVED pairing whose peer-link session is closed → not eligible."""
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            # No entry in open_peer_links → session closed / reconnecting.
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.local()


def test_approved_open_busy_still_returns_remote() -> None:
    """
    A connected-but-busy receiver still wins via the second pass.

    Single eligible pairing, snapshot reports
    ``idle=False`` → first-pass idle preference finds no
    candidate, second pass picks the same pairing and
    queues the dispatch behind whatever's currently
    building. Pre-policy-change the scheduler fell back to
    LOCAL here, which split the fleet across two compile
    contexts (warm receiver toolchain vs cold local) and
    re-flashed from a different build than the first
    Install — confusing and surprising for a user who
    didn't pick a build location. A future per-install
    "Force local" override link in the install dialog is
    the user-facing opt-out.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            open_peer_links={pin},
            peer_queue_status={
                pin: _stub_queue_status(pin_sha256=pin, idle=False, running=True),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin)


def test_approved_open_missing_queue_snapshot_still_returns_remote() -> None:
    """
    No queue snapshot for the pairing yet → REMOTE via the second pass.

    The first 5b ``queue_status`` push fires immediately
    on session open, but there's a tiny window between
    ``OFFLOADER_PEER_LINK_OPENED`` and the first snapshot
    arriving. During that window the first-pass idle
    preference can't qualify the pairing (no explicit
    ``idle=True`` to read), but the second pass treats it
    as eligible-with-unknown-state and queues there.
    Pre-policy-change the scheduler treated the unknown
    window as busy and fell back to LOCAL; now the
    receiver's queue absorbs the dispatch regardless of
    snapshot state, so the window stops affecting routing.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            open_peer_links={pin},
            # No queue snapshot received yet.
        )
    )
    assert decision == BuildPathDecision.remote(pin)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_approved_open_idle_returns_remote_for_that_pin() -> None:
    """Single eligible pairing → REMOTE with that pin."""
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            open_peer_links={pin},
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.remote(pin)


# ---------------------------------------------------------------------------
# Multi-candidate pick policy
# ---------------------------------------------------------------------------


def test_picks_oldest_eligible_pairing() -> None:
    """Oldest ``paired_at`` connected + idle APPROVED pairing wins.

    The design doc explicitly leaves a richer pick policy
    (round-robin / least-loaded / cache-hot affinity) to a
    7a-3+ iteration. For now the scheduler picks by
    ``paired_at`` ascending so the oldest trusted receiver
    handles the first dispatch — deterministic across
    ``Mapping`` impls (see
    :func:`test_picks_oldest_paired_first_regardless_of_dict_order`
    for the ordering-doesn't-match-insertion case).
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    pin_c = "c" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
                pin_c: _stub_pairing(pin_sha256=pin_c, paired_at=3.0),
            },
            open_peer_links={pin_a, pin_b, pin_c},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
                pin_c: _stub_queue_status(pin_sha256=pin_c),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_a)


def test_skips_disconnected_picks_next() -> None:
    """A disconnected first candidate falls through to the next."""
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
            },
            open_peer_links={pin_b},  # only B's session is live
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_b)


def test_disabled_pairing_skipped() -> None:
    """An ``enabled=False`` row is skipped even when otherwise eligible.

    7b per-pairing toggle: the operator wants this receiver
    paired (peer-link clients keep their sessions open and
    Send-builds manual dispatch still works) but doesn't want
    transparent install to route here. The scheduler skips
    the row the same way it skips PENDING / disconnected /
    busy candidates — silently, falling through to the next
    eligible pairing.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin, enabled=False)},
            open_peer_links={pin},
            peer_queue_status={pin: _stub_queue_status(pin_sha256=pin)},
        )
    )
    assert decision == BuildPathDecision.local()


def test_skips_disabled_picks_next_enabled() -> None:
    """A disabled first row falls through; second enabled row wins.

    Pins the loop-continuation behaviour for the per-pairing
    toggle so a disabled receiver doesn't shadow every
    later-paired enabled one.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, enabled=False),
                pin_b: _stub_pairing(pin_sha256=pin_b, enabled=True),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_b)


def test_skips_pending_picks_next_approved() -> None:
    """A PENDING first row falls through; second APPROVED row wins."""
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, status=PeerStatus.PENDING),
                pin_b: _stub_pairing(pin_sha256=pin_b, status=PeerStatus.APPROVED),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_b)


def test_all_candidates_busy_picks_oldest_to_queue_remote() -> None:
    """
    Every paired receiver is busy → REMOTE on oldest paired_at.

    Pins the two-tier policy: when no idle candidate
    qualifies the second pass picks the oldest connected
    APPROVED pairing and queues the dispatch behind whatever's
    currently building. Receiver-side firmware queues drain
    the backlog; silent fallback to LOCAL here used to split
    the fleet across two compile contexts (warm receiver
    toolchain vs cold local) and re-flash from a different
    build than the first Install — confusing for the user
    who didn't pick a build location.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a, idle=False, running=True),
                pin_b: _stub_queue_status(pin_sha256=pin_b, idle=False, running=True),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_a)


def test_no_idle_candidate_with_some_missing_snapshots_picks_oldest_to_queue() -> None:
    """
    Missing snapshot + busy snapshot → REMOTE on oldest paired_at.

    The first-pass idle preference requires an explicit
    ``idle=True`` snapshot. A pairing whose snapshot hasn't
    arrived yet (just-connected window) is *not* treated as
    idle by the first pass. The second pass then queues on
    the oldest pairing regardless. Pins that "snapshot
    missing" doesn't crash or short-circuit to LOCAL — it
    just routes through the second-pass queue-anyway branch.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                # A has no snapshot at all; B is busy. First
                # pass finds neither idle; second pass picks
                # the oldest (A).
                pin_b: _stub_queue_status(pin_sha256=pin_b, idle=False, running=True),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_a)


def test_first_busy_oldest_then_idle_younger_prefers_idle() -> None:
    """
    Idle younger pairing beats busy oldest in the first pass.

    Pins the fan-out-on-idle goal: given a busy oldest A and
    an idle younger B, the first pass picks B so concurrent
    installs spread across the idle remotes before any of
    them queue. The "second pass" (busy fallback) only fires
    when *no* idle candidate exists.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a, idle=False, running=True),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_b)


def test_all_candidates_disconnected_returns_local() -> None:
    """Every paired receiver's peer-link session is closed → LOCAL.

    Same exhaustion contract as ``test_all_candidates_busy``,
    but for the open-peer-link gate. Two APPROVED pairings,
    neither in ``open_peer_links`` (both mid-reconnect) →
    LOCAL, not arbitrary tiebreaker among unconnected pins.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
            },
            # Both sessions closed.
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.local()


def test_picks_oldest_paired_first_regardless_of_dict_order() -> None:
    """Pick order is by ``paired_at`` ascending, not by ``Mapping`` iteration order.

    Pins the explicit-sort contract: a caller that hands in
    a ``Mapping`` whose iteration order doesn't match
    ``paired_at`` (e.g. a future ``dict[str, StoredPairing]``
    built from a deserialise-then-update churn that inserts
    a newer pairing first) still gets the oldest pairing
    picked. Without the sort, the scheduler would silently
    flip the chosen receiver across refactors that change
    the caller's insertion sequence.
    """
    pin_a = "a" * 64  # oldest, deserves to win
    pin_b = "b" * 64
    pin_c = "c" * 64
    decision = pick_build_path(
        _inputs(
            # Inserted in c, b, a order — opposite of paired_at.
            pairings={
                pin_c: _stub_pairing(pin_sha256=pin_c, paired_at=3.0),
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=2.0),
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
            },
            open_peer_links={pin_a, pin_b, pin_c},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
                pin_c: _stub_queue_status(pin_sha256=pin_c),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_a)


def test_paired_at_tie_broken_by_pin_sort() -> None:
    """Two pairings with identical ``paired_at`` deterministically pick by pin sort.

    Pins the secondary sort key: when ``paired_at`` ties
    (clock resolution / fixture defaults / two pairings
    accepted in the same tick), the lower-sorted
    ``pin_sha256`` wins. Without a tiebreaker the choice would
    depend on the ``Mapping`` impl's iteration order — exactly
    the non-determinism the explicit sort is designed to
    remove.
    """
    pin_a = "a" * 64
    pin_b = "b" * 64
    decision = pick_build_path(
        _inputs(
            pairings={
                # Insert B before A so iteration order would
                # have picked B without the secondary sort.
                pin_b: _stub_pairing(pin_sha256=pin_b, paired_at=1.0),
                pin_a: _stub_pairing(pin_sha256=pin_a, paired_at=1.0),
            },
            open_peer_links={pin_a, pin_b},
            peer_queue_status={
                pin_a: _stub_queue_status(pin_sha256=pin_a),
                pin_b: _stub_queue_status(pin_sha256=pin_b),
            },
        )
    )
    assert decision == BuildPathDecision.remote(pin_a)


# ---------------------------------------------------------------------------
# BuildPathDecision shape
# ---------------------------------------------------------------------------


def test_local_decision_has_no_pin() -> None:
    """``BuildPathDecision.local()`` carries ``pin_sha256=None``.

    Pins the type-system narrowing contract: ``str | None``
    forces every consumer of ``decision.pin_sha256`` to
    narrow against ``None`` before reading the value, which
    is what prevents a forgotten ``path == REMOTE`` guard
    from silently passing a meaningless empty string to a
    downstream pin validator. The earlier shape used
    ``pin_sha256: str = ""`` for a "uniform" call site — the
    uniformity made the misuse impossible to spot until the
    validator's "not 64 hex chars" error fired far downstream.
    """
    decision = BuildPathDecision.local()
    assert decision.path is BuildPath.LOCAL
    assert decision.pin_sha256 is None


def test_remote_decision_carries_pin() -> None:
    """``BuildPathDecision.remote(pin)`` round-trips the pin verbatim."""
    decision = BuildPathDecision.remote("f" * 64)
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == "f" * 64


def test_decision_is_frozen() -> None:
    """Decisions are immutable so callers can stash + reuse without copy."""
    decision = BuildPathDecision.remote("a" * 64)
    with pytest.raises(Exception, match="cannot assign to field"):
        decision.pin_sha256 = "b" * 64  # type: ignore[misc]
