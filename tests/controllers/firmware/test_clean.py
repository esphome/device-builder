"""End-to-end coverage for ``FirmwareController.clean``.

Same shape as ``test_compile.py`` — ``clean`` is the smallest
submission handler after ``compile``: no port, no rename target,
just configuration → queued ``CLEAN`` job. The pieces it calls
are covered in isolation elsewhere
(``_validate_configuration_boundary`` in
``test_traversal_validation.py``, ``_create_job`` / ``_enqueue``
lifecycles across the broader suite); this file pins the
wiring.

Pinning matters because ``clean`` and ``compile`` share an
identical control-flow shape, and a refactor that "unifies" the
two handlers is the obvious accident that would silently flip
``CLEAN`` to ``COMPILE`` (or vice versa) without any production
test catching it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.build_scheduler import BuildSchedulerInputs
from esphome_device_builder.models import ErrorCode, EventType, JobSource, JobStatus, JobType
from esphome_device_builder.models.remote_build import PeerStatus, StoredPairing
from tests.controllers.firmware.conftest import (
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
)


def _wire_remote_build_with_peers(
    controller: object, *pairings_open: tuple[StoredPairing, bool]
) -> MagicMock:
    """Attach a ``remote_build`` stub whose snapshot lists *pairings*.

    Each tuple in ``pairings_open`` is ``(pairing, is_connected)``;
    the snapshot pins ``open_peer_links`` to the pin_sha256 of
    every entry whose ``is_connected`` is true. Returns the
    ``remote_build`` MagicMock so the test can also assert on
    follow-up controller calls if needed.
    """
    remote_build = MagicMock()
    remote_build.build_scheduler_snapshot.return_value = BuildSchedulerInputs(
        remote_builds_enabled=True,
        pairings={p.pin_sha256: p for (p, _) in pairings_open},
        open_peer_links=frozenset(p.pin_sha256 for (p, ok) in pairings_open if ok),
        peer_queue_status={},
    )
    # ``_db.remote_build_offloader`` is the controller's lookup site; replacing
    # the default ``None`` here is the minimum wiring the fan-out
    # path needs to enumerate connected approved peers.
    controller._db.remote_build_offloader = remote_build  # type: ignore[attr-defined]
    return remote_build


def _pairing(
    *,
    pin_sha256: str,
    label: str = "receiver",
    status: PeerStatus = PeerStatus.APPROVED,
    esphome_version: str = "",
) -> StoredPairing:
    # ``StoredPairing.pin_sha256`` is validated against a 64-char
    # min length (the wire format is lowercase hex SHA-256). The
    # test fixture's short identifiers (``"a"`` etc.) need to
    # be re-shaped to 64 chars before the dataclass accepts them.
    padded_pin = pin_sha256.ljust(64, "0")
    return StoredPairing(
        receiver_hostname="receiver.local",
        receiver_port=6055,
        pin_sha256=padded_pin,
        static_x25519_pub=b"\x00" * 32,
        label=label,
        paired_at=1.0,
        status=status,
        esphome_version=esphome_version,
    )


async def test_clean_returns_queued_job_with_clean_type(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``CLEAN``.

    The frontend's "live tasks" panel keys off ``status`` and
    ``job_type`` to render a row; pinning ``CLEAN`` here catches
    a refactor that defaults to ``COMPILE`` (the structurally
    identical neighbour — same handler shape, same control flow,
    just a different ``JobType`` constant).
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.CLEAN
    assert job.configuration == "kitchen.yaml"


async def test_clean_rejects_traversal_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    The validator helper itself is fully covered in
    ``test_traversal_validation.py``; pinning the wiring here
    too because every public WS submission handler needs the
    boundary gate, and a regression in this specific handler
    would mean a direct WS client could path-traverse via
    ``configuration`` even though every other submission
    handler stays gated.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.clean(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_clean_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract every other submission
    handler pins: a frontend that subscribes via
    ``firmware/follow_job`` on receipt of ``JOB_QUEUED`` would
    race the runner if the event broadcast preceded the queue
    insert — the follower could attach to a queue that hasn't
    seen the job yet, dropping the first line.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


async def test_clean_registers_job_in_jobs_map(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The new job is registered so ``get_job`` finds it by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job


@pytest.mark.parametrize(
    "active_type",
    ["compile", "upload", "install"],
)
async def test_clean_cancels_active_build_for_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    active_type: str,
) -> None:
    """``clean`` cancels an in-flight build for the same configuration.

    A clean is the user asking for a fresh build of that device, so the LOCAL
    clean's supersede cancels any queued compile / upload / install for the
    config rather than rejecting — and an install's COMPILE cascades to its
    held UPLOAD, so both halves go.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    if active_type == "compile":
        active = await controller.compile(configuration="kitchen.yaml")
    elif active_type == "upload":
        active = await controller.upload(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    else:
        active = await controller.install(configuration="kitchen.yaml")

    await controller.clean(configuration="kitchen.yaml")

    assert active.status == JobStatus.CANCELLED
    if active_type == "install":
        upload = next(
            j
            for j in controller.state.jobs.values()
            if j.job_type is JobType.UPLOAD and j.depends_on == active.job_id
        )
        assert upload.status == JobStatus.CANCELLED


async def test_reset_build_env_cancels_all_active_jobs(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """clean-all (reset_build_env) cancels every in-flight job across both lanes.

    The wipe trashes the whole build tree, so a concurrent compile or upload on
    either lane must be cancelled first — including an install's held upload.
    """
    (tmp_path / "alpha.yaml").write_text("")
    (tmp_path / "beta.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    compile_a = await controller.compile(configuration="alpha.yaml")
    install_b = await controller.install(configuration="beta.yaml")
    upload_b = next(
        j
        for j in controller.state.jobs.values()
        if j.job_type is JobType.UPLOAD and j.depends_on == install_b.job_id
    )

    reset = await controller.reset_build_env()

    assert compile_a.status == JobStatus.CANCELLED
    assert install_b.status == JobStatus.CANCELLED
    assert upload_b.status == JobStatus.CANCELLED
    assert reset.job_type is JobType.RESET_BUILD_ENV
    assert reset.status == JobStatus.QUEUED


async def test_clean_succeeds_when_active_build_targets_different_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A different device's build doesn't block cleaning this one.

    Sibling devices have independent build directories, so a
    compile on ``kitchen.yaml`` shouldn't prevent a clean on
    ``bedroom.yaml``.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "bedroom.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    other = await controller.compile(configuration="kitchen.yaml")
    other.status = JobStatus.RUNNING

    job = await controller.clean(configuration="bedroom.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.CLEAN


async def test_clean_supersedes_other_active_clean_on_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Two cleans for the same device still supersede.

    Re-running clean is harmless (just deletes build files
    already cleaned), and the second click is the user's
    explicit intent. Only compile/upload/install/rename block.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    first = await controller.clean(configuration="kitchen.yaml")
    first.status = JobStatus.RUNNING
    controller.state.compile_lane.current_job = first

    second = await controller.clean(configuration="kitchen.yaml")

    assert second.status == JobStatus.QUEUED
    assert second.job_type == JobType.CLEAN
    assert second.job_id != first.job_id


async def test_clean_succeeds_after_terminal_active_build(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A completed/failed/cancelled build doesn't block — only in-flight does.

    Terminal jobs hang around in ``_jobs`` for the recent-jobs
    history; the rejection check must filter them out so a
    crashed compile doesn't permanently lock the device out of
    cleaning.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    failed = await controller.compile(configuration="kitchen.yaml")
    failed.status = JobStatus.FAILED

    job = await controller.clean(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# Fan-out to connected paired receivers
# ---------------------------------------------------------------------------


async def test_clean_fans_out_to_connected_approved_peers(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """One ``clean`` click queues one LOCAL job + one REMOTE job per connected approved peer.

    The fan-out is what makes "Clean build files" actually clean
    every receiver this device might have been built on. Pre-fix
    a stale receiver-side build dir kept poisoning the next
    remote compile; this test pins that the operator's single
    click queues the expected per-peer REMOTE clean jobs so the
    receiver-side artifacts get dropped too.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    _wire_remote_build_with_peers(
        controller,
        (_pairing(pin_sha256="a", label="desktop", esphome_version="2026.5.0"), True),
        (_pairing(pin_sha256="b", label="laptop", esphome_version="2026.4.1"), True),
    )

    returned = await controller.clean(configuration="kitchen.yaml")

    # The handler returns the LOCAL clean — that's what the WS
    # client awaits — and the REMOTE jobs land silently in
    # _jobs via the fan-out.
    assert returned.source is JobSource.LOCAL
    assert returned.job_type is JobType.CLEAN
    # Crucially: every fan-out job stays QUEUED. The fan-out
    # passes ``supersede=False`` to ``_enqueue`` so the N+1 jobs
    # that all share one ``configuration`` don't cancel each
    # other. Pre-fix the default supersede semantics meant only
    # the last peer's clean survived (Copilot review on #608).
    # Assert on status, not just existence, so a regression that
    # re-introduced supersede shows up here rather than as silent
    # cancellation of every clean but the last one in production.
    clean_jobs = [j for j in controller.state.jobs.values() if j.job_type is JobType.CLEAN]
    assert len(clean_jobs) == 3  # 1 local + 2 remote
    assert all(j.status is JobStatus.QUEUED for j in clean_jobs), (
        "every fan-out job must stay QUEUED; if any are CANCELLED the "
        "supersede carve-out got dropped and only the last peer's clean ran"
    )
    assert returned.status is JobStatus.QUEUED

    remote_jobs = sorted(
        (j for j in clean_jobs if j.source is JobSource.REMOTE),
        key=lambda j: j.source_pin_sha256,
    )
    assert [j.source_pin_sha256 for j in remote_jobs] == [
        "a".ljust(64, "0"),
        "b".ljust(64, "0"),
    ]
    assert [j.source_label for j in remote_jobs] == ["desktop", "laptop"]
    assert [j.source_esphome_version for j in remote_jobs] == ["2026.5.0", "2026.4.1"]
    # Every fan-out job carries the same configuration the
    # operator clicked clean on.
    assert all(j.configuration == "kitchen.yaml" for j in clean_jobs)


async def test_clean_fan_out_does_not_supersede_sibling_jobs(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Regression test for the supersede-cancels-its-own-siblings bug.

    Pre-fix shape: ``_enqueue`` default-superseded any active job
    sharing the new job's ``configuration``. The clean fan-out
    queues N+1 jobs with one ``configuration``, so each
    ``_enqueue`` call cancelled its predecessors and only the
    LAST peer's clean survived. Locally-reproduced behaviour
    before the fix:

        clean src=local  pin=-          status=cancelled
        clean src=remote pin=a0...      status=cancelled
        clean src=remote pin=b0...      status=queued

    The fix passes ``supersede=False`` for fan-out remote jobs.
    This test pins the inverse of the failure mode: with two
    connected peers the local + both remotes all stay
    ``QUEUED``. A regression that drops the ``supersede=False``
    flag lands here as a CANCELLED status assertion fail rather
    than as a confusing "I clicked clean but my second receiver
    still has stale artifacts" report from the field.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    _wire_remote_build_with_peers(
        controller,
        (_pairing(pin_sha256="a", label="desktop"), True),
        (_pairing(pin_sha256="b", label="laptop"), True),
    )

    await controller.clean(configuration="kitchen.yaml")

    statuses = {
        (j.source.value, j.source_pin_sha256[:1] or "-"): j.status
        for j in controller.state.jobs.values()
        if j.job_type is JobType.CLEAN
    }
    assert statuses == {
        ("local", "-"): JobStatus.QUEUED,
        ("remote", "a"): JobStatus.QUEUED,
        ("remote", "b"): JobStatus.QUEUED,
    }


async def test_repeat_clean_supersedes_entire_prior_fan_out_batch(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A second clean click cancels every job from the first batch.

    Pins the supersede contract still works correctly for the
    user's "I clicked clean twice in a row" scenario, even with
    the fan-out carve-out: the second click's LOCAL clean
    enqueues with default ``supersede=True``, which walks every
    active job for the configuration and cancels it — sweeping
    the first batch's local clean AND every fan-out remote in
    one pass. The new batch's own remote fan-out then enqueues
    with ``supersede=False`` and stays intact.

    Without this guarantee a hyperactive operator clicking clean
    repeatedly would accumulate active jobs on the queue across
    every click. With it, the rule stays "the latest clean batch
    is the live one, all earlier batches are cancelled."
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    _wire_remote_build_with_peers(
        controller,
        (_pairing(pin_sha256="a", label="desktop"), True),
        (_pairing(pin_sha256="b", label="laptop"), True),
    )

    first_local = await controller.clean(configuration="kitchen.yaml")
    first_remotes = [
        j
        for j in controller.state.jobs.values()
        if j.source is JobSource.REMOTE
        and j.job_type is JobType.CLEAN
        and j.job_id != first_local.job_id
    ]
    assert first_local.status is JobStatus.QUEUED
    assert all(j.status is JobStatus.QUEUED for j in first_remotes)

    # Second click. The new LOCAL clean's ``supersede=True`` must
    # cancel the first local AND every first-batch fan-out remote.
    second_local = await controller.clean(configuration="kitchen.yaml")
    assert first_local.status is JobStatus.CANCELLED
    assert all(j.status is JobStatus.CANCELLED for j in first_remotes)

    # Second batch's own fan-out members still get to live, only
    # the second local + its two new remotes are active.
    active = [
        j
        for j in controller.state.jobs.values()
        if j.job_type is JobType.CLEAN and j.status is JobStatus.QUEUED
    ]
    assert {j.job_id for j in active} == {
        second_local.job_id,
        *(
            j.job_id
            for j in controller.state.jobs.values()
            if j.source is JobSource.REMOTE and j.status is JobStatus.QUEUED
        ),
    }
    # And the count is 1 + 2.
    assert len(active) == 3


async def test_clean_skips_disconnected_or_pending_peers(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Only APPROVED + currently-connected peers receive a fan-out job.

    PENDING rows can't accept submits at all (the receiver-side
    handler rejects them). Approved-but-disconnected rows would
    immediately FAIL on the runner's
    ``_lookup_open_peer_link_client`` step; queueing them just
    spams the firmware-jobs UI with predictable failures. Skip
    both — the next clean while the peer is online catches up.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    _wire_remote_build_with_peers(
        controller,
        # APPROVED + connected: gets a job.
        (_pairing(pin_sha256="c", label="online"), True),
        # APPROVED + disconnected: skipped.
        (_pairing(pin_sha256="d", label="offline"), False),
        # PENDING (regardless of connection state): skipped.
        (
            _pairing(pin_sha256="e", label="pending", status=PeerStatus.PENDING),
            True,
        ),
    )

    await controller.clean(configuration="kitchen.yaml")

    remote_pins = {
        j.source_pin_sha256
        for j in controller.state.jobs.values()
        if j.source is JobSource.REMOTE and j.job_type is JobType.CLEAN
    }
    assert remote_pins == {"c".ljust(64, "0")}


async def test_clean_with_no_remote_build_controller_skips_fan_out(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Pre-``start()`` race where ``remote_build`` is still ``None`` cleans local only.

    The firmware controller is constructed before
    ``DeviceBuilder.start()`` wires up the remote-build
    controller. A clean click that lands in that window must
    still run the local job; the fan-out simply produces no
    remote jobs. Mirrors the same defensive null-check that
    ``_resolve_install_source`` uses.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    # Default factory leaves ``_db.remote_build_offloader = None``.

    returned = await controller.clean(configuration="kitchen.yaml")

    assert returned.source is JobSource.LOCAL
    # No REMOTE jobs queued.
    assert not any(j.source is JobSource.REMOTE for j in controller.state.jobs.values())
