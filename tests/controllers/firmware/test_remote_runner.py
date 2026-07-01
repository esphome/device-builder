"""
Tests for the source-routed firmware runner branch.

Exercises ``FirmwareController._execute_remote_job``
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
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.firmware import remote_runner
from esphome_device_builder.controllers.firmware._state import Lane
from esphome_device_builder.controllers.remote_build.peer_link_client import (
    DownloadArtifactsError,
    DownloadArtifactsResult,
    PeerLinkNoSessionError,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.config_bundle import BundleBuildError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.remote_artifacts_materialise import MaterialiseError
from esphome_device_builder.models import (
    ErrorCode,
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
) -> tuple[Any, Any]:
    """Attach a stub ``_db.remote_build_offloader`` with a configurable lookup.

    Returns ``(remote_build, client)`` so the caller can both
    assert on the remote-build mock and reference the client
    for runner-state sync (``_wait_until_dispatched`` /
    ``_wait_for_wire_cancel`` poll on the client's mock
    counters). When *lookup_error* is passed the returned
    client is ``None`` — there's nothing to dispatch to and
    no submit-side sync to wait for.
    """
    remote_build = MagicMock()
    if lookup_error is not None:
        remote_build._lookup_open_peer_link_client.side_effect = lookup_error
        controller._db.remote_build_offloader = remote_build
        return remote_build, None
    client = client or _make_client()
    remote_build._lookup_open_peer_link_client.return_value = client
    controller._db.remote_build_offloader = remote_build
    return remote_build, client


def _make_packed_artifacts(tarball: bytes = b"FAKE-TARBALL") -> DownloadArtifactsResult:
    return DownloadArtifactsResult(tarball=tarball, firmware_offset="0x10000")


def _make_client(
    *,
    accepted: bool = True,
    reason: str | None = None,
    submit_error: Exception | None = None,
    cancel_return: bool = True,
    cancel_error: Exception | None = None,
) -> Any:
    """Build a stub :class:`PeerLinkClient` mock.

    Default shape: ``submit_job`` resolves to an ``accepted`` ack
    whose ``job_id`` echoes the caller's id (matches the real
    :class:`PeerLinkClient` contract — the receiver fans the
    same id back on the ack so the offloader can correlate);
    ``cancel_job`` resolves to ``True``. Overrides let a test
    swap either side independently — the runner's failure
    branches each lean on a different one of these.
    """
    client = MagicMock()
    if submit_error is not None:
        client.submit_job = AsyncMock(side_effect=submit_error)
    else:

        async def _echo_ack(**kwargs: Any) -> dict[str, Any]:
            ack: dict[str, Any] = {"job_id": kwargs["job_id"], "accepted": accepted}
            if reason is not None:
                ack["reason"] = reason
            return ack

        client.submit_job = AsyncMock(side_effect=_echo_ack)
    if cancel_error is not None:
        client.cancel_job = AsyncMock(side_effect=cancel_error)
    else:
        client.cancel_job = AsyncMock(return_value=cancel_return)
    # Default so COMPILE / UPLOAD / INSTALL tests don't have to
    # wire it per-test — the runner now goes through
    # ``_fetch_and_materialise`` for every non-CLEAN COMPLETED
    # job. Tests that want to inspect the call or simulate
    # failure override this assignment.
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
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


def _request_remote_cancel(controller: Any, job: FirmwareJob) -> None:
    """
    Drive the cancel from a test the way ``FirmwareController.cancel`` does.

    The runner used to poll ``_cancel_requested`` every 0.5s
    so a test that flipped the set on its own would wake the
    runner inside one poll iteration. The event-driven shape
    means the runner parks on ``cancel_event`` instead — so a
    bare ``_cancel_requested.add`` is no longer enough. Tests
    use this helper to flip both, mirroring the production
    cancel handler at :meth:`FirmwareController.cancel`. Going
    through the helper (rather than calling ``controller.cancel``
    directly) keeps the test focused on the runner under test
    without bringing in the cancel handler's QUEUED-job and
    ``_terminate_current_process`` branches that don't apply
    on the remote path.
    """
    controller.state.cancel_requested.add(job.job_id)
    event = controller.state.cancel_events.get(job.job_id)
    if event is not None:
        event.set()


async def _wait_until_dispatched(client: Any, *, timeout: float = 1.0) -> None:
    """
    Yield until the runner has finished its dispatch phase.

    Polls on :attr:`AsyncMock.await_count` for ``submit_job``
    — the count increments only after the mock's awaited
    coroutine has resolved, so this returns the instant the
    runner moves past ``await client.submit_job(...)`` and
    into ``_await_terminal``'s wait loop. Replaces fixed
    ``for _ in range(N): await asyncio.sleep(0)`` constructs
    that broke when residual coroutines from a prior test
    left the loop in a different state than the runner
    expected (the park-point depends on how many awaits the
    runner has yielded through, and the number drifts across
    refactors).

    Raises :class:`AssertionError` on timeout so a runner
    regression that never reaches the submit shows up as a
    clear test failure rather than a hung pytest run.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while client.submit_job.await_count == 0:
        if loop.time() >= deadline:
            msg = f"submit_job not awaited within {timeout}s"
            raise AssertionError(msg)
        await asyncio.sleep(0)


async def _wait_for_wire_cancel(client: Any, *, timeout: float = 1.0) -> None:
    """
    Yield until the runner has translated a local cancel into a wire ``cancel_job``.

    The runner parks on
    ``asyncio.wait({terminal, session_lost, cancel_wait},
    return_when=FIRST_COMPLETED)`` — fully event-driven, no
    poll cadence — so as soon as
    ``FirmwareController.cancel`` (or the test's
    ``_request_remote_cancel`` mirror) signals the cancel
    event, the runner wakes and dispatches
    ``client.cancel_job``. Polling on
    :attr:`AsyncMock.await_count` with a 50 ms granularity
    returns the instant that wire send lands.

    Raises :class:`AssertionError` on timeout for the same
    reason :func:`_wait_until_dispatched` does — a regression
    that never sends the wire cancel should be a clean fail,
    not a hang.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while client.cancel_job.await_count == 0:
        if loop.time() >= deadline:
            msg = f"cancel_job not awaited within {timeout}s"
            raise AssertionError(msg)
        await asyncio.sleep(0.05)


def _fire_session_closed(
    controller: Any,
    *,
    pin: str = _PIN,
    reason: str = "transport_error",
    error_detail: str = "",
) -> None:
    controller._db.bus.fire(
        EventType.OFFLOADER_PEER_LINK_CLOSED,
        {
            "receiver_hostname": "rx",
            "receiver_port": 6053,
            "pin_sha256": pin,
            "reason": reason,
            "error_detail": error_detail,
        },
    )


# ---------------------------------------------------------------------------
# Happy path: receiver completes, runner translates terminal frame
# ---------------------------------------------------------------------------


async def test_remote_compile_translates_output_and_completes(
    firmware_controller_factory: FirmwareControllerFactory,
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
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    # Yield until the runner is parked waiting on the terminal future.
    # Two ticks: one to let the bundle build await resolve, one to let
    # the submit_job await resolve, then we can fire wire events.
    await _wait_until_dispatched(client)

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
    # COMPILE now goes through materialise so firmware/download
    # serves the staged tree (#624).
    client.download_artifacts.assert_awaited_once()
    client.submit_job.assert_awaited_once_with(
        job_id=job.job_id,
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=b"FAKEBUNDLE",
        device_name="",
        device_friendly_name="",
    )


async def test_remote_clean_dispatches_with_clean_target_and_finalises_on_completed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A REMOTE clean job dispatches as ``target="clean"`` and finalises on ``completed``.

    Pins the fan-out contract: when ``FirmwareController.clean``
    creates a per-peer remote clean job, the runner routes it
    through ``submit_job`` with ``target="clean"`` (not the
    default ``"compile"``) so the receiver runs ``esphome clean
    <yaml>`` after extract instead of trying to build. No
    download_artifacts step — clean produces no firmware to
    flash. Receiver's ``completed`` terminal frame finalises the
    job as COMPLETED with ``exit_code=0`` so the offloader's
    ``follow_job`` framing doesn't coerce a successful clean to
    a failure.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = FirmwareJob(
        job_id="clean-1",
        configuration="kitchen.yaml",
        job_type=JobType.CLEAN,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
    )

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert len(captured[EventType.JOB_COMPLETED]) == 1
    assert captured[EventType.JOB_FAILED] == []
    # Wire target is "clean", not the default "compile".
    client.submit_job.assert_awaited_once_with(
        job_id=job.job_id,
        configuration_filename="kitchen.yaml",
        target="clean",
        bundle_bytes=b"FAKEBUNDLE",
        device_name="",
        device_friendly_name="",
    )
    # Crucially: no download_artifacts call — clean has nothing
    # to fetch back.
    client.download_artifacts.assert_not_called()


async def test_remote_compile_plumbs_device_names_from_local_scanner(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """The offloader looks up the local Device entry and sends names on the wire.

    Pins the receiver-side title-rendering contract: the
    offloader already has ``esphome.name`` /
    ``esphome.friendly_name`` from its local scanner at install
    time, so the receiver doesn't have to re-parse the bundled
    YAML just to render the firmware-tasks title. Stub the
    devices controller's ``get_by_configuration`` to return one
    Device matching the job's configuration; assert ``submit_job``
    is called with both names.

    A regression that dropped the lookup (or fell back to
    parsing the YAML on the receiver) would surface here.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)

    fake_device = MagicMock()
    fake_device.configuration = "kitchen.yaml"
    fake_device.name = "kitchen"
    fake_device.friendly_name = "AC Float Monitor 32"
    devices_stub = MagicMock()
    devices_stub.get_by_configuration.side_effect = lambda cfg: (
        fake_device if cfg == fake_device.configuration else None
    )
    controller._db.devices = devices_stub

    job = _make_remote_job()
    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.COMPLETED
    assert len(captured[EventType.JOB_COMPLETED]) == 1
    client.submit_job.assert_awaited_once_with(
        job_id=job.job_id,
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=b"FAKEBUNDLE",
        device_name="kitchen",
        device_friendly_name="AC Float Monitor 32",
    )


async def test_remote_compile_falls_through_when_no_device_matches(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """``submit_job`` ships empty names when the local scanner has no entry for the YAML."""
    controller = firmware_controller_factory(with_terminate=True)
    _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)

    unrelated_device = MagicMock()
    unrelated_device.configuration = "other.yaml"
    unrelated_device.name = "other"
    unrelated_device.friendly_name = "Other"
    devices_stub = MagicMock()
    devices_stub.get_by_configuration.side_effect = lambda cfg: (
        unrelated_device if cfg == unrelated_device.configuration else None
    )
    controller._db.devices = devices_stub

    job = _make_remote_job()
    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    client.submit_job.assert_awaited_once_with(
        job_id=job.job_id,
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=b"FAKEBUNDLE",
        device_name="",
        device_friendly_name="",
    )


async def test_remote_compile_progress_translates_to_local_progress_event(
    firmware_controller_factory: FirmwareControllerFactory,
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
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    _fire_output(controller, job_id=job.job_id, line="[ 47%] Compiling .pio/build/foo.o\n")
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert captured[EventType.JOB_PROGRESS]
    assert captured[EventType.JOB_PROGRESS][0]["progress"] == 47
    assert job.progress == 47


# ---------------------------------------------------------------------------
# Stray events on the same bus must not affect this runner
# ---------------------------------------------------------------------------


async def test_remote_compile_ignores_events_for_other_jobs(
    firmware_controller_factory: FirmwareControllerFactory,
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
    _, client = _wire_remote_build(controller)
    job = _make_remote_job(job_id="ours")

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

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


async def test_remote_compile_failed_status_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A receiver ``failed`` terminal lands as local ``JOB_FAILED`` with the error text."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(
        controller,
        job_id=job.job_id,
        status="failed",
        error_message="syntax error in YAML",
    )
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error == "remote build: syntax error in YAML"
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_rejected_ack_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """``submit_job`` rejection (``accepted=False``) finalises locally with the reason."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client(accepted=False, reason="receiver queue full")
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "receiver queue full" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_receiver_unreachable_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A missing peer-link client finalises the job as FAILED with the lookup error."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(
        controller,
        lookup_error=CommandError(ErrorCode.PRECONDITION_FAILED, "session not connected"),
    )
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "session not connected" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_unsupported_job_type_fails_locally(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """REMOTE with a job_type outside COMPILE/UPLOAD/INSTALL/CLEAN is rejected at the runner's top.

    The receiver's ``submit_job`` contract carries wire shapes
    for ``target`` ∈ {compile, upload, clean}; job types that
    don't have a corresponding wire flow (``RENAME``,
    ``RESET_BUILD_ENV``) must surface a clear FAILED with an
    explanatory ``error`` rather than running through the submit
    path with the wrong target.
    """
    controller = firmware_controller_factory(with_terminate=True)
    _capture_local_events(controller)
    _wire_remote_build(controller)
    job = FirmwareJob(
        job_id="x",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
    )

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "COMPILE" in job.error


# ---------------------------------------------------------------------------
# Cancel translation
# ---------------------------------------------------------------------------


async def test_remote_compile_local_cancel_translates_to_wire_cancel_job(
    firmware_controller_factory: FirmwareControllerFactory,
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
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    _request_remote_cancel(controller, job)
    # The poll cadence is 0.5s; wait at most one tick + headroom.
    await _wait_for_wire_cancel(client)
    client.cancel_job.assert_awaited_once_with(job_id=job.job_id)

    _fire_state(controller, job_id=job.job_id, status="cancelled")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert job.job_id not in controller.state.cancel_requested
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_compile_cancel_beats_receiver_completed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    User cancel + receiver-completed race finalises as CANCELLED.

    If a Stop click is registered (``_cancel_requested`` flips)
    while the receiver is mid-build, the runner sends a wire
    ``cancel_job`` — but the receiver may have already finished
    and emit ``completed`` before the cancel lands. The local
    contract (matching the local subprocess path) is that user
    intent wins: the job is CANCELLED, not COMPLETED. Without
    this branch the user would click Stop and still see a
    successful install offered for the receiver's bytes — but
    they explicitly asked to abort.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    # Register the cancel, then fire ``completed`` (instead of
    # ``cancelled``) — the receiver finished before our cancel
    # frame could arrive on its side.
    _request_remote_cancel(controller, job)
    await _wait_for_wire_cancel(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert captured[EventType.JOB_COMPLETED] == []
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_compile_receiver_initiated_cancel_finalises_as_cancelled(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    A receiver-reported ``cancelled`` without a local cancel still lands as CANCELLED.

    Distinct from the user-Stop path: an operator using the
    receiver-side admin UI can cancel an in-flight job on
    their end. The fan-out fires ``cancelled`` to us with no
    corresponding entry in ``_cancel_requested``; the runner
    must still finalise the job through ``_finalize_cancelled``
    rather than misroute it as ``FAILED``.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="cancelled")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert captured[EventType.JOB_FAILED] == []
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_compile_cancel_during_bundle_build_finalises_as_cancelled(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A Stop click while ``build_yaml_bundle`` runs lands CANCELLED, not FAILED.

    Mirrors the local subprocess path's "cancel intent wins"
    contract: if the user explicitly aborted before the dispatch
    even reached the peer-link, the resulting failure path must
    not surface as a red FAILED badge. ``_fail_locally`` checks
    ``_cancel_requested`` and routes through
    ``_finalize_cancelled`` instead.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    job = _make_remote_job()

    # Cancel was already requested by the time we get to bundle
    # build — and the bundle build itself fails (configuration
    # not on disk). Combined, the runner's failure path should
    # route through the cancel-aware branch.
    _request_remote_cancel(controller, job)
    monkeypatch.setattr(
        remote_runner, "build_yaml_bundle", AsyncMock(side_effect=FileNotFoundError)
    )

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.CANCELLED
    assert captured[EventType.JOB_FAILED] == []
    assert len(captured[EventType.JOB_CANCELLED]) == 1


# ---------------------------------------------------------------------------
# Bundle build failure paths
# ---------------------------------------------------------------------------


async def test_remote_compile_bundle_file_missing_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FileNotFoundError`` from the bundle subprocess surfaces in ``job.error``."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    monkeypatch.setattr(
        remote_runner, "build_yaml_bundle", AsyncMock(side_effect=FileNotFoundError)
    )
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "kitchen.yaml" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_bundle_build_error_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BundleBuildError.output`` surfaces in ``job.error`` for the user."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    bundle_error = BundleBuildError(
        "bundle subprocess failed", output="ERROR: syntax in kitchen.yaml"
    )
    monkeypatch.setattr(remote_runner, "build_yaml_bundle", AsyncMock(side_effect=bundle_error))
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "syntax in kitchen.yaml" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


# ---------------------------------------------------------------------------
# Dispatch / pre-flight failure paths
# ---------------------------------------------------------------------------


async def test_remote_compile_missing_source_pin_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A REMOTE job with empty ``source_pin_sha256`` fails before any wire work."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _wire_remote_build(controller)
    job = FirmwareJob(
        job_id="x",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        # source_pin_sha256 deliberately empty
    )

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "source_pin_sha256" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_no_remote_build_controller_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A REMOTE job dispatched before the remote-build controller is initialised fails cleanly."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    # No remote_build attached — production sets this in
    # ``DeviceBuilder.__init__`` but ``None`` is the typed
    # default the runner has to handle.
    controller._db.remote_build_offloader = None
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "not initialised" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_compile_submit_no_session_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """``submit_job`` raising :class:`PeerLinkNoSessionError` surfaces in ``job.error``."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client(submit_error=PeerLinkNoSessionError("session not open"))
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    await remote_runner.run_remote_job(controller, job)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "session not open" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


# ---------------------------------------------------------------------------
# Peer-link session loss while waiting on terminal
# ---------------------------------------------------------------------------


async def test_remote_compile_session_lost_mid_build_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    A peer-link close after submit + before terminal finalises the job FAILED.

    Without this branch the runner would wait on the terminal
    future forever — the receiver is gone, no ``job_state_changed``
    will ever land. The ``OFFLOADER_PEER_LINK_CLOSED`` listener
    feeds a sibling future the wait loop consults so the job
    fails fast rather than wedging the firmware queue.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    _fire_session_closed(
        controller,
        reason="transport_error",
        error_detail="ConnectionResetError: [Errno 54] Connection reset by peer",
    )
    # Cancel poll cadence is 0.5s; allow the wake-up.
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert "peer-link session lost" in job.error
    assert "transport_error" in job.error
    assert "ConnectionResetError" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1
    # A synthetic output line names the cause in the live log
    # stream too so the install dialog's main scroll buffer
    # ends with a clear "session closed" message right above the
    # red "Install failed." banner. Without it, the operator
    # sees the last (half-rendered) compile line and no
    # indication that the receiver dropped. Wording is umbrella
    # ("session closed") because the same code path covers
    # transport_error, server_shutting_down, superseded,
    # pin_mismatch / peer_revoked, and client_stopped — calling
    # all of them "lost connection" would mislead operators
    # about the cause.
    output_lines = [data["line"] for data in captured[EventType.JOB_OUTPUT]]
    assert any(
        "remote build session closed" in line
        and "transport_error" in line
        and "build was aborted" in line
        for line in output_lines
    ), output_lines


async def test_remote_compile_session_lost_raises_for_pool_retry(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """A mid-build session loss raises for pool retry instead of finalising locally."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    runner = asyncio.create_task(
        remote_runner.run_remote_job(controller, job, retry_on_server_loss=True)
    )
    await _wait_until_dispatched(client)
    _fire_session_closed(
        controller, reason="transport_error", error_detail="Connection reset by peer"
    )

    with pytest.raises(remote_runner.RemoteServerLostError, match="transport_error"):
        await asyncio.wait_for(runner, timeout=2.0)

    # The pool driver owns the re-route; the runner must not have failed it.
    assert job.status != JobStatus.FAILED
    assert captured[EventType.JOB_FAILED] == []


async def test_remote_compile_session_lost_synthetic_line_skips_leading_newline(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    r"""
    Synthetic line skips its leading newline when prior output already ends with one.

    The receiver-side compile streams ``\n``-terminated lines via the
    fan-out, so the common case has ``job.output[-1]`` already ending
    in ``\n``. A blanket leading ``\n`` on the synthetic line would
    add a visible empty row between the last compile line and the
    "session closed" message.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    # Seed an output line so the synthetic line's leading-newline
    # heuristic has a previous line to test against.
    _fire_output(controller, job_id=job.job_id, line="Compiling main.cpp\n")
    _fire_session_closed(controller, reason="server_shutting_down")
    await asyncio.wait_for(runner, timeout=2.0)

    output_lines = [data["line"] for data in captured[EventType.JOB_OUTPUT]]
    synthetic = next(line for line in output_lines if "remote build session closed" in line)
    # No leading newline because the previous line already
    # ended with one.
    assert not synthetic.startswith("\n"), repr(synthetic)
    assert "server_shutting_down" in synthetic


async def test_remote_compile_cancel_translation_handles_missing_session(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    Cancel arriving after the session dropped finalises locally without a wire send.

    Stop-during-an-already-broken-link path: the second lookup
    for ``firmware_remote_cancel`` raises ``CommandError`` (no
    session), so the runner finalises as CANCELLED without
    spinning waiting for a frame.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    # First lookup (for submit) succeeds; second lookup (for
    # cancel) raises. Both flow through the same MagicMock so
    # the side_effect list-pattern covers the sequence.
    initial_client = _make_client()
    remote_build = MagicMock()
    remote_build._lookup_open_peer_link_client.side_effect = [
        initial_client,
        CommandError(ErrorCode.PRECONDITION_FAILED, "session not connected (mid-reconnect)"),
    ]
    controller._db.remote_build_offloader = remote_build
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(initial_client)
    _request_remote_cancel(controller, job)
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert job.job_id not in controller.state.cancel_requested
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_compile_cancel_translation_handles_session_drop_on_send(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """``cancel_job`` raising ``PeerLinkNoSessionError`` finalises CANCELLED locally."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client(cancel_error=PeerLinkNoSessionError("session gone"))
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _request_remote_cancel(controller, job)
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


# ---------------------------------------------------------------------------
# Runner-task shutdown
# ---------------------------------------------------------------------------


async def test_remote_compile_runner_task_cancelled_finalises_as_cancelled(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    Cancelling the runner task (controller shutdown) finalises CANCELLED + re-raises.

    Mirrors the local subprocess path's
    ``asyncio.CancelledError`` branch: the cancelled coroutine
    fires ``JOB_CANCELLED`` so subscribers see a terminal
    event, then propagates the cancellation so the firmware
    queue runner can unwind.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


# ---------------------------------------------------------------------------
# Branch wiring inside ``FirmwareController._execute_job``
# ---------------------------------------------------------------------------


async def test_execute_job_routes_remote_source_through_remote_runner(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    ``_execute_job`` early-returns to ``_execute_remote_job`` for REMOTE source.

    Pins the controller-level wiring rather than the runner
    itself: a future refactor that drops the
    ``if job.source is JobSource.REMOTE`` guard would silently
    push REMOTE jobs through the local subprocess pipeline,
    where they'd try to ``esphome compile`` a configuration
    that may not even be on disk in the offloader's
    ``config_dir``. Going through ``_execute_job`` (rather
    than calling ``run_remote_job`` directly) covers
    that branch + the ``_execute_remote_job`` delegator
    method.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(controller._execute_job(job, controller.state.compile_lane))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.COMPLETED
    assert len(captured[EventType.JOB_COMPLETED]) == 1


# ---------------------------------------------------------------------------
# Wire ack ``job_id`` echo
# ---------------------------------------------------------------------------


async def test_submit_job_ack_echoes_caller_job_id(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    Stub ``submit_job`` ack echoes the caller's ``job_id``.

    Pins the fixture's :func:`_make_client` behaviour against
    the real :class:`PeerLinkClient` contract — the receiver
    correlates by echoing the offloader's id on the ack, and a
    fixture that hard-coded the value (instead of echoing)
    would silently diverge from production. If a future runner
    change adds an ``assert ack["job_id"] == job.job_id``, this
    test catches the stub drift instead of the divergence
    showing up as an unrelated runner-test failure.
    """
    controller = firmware_controller_factory(with_terminate=True)
    _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job(job_id="unique-echo-1234")

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    ack_call = client.submit_job.await_args
    assert ack_call.kwargs["job_id"] == "unique-echo-1234"


# ---------------------------------------------------------------------------
# Defensive filter coverage — stray cross-pin / teardown races
# ---------------------------------------------------------------------------


async def test_remote_compile_ignores_session_closed_for_other_pin(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    A ``OFFLOADER_PEER_LINK_CLOSED`` for an unrelated peer doesn't kill our job.

    The peer-link-closed listener is shared across every
    in-flight remote job on the bus. A close for a different
    receiver's session must not finalise this job — only the
    receiver matching ``job.source_pin_sha256`` should
    trigger the lost-session failure path. Without the pin
    filter, two paired offloaders building concurrently would
    take each other's jobs down on every reconnect.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    # Different pin — the listener must filter this out.
    _fire_session_closed(controller, pin="b" * 64, reason="transport_error")
    await asyncio.sleep(0)
    assert not runner.done()
    assert captured[EventType.JOB_FAILED] == []

    # Now the real terminal lands and the runner finishes
    # cleanly — proving the stray close didn't poison the
    # wait loop.
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)
    assert job.status == JobStatus.COMPLETED


async def test_remote_compile_cancel_before_runner_registers_event_still_fires(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    A cancel landed before the runner registered its event still fires the wire cancel.

    The race the event-driven design opened up: between
    ``_execute_job`` setting ``_current_job = job`` and the
    runner registering its ``cancel_event`` on the
    controller, the WS cancel handler may run. It would
    happily ``_cancel_requested.add(job_id)`` and then find
    no event in ``_cancel_events`` (the runner hasn't
    arrived yet) — so the ``set()`` is skipped and the
    runner parks on a future that will never wake. Without
    the late-bind replay at registration time, the runner
    hangs until either the receiver completes or the
    peer-link heartbeat surfaces ``session_lost``.

    The fix: at registration, the runner checks
    ``_cancel_requested`` and self-fires the event if the
    cancel already arrived. This test pins that path by
    flipping ``_cancel_requested`` before the runner
    starts, then asserting the runner still translates the
    cancel onto the wire (instead of hanging on its newly-
    created event).
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    # Cancel landed BEFORE the runner registers its event —
    # ``_cancel_requested`` carries the flag, but no entry
    # exists in ``_cancel_events`` yet (the runner hasn't
    # been called).
    controller.state.cancel_requested.add(job.job_id)

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    # The runner's bundle build + submit will still complete
    # (the cancel-aware ``_fail_locally`` short-circuit only
    # kicks in when one of those branches raises). The
    # registration's self-fire is what keeps the happy
    # dispatch path from hanging on the cancel event.
    await _wait_for_wire_cancel(client)
    client.cancel_job.assert_awaited_once_with(job_id=job.job_id)

    _fire_state(controller, job_id=job.job_id, status="cancelled")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_firmware_cancel_handler_wakes_remote_runner_via_event(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    The ``firmware/cancel`` WS handler signals the runner's cancel event.

    Pins the wiring on the controller side: the cancel
    handler must look up
    ``self.state.cancel_events[job_id]`` and call ``set()`` so a
    runner parked on
    ``asyncio.wait({..., cancel_wait})`` wakes immediately.
    Without that signal the runner would deadlock until the
    receiver pushed a terminal frame (or, before the
    event-driven refactor, until the next 0.5 s poll tick).
    Drives through ``controller.cancel(job_id=...)`` —
    rather than the ``_request_remote_cancel`` test helper —
    so the regression test is honest about the production
    code path.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    job = _make_remote_job()

    # The WS cancel handler refuses non-existent jobs and the
    # ``_current_job`` mismatch is a hard error — wire both so
    # the handler's ``RUNNING`` branch runs.
    controller.state.jobs[job.job_id] = job
    job.status = JobStatus.RUNNING
    controller.state.compile_lane.current_job = job

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    # Drive through the real handler — the cancel-event
    # signal is its only job for the REMOTE path (the
    # ``_terminate_current_process`` call is a no-op because
    # ``_current_process`` is None).
    await controller.cancel(job_id=job.job_id)
    await _wait_for_wire_cancel(client)
    client.cancel_job.assert_awaited_once_with(job_id=job.job_id)

    _fire_state(controller, job_id=job.job_id, status="cancelled")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_compile_cancel_after_remote_build_torn_down_finalises_locally(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """
    Cancel after ``_db.remote_build_offloader`` reset to ``None`` finalises CANCELLED locally.

    Teardown race: ``DeviceBuilder`` clears its
    ``remote_build`` controller during shutdown, but a
    remote-runner task may still be parked on
    ``_await_terminal`` for an unfinished job. If the user
    (or shutdown logic) then flips ``_cancel_requested``,
    the runner must not call into ``None._lookup_open_peer_link_client``.
    The defensive ``remote_build is None`` branch short-
    circuits to ``_finalize_cancelled`` so subscribers see a
    clean CANCELLED event.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    _, client = _wire_remote_build(controller)
    job = _make_remote_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)

    # Simulate the receiver-controller teardown race: clear
    # ``remote_build`` mid-flight, then register the cancel.
    controller._db.remote_build_offloader = None
    _request_remote_cancel(controller, job)

    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1
    assert job.job_id not in controller.state.cancel_requested


# ---------------------------------------------------------------------------
# UPLOAD / INSTALL — local flash step after receiver compile
# ---------------------------------------------------------------------------


def _make_remote_install_job(*, job_id: str = "remote-1", port: str = "OTA") -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration="kitchen.yaml",
        job_type=JobType.INSTALL,
        port=port,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
        source_label="desktop",
    )


def _wire_upload_subprocess(
    controller: Any,
    *,
    exit_code: int = 0,
    stdout: str = "Uploading 100%...\n",
) -> None:
    """Point the controller's runner at a python-one-liner ``esphome upload``.

    The runner builds the cmd as ``[*controller.state.esphome_cmd,
    '--dashboard', ..., 'upload', <yaml>, '--device', <port>,
    '--file', <staged_firmware>]``. Pointing
    ``_esphome_cmd`` at ``python -c <script>`` lets the test
    drive a deterministic subprocess (controlled stdout +
    exit code) without an actual esphome install on PATH.

    ``_build_cache_args`` is stubbed to return ``[]`` so the
    runner doesn't reach into the (absent in unit tests)
    devices controller's address cache. The runner's
    ``_tracked_subprocess`` is the real method — it
    registers the spawn with ``_current_process`` so the
    cancel-during-upload tests can SIGTERM the chain.
    """
    quoted_stdout = repr(stdout)
    script = (
        f"import sys; sys.stdout.write({quoted_stdout}); sys.stdout.flush(); sys.exit({exit_code})"
    )
    controller.state.esphome_cmd = [sys.executable, "-c", script]
    controller._build_cache_args = lambda _job: []


@pytest.fixture(autouse=True)
def patch_materialise(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Autouse: stub ``materialise_remote_artifacts`` so the runner skips real I/O.

    Every test in this file exercises the runner; the runner now
    materialises for every non-CLEAN COMPLETED job. Autouse keeps
    per-test signatures uncluttered; tests that want to inspect
    or override the stub still request the fixture by name.
    """
    stub = MagicMock(return_value=Path("/fake/staged/build/path"))
    monkeypatch.setattr(remote_runner, "materialise_remote_artifacts", stub)
    return stub


async def test_remote_install_resets_progress_between_compile_and_upload(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_materialise: MagicMock,
) -> None:
    """
    The compile → upload seam fires ``JOB_PROGRESS{0}`` and clears ``job.progress``.

    Pins the phase-transition reset. :func:`helpers._ingest_output_line`
    monotonically clamps, so a receiver-side compile that pushed
    the gauge near 100 via linker / PIO ``(N%)`` lines would
    silently suppress every ``Uploading: [..] 5% / 10% / ...``
    line the local flash subprocess emits (5 isn't > 95).
    Without an explicit reset at the boundary the progress bar
    appears frozen at the compile peak for the entire upload
    phase. The runner fires a 0% event at the start of
    :func:`remote_runner._fetch_and_run_local_upload` so the
    frontend visibly resets and subsequent upload percents
    advance the gauge from 0.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    _wire_upload_subprocess(controller, exit_code=0)
    job = _make_remote_install_job()
    # Pre-seed the gauge near 100, the way a receiver-side
    # compile's linker / PIO ``(95%)`` line would. Without the
    # phase reset the upload's lower percents would all be
    # silently clamped against this value.
    job.progress = 95

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=5.0)

    # The phase-transition reset is the first ``JOB_PROGRESS``
    # event the local bus sees from the upload half (we
    # pre-seeded ``job.progress=95`` directly on the model so
    # the compile half didn't go through the bus).
    assert captured[EventType.JOB_PROGRESS]
    assert captured[EventType.JOB_PROGRESS][0]["progress"] == 0
    assert captured[EventType.JOB_PROGRESS][0]["job_id"] == job.job_id


async def test_remote_install_completes_after_local_upload_succeeds(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_materialise: MagicMock,
    tmp_path: Any,
) -> None:
    """
    Happy path: receiver compile → download_artifacts → local upload → COMPLETED.

    Pins the end-to-end shape of the transparent-install
    REMOTE branch: dispatch is ``submit_job(target=compile)``,
    receiver returns completed, runner pulls the tarball back
    via ``download_artifacts``, stages ``firmware.bin`` to a
    tmpdir, and spawns the local ``esphome upload`` subprocess
    against the staged file. A zero exit code closes the job
    as ``COMPLETED`` and fires the local terminal event.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    _wire_upload_subprocess(controller, exit_code=0)
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=5.0)

    assert job.status == JobStatus.COMPLETED
    assert job.exit_code == 0
    assert len(captured[EventType.JOB_COMPLETED]) == 1
    client.download_artifacts.assert_awaited_once_with(job_id=job.job_id)
    # ``firmware.bin`` was staged + ``esphome upload`` saw it.
    patch_materialise.assert_called_once()


async def test_remote_install_local_upload_failure_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_materialise: MagicMock,
) -> None:
    """A non-zero ``esphome upload`` exit lands as JOB_FAILED with exit-code in error."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    _wire_upload_subprocess(controller, exit_code=7)
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=5.0)

    assert job.status == JobStatus.FAILED
    assert job.exit_code == 7
    assert job.error is not None and "exit 7" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_install_download_artifacts_failure_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
) -> None:
    """``download_artifacts`` failing surfaces in ``job.error`` with the receiver's reason."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(
        side_effect=DownloadArtifactsError("build dir wiped", reason="build_dir_missing")
    )
    _wire_remote_build(controller, client=client)
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "build dir wiped" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_install_materialise_failure_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Materialise failure (malformed tarball, missing member) lands as JOB_FAILED."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    monkeypatch.setattr(
        remote_runner,
        "materialise_remote_artifacts",
        MagicMock(side_effect=MaterialiseError("tarball missing required member: 'storage.json'")),
    )
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "materialise failed" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_remote_install_materialise_oserror_fires_job_failed(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError from materialise (disk full / permissions) lands as JOB_FAILED."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    _wire_remote_build(controller, client=client)
    monkeypatch.setattr(
        remote_runner,
        "materialise_remote_artifacts",
        MagicMock(side_effect=OSError("ENOSPC: disk full")),
    )
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=2.0)

    assert job.status == JobStatus.FAILED
    assert job.error is not None and "materialise IO error" in job.error
    assert len(captured[EventType.JOB_FAILED]) == 1


async def test_fetch_and_run_local_upload_pre_spawn_cancel_finalises_locally(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancel landing after ``_fetch_and_materialise`` returns but before the spawn."""
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    job = _make_remote_install_job()
    controller.state.cancel_requested.add(job.job_id)
    controller._tracked_subprocess = MagicMock(  # type: ignore[method-assign]
        side_effect=AssertionError("subprocess should not have spawned")
    )

    await remote_runner._fetch_and_run_local_upload(controller=controller, job=job, client=client)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_remote_install_cancel_during_local_upload_finalises_as_cancelled(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_materialise: MagicMock,
) -> None:
    """
    User Stop during the ``esphome upload`` subprocess finalises as CANCELLED.

    The runner's ``_tracked_subprocess`` registers the
    upload spawn with ``controller.state.compile_lane.current_process``, and
    ``FirmwareController.cancel``'s
    ``_terminate_current_process`` lands SIGTERM on the
    spawned tree. The subprocess exits non-zero (terminated
    by signal); the runner's post-spawn cancel-check
    routes through ``_finalize_cancelled`` rather than the
    FAILED branch the non-zero exit would normally trigger.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    # A subprocess that runs long enough for us to cancel it.
    _wire_upload_subprocess(
        controller,
        exit_code=0,
        stdout=(
            "import sys, time; sys.stdout.write('starting upload\\n'); "
            "sys.stdout.flush(); time.sleep(30)"
        ),
    )
    # Replace the script with one that actually sleeps, so the
    # subprocess is parked when we cancel.
    controller.state.esphome_cmd = [
        sys.executable,
        "-c",
        "import sys, time; sys.stdout.write('starting upload\\n'); "
        "sys.stdout.flush(); time.sleep(30)",
    ]

    async def _terminate(lane: Lane) -> None:
        assert lane.current_process is not None  # type narrowing
        lane.current_process.terminate()

    controller._terminate_current_process = _terminate  # type: ignore[method-assign]
    job = _make_remote_install_job()

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")

    # Wait until the subprocess is up.
    while controller.state.compile_lane.current_process is None:
        await asyncio.sleep(0.01)

    _request_remote_cancel(controller, job)
    await controller._terminate_current_process(controller.state.compile_lane)
    await asyncio.wait_for(runner, timeout=5.0)

    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_run_upload_subprocess_cancel_landing_between_pre_check_and_spawn_terminates(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_materialise: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cancel landing during the staging executor hops fires ``_terminate_current_process`` post-spawn.

    The runner has two cancel-check sites for this path:

    1. **Pre-spawn early check** in
       ``_fetch_and_run_local_upload`` — covered by
       ``test_fetch_and_run_local_upload_cancel_pre_spawn_skips_subprocess``.
    2. **Post-spawn in-context-manager check** in
       ``_run_upload_subprocess`` — fires when the cancel
       flag was *not* set at the pre-check moment but *is*
       set by the time the spawn returns. The window is the
       executor hops for ``mkdtemp`` + ``firmware_path.write_bytes``
       between the two checks.

    This test races a cancel into that window by patching
    ``firmware_path.write_bytes`` (via ``Path.write_bytes``)
    to flip ``_cancel_requested`` as a side effect — so by
    the time the spawn returns, the in-context check sees
    the flag and fires ``_terminate_current_process``.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    job = _make_remote_install_job()

    terminate_calls: list[None] = []

    async def _terminate(_lane: Lane) -> None:
        terminate_calls.append(None)

    controller._terminate_current_process = _terminate  # type: ignore[method-assign]
    # Subprocess emits one line + exits 0 — the
    # cancel-aware finalise routes through CANCELLED
    # regardless of the exit.
    _wire_upload_subprocess(controller, exit_code=0, stdout="ok\n")

    # Flip ``_cancel_requested`` from inside ``_build_cache_args``,
    # which runs after the pre-spawn check and before the
    # subprocess spawn. The in-context-manager check fires
    # post-spawn.
    def _build_cache_args_then_cancel(_job: object) -> list[str]:
        controller.state.cancel_requested.add(job.job_id)
        return []

    controller._build_cache_args = _build_cache_args_then_cancel  # type: ignore[method-assign]

    await remote_runner._fetch_and_run_local_upload(controller=controller, job=job, client=client)

    # The post-spawn check inside ``_run_upload_subprocess``
    # called ``_terminate_current_process``.
    assert terminate_calls, (
        "expected _terminate_current_process to fire from the in-context-manager cancel check"
    )
    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1


async def test_fetch_and_materialise_cancel_post_staging_finalises_locally(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_materialise: MagicMock,
) -> None:
    """Cancel landing during the wire / extract executor hops finalises the job locally.

    download_artifacts + materialise happen first (they can't be
    cleanly cancelled mid-flight); the post-staging cancel check
    then routes through ``_finalize_cancelled`` and returns
    ``False`` so the caller skips the spawn.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    job = _make_remote_install_job()
    controller.state.cancel_requested.add(job.job_id)

    proceed = await remote_runner._fetch_and_materialise(
        controller=controller, job=job, client=client
    )

    assert proceed is False
    assert job.status == JobStatus.CANCELLED
    assert len(captured[EventType.JOB_CANCELLED]) == 1
    client.download_artifacts.assert_awaited_once()
    patch_materialise.assert_called_once()


async def test_remote_upload_runs_the_same_local_flash_chain_as_install(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_materialise: MagicMock,
) -> None:
    """
    ``JobType.UPLOAD`` with REMOTE source follows the same artifact-fetch + flash chain.

    INSTALL and UPLOAD share the runner's UPLOAD/INSTALL
    branch — the difference between the two on the local
    subprocess path (compile-then-flash vs flash-only)
    doesn't matter here because the receiver did the
    compile already. Pinning this prevents a future
    branch that special-cases on ``job_type`` from
    silently breaking the UPLOAD path.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    _wire_upload_subprocess(controller, exit_code=0)
    job = FirmwareJob(
        job_id="upload-1",
        configuration="kitchen.yaml",
        job_type=JobType.UPLOAD,
        port="192.168.1.50",
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN,
    )

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=5.0)

    assert job.status == JobStatus.COMPLETED
    assert len(captured[EventType.JOB_COMPLETED]) == 1
    client.download_artifacts.assert_awaited_once()
