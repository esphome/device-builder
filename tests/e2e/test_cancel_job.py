"""
End-to-end: offloader-driven cancel routes to receiver-side firmware.cancel.

Exercises the 5d ``cancel_job`` reverse-direction wire path. The
unit tests in ``test_remote_build_peer_link_client.py`` cover the
offloader-side ``PeerLinkClient.cancel_job`` send in isolation;
the unit tests in ``test_remote_build_controller.py`` cover the
receiver-side ``handle_cancel_job`` handler with a synthetic
frame; this PR's tests pin the wire round-trip across both halves
so a wire-shape mismatch on either side surfaces here rather than
slipping past two unit suites that pass on the same drift.

The chain:

  offloader-side ``RemoteBuildController.cancel_job`` WS handler
                       →  ``PeerLinkClient.cancel_job``
                       →  peer-link ``cancel_job`` frame
                          (real Noise AEAD)
                       →  receiver-side ``_run_session_loops``
                          receive loop
                       →  ``handle_cancel_job`` resolves
                          ``(remote_peer, remote_job_id)`` →
                          ``firmware_job_id`` via
                          ``JobFanout.resolve_firmware_job_id``
                       →  ``firmware.cancel(job_id=...)``

The receiver-side firmware controller is an :class:`AsyncMock`
on ``db.firmware`` — we don't need a real firmware queue to
verify the cancel landed at the right primitive, and a real
queue would couple this test to the firmware controller's own
state machine. The point of the e2e variant is the wire shape +
the JobFanout correlation; the firmware-cancel side-effect is
already covered by ``test_firmware_controller.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.models import (
    EventType,
    JobLifecycleData,
)

from ..conftest import capture_events
from .conftest import PairedInstances
from .test_submit_job_fanout import _make_and_seed_remote_peer_job


@pytest.mark.asyncio
async def test_offloader_cancel_job_routes_to_receiver_firmware_cancel(
    paired_instances: PairedInstances,
) -> None:
    """``cancel_job`` over the wire lands at ``firmware.cancel`` on the receiver.

    Pins the 5d round-trip:

    1. Receiver fires ``JOB_QUEUED`` so the :class:`JobFanout`
       cache learns the ``(remote_peer, remote_job_id)`` →
       ``firmware_job_id`` correlation.
    2. Offloader's ``cancel_job`` WS handler fires the wire
       frame via :meth:`PeerLinkClient.cancel_job`.
    3. Receiver's :meth:`handle_cancel_job` resolves the
       offloader's ``job_id`` back to the receiver-local
       firmware id via :meth:`JobFanout.resolve_firmware_job_id`,
       then routes to :meth:`FirmwareController.cancel` — the
       same primitive a local operator-driven cancel uses.

    The firmware controller stays stubbed; we assert the cancel
    landed with the right kwargs rather than driving the real
    queue.
    """
    await paired_instances.wait_until_session_opened()
    cancel_mock = AsyncMock()
    paired_instances.receiver._db.firmware = MagicMock()
    paired_instances.receiver._db.firmware.cancel = cancel_mock
    job = await _make_and_seed_remote_peer_job(paired_instances)

    result = await paired_instances.offloader.cancel_job(
        pin_sha256=paired_instances.pin_sha256,
        job_id=job.remote_job_id,
    )

    assert result == {"sent": True}
    # Wait for the wire round-trip; the cancel frame is sent
    # fire-and-forget on the offloader side, decrypt + dispatch
    # happens on the receiver's receive loop on its own task.
    # Poll the mock's call list rather than a bus event because
    # the receiver-side ``firmware.cancel`` is the assertion
    # surface for this test, not a derived event.
    deadline = asyncio.get_running_loop().time() + 2.0
    while not cancel_mock.await_count and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    cancel_mock.assert_awaited_once_with(job_id=job.job_id)


@pytest.mark.asyncio
async def test_offloader_cancel_job_full_round_trip_to_state_changed(
    paired_instances: PairedInstances,
) -> None:
    """Cancel → simulated ``JOB_CANCELLED`` → ``OFFLOADER_JOB_STATE_CHANGED{cancelled}``.

    Extends the firmware-cancel test with the lifecycle
    confirmation leg: once :meth:`FirmwareController.cancel`
    completes, the firmware queue would fire :attr:`JOB_CANCELLED`,
    :class:`JobFanout` would fan that out as a
    ``job_state_changed{status: "cancelled"}`` frame, and the
    offloader's existing :attr:`OFFLOADER_JOB_STATE_CHANGED`
    plumbing would surface the terminal state on its own bus.

    Stub firmware doesn't fire :attr:`JOB_CANCELLED` itself, so
    the test simulates that side-effect by firing the bus event
    manually after the cancel mock is awaited. The wire round-
    trip downstream of :attr:`JOB_CANCELLED` is identical to
    the lifecycle path covered in
    ``test_submit_job_fanout.py``; rerunning it here pins that
    cancel funnels through the same fan-out as any other
    terminal transition (no special cancel-only event type).
    """
    await paired_instances.wait_until_session_opened()
    cancel_mock = AsyncMock()
    paired_instances.receiver._db.firmware = MagicMock()
    paired_instances.receiver._db.firmware.cancel = cancel_mock
    job = await _make_and_seed_remote_peer_job(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    await paired_instances.offloader.cancel_job(
        pin_sha256=paired_instances.pin_sha256,
        job_id=job.remote_job_id,
    )

    deadline = asyncio.get_running_loop().time() + 2.0
    while not cancel_mock.await_count and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    cancel_mock.assert_awaited_once_with(job_id=job.job_id)

    # Simulate the firmware queue's JOB_CANCELLED that the
    # stub didn't fire on its own.
    paired_instances.receiver_bus.fire(EventType.JOB_CANCELLED, JobLifecycleData(job=job))

    await asyncio.wait_for(state_changes.received.wait(), timeout=2.0)
    payload = state_changes[-1]
    assert payload["job_id"] == job.remote_job_id
    assert payload["status"] == "cancelled"
    assert payload["pin_sha256"] == paired_instances.pin_sha256


@pytest.mark.asyncio
async def test_offloader_cancel_job_unknown_correlation_drops_silently(
    paired_instances: PairedInstances,
) -> None:
    """A cancel for an unknown ``job_id`` is silently dropped on the receiver.

    Pins the best-effort contract from
    :meth:`handle_cancel_job`'s docstring: a
    ``(remote_peer, remote_job_id)`` correlation that's missing
    from the :class:`JobFanout` cache (typical race: receiver
    already evicted the entry on terminal transition before the
    offloader's cancel arrived) gets a debug-level skip — no
    exception propagates, ``firmware.cancel`` is never called,
    no terminate-frame is sent.

    The offloader's WS handler still returns ``sent=true``: the
    frame made it onto the wire. Whether the receiver acted on
    it is the receiver's call, and the offloader's UI relies on
    the next observed ``job_state_changed`` (or its absence) for
    the actual state.
    """
    await paired_instances.wait_until_session_opened()
    cancel_mock = AsyncMock()
    paired_instances.receiver._db.firmware = MagicMock()
    paired_instances.receiver._db.firmware.cancel = cancel_mock
    # Deliberately skip the JOB_QUEUED seed so JobFanout's cache
    # has no correlation for the offloader's job_id below.

    result = await paired_instances.offloader.cancel_job(
        pin_sha256=paired_instances.pin_sha256,
        job_id="off-job-never-seen",
    )

    assert result == {"sent": True}
    # Yield the loop a few times to let any incorrectly-routed
    # cancel land before asserting it didn't.
    for _ in range(10):
        await asyncio.sleep(0.01)
    cancel_mock.assert_not_awaited()


# WS-layer error-mapping (CommandError(NOT_FOUND) /
# CommandError(PRECONDITION_FAILED) / CommandError(INVALID_ARGS))
# is pinned by unit tests on the same handler in
# ``test_remote_build_controller.py``; the e2e variant adds value
# only on the wire round-trip cases above, where the contract
# spans both halves of the pair.
