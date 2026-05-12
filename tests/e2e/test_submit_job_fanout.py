"""
End-to-end: receiver-side ``JOB_*`` events fan out to the offloader bus.

Exercises the wire-round-trip path that the unit tests
in ``tests/test_remote_build_job_fanout.py`` only cover up to
``send_app_frame``-was-called. The harness here drives a real
peer-link session, so every assertion below proves the chain
that production runs end-to-end:

  receiver-side bus  →  JobFanout listener
                     →  peer-link ``job_state_changed`` /
                        ``job_output`` frame (real Noise AEAD)
                     →  offloader-side ``_run_session_loops``
                        receive loop
                     →  ``_dispatch_job_state_changed`` /
                        ``_dispatch_job_output``
                     →  offloader-side bus
                        (``OFFLOADER_JOB_STATE_CHANGED`` /
                        ``OFFLOADER_JOB_OUTPUT``)

The receiver-side firmware controller stays a ``MagicMock`` (the
harness's default) — we don't need a real build pipeline to fire
synthetic ``JOB_*`` events on the controller's bus, and a real
build would balloon the wall-clock to multi-minute. The point of
the e2e variant is the wire shape, not the queue plumbing.

The ``submit_job`` accept path itself (header → chunks → ack) is
covered by the receiver-side unit tests
(``test_remote_build_submit_job.py``) and the offloader-side
unit tests (``test_remote_build_peer_link_client.py``); the
value-add of an end-to-end submit_job test is
catching wire-shape mismatches between the two halves, which
``test_pair_and_session.py`` already pins for the handshake +
session lifecycle. A submit_job e2e test is the natural next
follow-up once the receiver-side firmware controller can be
swapped for a recording stub without coupling the harness to
``DeviceBuilder``.
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.models import (
    EventType,
    JobLifecycleData,
    JobOutputData,
)

from ..conftest import capture_events
from .conftest import PairedInstances, make_and_seed_remote_peer_job


@pytest.mark.asyncio
async def test_remote_peer_job_lifecycle_fans_out_to_offloader_bus(
    paired_instances: PairedInstances,
) -> None:
    """``JOB_STARTED`` on the receiver bus → ``OFFLOADER_JOB_STATE_CHANGED`` on the offloader bus.

    Pins the fan-out's wire-round-trip contract:

    1. Receiver fires :attr:`EventType.JOB_QUEUED` so the
       :class:`JobFanout` cache learns this job's
       ``(remote_peer, remote_job_id)`` correlation.
    2. Receiver fires :attr:`EventType.JOB_STARTED`; the
       fan-out looks up the session for ``remote_peer`` in
       :attr:`ReceiverController._peer_link_sessions`,
       sends a typed :class:`JobStateChangedFrameData` over the
       live peer-link channel.
    3. Offloader's receive loop deserialises the frame, validates
       shape, fires :attr:`EventType.OFFLOADER_JOB_STATE_CHANGED`
       on its own bus carrying the offloader's ``remote_job_id``
       (so the offloader's submit-side caller can match against
       its own job tag, not the receiver's local id).

    The receiver-side ``error`` field rides through to
    ``error_message`` only on terminal failure / cancel paths;
    a ``running`` transition carries an empty
    ``error_message`` regardless of whatever's on the
    :class:`FirmwareJob`.
    """
    await paired_instances.wait_until_session_opened()
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )
    # ``make_and_seed_remote_peer_job`` fires JOB_QUEUED on the
    # receiver, which fans out a queued frame the offloader sees
    # before the running one. Poll until both lifecycle frames
    # have landed so the lifecycle assertion below is order-
    # independent against the wire round-trip.
    job = await make_and_seed_remote_peer_job(paired_instances)
    paired_instances.receiver_bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=job))

    queued = await state_changes.wait_for_status("queued")
    running = await state_changes.wait_for_status("running")

    assert queued["job_id"] == "off-job-1"
    assert queued["error_message"] == ""

    assert running["job_id"] == "off-job-1"  # offloader's tag echoed back
    assert running["error_message"] == ""
    assert running["pin_sha256"] == paired_instances.pin_sha256
    assert running["receiver_hostname"] == "127.0.0.1"
    assert running["receiver_port"] == paired_instances.receiver_server.port


@pytest.mark.asyncio
async def test_remote_peer_terminal_failure_carries_error_message(
    paired_instances: PairedInstances,
) -> None:
    """``JOB_FAILED`` rides the receiver's ``FirmwareJob.error`` into ``error_message``.

    Distinct from the ``running`` test because failure is the
    one path the offloader needs a human-readable hint for —
    the firmware-tasks panel surfaces ``error_message`` as the
    failed-row tooltip without round-tripping the full job
    output.
    """
    await paired_instances.wait_until_session_opened()
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )
    job = await make_and_seed_remote_peer_job(
        paired_instances, error="esphome compile failed: undefined reference"
    )

    paired_instances.receiver_bus.fire(EventType.JOB_FAILED, JobLifecycleData(job=job))

    payload = await state_changes.wait_for_status("failed")
    assert payload["error_message"] == "esphome compile failed: undefined reference"


@pytest.mark.asyncio
async def test_remote_peer_job_output_fans_out_to_offloader_bus(
    paired_instances: PairedInstances,
) -> None:
    """``JOB_OUTPUT`` on the receiver bus → ``OFFLOADER_JOB_OUTPUT`` on the offloader bus.

    Same wire chain as the lifecycle test, different frame
    type. ``JobFanout`` caches the correlation on
    ``JOB_QUEUED`` (the seed step) and reads it on every
    ``JOB_OUTPUT`` to look up the submitting session — this is
    the high-rate path during an active build (one event per
    line of compiler / linker output) so the cache hit is what
    keeps the fan-out off the hot path.

    Pins the ``stream`` field rides through verbatim — the
    receiver classifies stdout vs. stderr; the offloader's
    ansi-log renderer needs both classes to colour them
    differently.
    """
    await paired_instances.wait_until_session_opened()
    outputs = capture_events(paired_instances.offloader_bus, EventType.OFFLOADER_JOB_OUTPUT)
    job = await make_and_seed_remote_peer_job(paired_instances)

    paired_instances.receiver_bus.fire(
        EventType.JOB_OUTPUT,
        JobOutputData(job_id=job.job_id, line="Compiling kitchen.cpp...\n"),
    )

    await asyncio.wait_for(outputs.received.wait(), timeout=2.0)
    payload = outputs[-1]
    assert payload["job_id"] == "off-job-1"  # offloader's tag, not the receiver's
    assert payload["line"] == "Compiling kitchen.cpp...\n"
    assert payload["stream"] == "stdout"
    assert payload["pin_sha256"] == paired_instances.pin_sha256


@pytest.mark.asyncio
async def test_remote_peer_lifecycle_drops_when_session_already_closed(
    paired_instances: PairedInstances,
) -> None:
    """A lifecycle event fired after session close is silently dropped.

    Pins the best-effort contract from the ``JobFanout`` module
    docstring: a session that's gone away (registry empty for
    that ``remote_peer``) gets a debug-level skip — no exception
    propagates, no half-formed frame leaves the wire, no
    ``OFFLOADER_JOB_STATE_CHANGED`` fires on the offloader's bus.
    """
    await paired_instances.wait_until_session_opened()
    job = await make_and_seed_remote_peer_job(paired_instances)

    # Tear the session down before firing the lifecycle event.
    await paired_instances.offloader.stop()
    await paired_instances.wait_until_session_closed()
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    paired_instances.receiver_bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=job))

    # Give any incorrectly-scheduled background task a tick to
    # run before asserting nothing fired. The fan-out's
    # ``send_app_frame`` lookup against the empty registry
    # short-circuits before any task gets created, but yielding
    # once is the standard pattern for "no event should fire."
    await asyncio.sleep(0)
    assert len(state_changes) == 0
