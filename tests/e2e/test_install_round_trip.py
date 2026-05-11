"""
End-to-end: 7a-3 transparent install — submit_job + fan-out + download_artifacts on one session.

Phase 7a-3 (#568) wired the offloader-side ``firmware/install``
through :func:`helpers.build_scheduler.pick_build_path` and
extended :func:`remote_runner.run_remote_job` to run both
sides of a transparent install on one paired session:

* ``client.submit_job(target="compile")`` to dispatch the
  compile to the receiver;
* the receiver-side :class:`JobFanout` translates its local
  firmware queue's ``JOB_*`` lifecycle into wire
  ``job_state_changed`` frames;
* on receiver-completed the offloader pulls the artifact
  tarball back via ``client.download_artifacts(job_id=...)``
  and flashes locally.

Existing e2e tests cover each piece in isolation —
``test_submit_job.py`` (submit_job ack + extracted YAML),
``test_submit_job_fanout.py`` (single ``JOB_STARTED`` →
``OFFLOADER_JOB_STATE_CHANGED``), ``test_download_artifacts.py``
(download_artifacts round-trip from a pre-seeded receiver job).
What 7a-3 introduced is the **combination**: the same paired
Noise session has to carry submit_job, then the lifecycle
fan-out, then download_artifacts, all keyed on the same
``(offloader dashboard_id, offloader-side job_id)`` correlation
through to the receiver's
``ArtifactsDownloadSender._find_remote_job`` linear scan over
``firmware._jobs``. A regression that breaks the correlation
between the submit-side and download-side reads, or one that
fails to keep the session healthy across two application-
message types, would slip past the per-flow tests but surface
on this combined round-trip.

The harness's per-side firmware controller stays a
``MagicMock`` (single source of truth: ``make_remote_build_controller``
in ``tests/conftest.py``) — we synthesise the receiver-side
``JOB_*`` events here rather than running a real compile
subprocess. The point is pinning the wire shape across the
two-flow combination, not the build pipeline. Wall-clock stays
sub-second.

The local upload subprocess that the production runner would
spawn after ``download_artifacts`` is out of scope here too —
unit tests in ``test_remote_runner.py`` already pin the
download + extract + spawn chain; the e2e variant stops at
"both wire flows ran on one session and the artifacts decoded
on the offloader side."
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.helpers.build_scheduler import (
    BuildPath,
    pick_build_path,
)
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
    PeerQueueStatusSnapshotEntry,
)

from .._storage_fixtures import write_storage_json
from ..conftest import capture_events
from .conftest import PairedInstances


def _build_real_bundle(*, configuration_filename: str = "kitchen.yaml") -> bytes:
    """Build a minimal-but-valid esphome bundle the upstream extractor accepts.

    Mirror of ``test_submit_job._build_real_bundle`` — kept local
    so this test file is self-contained. Upstream
    ``esphome.bundle.extract_bundle`` only needs a manifest plus
    the referenced ``config_filename`` member.
    """
    manifest = {
        "manifest_version": 1,
        "config_filename": configuration_filename,
    }
    yaml_body = b"esphome:\n  name: kitchen\n"
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


def _wire_receiver_firmware_recorder(instances: PairedInstances) -> list[FirmwareJob]:
    """Wire receiver's ``db.firmware`` to record submitted jobs.

    Mirror of ``test_submit_job._wire_receiver_firmware_recorder``.
    The receiver-side ``_create_job`` builds a :class:`FirmwareJob`
    carrying every field the production controller's dispatch
    sets (configuration / job_type / remote_peer / remote_job_id);
    ``_enqueue`` resolves with ``accepted=True`` so the
    ``submit_job_ack`` lands on the success branch. The recorded
    list lets the test mutate the job's ``status`` from
    ``QUEUED`` → ``COMPLETED`` after firing the lifecycle events
    so the download-side ``_find_remote_job`` accepts it.

    ``firmware._jobs`` is a real dict (not a mock) so the
    receiver-side download path's ``firmware._jobs.values()``
    iteration finds the recorded job. Production has the real
    queue populating this dict; here we populate it from
    ``_create_job``.
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
    firmware._jobs = receiver_jobs
    # ``_on_firmware_queue_transition`` (registered on every
    # JOB_QUEUED / JOB_STARTED / terminal event) reads
    # ``queue_status_snapshot()`` and tuple-unpacks the result.
    # The harness's ``MagicMock`` firmware controller returns a
    # MagicMock by default — unpacks as zero values and trips a
    # ValueError. Pin a sane tuple so the listener runs cleanly
    # rather than spamming the test log with swallowed
    # exceptions on every fire().
    firmware.queue_status_snapshot = MagicMock(return_value=(True, False, 0))
    return created_jobs


def _write_build_artifacts_on_disk(tmp_path: Path, *, configuration: str) -> dict[str, bytes]:
    """Lay down a real StorageJSON sidecar + idedata.json + per-image binaries.

    Mirror of ``test_download_artifacts._write_build_artifacts_on_disk``,
    but parameterised on *configuration* so the StorageJSON
    sidecar lands at exactly the path
    :func:`load_build_artifacts` will read for the
    receiver-recorded job — which submit_job sets to the
    relative path under ``.esphome/.remote_builds/<dashboard_id>/<device>/``.
    Production would have the real build pipeline write these
    files there; the e2e variant short-circuits the build.

    The autouse ``_core_config_path_in_tmp`` fixture pins
    ``CORE.data_dir`` to ``tmp_path / .esphome``; the
    StorageJSON path is therefore
    ``tmp_path / .esphome / storage / <configuration>.json``.
    """
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    images: dict[str, bytes] = {
        "firmware.bin": b"firmware-bin-bytes",
        "bootloader.bin": b"bootloader-bytes",
        "partitions.bin": b"partitions-bytes",
    }
    image_paths: dict[str, Path] = {}
    for name, payload in images.items():
        path = build_dir / name
        path.write_bytes(payload)
        image_paths[name] = path

    # ``write_storage_json`` lays down
    # ``CORE.data_dir/storage/<configuration>.json``. A
    # submit_job-derived configuration is a relative path with
    # segments like ``.esphome/.remote_builds/<id>/kitchen/kitchen.yaml``,
    # so the sidecar lands under several intermediate dirs that
    # the helper doesn't create. Ensure them here so the
    # ``.write_text`` inside ``write_storage_json`` succeeds.
    sidecar_path = tmp_path / ".esphome" / "storage" / f"{configuration}.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    write_storage_json(
        tmp_path,
        configuration,
        firmware_bin_path=image_paths["firmware.bin"],
        overrides={"target_platform": "esp32"},
    )

    stem = Path(configuration).stem
    idedata_dir = tmp_path / ".esphome" / "idedata"
    idedata_dir.mkdir(parents=True, exist_ok=True)
    idedata = {
        "extra": {
            "flash_images": [
                {"path": str(image_paths["bootloader.bin"]), "offset": "0x1000"},
                {"path": str(image_paths["partitions.bin"]), "offset": "0x8000"},
            ]
        }
    }
    (idedata_dir / f"{stem}.json").write_text(json.dumps(idedata), encoding="utf-8")
    return images


def _seed_idle_queue_status(instances: PairedInstances) -> None:
    """Pretend the receiver broadcast a single idle ``queue_status`` snapshot.

    The 7a-3 install routing reads
    :attr:`RemoteBuildController._peer_queue_status` through
    :meth:`build_scheduler_snapshot` and refuses to pick REMOTE
    for any pin without an idle entry — the scheduler's
    "no signal that the receiver can accept work" gate. In
    production the receiver's
    :meth:`FirmwareController.queue_status_snapshot` fires on
    queue idle/run transitions and the offloader's
    :meth:`_on_offloader_queue_status_changed` listener
    populates the cache. The harness's receiver-side firmware
    controller is a :class:`MagicMock`, so the live broadcast
    never fires; seed the offloader cache directly with the
    shape the scheduler expects.
    """
    instances.offloader._peer_queue_status[instances.pin_sha256] = PeerQueueStatusSnapshotEntry(
        receiver_hostname="127.0.0.1",
        receiver_port=instances.receiver_server.port or 0,
        pin_sha256=instances.pin_sha256,
        idle=True,
        running=False,
        queue_depth=0,
    )


@pytest.mark.asyncio
async def test_paired_offloader_scheduler_picks_remote_for_idle_receiver(
    paired_instances: PairedInstances,
) -> None:
    """The live paired-instances RAM state resolves to ``BuildPath.REMOTE``.

    Pins the 7a-3 install routing's RAM-canonical contract:

    * :meth:`RemoteBuildController.build_scheduler_snapshot`
      reads ``_pairings`` (APPROVED + paired_at-sorted),
      ``_open_peer_links`` (live peer-link session set), and
      ``_peer_queue_status`` (per-pin idle/running cache) into
      one :class:`BuildSchedulerInputs`.
    * :func:`helpers.build_scheduler.pick_build_path` returns
      ``BuildPath.REMOTE`` with the pin matching the harness's
      live receiver.

    The unit tests in ``test_remote_build_controller.py`` cover
    ``build_scheduler_snapshot`` against a stub controller's
    pre-seeded dicts. The e2e value here is pinning that a
    real-handshake / real-approve / real-peer-link-session
    lifecycle lands the RAM state in the shape the scheduler
    expects — without an explicit gate in between, a future
    regression on any of those three mutation sites would slip
    past the unit suite (which doesn't drive the live
    transitions) but surface here.
    """
    await paired_instances.wait_until_session_opened()
    _seed_idle_queue_status(paired_instances)

    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    decision = pick_build_path(snapshot)

    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == paired_instances.pin_sha256


@pytest.mark.asyncio
async def test_paired_offloader_scheduler_falls_back_local_without_queue_snapshot(
    paired_instances: PairedInstances,
) -> None:
    """No idle queue snapshot → scheduler falls back to LOCAL.

    Same APPROVED + live-session state as the previous test but
    no ``_peer_queue_status`` entry. The scheduler's "missing
    entry disqualifies the pairing" rule is what's pinned here —
    the design's safe-fallback stance, asserted against the
    live paired state to catch a future regression that pre-
    seeds the snapshot from the pairing row (which would
    silently route to a receiver whose queue depth we have no
    signal on).
    """
    await paired_instances.wait_until_session_opened()
    # Deliberately don't seed _peer_queue_status — production
    # populates it from the receiver's queue_status broadcast,
    # which the harness's MagicMock firmware controller never
    # fires.
    assert paired_instances.pin_sha256 not in paired_instances.offloader._peer_queue_status

    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    decision = pick_build_path(snapshot)

    assert decision.path is BuildPath.LOCAL
    assert decision.pin_sha256 is None


@pytest.mark.asyncio
async def test_remote_install_submit_then_lifecycle_then_download_on_one_session(
    paired_instances: PairedInstances,
    tmp_path: Path,
) -> None:
    """7a-3's transparent install: submit + fan-out + download on one Noise session.

    The full chain the offloader-side
    :func:`remote_runner.run_remote_job` runs end-to-end for an
    ``UPLOAD`` / ``INSTALL`` job, minus the local
    ``esphome upload --file`` subprocess (out of scope —
    ``test_remote_runner.py`` covers the spawn + stream + exit-
    code translation in isolation).

    Sequence:

    1. ``client.submit_job(target="compile")`` with a real
       bundle. Receiver-side dispatch lands a queued
       :class:`FirmwareJob` whose ``remote_peer`` matches the
       harness's offloader and ``remote_job_id`` echoes the
       offloader-side tag.
    2. Fire ``JOB_QUEUED`` on the receiver bus so
       :class:`JobFanout` populates its
       ``(offloader, offloader-side job_id)`` correlation cache.
    3. Fire ``JOB_STARTED`` → fan-out emits a ``running``
       ``job_state_changed`` over the same paired session;
       offloader's receive loop fires
       ``OFFLOADER_JOB_STATE_CHANGED``.
    4. Fire ``JOB_COMPLETED`` → terminal ``completed`` frame
       lands on the offloader bus.
    5. The receiver-side recorded job's ``status`` is flipped
       to ``COMPLETED`` so
       :meth:`ArtifactsDownloadSender._find_remote_job` accepts
       it for download.
    6. ``paired_instances.offloader.download_artifacts(pin,
       job_id=<offloader-side id>)`` runs on the same session
       and returns the unpacked artifact set.

    Assertions cover the two-flow contract end-to-end:

    * ``submit_job_ack{accepted: true}`` flows back; the
      receiver's recorded job carries the correlation fields.
    * Two ``OFFLOADER_JOB_STATE_CHANGED`` events landed
      (``running`` then ``completed``), both echoing the
      offloader-supplied ``job_id`` and the live ``pin_sha256``
      from the harness's handshake — the fan-out preserves the
      offloader's tag rather than leaking the receiver's local
      id.
    * ``download_artifacts`` returns the StorageJSON +
      ``idedata`` + base64-enveloped image bytes the receiver
      packed; the artifact set survives the round-trip on the
      same Noise channel that just carried submit_job + the
      fan-out frames.
    """
    await paired_instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )
    # Snapshot the OPENED counts AFTER the initial pair-up but
    # BEFORE driving submit_job → fan-out → download_artifacts.
    # The point of the assertion at the end of this test is to
    # prove all three flows ran on the *same* Noise session that
    # was open here, not on a re-opened one — :class:`PeerLinkClient`
    # auto-reconnects on transport drops, and a regression that
    # closes the session between message types would otherwise
    # silently get a fresh session for download_artifacts.
    opened_at_start = (
        len(paired_instances.offloader_opened),
        len(paired_instances.receiver_opened),
    )

    # 1. submit_job with a real bundle.
    handle = paired_instances.offloader._peer_link_clients[paired_instances.pin_sha256]
    bundle_bytes = _build_real_bundle()
    ack = await handle.client.submit_job(
        job_id="off-job-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle_bytes,
    )
    assert ack["accepted"] is True
    assert len(created_jobs) == 1
    receiver_job = created_jobs[0]
    assert receiver_job.remote_peer == paired_instances.offloader_dashboard_id
    assert receiver_job.remote_job_id == "off-job-1"

    # Now that the receiver-side dispatch picked the YAML path
    # for this job (under ``.esphome/.remote_builds/<id>/<device>/``),
    # write the storage sidecar + idedata + image bytes at that
    # exact configuration so the download-side
    # :func:`load_build_artifacts` reads them back. Production has
    # the real build pipeline produce these files; the test writes
    # them in lieu of running esphome run.
    images = _write_build_artifacts_on_disk(tmp_path, configuration=receiver_job.configuration)

    # 2-4. Drive the receiver-side lifecycle. Each ``fire`` runs
    # the synchronous bus listeners inline; ``JobFanout._dispatch``
    # schedules the actual wire-frame send via
    # ``create_background_task`` (asyncio.create_task in the
    # harness), so we yield twice after each fire to let the
    # send + offloader's receive loop dispatch on the same loop
    # iteration. ``wait_for(state_changes.received.wait())`` is
    # the deterministic sync point.
    paired_instances.receiver_bus.fire(EventType.JOB_QUEUED, JobLifecycleData(job=receiver_job))
    paired_instances.receiver_bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=receiver_job))
    await asyncio.wait_for(state_changes.received.wait(), timeout=2.0)
    running_payload = state_changes[-1]
    assert running_payload["job_id"] == "off-job-1"
    assert running_payload["status"] == "running"
    assert running_payload["pin_sha256"] == paired_instances.pin_sha256

    # Re-arm the captured event for the next deterministic wait
    # — the helper's ``received`` event stays set after the
    # first deliver, so a second wait_for would return
    # immediately on the old payload without checking that
    # JOB_COMPLETED actually fanned out.
    state_changes.received.clear()
    paired_instances.receiver_bus.fire(EventType.JOB_COMPLETED, JobLifecycleData(job=receiver_job))
    await asyncio.wait_for(state_changes.received.wait(), timeout=2.0)
    completed_payload = state_changes[-1]
    assert completed_payload["job_id"] == "off-job-1"
    assert completed_payload["status"] == "completed"
    assert completed_payload["pin_sha256"] == paired_instances.pin_sha256

    # 5. Flip the recorded receiver-side job to COMPLETED so
    # ``ArtifactsDownloadSender._find_remote_job`` accepts it.
    # Production has the real queue do this on JOB_COMPLETED; the
    # MagicMock firmware controller skips that bookkeeping.
    receiver_job.status = JobStatus.COMPLETED

    # 6. Pull the artifacts back on the same Noise session.
    result = await paired_instances.offloader.download_artifacts(
        pin_sha256=paired_instances.pin_sha256,
        job_id="off-job-1",
    )

    assert result["job_id"] == "off-job-1"
    response_images = result["images"]
    assert [img["name"] for img in response_images] == [
        "firmware.bin",
        "bootloader.bin",
        "partitions.bin",
    ]
    # Receiver-resolved offsets ride back through the tarball.
    # esp32 firmware.bin at 0x10000, plus the two extras at their
    # declared offsets.
    assert response_images[0]["offset"] == "0x10000"
    assert response_images[1]["offset"] == "0x1000"
    assert response_images[2]["offset"] == "0x8000"
    # The per-image bytes survived the base64 envelope on a
    # session that also carried submit_job and the fan-out
    # frames; a session-state regression that didn't reset
    # between message types would surface as wrong bytes here.
    import base64  # noqa: PLC0415

    for img in response_images:
        assert base64.b64decode(img["data_b64"]) == images[img["name"]]
    assert result["total_bytes"] == sum(int(img["size"]) for img in response_images)

    # Pin "same session" — no CLOSED events fired and no
    # additional OPENED events landed past the pre-test snapshot.
    # PeerLinkClient auto-reconnects on drops, so a regression
    # that broke session liveness between message types could
    # otherwise close + re-open transparently and let
    # download_artifacts succeed on the second session; the
    # test's "all three flows on one session" claim would still
    # appear to hold.
    assert len(paired_instances.offloader_closed) == 0
    assert len(paired_instances.receiver_closed) == 0
    assert len(paired_instances.offloader_opened) == opened_at_start[0]
    assert len(paired_instances.receiver_opened) == opened_at_start[1]
