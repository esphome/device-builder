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

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.build_scheduler import BuildSchedulerInputs
from esphome_device_builder.models import (
    ErrorCode,
    EventType,
    JobSource,
    JobStatus,
    JobType,
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    StoredPairing,
)
from tests.controllers.firmware.conftest import (
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
)


async def test_install_returns_queued_job_with_install_type(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``INSTALL``.

    The frontend keys its "live tasks" panel off the ``status`` and
    ``job_type`` fields; pin both so a future refactor that defaults
    to ``COMPILE`` (the most common job type) shows up immediately.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.INSTALL
    assert job.configuration == "kitchen.yaml"


async def test_install_defaults_port_to_ota(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``port`` defaults to ``"OTA"``, not the empty ``upload`` default.

    The CLI treats ``"OTA"`` as a request to resolve the configured
    device's address from the YAML. The ``upload`` handler keeps
    the empty default for backward compat with the legacy spawn
    protocol; ``install`` defaults to ``"OTA"`` so the common case
    of "flash the device named in the YAML" doesn't need a port
    arg from the caller.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.port == "OTA"


@pytest.mark.parametrize(
    "port",
    ["/dev/ttyUSB0", "192.168.1.5", "kitchen.local", "fe80::1"],
)
async def test_install_forwards_custom_port_to_job(
    tmp_path: Path, port: str, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Caller-supplied port shapes (serial / IP / hostname) round-trip onto the job.

    ``_build_command`` reads ``job.port`` to render the
    ``--device`` flag at compile time; if the handler dropped or
    mutated the value here, the install would silently re-target
    OTA instead of the user-named address.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml", port=port)

    assert job.port == port


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


async def test_install_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    The all-jobs panel keys off ``JOB_QUEUED`` to add a row when a
    new job lands; without this event the panel goes silent until
    the first ``JOB_OUTPUT`` line arrives (sometimes a few seconds
    later for cold-start compiles).

    Ordering matters: ``_enqueue`` calls ``await self.state.queue.put``
    *before* firing the bus event. A frontend that receives
    ``JOB_QUEUED`` and immediately calls ``firmware/follow_job``
    races the runner — if the event broadcast preceded the queue
    insert, the follower could attach to a queue that hasn't seen
    the job yet, producing a dropped first line. Verify both
    halves: the event fires with the right payload, *and* the
    queue had already received the job by the time the event
    fired.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


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
    """Build a passing :class:`StoredPairing` for the scheduler tests."""
    return StoredPairing(
        receiver_hostname="build.local",
        receiver_port=6055,
        pin_sha256=_PIN,
        static_x25519_pub=b"\x01" * 32,
        label=label,
        paired_at=1.0,
        status=PeerStatus.APPROVED,
        esphome_version=esphome_version,
    )


def _stub_remote_build(
    controller: Any,
    *,
    pairings: list[StoredPairing] | None = None,
    open_pins: frozenset[str] = frozenset(),
    idle_pins: frozenset[str] = frozenset(),
) -> None:
    """
    Wire a stub ``_db.remote_build_offloader`` with a scripted scheduler snapshot.

    The scheduler walks ``pairings`` (APPROVED-only) and
    requires membership in ``open_pins`` for the peer-link
    session gate. ``idle_pins`` controls which pairings get
    an ``idle=True`` snapshot entry; pairings *not* listed in
    ``idle_pins`` have no entry at all. Under the two-tier
    scheduler policy the first pass picks oldest-idle and
    the second pass queues on oldest-otherwise — so a busy
    receiver (open + not idle) routes REMOTE on the second
    pass when no idle candidate exists. Pre-two-tier this
    helper's docstring claimed "open_pins + idle entry"
    *gated* the candidate; that's no longer accurate. Tests
    that want LOCAL routing have to omit the pairing from
    ``open_pins`` or skip the pairing fixture entirely.
    """
    rows = pairings or []
    pairings_map = {p.pin_sha256: p for p in rows}
    queue_status = {
        pin: PeerQueueStatusSnapshotEntry(
            receiver_hostname="build.local",
            receiver_port=6055,
            pin_sha256=pin,
            idle=True,
            running=False,
            queue_depth=0,
        )
        for pin in idle_pins
    }
    remote_build = MagicMock()
    remote_build.build_scheduler_snapshot.return_value = BuildSchedulerInputs(
        remote_builds_enabled=True,
        pairings=pairings_map,
        open_peer_links=open_pins,
        peer_queue_status=queue_status,
    )
    remote_build.get_pairing.side_effect = pairings_map.get
    controller._db.remote_build_offloader = remote_build


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
    An APPROVED + connected + idle pairing routes the install to REMOTE.

    Pins the transparent install flow's user-visible
    behaviour: Install with a paired build server up routes
    transparently to that server; the user doesn't choose,
    the scheduler decides. ``source_pin_sha256`` carries the
    machine handle the runner uses to look up the
    PeerLinkClient; ``source_label`` is the display string
    the install dialog renders as "Building on
    {receiver_label}".
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

    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == _PIN
    assert job.source_label == "desktop"
    assert job.source_esphome_version == "2026.5.0"


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

    assert job.source is JobSource.REMOTE


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

    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == _PIN


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

    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == _PIN


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


async def test_install_falls_back_to_local_when_scheduler_picked_pin_disappeared(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    Scheduler picks a pin → ``get_pairing`` returns ``None`` → falls back to LOCAL.

    Defensive against a TOCTOU window: the scheduler walks
    one snapshot, then ``_resolve_install_source`` looks up
    the chosen pin's label from a fresh ``get_pairing`` call.
    If an ``unpair`` ran on the same loop tick between the
    two reads, the second read returns ``None`` and we
    silently fall back to LOCAL — feeding an empty
    ``source_pin_sha256`` to the runner would otherwise land
    on its missing-pin FAILED branch.

    Near-impossible in practice but the typed-return surface
    is the gate, so pin it.
    """
    controller = firmware_controller_factory(with_queue=True)
    # Scheduler picks ``_PIN`` (snapshot says it's APPROVED +
    # connected + idle), but ``get_pairing(_PIN)`` returns
    # ``None`` — the unpair landed between the two reads.
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
    # ``get_pairing`` returns None — the unpair happened
    # after the snapshot was taken.
    remote_build.get_pairing.return_value = None
    controller._db.remote_build_offloader = remote_build
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.source is JobSource.LOCAL
    assert job.source_pin_sha256 == ""


async def test_install_bulk_routes_each_config_through_the_scheduler(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """
    ``install_bulk`` resolves the install source per-config.

    Every entry in the bulk call goes through
    ``_resolve_install_source``, so a bulk request lands all
    eligible jobs as REMOTE when a paired receiver is healthy
    + idle (and stays LOCAL when none is available). Pins the
    per-config shape so a future refactor that hoists the
    scheduler call to call-time-once-and-share doesn't
    silently drop the per-job ``source_label`` stamp.
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
    (tmp_path / "garage.yaml").write_text("")
    (tmp_path / "office.yaml").write_text("")

    jobs = await controller.install_bulk(
        configurations=["kitchen.yaml", "garage.yaml", "office.yaml"]
    )

    assert [j.source for j in jobs] == [JobSource.REMOTE] * 3
    assert all(j.source_pin_sha256 == _PIN for j in jobs)
    assert all(j.source_label == "desktop" for j in jobs)


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

    assert [j.source for j in jobs] == [JobSource.REMOTE, JobSource.REMOTE]
