"""
Tests for the source-routed firmware runner branch.

Exercises ``FirmwareController._execute_remote_job`` (7a-2b)
end-to-end against a real :class:`EventBus`. The test scaffolding
substitutes the bundle build + peer-link client surfaces with
:class:`AsyncMock` shims so the runner's wire-event translation
can be driven deterministically: a test fires a stub
``OFFLOADER_JOB_OUTPUT`` or ``OFFLOADER_JOB_STATE_CHANGED`` and
asserts the matching local ``JOB_*`` translation lands on the
same bus.

The receiver's correlation-id contract (echoes the offloader's
``job_id`` back on every fan-out frame) is built into the
fixtures — every fake event the test fires carries the
offloader-side ``job.job_id``, so the runner's filter accepts
it and exercises the translation path. A mismatched id on a
stray frame from another in-flight remote job is covered by a
separate test that asserts the runner ignores it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.firmware import remote_runner
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobSource,
    JobStatus,
    JobType,
)

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


_PIN = "a" * 64


def _make_remote_job(*, job_id: str = "remote-1") -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
        source_label="desktop",
    )


def _wire_remote_build(
    controller: Any,
    *,
    client: Any | None = None,
    lookup_error: Exception | None = None,
) -> Any:
    """Attach a stub ``_db.remote_build`` with a configurable lookup.

    Returns the stub remote-build object so the test can assert on
    its mock attributes. Either *client* is returned by every
    lookup call, or *lookup_error* is raised — the runner's
    receiver-unreachable branch consumes the latter.
    """
    remote_build = MagicMock()
    if lookup_error is not None:
        remote_build._lookup_open_peer_link_client.side_effect = lookup_error
    else:
        remote_build._lookup_open_peer_link_client.return_value = client or _make_client()
    controller._db.remote_build = remote_build
    return remote_build


def _make_client(
    *,
    accepted: bool = True,
    reason: str | None = None,
    submit_error: Exception | None = None,
    cancel_return: bool = True,
) -> Any:
    """Build a stub :class:`PeerLinkClient` mock.

    Default shape: ``submit_job`` resolves to an ``accepted`` ack;
    ``cancel_job`` resolves to ``True``. Overrides let a test
    swap either side independently — the runner's failure
    branches each lean on a different one of these.
    """
    client = MagicMock()
    if submit_error is not None:
        client.submit_job = AsyncMock(side_effect=submit_error)
    else:
        ack: dict[str, Any] = {"job_id": "remote-1", "accepted": accepted}
        if reason is not None:
            ack["reason"] = reason
        client.submit_job = AsyncMock(return_value=ack)
    client.cancel_job = AsyncMock(return_value=cancel_return)
    return client


@pytest.fixture
def patch_bundle(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``build_yaml_bundle`` with an awaitable returning bytes.

    Every remote runner test goes through bundle build before
    the peer-link submit. Patching at module scope keeps each
    test's setup focused on the runner-under-test rather than
    spawning a real ``esphome bundle`` subprocess (which would
    need an actual esphome install + a real YAML).
    """
    mock = AsyncMock(return_value=b"FAKEBUNDLE")
    monkeypatch.setattr(remote_runner, "build_yaml_bundle", mock)
    return mock


def _capture_local_events(
    controller: Any,
) -> dict[EventType, list[dict[str, Any]]]:
    """Subscribe a real ``EventBus`` to the local ``JOB_*`` events.

    Returns a captured-events dict the assertion side can index
    by event type. The fixture installs the bus on
    ``controller._db.bus`` so the runner's fires land here.
    """
    bus = EventBus()
    captured: dict[EventType, list[dict[str, Any]]] = {
        EventType.JOB_OUTPUT: [],
        EventType.JOB_PROGRESS: [],
        EventType.JOB_COMPLETED: [],
        EventType.JOB_FAILED: [],
        EventType.JOB_CANCELLED: [],
    }

    def _make_listener(key: EventType) -> Any:
        def _listen(event: Any) -> None:
            captured[key].append(event.data)

        return _listen

    for et in captured:
        bus.add_listener(et, _make_listener(et))
    controller._db.bus = bus
    return captured


def _fire_state(
    controller: Any,
    *,
    job_id: str,
    status: str,
    pin: str = _PIN,
    error_message: str = "",
) -> None:
    controller._db.bus.fire(
        EventType.OFFLOADER_JOB_STATE_CHANGED,
        {
            "receiver_hostname": "rx",
            "receiver_port": 6053,
            "pin_sha256": pin,
            "job_id": job_id,
            "status": status,
            "error_message": error_message,
        },
    )


def _fire_output(
    controller: Any,
    *,
    job_id: str,
    line: str,
    pin: str = _PIN,
    stream: str = "stdout",
) -> None:
    controller._db.bus.fire(
        EventType.OFFLOADER_JOB_OUTPUT,
        {
            "receiver_hostname": "rx",
            "receiver_port": 6053,
            "pin_sha256": pin,
            "job_id": job_id,
            "stream": stream,
            "line": line,
        },
    )


# ---------------------------------------------------------------------------
# Happy path: receiver completes, runner translates terminal frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_compile_translates_output_and_completes(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """Receiver fan-out events translate into local ``JOB_*`` fires.

    The full happy path: bundle build returns bytes, ``submit_job``
    accepts, two ``OFFLOADER_JOB_OUTPUT`` frames land + get
    re-fired as ``JOB_OUTPUT`` on the same bus, then a
    ``OFFLOADER_JOB_STATE_CHANGED{completed}`` terminal frame
    causes the runner to mark the job ``COMPLETED`` and fire
    ``JOB_COMPLETED``. Local subscribers see one event stream
    regardless of which CPU compiled the bytes.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_compile_job(controller, job))
    # Yield until the runner is parked waiting on the terminal future.
    # Two ticks: one to let the bundle build await resolve, one to let
    # the submit_job await resolve, then we can fire wire events.
    for _ in range(4):
        await asyncio.sleep(0)

    _fire_output(controller, job_id=job.job_id, line="Reading configuration\n")
    _fire_output(controller, job_id=job.job_id, line="Compile finished\n")
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.COMPLETED
    assert [d["line"] for d in captured[EventType.JOB_OUTPUT]] == [
        "Reading configuration\n",
        "Compile finished\n",
    ]
    assert len(captured[EventType.JOB_COMPLETED]) == 1
    assert captured[EventType.JOB_COMPLETED][0]["job"] is job
    assert captured[EventType.JOB_FAILED] == []
    client.submit_job.assert_awaited_once_with(
        job_id=job.job_id,
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=b"FAKEBUNDLE",
    )


@pytest.mark.asyncio
async def test_remote_compile_progress_translates_to_local_progress_event(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """A wire output line carrying a percentage fires a local ``JOB_PROGRESS``.

    Progress detection runs on the offloader side — receiver
    output is raw text per :class:`OffloaderJobOutputData`, no
    structured progress field on the wire. The local
    ``_parse_progress`` extracts the percentage and the runner
    fires ``JOB_PROGRESS`` so the firmware-tasks progress bar
    advances on remote builds the same way it does on local
    ones.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_compile_job(controller, job))
    for _ in range(4):
        await asyncio.sleep(0)

    _fire_output(controller, job_id=job.job_id, line="[ 47%] Compiling .pio/build/foo.o\n")
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert captured[EventType.JOB_PROGRESS]
    assert captured[EventType.JOB_PROGRESS][0]["progress"] == 47
    assert job.progress == 47


# ---------------------------------------------------------------------------
# Stray events on the same bus must not affect this runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_compile_ignores_events_for_other_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """A wire frame for a different ``job_id`` doesn't leak into this runner.

    The bus is process-wide; multiple in-flight remote jobs
    share it. Each runner instance filters frames by both
    ``pin_sha256`` and ``job_id`` so output for job A can't
    bleed into job B's ``output`` buffer or trigger job B's
    terminal.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job(job_id="ours")

    runner = asyncio.create_task(remote_runner.run_remote_compile_job(controller, job))
    for _ in range(4):
        await asyncio.sleep(0)

    # Stray traffic from a sibling job — must not appear in our captures
    # and must not terminate our runner.
    _fire_output(controller, job_id="someone-else", line="other job output\n")
    _fire_state(controller, job_id="someone-else", status="completed")
    await asyncio.sleep(0)
    assert captured[EventType.JOB_OUTPUT] == []
    assert captured[EventType.JOB_COMPLETED] == []
    assert not runner.done()

    # Now the real terminal arrives and the runner finishes.
    _fire_state(controller, job_id="ours", status="completed")
    await asyncio.wait_for(runner, timeout=2.0)
    assert job.status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Failure / rejection / unreachable paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_compile_failed_status_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """A receiver ``failed`` terminal lands as local ``JOB_FAILED`` with the error text."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_compile_job(controller, job))
    for _ in range(4):
        await asyncio.sleep(0)
    _fire_state(
        controller,
        job_id=job.job_id,
        status="failed",
        error_message="syntax error in YAML",
    )
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error == "syntax error in YAML"
    assert len(captured[EventType.JOB_FAILED]) == 1


@pytest.mark.asyncio
async def test_remote_compile_rejected_ack_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """``submit_job`` rejection (``accepted=False``) finalises locally with the reason."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client(accepted=False, reason="receiver queue full")
    _wire_remote_build(controller, client=client)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    await remote_runner.run_remote_compile_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "receiver queue full" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


@pytest.mark.asyncio
async def test_remote_compile_receiver_unreachable_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """A missing peer-link client finalises the job as FAILED with the lookup error."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller, lookup_error=RuntimeError("session not connected"))
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    await remote_runner.run_remote_compile_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "session not connected" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


@pytest.mark.asyncio
async def test_remote_compile_non_compile_job_type_fails_locally(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """REMOTE with a non-COMPILE ``job_type`` is rejected at the runner's top.

    7a-2b's scope is COMPILE only — UPLOAD / INSTALL land in
    7a-3. Anything else here must surface a clear FAILED with
    an explanatory ``error`` instead of running through the
    submit path with the wrong target.
    """
    controller = firmware_controller_factory(with_terminate=True)
    _capture_local_events(controller)
    _wire_remote_build(controller)
    job = FirmwareJob(
        job_id="x",
        configuration="kitchen.yaml",
        job_type=JobType.INSTALL,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
    )

    await remote_runner.run_remote_compile_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "COMPILE" in job.error


# ---------------------------------------------------------------------------
# Cancel translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_compile_local_cancel_translates_to_wire_cancel_job(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    patch_bundle: AsyncMock,
) -> None:
    """Adding the job to ``_cancel_requested`` triggers a wire ``cancel_job`` send.

    User Stop click flows through the existing
    ``firmware/cancel`` handler, which adds the job id to
    ``_cancel_requested``. The runner's poll loop notices and
    invokes :meth:`PeerLinkClient.cancel_job` against the
    receiver. The receiver's resulting cancelled terminal
    frame finalises the local job as CANCELLED.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n")
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_compile_job(controller, job))
    for _ in range(4):
        await asyncio.sleep(0)

    controller._cancel_requested.add(job.job_id)
    # The poll cadence is 0.5s; wait at most one tick + headroom.
    await asyncio.sleep(0.6)
    client.cancel_job.assert_awaited_once_with(job_id=job.job_id)

    _fire_state(controller, job_id=job.job_id, status="cancelled")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert job.job_id not in controller._cancel_requested
    assert len(captured[EventType.JOB_CANCELLED]) == 1
