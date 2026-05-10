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
) -> StoredPairing:
    """Build a :class:`StoredPairing` with defaults aimed at the scheduler tests.

    Defaults to APPROVED because the scheduler's interesting
    cases all start from "this pairing would be eligible if it
    cleared the rest of the filter"; PENDING-rejection is one
    test, not the baseline.
    """
    return StoredPairing(
        receiver_hostname=receiver_hostname,
        receiver_port=receiver_port,
        pin_sha256=pin_sha256,
        static_x25519_pub=b"\x00" * 32,
        label=label,
        paired_at=paired_at,
        status=status,
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


def test_approved_open_but_busy_skipped() -> None:
    """A connected receiver currently running a job → not eligible."""
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
    assert decision == BuildPathDecision.local()


def test_approved_open_but_missing_queue_snapshot_skipped() -> None:
    """A connected receiver with no queue snapshot yet → not eligible.

    The first 5b ``queue_status`` push fires immediately on
    session open, but there's a tiny window between
    ``OFFLOADER_PEER_LINK_OPENED`` and the first snapshot
    arriving. During that window the receiver could be busy
    with the queue we haven't been told about yet; routing
    blind would risk dispatching onto a saturated peer. The
    safer move is "treat unknown as busy" and fall back to
    local.
    """
    pin = "a" * 64
    decision = pick_build_path(
        _inputs(
            pairings={pin: _stub_pairing(pin_sha256=pin)},
            open_peer_links={pin},
            # No queue snapshot received yet.
        )
    )
    assert decision == BuildPathDecision.local()


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


def test_skips_busy_candidate_picks_next() -> None:
    """A busy first candidate falls through to the next eligible entry."""
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


def test_all_candidates_busy_returns_local() -> None:
    """Every paired receiver is busy → LOCAL.

    Pins the loop-exhaustion exit: the prior multi-candidate
    tests verify a single failing candidate falls through to a
    sibling, but exhausting *every* candidate has to land at
    LOCAL (not stick on the last sibling in the chain). Pins
    the design doc's "silent fallback to local" stance for
    the saturation case explicitly.
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
    assert decision == BuildPathDecision.local()


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
