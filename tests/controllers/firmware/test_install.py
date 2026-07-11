"""End-to-end coverage for ``FirmwareController.install``.

The handler itself is small — it forwards to ``_validate_port``,
``_validate_configuration_boundary``, ``_create_job`` and
``_enqueue``. Each piece is tested in isolation elsewhere
(``test_install_to_specific_address.py`` for port shapes,
``test_traversal_validation.py`` for configuration validation,
``test_rename_lock.py`` for lock handling). What was missing was
the wiring: that ``install`` actually composes those pieces with
the right defaults and order. This file pins:

- Happy path returns a queued ``FirmwareJob`` with
  ``JobType.INSTALL`` and the user-supplied port.
- ``port`` defaults to ``"OTA"`` (not the empty string the
  ``upload`` handler uses).
- A bad ``port`` is rejected before the (potentially expensive)
  configuration validation runs — so a typo with a missing config
  still names the port as the offending input.
- ``JOB_QUEUED`` fires with the new job after enqueue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware.constants import _TARGET_OFFLINE_DEFERRED_ERROR
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.build_scheduler import BuildSchedulerInputs
from esphome_device_builder.models import (
    DeviceState,
    ErrorCode,
    EventType,
    JobSource,
    JobStatus,
    JobType,
    PeerQueueStatusSnapshotEntry,
    StoredPairing,
)
from tests.controllers.firmware.conftest import (
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
    attach_device,
    build_scheduler_inputs,
    stub_offloader,
    stub_pairing,
)
from tests.controllers.firmware.conftest import (
    upload_of as _upload_of,
)


async def test_install_creates_compile_then_dependent_upload(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Install splits into a COMPILE job + a dependent UPLOAD job (the chain).

    The handler returns the COMPILE head; the UPLOAD is held (QUEUED but
    off its lane) until the compile succeeds, so the network flash runs on
    the upload lane without blocking the next compile.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml")

    assert compile_job.job_type is JobType.COMPILE
    assert compile_job.status == JobStatus.QUEUED
    assert compile_job.depends_on == ""
    upload = _upload_of(controller, compile_job)
    assert upload.job_type is JobType.UPLOAD
    assert upload.status == JobStatus.QUEUED
    assert upload.configuration == "kitchen.yaml"


async def test_install_defaults_upload_port_to_ota(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``port`` defaults to ``"OTA"`` and lands on the UPLOAD half of the chain."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml")

    assert _upload_of(controller, compile_job).port == "OTA"


@pytest.mark.parametrize(
    "port",
    ["/dev/ttyUSB0", "192.168.1.5", "kitchen.local", "fe80::1"],
)
async def test_install_forwards_custom_port_to_upload(
    tmp_path: Path, port: str, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Caller-supplied port shapes round-trip onto the chain's UPLOAD job."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml", port=port)

    assert _upload_of(controller, compile_job).port == port


async def test_install_validates_port_before_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A typo'd port raises before the configuration validator runs.

    ``_validate_port`` is the first line of the handler. Its check
    is sub-microsecond; the configuration validator wraps a real
    ``Path.resolve`` syscall through an executor. Putting port
    first means a request that's bad on both fronts surfaces the
    cheap-to-detect failure first — and the offending value named
    in the error message identifies the *port*, not the
    configuration.

    Pin the order with a configuration the boundary validator
    would actually reject (a traversal payload). A swap of the
    two checks would surface the configuration error
    ("Invalid configuration filename …") instead of the
    port-shape error, and this assertion catches it.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.install(configuration="../etc/passwd", port="not a port")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "not a port" in exc.value.message
    assert "Invalid configuration filename" not in exc.value.message


async def test_install_rejects_traversal_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    Already covered for every install / compile / upload variant in
    ``test_traversal_validation.py``'s ``_validate_configuration_boundary``
    suite; pinning it here too because ``install`` is the busiest
    public entry point and a regression in this handler specifically
    would be felt by every "Update" button click.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.install(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_install_compile_enqueued_before_firing_its_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """The compile lands on its lane *before* its ``JOB_QUEUED`` fires.

    A follower attaching on ``JOB_QUEUED`` must find the job already
    queued, else it races the runner and drops the first line. The
    held UPLOAD has no PUT (it's off its lane until the compile
    succeeds) but still fires ``JOB_QUEUED`` so it renders as queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml")

    put_idx = next(
        i for i, (step, item) in enumerate(log) if step is EnqueueStep.PUT and item is compile_job
    )
    fire_idx = next(
        i
        for i, (step, item) in enumerate(log)
        if step is EnqueueStep.FIRE and item.data["job"] is compile_job
    )
    assert put_idx < fire_idx
    fired = [item.data["job"] for step, item in log if step is EnqueueStep.FIRE]
    assert _upload_of(controller, compile_job) in fired  # held upload still announced


async def test_cancelling_queued_compile_cancels_held_upload(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling the compile in the UI must not go on to upload (the key #3702 guard).

    A queued install's compile is cancelled before it runs; the held
    UPLOAD cascades to CANCELLED so the device is never flashed from a
    build the user aborted.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    upload = _upload_of(controller, compile_job)

    await controller.cancel(job_id=compile_job.job_id)

    assert compile_job.status == JobStatus.CANCELLED
    assert upload.status == JobStatus.CANCELLED


async def test_cancelling_queued_compile_persists_the_cascade(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The cascade-cancel of the held upload is persisted, not left QUEUED on disk.

    The QUEUED-cancel path persists the compile before cascading; without a
    second persist the upload's CANCELLED status never reaches disk and a
    restart would re-cancel it every boot.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    controller._persist_jobs.reset_mock()

    await controller.cancel(job_id=compile_job.job_id)

    # Two persists: the cancelled compile (before fire) + the cascaded upload.
    assert controller._persist_jobs.await_count == 2


async def test_failed_compile_cancels_held_upload(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A compile that fails cancels its held upload — no flash after a broken build."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    upload = _upload_of(controller, compile_job)

    controller._finalize_terminal(compile_job, JobStatus.FAILED)

    assert upload.status == JobStatus.CANCELLED


async def test_successful_compile_releases_upload_to_upload_lane(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A successful compile releases its held upload onto the upload lane."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    upload = _upload_of(controller, compile_job)
    assert controller.state.upload_lane.queue.qsize() == 0  # held until compile done

    controller._finalize_terminal(compile_job, JobStatus.COMPLETED)

    assert upload.status == JobStatus.QUEUED
    assert controller.state.upload_lane.queue.qsize() == 1


async def test_compile_completion_defers_upload_when_device_went_offline(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A device that went OFFLINE during the compile converts the chain to a queued update.

    The enqueue-time check saw a not-yet-OFFLINE device; re-checking at
    release cancels the held upload instead of flashing a dead address.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    upload = _upload_of(controller, compile_job)
    attach_device(controller, "kitchen.yaml", DeviceState.OFFLINE)

    controller._finalize_terminal(compile_job, JobStatus.COMPLETED)

    assert compile_job.is_deferred_install is True
    assert upload.status == JobStatus.CANCELLED
    assert upload.error == _TARGET_OFFLINE_DEFERRED_ERROR
    assert controller.state.upload_lane.queue.qsize() == 0


@pytest.mark.parametrize(
    ("install_kwargs", "state"),
    [
        pytest.param({}, DeviceState.ONLINE, id="online"),
        pytest.param({}, DeviceState.UNKNOWN, id="unknown"),
        pytest.param({"bootloader": True}, DeviceState.OFFLINE, id="bootloader"),
        pytest.param({"port": "192.168.1.5"}, DeviceState.OFFLINE, id="explicit_target"),
    ],
)
async def test_compile_completion_releases_upload_when_not_deferrable(
    tmp_path: Path,
    install_kwargs: dict[str, Any],
    state: DeviceState,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Only a known-OFFLINE OTA app target converts; everything else releases normally.

    ONLINE and the UNKNOWN startup window release, and the bootloader /
    explicit-target carve-outs mirror the enqueue-time gate.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml", **install_kwargs)
    upload = _upload_of(controller, compile_job)
    attach_device(controller, "kitchen.yaml", state)

    controller._finalize_terminal(compile_job, JobStatus.COMPLETED)

    assert compile_job.is_deferred_install is False
    assert upload.status == JobStatus.QUEUED
    assert controller.state.upload_lane.queue.qsize() == 1


async def test_converted_chain_arms_via_the_job_completed_listener(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The conversion lands before the terminal fire, so the arming hook sees the flag."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    attach_device(controller, "kitchen.yaml", DeviceState.OFFLINE)
    controller._db.bus.add_listener(EventType.JOB_COMPLETED, controller._handle_job_completed)

    controller._finalize_terminal(compile_job, JobStatus.COMPLETED)

    controller._db.devices.set_queued_update.assert_called_with("kitchen.yaml")


async def test_failed_upload_arms_via_the_job_failed_listener(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A released upload that fails against a now-OFFLINE device arms through the bus."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    (tmp_path / "kitchen.yaml").write_text("")
    compile_job = await controller.install(configuration="kitchen.yaml")
    upload = _upload_of(controller, compile_job)
    controller._finalize_terminal(compile_job, JobStatus.COMPLETED)
    attach_device(controller, "kitchen.yaml", DeviceState.OFFLINE)
    controller._db.bus.add_listener(EventType.JOB_FAILED, controller._handle_job_failed)

    controller._finalize_terminal(upload, JobStatus.FAILED, error="resolve failed")

    assert upload.is_deferred_install is True
    controller._db.devices.set_queued_update.assert_called_with("kitchen.yaml")


async def test_reinstalling_supersedes_prior_chain_without_raising(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Re-installing a config supersedes the prior chain without re-raising.

    The cascade cancels the held upload, so the supersede loop later hits an
    already-cancelled job; ``cancel``'s ``CommandError`` must be swallowed.
    """
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    (tmp_path / "kitchen.yaml").write_text("")
    first = await controller.install(configuration="kitchen.yaml")
    first_upload = _upload_of(controller, first)

    second = await controller.install(configuration="kitchen.yaml")

    assert first.status == JobStatus.CANCELLED
    assert first_upload.status == JobStatus.CANCELLED
    assert second.status == JobStatus.QUEUED
    assert _upload_of(controller, second).status == JobStatus.QUEUED


async def test_install_registers_job_in_jobs_map(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The new job is registered so ``get_job`` finds it by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job


# ---------------------------------------------------------------------------
# Scheduler integration — install routes through pick_build_path
# ---------------------------------------------------------------------------


_PIN = "a" * 64


def _make_pairing(label: str = "desktop", esphome_version: str = "") -> StoredPairing:
    """Build a passing :class:`StoredPairing` (fixed ``_PIN``) for the scheduler tests."""
    return stub_pairing(pin_sha256=_PIN, label=label, esphome_version=esphome_version)


def _stub_remote_build(
    controller: Any,
    *,
    pairings: list[StoredPairing] | None = None,
    open_pins: frozenset[str] = frozenset(),
    idle_pins: frozenset[str] = frozenset(),
) -> None:
    """Wire a stub ``_db.remote_build_offloader`` with a scripted scheduler snapshot.

    ``idle_pins`` get an ``idle=True`` queue entry; an open-but-not-idle
    pairing still routes REMOTE (the scheduler's second pass), so LOCAL
    routing means omitting the pairing from ``open_pins`` entirely.
    """
    stub_offloader(
        controller,
        build_scheduler_inputs(
            pairings=pairings or [],
            open_pins=set(open_pins),
            idle_pins=set(idle_pins),
        ),
    )


async def test_install_routes_to_local_when_no_paired_receivers(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """No paired receivers → ``install`` falls through to LOCAL.

    The scheduler only picks REMOTE when at least one
    APPROVED + connected + idle pairing is available. A fresh
    dashboard with no pairings stays on the local subprocess
    pipeline — the existing behaviour, with no user-visible
    change.
    """
    controller = firmware_controller_factory(with_queue=True)
    _stub_remote_build(controller, pairings=[])
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.LOCAL
    assert job.source_pin_sha256 == ""
    assert job.source_label == ""


async def test_install_routes_to_remote_when_pairing_is_idle_and_connected(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    An eligible pairing marks the install ``REMOTE_PENDING``; the pin binds at dispatch.

    Submit only decides remote-eligibility — the dispatch pool
    picks *which* server (and stamps the pin / label / version)
    when one frees, so ``source_pin_sha256`` is empty here.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing(label="desktop", esphome_version="2026.5.0")
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    assert job.source_label == ""
    assert job.source_esphome_version == ""


async def test_install_force_local_bypasses_scheduler(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    ``force_local=True`` keeps the install LOCAL even with an idle pairing.

    Pins the override path the install dialog's "Build
    locally instead" link uses: an idle APPROVED paired
    receiver would normally route REMOTE, but the operator
    can opt out and get a LOCAL build regardless. Mirrors
    the scheduler-disabled-by-master-switch shape but is a
    per-install decision rather than a global one — the
    next install (without the flag) routes REMOTE again as
    usual.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing(label="desktop")
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml", force_local=True)

    assert job.source is JobSource.LOCAL
    assert job.source_pin_sha256 == ""
    assert job.source_label == ""


async def test_compile_force_local_bypasses_scheduler(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/compile`` with ``force_local=True`` skips the remote-build route."""
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml", force_local=True)

    assert job.source is JobSource.LOCAL
    assert job.source_pin_sha256 == ""


async def test_compile_bulk_force_local_bypasses_scheduler(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/compile_bulk`` with ``force_local=True`` keeps every job LOCAL."""
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "garage.yaml").write_text("")

    jobs = await controller.compile_bulk(
        configurations=["kitchen.yaml", "garage.yaml"], force_local=True
    )

    assert [j.source for j in jobs] == [JobSource.LOCAL, JobSource.LOCAL]


async def test_install_force_local_default_false_keeps_scheduler_behaviour(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    Default ``force_local=False`` keeps the transparent-install routing.

    Pin the default to catch a future regression that flips
    the flag's default to ``True`` — every existing caller
    would silently lose the transparent-install routing.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing(label="desktop")
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.REMOTE_PENDING


async def test_install_still_routes_remote_when_receiver_is_busy(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    A busy paired receiver still wins REMOTE — receiver queues the dispatch.

    The scheduler's two-tier pick prefers idle pairings first
    but falls through to busy ones (rather than LOCAL) when
    no idle candidate exists. Receiver-side firmware queues
    drain the backlog; silent fallback to LOCAL here used to
    split the fleet across two compile contexts (warm
    receiver toolchain vs cold local) and re-flash from a
    different build than the first Install. A future
    per-install "Force local" override link in the install
    dialog is the user-facing opt-out.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    # APPROVED + connected, but ``idle_pins`` is empty so the
    # first-pass idle preference skips it. Second pass picks
    # the same (only) pairing and queues on the receiver.
    _stub_remote_build(controller, pairings=[pairing], open_pins=frozenset({_PIN}))
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""


async def test_install_serial_port_can_route_remote(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Serial ports are eligible for REMOTE source routing.

    With the materialise-locally runner the offloader stages
    the receiver's full build tree and spawns ``esphome upload
    <yaml> --device <port>`` (no ``--file``). That handles
    multi-image ESP32 wired flash cleanly via esphome's normal
    per-platform dispatch.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")

    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""


async def test_install_falls_back_to_local_when_remote_build_controller_absent(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    ``_db.remote_build_offloader is None`` falls through to LOCAL without raising.

    Production sets ``DeviceBuilder.remote_build`` during
    ``start()``; a firmware-queue restart-recovery path that
    fires before remote-build start would otherwise reach
    into ``None``. The resolver's None check is the gate.
    """
    controller = firmware_controller_factory(with_queue=True)
    controller._db.remote_build_offloader = None
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.LOCAL


async def test_install_marks_remote_pending_without_reading_get_pairing(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    Submit defers the pin: it marks ``REMOTE_PENDING`` without a ``get_pairing`` read.

    The old TOCTOU (snapshot picks a pin, ``get_pairing`` finds it
    unpaired) moved to dispatch, where ``remote_dispatch`` binds the
    server. So ``get_pairing`` is never consulted at submit.
    """
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    remote_build = MagicMock()
    remote_build.build_scheduler_snapshot.return_value = BuildSchedulerInputs(
        remote_builds_enabled=True,
        pairings={_PIN: pairing},
        open_peer_links=frozenset({_PIN}),
        peer_queue_status={
            _PIN: PeerQueueStatusSnapshotEntry(
                receiver_hostname="build.local",
                receiver_port=6055,
                pin_sha256=_PIN,
                idle=True,
                running=False,
                queue_depth=0,
            ),
        },
    )
    controller._db.remote_build_offloader = remote_build
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    remote_build.get_pairing.assert_not_called()


async def test_install_bulk_routes_each_config_through_the_scheduler(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``install_bulk`` resolves each config's source, marking every eligible one REMOTE_PENDING."""
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing(label="desktop")
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "garage.yaml").write_text("")
    (tmp_path / "office.yaml").write_text("")

    jobs = await controller.install_bulk(
        configurations=["kitchen.yaml", "garage.yaml", "office.yaml"]
    )

    assert [j.source for j in jobs] == [JobSource.REMOTE_PENDING] * 3
    assert all(j.source_pin_sha256 == "" for j in jobs)


async def test_install_bulk_serial_port_routes_every_config_remote(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Serial-port bulk install routes every config to REMOTE when a paired peer is open."""
    controller = firmware_controller_factory(with_queue=True)
    pairing = _make_pairing()
    _stub_remote_build(
        controller,
        pairings=[pairing],
        open_pins=frozenset({_PIN}),
        idle_pins=frozenset({_PIN}),
    )
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "garage.yaml").write_text("")

    jobs = await controller.install_bulk(
        configurations=["kitchen.yaml", "garage.yaml"], port="/dev/ttyUSB0"
    )

    assert [j.source for j in jobs] == [JobSource.REMOTE_PENDING, JobSource.REMOTE_PENDING]
