"""
Tests for :mod:`controllers.firmware.remote_dispatch` — the build-server pool.

Drive ``_dispatch_pending`` against a scripted offloader snapshot and
assert how ``REMOTE_PENDING`` compiles bind to free servers: concurrent
use of every connected server, mid-queue pickup of a new host, one job
per server, the ``WAIT`` hold, and the LOCAL / NO_COMPATIBLE_PEER
fallbacks. Cancel of a pending and an in-flight remote compile is
pinned here too (the off-lane ``cancel`` branches).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import remote_dispatch, runner
from esphome_device_builder.controllers.firmware.remote_runner import RemoteServerLostError
from esphome_device_builder.helpers.async_ import create_eager_task
from esphome_device_builder.helpers.build_scheduler import BuildSchedulerInputs
from esphome_device_builder.helpers.version_compat import VersionMatchPolicy
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobSource,
    JobStatus,
    JobType,
    StoredPairing,
)

from .conftest import build_scheduler_inputs, stub_offloader
from .conftest import stub_pairing as _pairing_kw

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory

pytestmark = pytest.mark.asyncio

_PIN_A = "a" * 64
_PIN_B = "b" * 64
_PIN_C = "c" * 64

# Thin positional-pin / ``version=`` adapters over the shared conftest
# factories so the call sites below read compactly.
_stub_offloader = stub_offloader


def _pairing(pin: str, *, paired_at: float, label: str = "srv", version: str = "") -> StoredPairing:
    return _pairing_kw(pin_sha256=pin, paired_at=paired_at, label=label, esphome_version=version)


def _snapshot(
    pairings: list[StoredPairing],
    *,
    open_pins: set[str],
    idle_pins: set[str],
    offloader_version: str = "",
    policy: VersionMatchPolicy = VersionMatchPolicy.ANY,
    include_local_in_pool: bool = False,
) -> BuildSchedulerInputs:
    return build_scheduler_inputs(
        pairings=pairings,
        open_pins=open_pins,
        idle_pins=idle_pins,
        offloader_version=offloader_version,
        policy=policy,
        include_local_in_pool=include_local_in_pool,
    )


def _add_pending(controller: object, job_id: str, *, config: str = "dev.yaml") -> FirmwareJob:
    """Register a ``REMOTE_PENDING`` compile in ``state.jobs`` and the pending pool."""
    job = FirmwareJob(
        job_id=job_id,
        configuration=config,
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE_PENDING,
    )
    controller.state.jobs[job_id] = job
    controller.state.remote_dispatch.pending[job_id] = job
    return job


async def test_every_connected_server_compiles_concurrently(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Three idle servers + a backlog → one pass binds one job to each, distinctly."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    snapshot = _snapshot(
        [
            _pairing(_PIN_A, paired_at=1.0),
            _pairing(_PIN_B, paired_at=2.0),
            _pairing(_PIN_C, paired_at=3.0),
        ],
        open_pins={_PIN_A, _PIN_B, _PIN_C},
        idle_pins={_PIN_A, _PIN_B, _PIN_C},
    )
    _stub_offloader(controller, snapshot)
    for jid in ("j1", "j2", "j3", "j4"):
        _add_pending(controller, jid)

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert set(pool.in_flight) == {"j1", "j2", "j3"}
    assert set(pool.job_peer.values()) == {_PIN_A, _PIN_B, _PIN_C}
    # Fourth waits: every server is busy this pass.
    assert set(pool.pending) == {"j4"}


async def test_host_added_mid_queue_is_used(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A server appearing between passes pulls a still-waiting compile."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    offloader = _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    _add_pending(controller, "j1")
    _add_pending(controller, "j2")

    await remote_dispatch._dispatch_pending(controller)
    assert controller.state.remote_dispatch.job_peer == {"j1": _PIN_A}
    assert set(controller.state.remote_dispatch.pending) == {"j2"}

    # A second host connects; the next pass binds the waiting compile to it.
    offloader.build_scheduler_snapshot.return_value = _snapshot(
        [_pairing(_PIN_A, paired_at=1.0), _pairing(_PIN_B, paired_at=2.0)],
        open_pins={_PIN_A, _PIN_B},
        idle_pins={_PIN_A, _PIN_B},
    )
    await remote_dispatch._dispatch_pending(controller)

    assert controller.state.remote_dispatch.job_peer["j2"] == _PIN_B
    assert not controller.state.remote_dispatch.pending


async def test_freed_server_pulls_next_compile(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """When the in-flight job finishes, the freed server takes the next waiter."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    _add_pending(controller, "j1")
    _add_pending(controller, "j2")

    await remote_dispatch._dispatch_pending(controller)
    assert controller.state.remote_dispatch.job_peer == {"j1": _PIN_A}

    # Simulate j1 finishing: the driver's finally drops it from the pool.
    pool = controller.state.remote_dispatch
    pool.in_flight.pop("j1")
    pool.job_peer.pop("j1")

    await remote_dispatch._dispatch_pending(controller)
    assert pool.job_peer == {"j2": _PIN_A}
    assert not pool.pending


async def test_one_job_per_server(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """One server, three waiting compiles → exactly one runs, two hold."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    for jid in ("j1", "j2", "j3"):
        _add_pending(controller, jid)

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert set(pool.in_flight) == {"j1"}
    assert set(pool.pending) == {"j2", "j3"}


async def test_no_server_connected_falls_back_to_local(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """No reachable server → the compile flips LOCAL onto the compile lane, never stranded."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    # Paired but disconnected (not in open_peer_links).
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins=set(), idle_pins=set()),
    )
    job = _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    assert job.source is JobSource.LOCAL
    assert job.source_pin_sha256 == ""
    assert "j1" not in controller.state.remote_dispatch.pending


async def test_exact_required_connected_but_incompatible_fails_the_job(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """EXACT_REQUIRED with a connected-but-incompatible server fails (waiting can't fix it)."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0, version="2026.6.1")],
            open_pins={_PIN_A},
            idle_pins={_PIN_A},
            offloader_version="2026.6.0",
            policy=VersionMatchPolicy.EXACT_REQUIRED,
        ),
    )
    job = _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    assert job.status is JobStatus.FAILED
    assert "exact_required" in (job.error or "")
    assert job.source is not JobSource.LOCAL


async def test_exact_required_disconnected_server_waits_not_fails(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """EXACT_REQUIRED, only compatible server merely offline → holds the job (may reconnect)."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0, version="2026.5.0")],
            open_pins=set(),  # paired but its peer-link is down (transient drop)
            idle_pins=set(),
            offloader_version="2026.5.0",
            policy=VersionMatchPolicy.EXACT_REQUIRED,
        ),
    )
    job = _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    assert job.status is JobStatus.QUEUED
    assert "j1" in controller.state.remote_dispatch.pending


async def test_exact_required_busy_server_waits_not_fails(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """EXACT_REQUIRED with the only compatible server busy holds the job (no hard-fail)."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0, version="2026.5.0")],
            open_pins={_PIN_A},
            idle_pins={_PIN_A},
            offloader_version="2026.5.0",
            policy=VersionMatchPolicy.EXACT_REQUIRED,
        ),
    )
    # Server A is already driving another job.
    controller.state.remote_dispatch.job_peer["other"] = _PIN_A
    job = _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    assert job.status is JobStatus.QUEUED
    assert "j1" in controller.state.remote_dispatch.pending


async def test_include_local_overflow_runs_on_local_lane(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """One server, two compiles, opt-in on → one binds remote, the other runs local."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0)],
            open_pins={_PIN_A},
            idle_pins={_PIN_A},
            include_local_in_pool=True,
        ),
    )
    controller.state.compile_lane.queue = asyncio.Queue()
    j1 = _add_pending(controller, "j1")
    j2 = _add_pending(controller, "j2")

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert pool.job_peer == {"j1": _PIN_A}
    # j2 overflowed onto the local compile lane instead of holding.
    assert j2.source is JobSource.LOCAL
    assert "j2" not in pool.pending
    assert controller.state.compile_lane.queue.qsize() == 1
    assert j1.source is not JobSource.LOCAL


async def test_include_local_one_local_slot_third_compile_waits(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """One local slot: with one server, three compiles place 1 remote + 1 local, 1 waits."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0)],
            open_pins={_PIN_A},
            idle_pins={_PIN_A},
            include_local_in_pool=True,
        ),
    )
    controller.state.compile_lane.queue = asyncio.Queue()
    for jid in ("j1", "j2", "j3"):
        _add_pending(controller, jid)

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert set(pool.job_peer) == {"j1"}
    # j2 took the single local slot; j3 must hold (queue is non-empty this pass).
    assert controller.state.compile_lane.queue.qsize() == 1
    assert set(pool.pending) == {"j3"}


async def test_include_local_off_overflow_holds_pending(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Opt-in off (default) keeps the overflow compile waiting rather than running it local."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    controller.state.compile_lane.queue = asyncio.Queue()
    _add_pending(controller, "j1")
    _add_pending(controller, "j2")

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert pool.job_peer == {"j1": _PIN_A}
    assert set(pool.pending) == {"j2"}
    assert controller.state.compile_lane.queue.qsize() == 0


async def test_compile_lane_completion_wakes_dispatcher_when_pending(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compile-lane job finishing re-arms the matcher so an overflow compile can take the slot."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    controller.state.compile_lane.queue = asyncio.Queue()
    _add_pending(controller, "j1")  # an overflow compile held in the pool
    pool = controller.state.remote_dispatch
    pool.wake.clear()

    async def _noop_execute(job: FirmwareJob, lane: Any) -> None:
        lane.current_job = None  # mirror execute_job's finally clearing the slot

    monkeypatch.setattr(controller, "_execute_job", _noop_execute)
    lane_job = FirmwareJob(
        job_id="local1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.LOCAL,
    )
    controller.state.compile_lane.queue.put_nowait(lane_job)

    loop_task = asyncio.create_task(runner.run_lane(controller, controller.state.compile_lane))
    try:
        await asyncio.wait_for(pool.wake.wait(), timeout=2.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert pool.wake.is_set()


async def test_cancelled_compile_lane_job_still_rearms_dispatcher(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A dequeued-then-cancelled compile still shortens the queue, so the matcher re-arms."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    controller.state.compile_lane.queue = asyncio.Queue()
    _add_pending(controller, "j1")  # an overflow compile held in the pool
    pool = controller.state.remote_dispatch
    pool.wake.clear()

    cancelled = FirmwareJob(
        job_id="local1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.CANCELLED,  # cancelled before the runner claimed it
        source=JobSource.LOCAL,
    )
    controller.state.compile_lane.queue.put_nowait(cancelled)

    loop_task = asyncio.create_task(runner.run_lane(controller, controller.state.compile_lane))
    try:
        await asyncio.wait_for(pool.wake.wait(), timeout=2.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert pool.wake.is_set()


async def test_supersede_drops_a_pending_remote_compile(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Re-compiling the same config supersedes the still-pending remote compile out of the pool."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    (tmp_path / "dev.yaml").write_text("")

    first = await controller.compile(configuration="dev.yaml")
    assert first.job_id in controller.state.remote_dispatch.pending

    second = await controller.compile(configuration="dev.yaml")

    assert first.status is JobStatus.CANCELLED
    assert first.job_id not in controller.state.remote_dispatch.pending
    assert second.job_id in controller.state.remote_dispatch.pending


async def test_cancel_pending_remote_compile_never_dispatches(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling a still-pending remote compile drops it from the pool before any dispatch."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    offloader = _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    job = _add_pending(controller, "j1")

    await controller.cancel(job_id="j1")

    assert job.status is JobStatus.CANCELLED
    assert "j1" not in controller.state.remote_dispatch.pending
    await remote_dispatch._dispatch_pending(controller)
    offloader.get_pairing.assert_not_called()


async def test_cancel_in_flight_remote_compile_signals_event(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling an off-lane in-flight remote compile flags it and wakes its runner."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    job = FirmwareJob(
        job_id="j1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        source=JobSource.REMOTE,
    )
    controller.state.jobs["j1"] = job
    controller.state.remote_dispatch.in_flight["j1"] = MagicMock()
    cancel_event = asyncio.Event()
    controller.state.cancel_events["j1"] = cancel_event

    await controller.cancel(job_id="j1")

    assert "j1" in controller.state.cancel_requested
    assert cancel_event.is_set()


async def test_drive_remote_finalizes_failed_on_unexpected_exception(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected raise out of run_remote_job finalizes FAILED, never leaves the job RUNNING."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    failed: list[str] = []
    controller.bus.add_listener(
        EventType.JOB_FAILED, lambda event: failed.append(event.data["job"].job_id)
    )

    async def _boom(_ctrl: object, _job: FirmwareJob, **_kw: object) -> None:
        raise KeyError("malformed wire frame")

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _boom)
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    pool = controller.state.remote_dispatch
    pool.in_flight["c1"] = MagicMock()
    pool.job_peer["c1"] = _PIN_A

    await remote_dispatch._drive_remote(controller, job)

    assert job.status is JobStatus.FAILED
    assert "malformed wire frame" in (job.error or "")
    assert failed == ["c1"]
    assert "c1" not in pool.in_flight
    assert pool.busy_pins() == frozenset()


async def test_drive_remote_reraises_cancelled_error_and_frees_slot(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CancelledError out of run_remote_job propagates (shutdown), freeing the pool slot."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)

    async def _cancelled(_ctrl: object, _job: FirmwareJob, **_kw: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _cancelled)
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    pool = controller.state.remote_dispatch
    pool.in_flight["c1"] = MagicMock()
    pool.job_peer["c1"] = _PIN_A

    with pytest.raises(asyncio.CancelledError):
        await remote_dispatch._drive_remote(controller, job)

    assert "c1" not in pool.in_flight  # finally still freed the slot
    assert pool.busy_pins() == frozenset()


async def test_drive_remote_skips_build_when_already_terminal(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job cancelled in the dispatch window is skipped by its driver, slot freed, no build."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    started: list[str] = []

    async def _spy_run(_ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        started.append(job.job_id)

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _spy_run)
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.CANCELLED,  # cancelled before this task ran
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    pool = controller.state.remote_dispatch
    pool.in_flight["c1"] = MagicMock()
    pool.job_peer["c1"] = _PIN_A

    await remote_dispatch._drive_remote(controller, job)

    assert started == []  # run_remote_job never called
    assert "c1" not in pool.in_flight
    assert "c1" not in pool.job_peer


async def test_cancel_queued_but_bound_in_flight_frees_the_server(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Cancelling a QUEUED job that's already bound in-flight frees the server binding too."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,  # bound but not yet stamped RUNNING
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    pool = controller.state.remote_dispatch
    pool.in_flight["c1"] = MagicMock()
    pool.job_peer["c1"] = _PIN_A

    await controller.cancel(job_id="c1")

    assert job.status is JobStatus.CANCELLED
    assert "c1" not in pool.in_flight
    assert pool.busy_pins() == frozenset()


async def test_drive_remote_finalizes_cleans_pool_and_releases_dependent(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver fires JOB_STARTED, finalises, frees the server, and lands the held upload."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    compile_job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    upload_job = FirmwareJob(
        job_id="u1",
        configuration="dev.yaml",
        job_type=JobType.UPLOAD,
        depends_on="c1",
    )
    controller.state.jobs = {"c1": compile_job, "u1": upload_job}
    controller.state.remote_dispatch.in_flight["c1"] = MagicMock()
    controller.state.remote_dispatch.job_peer["c1"] = _PIN_A

    started: list[str] = []
    controller.bus.add_listener(
        EventType.JOB_STARTED, lambda event: started.append(event.data["job"].job_id)
    )

    async def _fake_run(ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        ctrl._finalize_terminal(job, JobStatus.COMPLETED)

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _fake_run)

    await remote_dispatch._drive_remote(controller, compile_job)

    assert started == ["c1"]
    assert compile_job.status is JobStatus.COMPLETED
    pool = controller.state.remote_dispatch
    assert "c1" not in pool.in_flight
    assert "c1" not in pool.job_peer
    assert pool.wake.is_set()
    # The compile completing released its dependent upload onto the upload lane.
    assert controller.state.upload_lane.queue.qsize() == 1


async def test_loop_dispatches_three_servers_concurrently(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the real loop: three idle servers run three compiles at once."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [
                _pairing(_PIN_A, paired_at=1.0),
                _pairing(_PIN_B, paired_at=2.0),
                _pairing(_PIN_C, paired_at=3.0),
            ],
            open_pins={_PIN_A, _PIN_B, _PIN_C},
            idle_pins={_PIN_A, _PIN_B, _PIN_C},
        ),
    )
    controller._db.create_background_task = asyncio.create_task

    running_pins: list[str] = []
    all_three = asyncio.Event()
    release = asyncio.Event()

    async def _fake_run(ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        running_pins.append(job.source_pin_sha256)
        if len(running_pins) == 3:
            all_three.set()
        await release.wait()  # hold so all three overlap
        ctrl._finalize_terminal(job, JobStatus.COMPLETED)

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _fake_run)
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 0)

    loop_task = asyncio.create_task(remote_dispatch.run_dispatch_loop(controller))
    try:
        for jid in ("j1", "j2", "j3"):
            _add_pending(controller, jid)
        controller.state.remote_dispatch.wake.set()

        await asyncio.wait_for(all_three.wait(), timeout=2.0)
        # All three servers are compiling at the same instant.
        assert set(running_pins) == {_PIN_A, _PIN_B, _PIN_C}
        assert len(controller.state.remote_dispatch.in_flight) == 3
    finally:
        release.set()
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_loop_wakes_on_peer_link_opened_event(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firing ``OFFLOADER_PEER_LINK_OPENED`` (not a manual wake) drives a dispatch pass."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    controller._db.create_background_task = asyncio.create_task
    dispatched = asyncio.Event()

    async def _fake_run(ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        dispatched.set()
        ctrl._finalize_terminal(job, JobStatus.COMPLETED)

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _fake_run)
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 0)

    loop_task = asyncio.create_task(remote_dispatch.run_dispatch_loop(controller))
    try:
        await asyncio.sleep(0)  # let the loop attach its bus listeners and park
        # ``_add_pending`` inserts without waking — only the bus event can.
        _add_pending(controller, "j1")
        controller.bus.fire(EventType.OFFLOADER_PEER_LINK_OPENED, {})
        await asyncio.wait_for(dispatched.wait(), timeout=2.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_loop_wakes_on_include_local_changed_event(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Firing the include-local toggle event re-evaluates a compile stuck behind a busy server.

    Without the wake, turning the toggle on wouldn't unstick a REMOTE_PENDING
    compile until some unrelated pool event fired.
    """
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    controller.state.compile_lane.queue = asyncio.Queue()
    # One eligible server, busy, with the opt-in on: the only path off WAIT is local.
    snapshot = _snapshot(
        [_pairing(_PIN_A, paired_at=1.0)],
        open_pins={_PIN_A},
        idle_pins={_PIN_A},
        include_local_in_pool=True,
    )
    _stub_offloader(controller, snapshot)
    controller.state.remote_dispatch.job_peer["busy-other"] = _PIN_A  # server busy
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 0)

    loop_task = asyncio.create_task(remote_dispatch.run_dispatch_loop(controller))
    try:
        await asyncio.sleep(0)  # let the loop attach its listeners and park
        job = _add_pending(controller, "j1")
        controller.bus.fire(
            EventType.OFFLOADER_INCLUDE_LOCAL_CHANGED, {"include_local_in_pool": True}
        )
        async with asyncio.timeout(2.0):
            while job.source is not JobSource.LOCAL:
                await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert job.source is JobSource.LOCAL


async def test_startup_grace_holds_restored_compiles_before_servers_connect(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During the startup grace a restored remote compile holds, not flushed to local.

    Regression for the restart race: firmware.start() runs the loop before the
    offloader loads its pairings, so without the grace the first pass would see
    zero servers and fall the restored compile back to local.
    """
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    # Offloader present but no servers connected yet (mid-startup).
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins=set(), idle_pins=set()),
    )
    controller._db.create_background_task = asyncio.create_task
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 30)
    job = _add_pending(controller, "j1")
    controller.state.remote_dispatch.wake.set()  # as a restore-time hold would

    loop_task = asyncio.create_task(remote_dispatch.run_dispatch_loop(controller))
    try:
        await asyncio.sleep(0)  # loop starts and parks in the grace sleep
        await asyncio.sleep(0)
        # Still within the grace — the compile must not have run locally.
        assert "j1" in controller.state.remote_dispatch.pending
        assert job.source is JobSource.REMOTE_PENDING
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)


async def test_offloader_gone_flushes_pending_to_local(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """If remote build is torn down, waiting compiles run locally rather than strand."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    controller._db.remote_build_offloader = None
    job = _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    assert job.source is JobSource.LOCAL
    assert "j1" not in controller.state.remote_dispatch.pending


async def test_stale_pending_job_is_dropped_not_dispatched(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A pending job that went terminal under us is dropped, never bound to a server."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    offloader = _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    job = _add_pending(controller, "j1")
    job.status = JobStatus.CANCELLED  # finalised between waking and this pass

    await remote_dispatch._dispatch_pending(controller)

    assert "j1" not in controller.state.remote_dispatch.pending
    assert "j1" not in controller.state.remote_dispatch.in_flight
    offloader.get_pairing.assert_not_called()


async def test_unpair_race_leaves_job_pending(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The scheduler picks a pin but ``get_pairing`` returns None (raced unpair) → stay pending."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    offloader = MagicMock()
    offloader.build_scheduler_snapshot.return_value = _snapshot(
        [_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}
    )
    offloader.get_pairing.return_value = None  # unpaired between snapshot and bind
    controller._db.remote_build_offloader = offloader
    _add_pending(controller, "j1")

    await remote_dispatch._dispatch_pending(controller)

    pool = controller.state.remote_dispatch
    assert "j1" in pool.pending
    assert "j1" not in pool.in_flight
    # Re-armed so a missed peer-link-close event can't strand the compile.
    assert pool.wake.is_set()


async def test_dispatch_leaves_consistent_inflight_state(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After dispatch the job is RUNNING + in-flight + JOB_STARTED-fired in one consistent step.

    Pins the eager-task ordering: ``begin_run`` (RUNNING + JOB_STARTED) and
    ``start()`` (in_flight) both run synchronously before any interleave, so a
    cancel landing right after sees ``is_in_flight`` true and is accepted.
    """
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot([_pairing(_PIN_A, paired_at=1.0)], open_pins={_PIN_A}, idle_pins={_PIN_A}),
    )
    # Production spawns eagerly, so the prologue runs before start() returns.
    controller._db.create_background_task = create_eager_task
    release = asyncio.Event()
    started: list[str] = []
    controller.bus.add_listener(
        EventType.JOB_STARTED, lambda event: started.append(event.data["job"].job_id)
    )

    async def _block(ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        await release.wait()
        ctrl._finalize_terminal(job, JobStatus.COMPLETED)

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _block)

    job = _add_pending(controller, "j1")
    await remote_dispatch._dispatch_pending(controller)
    pool = controller.state.remote_dispatch
    task = pool.in_flight["j1"]
    try:
        assert pool.is_in_flight("j1")
        assert _PIN_A in pool.busy_pins()
        assert job.status is JobStatus.RUNNING
        assert started == ["j1"]
        # Cancel in this window is accepted (is_in_flight true), no RuntimeError.
        await controller.cancel(job_id="j1")
        assert "j1" in controller.state.cancel_requested
    finally:
        release.set()
        await task


async def test_server_lost_mid_build_reroutes_to_another_worker(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compile whose server vanishes mid-build re-queues onto the next worker, not failed."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0), _pairing(_PIN_B, paired_at=2.0)],
            open_pins={_PIN_A, _PIN_B},
            idle_pins={_PIN_A, _PIN_B},
        ),
    )
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    controller.state.remote_dispatch.in_flight["c1"] = MagicMock()
    controller.state.remote_dispatch.job_peer["c1"] = _PIN_A

    async def _server_a_drops(_ctrl: object, _job: FirmwareJob, **_kw: object) -> None:
        raise RemoteServerLostError("transport_error")

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _server_a_drops)

    await remote_dispatch._drive_remote(controller, job)

    pool = controller.state.remote_dispatch
    # Re-queued for re-dispatch, not failed; the old binding is cleared.
    assert job.status is JobStatus.QUEUED
    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    assert "c1" in pool.pending
    assert "c1" not in pool.in_flight
    assert pool.retries["c1"] == 1
    # A re-route is not a restart: the log must not claim the dashboard restarted.
    assert not any("restarted mid-build" in line for line in job.output)


async def test_server_loss_retry_is_bounded(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Past the retry cap a repeatedly-lost compile fails instead of looping forever."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    job = FirmwareJob(
        job_id="c1",
        configuration="dev.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        source=JobSource.REMOTE,
        source_pin_sha256=_PIN_A,
    )
    controller.state.jobs["c1"] = job
    controller.state.remote_dispatch.retries["c1"] = remote_dispatch._MAX_SERVER_LOSS_RETRIES

    remote_dispatch._requeue_after_server_loss(controller, job, "transport_error")

    assert job.status is JobStatus.FAILED
    assert "c1" not in controller.state.remote_dispatch.pending
    assert "c1" not in controller.state.remote_dispatch.retries


async def test_server_lost_mid_build_completes_on_alternate_server(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2E: server A drops mid-build, the loop re-dispatches the compile to B, which finishes it."""
    controller = firmware_controller_factory(with_queue=True, with_real_bus=True)
    offloader = _stub_offloader(
        controller,
        _snapshot(
            [_pairing(_PIN_A, paired_at=1.0), _pairing(_PIN_B, paired_at=2.0)],
            open_pins={_PIN_A, _PIN_B},
            idle_pins={_PIN_A, _PIN_B},
        ),
    )
    controller._db.create_background_task = asyncio.create_task
    monkeypatch.setattr(remote_dispatch, "_STARTUP_GRACE_SECONDS", 0)

    runs: list[str] = []
    done = asyncio.Event()

    async def _fake_run(ctrl: object, job: FirmwareJob, **_kw: object) -> None:
        runs.append(job.source_pin_sha256)  # bound pin at dispatch
        if job.source_pin_sha256 == _PIN_A:
            # A's peer-link drops mid-build: it leaves the pool, then the build fails.
            offloader.build_scheduler_snapshot.return_value = _snapshot(
                [_pairing(_PIN_B, paired_at=2.0)], open_pins={_PIN_B}, idle_pins={_PIN_B}
            )
            raise RemoteServerLostError("transport_error")
        ctrl._finalize_terminal(job, JobStatus.COMPLETED)
        done.set()

    monkeypatch.setattr(remote_dispatch, "run_remote_job", _fake_run)

    loop_task = asyncio.create_task(remote_dispatch.run_dispatch_loop(controller))
    try:
        job = _add_pending(controller, "c1")
        controller.state.remote_dispatch.wake.set()
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    pool = controller.state.remote_dispatch
    assert runs == [_PIN_A, _PIN_B]  # tried A, re-routed to the alternate B
    assert job.status is JobStatus.COMPLETED
    assert job.source_pin_sha256 == _PIN_B  # completed on the alternate server
    assert "c1" not in pool.in_flight
    assert "c1" not in pool.retries  # loss counter cleared on the real terminal
