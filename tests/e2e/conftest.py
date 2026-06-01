"""
End-to-end harness for the remote-build offload feature (issue #106).

Two real :class:`RemoteBuildController` instances stood up
side-by-side — one acting as the receiver (peer-link listener
bound on a real TCP port via :class:`aiohttp.test_utils.TestServer`),
one acting as the offloader (long-lived
:class:`PeerLinkClient` connecting to the receiver). Both run on
real :class:`EventBus` instances so per-mutation events flow
through the same wire surface a production frontend would
subscribe to.

Tests built on top of this harness exercise behaviour that
spans both sides of the wire — handshake → pair → peer-link
session → application messages (5b/5c/5d) → bundle upload +
firmware download (later phases). Single-side unit tests in
``test_remote_build_peer_link.py`` /
``test_remote_build_peer_link_client.py`` already pin the
per-side wire shapes; the harness's value is catching mismatches
between the two (event payload contracts, dashboard_id collisions,
terminate flow with both sides observing).

The harness drives the real pair flow end-to-end (no
dict-mocking shortcuts): receiver opens its pairing window,
offloader runs ``preview_pair`` + ``request_pair`` over real
Noise XX handshakes, receiver calls ``approve_peer``, then
the offloader's pair-status listener observes the flip and
spawns the long-lived peer-link client. Tests built on top of
``paired_instances`` start from "both sides have an APPROVED
row, the long-lived peer-link session is open, ready for
application messages."
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from esphome.core import CORE

from esphome_device_builder.api.ws import init_ws_app
from esphome_device_builder.controllers.remote_build import (
    OffloaderController,
    ReceiverController,
)
from esphome_device_builder.controllers.remote_build.peer_link import (
    PEER_LINK_PATH,
    make_peer_link_handler,
)
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.peer_link_identity import PeerLinkIdentityStore
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    materialise_remote_artifacts,
)
from esphome_device_builder.helpers.remote_build_layout import parse_from_configuration
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
    QueueStatus,
)

from ..conftest import (
    RemoteBuildTestHandles,
    _CapturedEvents,
    capture_events,
    make_remote_build_controller,
    wire_firmware_remote_peer_api_mocks,
)


@dataclass
class PairedInstances:
    """Two paired dashboards + a TestServer, pre-paired and ready to drive.

    Production has two sibling controllers per dashboard
    (:class:`OffloaderController` and :class:`ReceiverController`);
    the e2e harness simulates two whole dashboards, each with both
    halves. ``receiver`` / ``offloader`` are the role-relevant
    sibling on the role-relevant dashboard:

    * ``receiver``: the receiver-role dashboard's receiver-side
      sibling. Test code drives ``record_pair_request`` /
      ``approve_peer`` / inspects ``_approved_peers`` here.
    * ``offloader``: the offloader-role dashboard's offloader-side
      sibling. Test code drives ``submit_job`` /
      ``cancel_job`` / inspects ``_pairings`` here.

    The full handles (``receiver_handles`` / ``offloader_handles``)
    are exposed for tests that need both halves of a single
    dashboard or the convenience ``start`` / ``stop`` lifecycle.

    :meth:`wait_until_session_opened` is the single conventional
    sync point; tests that need to assert on post-session state
    call it before their assertions instead of polling the
    registry by hand.
    """

    receiver_handles: RemoteBuildTestHandles
    offloader_handles: RemoteBuildTestHandles
    receiver_server: TestServer
    receiver_bus: EventBus
    offloader_bus: EventBus
    offloader_dashboard_id: str
    # Lowercase-hex SHA-256 of the receiver's Noise static
    # X25519 public key, observed by the offloader during the
    # live Noise XX handshake (see
    # :func:`helpers.peer_link_noise.pin_sha256_for_pubkey`).
    # Tests that drive post-pairing application messages
    # (5b/5c/5d) look the offloader-side peer-link client up
    # via ``_lookup_open_peer_link_client(pin_sha256)``;
    # capturing the pin here means the harness's pre-paired
    # state is immediately addressable from test bodies
    # without each one re-walking the pair flow.
    pin_sha256: str
    # Pre-subscribed at fixture-construct time, before either
    # ``start()`` runs. Tests assert against these captured
    # lists rather than re-subscribing after the fixture yields
    # (by which point the OPENED events have already fired and
    # a fresh listener would never see them).
    offloader_opened: _CapturedEvents
    offloader_closed: _CapturedEvents
    receiver_opened: _CapturedEvents
    receiver_closed: _CapturedEvents

    @property
    def offloader(self) -> OffloaderController:
        """The offloader-role dashboard's offloader-side sibling."""
        return self.offloader_handles.offloader

    @property
    def receiver(self) -> ReceiverController:
        """The receiver-role dashboard's receiver-side sibling."""
        return self.receiver_handles.receiver

    async def wait_until_session_opened(self, *, timeout: float = 2.0) -> None:
        """Block until both sides have observed the peer-link session opening.

        Two awaits because the two sides reach "opened" on slightly
        different schedules:

        * Offloader fires :attr:`EventType.OFFLOADER_PEER_LINK_OPENED`
          right after its :class:`PeerLinkClient` processes the
          receiver's post-handshake ``intent_response: ok``.
        * Receiver fires
          :attr:`EventType.RECEIVER_PEER_LINK_SESSION_OPENED`
          from inside :meth:`ReceiverController.register_peer_link_session`,
          which the receiver handler enters *after* sending the
          post-handshake response — so receiver-side registration
          can lag the offloader's OPENED fire by an event-loop tick.

        Waiting on both gives callers a single sync point that
        holds true on both sides without each test having to
        layer its own wait on top.
        """
        await asyncio.wait_for(self.offloader_opened.received.wait(), timeout=timeout)
        await asyncio.wait_for(self.receiver_opened.received.wait(), timeout=timeout)

    async def wait_until_session_closed(self, *, timeout: float = 2.0) -> None:
        """Block until both sides have observed the peer-link session closing.

        Mirror of :meth:`wait_until_session_opened` for the
        teardown direction. Waits for the offloader's
        ``OFFLOADER_PEER_LINK_CLOSED`` AND the receiver's
        ``RECEIVER_PEER_LINK_SESSION_CLOSED`` so post-close
        registry-empty assertions hold on both sides.
        """
        await asyncio.wait_for(self.offloader_closed.received.wait(), timeout=timeout)
        await asyncio.wait_for(self.receiver_closed.received.wait(), timeout=timeout)


@asynccontextmanager
async def _paired_instances_ctx(
    receiver_dir: Path,
    offloader_dir: Path,
) -> AsyncIterator[PairedInstances]:
    """Yield two :class:`RemoteBuildController` instances paired via the real flow.

    Drives the production pair sequence end-to-end against two
    in-process controllers — no dict-mocking shortcuts:

    1. Both controllers ``start()`` (loads identities,
       installs the long-poll listener slot for any future
       PENDING rows, etc.).
    2. Receiver opens its pairing window
       (``set_pairing_window(open=True)``).
    3. Offloader runs ``preview_pair`` over a real Noise XX WS
       to capture the receiver's pubkey + pin from the
       handshake transcript.
    4. Offloader runs ``request_pair`` (also a real Noise WS)
       carrying the offloader's ``dashboard_id``; receiver's
       handler creates a PENDING :class:`StoredPeer` row and
       fires ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.
    5. Receiver runs ``approve_peer`` to flip PENDING →
       APPROVED; fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED``.
    6. Offloader's pair-status listener (spawned in step 4)
       observes the flip via its long-poll WS, updates the
       local :class:`StoredPairing` to APPROVED, and spawns
       the long-lived :class:`PeerLinkClient`.

    Per-side event buses are real, so production-shape event
    fan-out runs end-to-end. The handshake reads pin + dashboard_id
    from the live Noise transcript, so any wire-shape regression
    on either side surfaces here rather than being hidden behind
    a pre-seeded RAM dict.

    Teardown drains both controllers in dependency order:
    offloader first (its client task sends a
    ``terminate{client_stopped}`` to the receiver, the
    receiver's session loop unwinds), then the receiver (closing
    any remaining server-side state), then the TestServer.
    """
    receiver_bus = EventBus()
    offloader_bus = EventBus()
    receiver = make_remote_build_controller(config_dir=receiver_dir, bus=receiver_bus)
    offloader = make_remote_build_controller(config_dir=offloader_dir, bus=offloader_bus)
    # Pre-subscribe to all four session-lifecycle events before
    # any ``start()`` runs — the offloader's ``PeerLinkClient``
    # connects on its own task and fires OPENED essentially
    # immediately; tests that subscribed after the fixture
    # yielded would never see it. ``wait_until_session_opened`` /
    # ``wait_until_session_closed`` wait on these pre-rolled
    # captures.
    offloader_opened = capture_events(offloader_bus, EventType.OFFLOADER_PEER_LINK_OPENED)
    offloader_closed = capture_events(offloader_bus, EventType.OFFLOADER_PEER_LINK_CLOSED)
    receiver_opened = capture_events(receiver_bus, EventType.RECEIVER_PEER_LINK_SESSION_OPENED)
    receiver_closed = capture_events(receiver_bus, EventType.RECEIVER_PEER_LINK_SESSION_CLOSED)

    # Stand up the receiver's peer-link WS endpoint on a real
    # TCP port. ``TestServer`` picks an ephemeral port; the
    # offloader dials ``("127.0.0.1", server.port)``.
    app = web.Application()
    init_ws_app(app)
    handler = make_peer_link_handler(
        receiver.receiver, await PeerLinkIdentityStore(receiver_dir).async_load()
    )
    app.router.add_get(PEER_LINK_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    assert server.port is not None  # TestServer always binds; narrow for type-checkers.

    # Both controllers start before any pair-flow calls — the
    # offloader needs its pair-status listener slot wired so
    # ``request_pair`` can register the per-row long-poll task,
    # and the receiver needs its identity + handler factory ready
    # so the offloader's WS dials succeed.
    await receiver.start()
    await offloader.start()

    # 1. Receiver opens the pairing window so its handler will
    #    accept ``intent="pair_request"`` frames.
    await receiver.receiver.set_pairing_window(open=True, client="receiver-tab")

    # 2. Offloader runs preview to capture the receiver's pin
    #    over a live Noise XX handshake.
    preview = await offloader.offloader.preview_pair(hostname="127.0.0.1", port=server.port)
    pin_sha256 = preview["pin_sha256"]

    # 3. Offloader requests pairing. Receiver lands a PENDING
    #    ``StoredPeer`` and fires REMOTE_BUILD_PAIR_REQUEST_RECEIVED;
    #    the offloader spawns its pair-status long-poll listener
    #    against this row.
    await offloader.offloader.request_pair(
        hostname="127.0.0.1",
        port=server.port,
        pin_sha256=pin_sha256,
        receiver_label="receiver",
        offloader_label="offloader",
    )

    # 4. Receiver-side admin clicks Accept. The PENDING peer's
    #    ``dashboard_id`` is the offloader's stable identity —
    #    pull it off the row the receiver just landed. Subscribe
    #    to OFFLOADER_PAIR_STATUS_CHANGED *before* approve_peer
    #    fires so the receiver's APPROVED → offloader's
    #    pair-status listener → status-flip-event chain can be
    #    awaited deterministically rather than spun on.
    [pending_dashboard_id] = list(receiver.receiver.state.pending_peers.keys())
    pair_status_changed = capture_events(offloader_bus, EventType.OFFLOADER_PAIR_STATUS_CHANGED)
    await receiver.receiver.approve_peer(dashboard_id=pending_dashboard_id)

    # 5. Wait for the offloader's pair-status listener to observe
    #    the flip. The listener's long-poll WS unblocks on the
    #    receiver's bus event, then ``_apply_pair_status_result``
    #    flips the local row to APPROVED, fires
    #    OFFLOADER_PAIR_STATUS_CHANGED, and spawns the long-lived
    #    peer-link client.
    await asyncio.wait_for(pair_status_changed.received.wait(), timeout=2.0)
    assert pair_status_changed[-1]["status"] == "approved"

    instances = PairedInstances(
        receiver_handles=receiver,
        offloader_handles=offloader,
        receiver_server=server,
        receiver_bus=receiver_bus,
        offloader_bus=offloader_bus,
        offloader_dashboard_id=pending_dashboard_id,
        pin_sha256=pin_sha256,
        offloader_opened=offloader_opened,
        offloader_closed=offloader_closed,
        receiver_opened=receiver_opened,
        receiver_closed=receiver_closed,
    )
    try:
        yield instances
    finally:
        # Teardown order matters: the offloader's ``stop()``
        # cancels its peer-link client task, whose
        # ``CancelledError`` handler sends a structured
        # ``terminate{client_stopped}`` frame to the receiver.
        # Stopping the receiver first would race that frame
        # against the receiver's WS shutdown.
        await offloader.stop()
        await receiver.stop()
        await server.close()


@pytest.fixture
async def paired_instances(
    tmp_path: Path,
) -> AsyncGenerator[PairedInstances, None]:
    """Yield two :class:`RemoteBuildController` instances paired via the real flow."""
    receiver_dir = tmp_path / "receiver"
    receiver_dir.mkdir()
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()
    async with _paired_instances_ctx(receiver_dir, offloader_dir) as instances:
        yield instances


@pytest.fixture
async def paired_instances_relative_receiver_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[PairedInstances, None]:
    """Like :func:`paired_instances` but the receiver's ``config_dir`` is relative (#678)."""
    monkeypatch.chdir(tmp_path)
    receiver_dir = Path("receiver")
    receiver_dir.mkdir()
    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()
    async with _paired_instances_ctx(receiver_dir, offloader_dir) as instances:
        yield instances


def make_remote_peer_job(
    *,
    remote_peer: str,
    remote_job_id: str = "off-job-1",
    job_id: str = "rcv-job-1",
    error: str | None = None,
) -> FirmwareJob:
    """Build a synthetic :class:`FirmwareJob` carrying the remote-peer correlation.

    Shared harness helper for fan-out / cancel / submit-job e2e
    tests. The wire path only inspects ``job_id`` (cache key),
    ``remote_peer`` (session lookup), ``remote_job_id`` (echoed
    on the wire frame), and ``error`` (used on failed /
    cancelled). Other fields take their dataclass defaults; we
    deliberately don't run the firmware queue here since the
    point is exercising the receiver-bus → wire → offloader-bus
    chain on a synthetic event, not the queue's own state
    transitions.
    """
    return FirmwareJob(
        job_id=job_id,
        configuration=".esphome/.remote_builds/foo/kitchen/kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.QUEUED,
        remote_peer=remote_peer,
        remote_job_id=remote_job_id,
        error=error,
    )


def make_real_bundle(
    *,
    configuration_filename: str = "kitchen.yaml",
    yaml_body: bytes = b"esphome:\n  name: kitchen\n",
) -> bytes:
    """
    Build a minimal-but-valid esphome bundle the upstream extractor accepts.

    Emits a ``manifest.json`` + the referenced YAML member; skips
    :class:`BundleBuilder` so the test doesn't need a real
    ``CORE.config_dir`` / ``CORE.config_path`` setup. Pass *yaml_body*
    to drive a real ``esphome compile`` against a platform-specific
    config (the LibreTiny e2e ships a ``bk72xx`` board here).
    """
    manifest = {
        "manifest_version": 1,
        "config_filename": configuration_filename,
    }
    members: list[tuple[str, bytes]] = [
        ("manifest.json", json.dumps(manifest).encode("utf-8")),
        (configuration_filename, yaml_body),
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def run_esphome_compile(
    yaml_path: Path, *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``esphome compile`` on *yaml_path* with *env*'s ESPHOME_DATA_DIR override."""
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell, test-only invocation
        [sys.executable, "-m", "esphome", "compile", str(yaml_path)],
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
        env=env,
    )


async def run_offload_compile_round_trip(
    instances: PairedInstances,
    *,
    job_id: str,
    configuration_filename: str,
    yaml_body: bytes,
) -> tuple[Path, Path]:
    """Submit + real-compile *yaml_body* on the receiver, download + materialise on the offloader.

    Returns ``(receiver_data_dir, offloader_build_path)``. Owns the
    submit -> compile -> lifecycle -> download -> materialise
    orchestration the platform-specific compile e2es share; callers
    assert on the produced artifacts. The compile + materialise run via
    ``asyncio.to_thread`` so their blocking I/O stays off the loop.
    """
    await instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(instances)
    state_changes = capture_events(instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED)

    handle = instances.offloader.state.peer_link_clients[instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id=job_id,
        configuration_filename=configuration_filename,
        target="compile",
        bundle_bytes=make_real_bundle(
            configuration_filename=configuration_filename, yaml_body=yaml_body
        ),
    )
    assert ack["accepted"] is True
    receiver_job = created_jobs[0]

    remote_build_path = parse_from_configuration(receiver_job.configuration)
    assert remote_build_path is not None
    data_dir = remote_build_path.data_dir(Path(CORE.data_dir))
    yaml_path = Path(instances.receiver._db.settings.config_dir) / receiver_job.configuration
    result = await asyncio.to_thread(
        run_esphome_compile, yaml_path, env={**os.environ, "ESPHOME_DATA_DIR": str(data_dir)}
    )
    assert result.returncode == 0, (
        f"compile failed:\nstdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-2000:]}"
    )

    await drive_remote_job_to_completed(instances, receiver_job, state_changes)
    packed = await handle.client.download_artifacts(job_id=job_id)
    build_path = await asyncio.to_thread(
        materialise_remote_artifacts, packed.tarball, configuration_filename
    )
    return data_dir, build_path


async def make_and_seed_remote_peer_job(
    instances: PairedInstances,
    *,
    error: str | None = None,
) -> FirmwareJob:
    """Build a synthetic remote-peer job and seed ``JOB_QUEUED`` so the fan-out caches it.

    Combines :func:`make_remote_peer_job` (build a
    :class:`FirmwareJob` whose ``remote_peer`` matches the
    harness's offloader) with the ``JOB_QUEUED`` seed step that
    populates :attr:`JobFanout._remote_jobs` so subsequent
    lifecycle / output / cancel events fan out instead of
    dropping on the floor. Every test that drives a
    correlated :class:`JobFanout` lookup needs both, in this
    order, against the same harness offloader id; the helper
    collapses the two-line prelude into one call.

    :class:`JobFanout._on_lifecycle` is a sync bus listener that
    looks up the correlation in :attr:`JobFanout._remote_jobs`,
    populated by ``JOB_QUEUED``. The queued event itself also
    fans out a ``job_state_changed{queued}`` frame to the
    submitting offloader so the cross-offloader "waiting in
    line" screen has its trigger.
    """
    job = make_remote_peer_job(remote_peer=instances.offloader_dashboard_id, error=error)
    instances.receiver_bus.fire(EventType.JOB_QUEUED, JobLifecycleData(job=job))
    # Listener runs synchronously inside ``fire``; nothing to
    # await. Yielding once lets any background-task scheduling
    # the listener's send-frame work would have done settle
    # before the test fires the next event.
    await asyncio.sleep(0)
    return job


def wire_receiver_firmware_recorder(instances: PairedInstances) -> list[FirmwareJob]:
    """Wire receiver's ``db.firmware`` to record submitted jobs.

    The receiver-side ``_create_job`` builds a :class:`FirmwareJob`
    carrying every field the production controller's dispatch
    sets (configuration / job_type / remote_peer / remote_job_id);
    ``_enqueue`` resolves with ``accepted=True`` so the
    ``submit_job_ack`` lands on the success branch. The recorded
    list lets the test mutate the job's ``status`` from
    ``QUEUED`` → ``COMPLETED`` after firing the lifecycle events
    so the download-side ``_find_remote_job`` accepts it.

    ``firmware.state.jobs`` is a real dict (not a mock) so the
    receiver-side download path's ``firmware.state.jobs.values()``
    iteration finds the recorded job.
    """
    created_jobs: list[FirmwareJob] = []
    receiver_jobs: dict[str, FirmwareJob] = {}

    def _create_job(
        configuration: str,
        job_type: JobType,
        *,
        remote_peer: str = "",
        remote_job_id: str = "",
        **_: Any,
    ) -> FirmwareJob:
        job = FirmwareJob(
            job_id=f"rcv-{len(created_jobs)}",
            configuration=configuration,
            job_type=job_type,
            status=JobStatus.QUEUED,
            remote_peer=remote_peer,
            remote_job_id=remote_job_id,
        )
        created_jobs.append(job)
        receiver_jobs[job.job_id] = job
        return job

    firmware = instances.receiver._db.firmware
    firmware._create_job = MagicMock(side_effect=_create_job)
    firmware._enqueue = AsyncMock(side_effect=lambda job: job)
    wire_firmware_remote_peer_api_mocks(firmware, receiver_jobs)
    # ``_on_firmware_queue_transition`` (registered on every
    # JOB_QUEUED / JOB_STARTED / terminal event) reads
    # ``queue_status_snapshot()`` and tuple-unpacks the result.
    # The harness's ``MagicMock`` firmware controller returns a
    # MagicMock by default — unpacks as zero values and trips a
    # ValueError. Pin a sane tuple so the listener runs cleanly
    # rather than spamming the test log with swallowed
    # exceptions on every fire().
    firmware.queue_status_snapshot = MagicMock(
        return_value=QueueStatus(idle=True, running=False, queue_depth=0)
    )
    return created_jobs


async def drive_remote_job_to_completed(
    instances: PairedInstances,
    job: FirmwareJob,
    state_changes: _CapturedEvents,
) -> None:
    """Drive *job* QUEUED → STARTED → COMPLETED on the receiver bus and flip it COMPLETED.

    Awaits the offloader's ``running`` then ``completed``
    ``OFFLOADER_JOB_STATE_CHANGED`` via *state_changes* (a
    :func:`capture_events` handle), then sets ``job.status`` so the
    download side's ``_find_remote_job`` accepts it for
    ``download_artifacts``.
    """
    instances.receiver_bus.fire(EventType.JOB_QUEUED, JobLifecycleData(job=job))
    instances.receiver_bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=job))
    await state_changes.wait_for_status("running")
    instances.receiver_bus.fire(EventType.JOB_COMPLETED, JobLifecycleData(job=job))
    await state_changes.wait_for_status("completed")
    job.status = JobStatus.COMPLETED
