"""
End-to-end: ``submit_job`` round-trip across the live peer-link with a real bundle.

Closes the gap that let the "Bundle file not found" regression
(receiver-side bundle path collision with
:func:`esphome.bundle.prepare_bundle_for_compile`'s wipe step)
ship despite the unit suite passing. The unit tests in
``tests/test_remote_build_submit_job.py`` stub
:func:`esphome.bundle.prepare_bundle_for_compile` with a
trivial pass-through, so the upstream wipe-then-extract
semantics never ran against the production bundle layout; the
e2e harness here drives the real upstream function against a
real bundle written by the real receive loop, so a regression
in that seam surfaces on the ack instead of in production.

The chain:

  offloader-side :meth:`PeerLinkClient.submit_job`
                       →  ``submit_job`` header
                          (real Noise AEAD)
                       →  ``submit_job_chunk`` frames
                          (chunked + base64-enveloped by
                          :func:`chunk_bundle`)
                       →  receiver-side ``_run_session_loops``
                          receive loop
                       →  :meth:`SubmitJobReceiver.handle_submit_job`
                          + :meth:`handle_submit_job_chunk`
                          run the real :class:`BundleAssembler`,
                          verify SHA-256, write the assembled
                          tarball to disk
                       →  real
                          :func:`esphome.bundle.prepare_bundle_for_compile`
                          wipes target_dir's non-preserved
                          entries, extracts the bundle, returns
                          the absolute YAML path
                       →  ``firmware._create_job`` + ``_enqueue``
                          land the :class:`FirmwareJob` with
                          ``remote_peer=offloader_dashboard_id``
                       →  ``submit_job_ack{accepted: true}`` rides
                          back to the offloader

The offloader's ``remote_build/submit_job`` WS command
additionally spawns the ``esphome bundle`` CLI subprocess to
build *bundle_bytes* from a YAML on disk; we bypass that step
and call :meth:`PeerLinkClient.submit_job` with a pre-built
in-test bundle so the test stays focused on the receiver-side
gap. The subprocess invocation is upstream esphome's contract,
covered separately by tests on :func:`build_yaml_bundle`.
"""

from __future__ import annotations

import io
import json
import tarfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.helpers.remote_build_layout import RemoteBuildPath
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
)

from ..conftest import capture_events
from .conftest import PairedInstances


def _build_real_bundle(*, configuration_filename: str = "kitchen.yaml") -> bytes:
    """Build a minimal-but-valid esphome bundle the upstream extractor accepts.

    Upstream :func:`esphome.bundle.extract_bundle` needs:

    * A ``manifest.json`` member with
      ``{"manifest_version": 1, "config_filename": "..."}``.
    * The referenced ``config_filename`` member, with non-empty
      content.

    Nothing else — :func:`_validate_tar_members` rejects symlinks
    / absolute paths / path traversal / oversized archives, all
    of which we naturally avoid by emitting two regular file
    members at the top level.

    Deliberately not going through :class:`BundleBuilder`: that
    class drives ``BundleBuilder.discover_files`` off real
    ``CORE.config_dir`` + ``CORE.config_path`` state, which
    would couple this test to a real config-dir layout when all
    we want is the wire-format contract. The minimal bundle
    here exercises the same upstream extract code as a
    BundleBuilder-emitted one for the path that the receiver-
    side gap lives in.
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
    """Make the receiver's ``db.firmware`` record submitted jobs and report success.

    Mirrors :func:`tests.test_remote_build_submit_job._make_firmware_controller`'s
    shape: ``_create_job`` builds a :class:`FirmwareJob` carrying every
    field the receiver-side dispatch passes through, ``_enqueue`` resolves
    accepted=True so the ``submit_job_ack`` lands on the success branch.
    Returns the list ``_create_job`` appends to so the test body can
    assert on the queued job's fields after the round-trip.
    """
    created_jobs: list[FirmwareJob] = []

    def _create_job(
        configuration: str,
        job_type: JobType,
        *,
        remote_peer: str = "",
        remote_peer_label: str = "",
        remote_job_id: str = "",
        device_name: str = "",
        device_friendly_name: str = "",
        **_: Any,
    ) -> FirmwareJob:
        job = FirmwareJob(
            job_id=f"rcv-{len(created_jobs)}",
            configuration=configuration,
            job_type=job_type,
            status=JobStatus.QUEUED,
            remote_peer=remote_peer,
            remote_peer_label=remote_peer_label,
            remote_job_id=remote_job_id,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
        )
        created_jobs.append(job)
        return job

    firmware = instances.receiver._db.firmware
    firmware._create_job = MagicMock(side_effect=_create_job)
    firmware._enqueue = AsyncMock(side_effect=lambda job: job)
    # ``submit_job`` resolves the offloader's display label by
    # going through
    # ``self._firmware._db.remote_build_receiver.approved_peer_label``.
    # The firmware controller is a MagicMock in this harness, so
    # the ``_db.remote_build_receiver`` chain returns yet-another
    # MagicMock; pin it to the real receiver-side controller so the
    # label lookup at job-create time hits the actual approved-peer
    # registry the pairing fixture populated.
    firmware._db.remote_build_receiver = instances.receiver
    return created_jobs


@pytest.mark.asyncio
async def test_submit_job_round_trip_extracts_real_bundle_and_queues_job(
    paired_instances: PairedInstances,
) -> None:
    """``submit_job`` from the offloader lands a queued :class:`FirmwareJob` on the receiver.

    Pins the full wire-and-extract round-trip end-to-end. The
    happy-path assertions cover both the wire contract and the
    on-disk contract that the unit tests + the existing e2e
    harness skipped:

    * ``submit_job_ack{accepted: true, job_id}`` flows back over
      the same Noise channel.
    * Receiver's ``_create_job`` was called with the canonical
      relative YAML path the dispatch resolved, the offloader's
      ``dashboard_id`` on ``remote_peer``, and the offloader-
      supplied ``job_id`` on ``remote_job_id``.
    * Receiver's ``_enqueue`` was awaited (the queue side
      observes the job).
    * The extracted YAML actually exists on disk at
      ``<receiver_config_dir>/.esphome/.remote_builds/<dashboard_id>/<device_name>/<configuration>.yaml``,
      with the body the offloader-side bundle carried. This is
      the load-bearing assertion the bundle-path fix unblocks:
      a regression that puts the bundle back inside target_dir
      would land here as "Bundle file not found" on the
      upstream extract and the ack would carry ``accepted=False``,
      not ``True``.
    """
    await paired_instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware_recorder(paired_instances)

    bundle_bytes = _build_real_bundle()
    handle = paired_instances.offloader._peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-job-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle_bytes,
    )

    assert ack["accepted"] is True
    assert ack["job_id"] == "off-job-1"
    assert "reason" not in ack

    assert len(created_jobs) == 1
    job = created_jobs[0]
    assert job.remote_peer == paired_instances.offloader_dashboard_id
    assert job.remote_job_id == "off-job-1"
    assert job.job_type is JobType.COMPILE
    # Pin that the awaited _enqueue saw the same FirmwareJob
    # _create_job built — assert_awaited_once() alone would
    # accept a second enqueue of any object.
    paired_instances.receiver._db.firmware._enqueue.assert_awaited_once_with(job)

    receiver_config_dir = paired_instances.receiver._db.settings.config_dir
    extracted_yaml = (
        RemoteBuildPath(
            dashboard_id=paired_instances.offloader_dashboard_id,
            device_name="kitchen",
        ).subtree(receiver_config_dir)
        / "kitchen.yaml"
    )
    assert extracted_yaml.is_file(), (
        f"extracted YAML missing at {extracted_yaml} — upstream "
        "prepare_bundle_for_compile didn't write it (possibly the "
        "bundle-path-inside-target_dir regression)"
    )
    assert extracted_yaml.read_bytes() == b"esphome:\n  name: kitchen\n"
    # FirmwareJob.configuration is the receiver-relative POSIX path
    # (same shape upstream emits for local jobs); pin it so a
    # cross-platform regression on path separator handling surfaces
    # here rather than later in the queue's working-dir logic.
    assert job.configuration == extracted_yaml.relative_to(receiver_config_dir).as_posix()


@pytest.mark.asyncio
async def test_submit_job_round_trip_with_relative_receiver_config_dir(
    paired_instances_relative_receiver_config_dir: PairedInstances,
) -> None:
    """``submit_job`` round-trip succeeds when the receiver's config_dir is relative (#678)."""
    instances = paired_instances_relative_receiver_config_dir
    await instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware_recorder(instances)

    bundle_bytes = _build_real_bundle()
    handle = instances.offloader._peer_link_clients[instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-job-678",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle_bytes,
    )

    assert ack["accepted"] is True, ack
    assert ack["job_id"] == "off-job-678"
    assert "reason" not in ack

    assert len(created_jobs) == 1
    job = created_jobs[0]
    assert job.remote_peer == instances.offloader_dashboard_id
    assert job.remote_job_id == "off-job-678"
    assert job.job_type is JobType.COMPILE
    instances.receiver._db.firmware._enqueue.assert_awaited_once_with(job)

    receiver_config_dir = instances.receiver._db.settings.config_dir.resolve()
    extracted_yaml = (
        RemoteBuildPath(
            dashboard_id=instances.offloader_dashboard_id,
            device_name="kitchen",
        ).subtree(receiver_config_dir)
        / "kitchen.yaml"
    )
    assert extracted_yaml.is_file()
    assert extracted_yaml.read_bytes() == b"esphome:\n  name: kitchen\n"
    assert job.configuration == extracted_yaml.relative_to(receiver_config_dir).as_posix()


@pytest.mark.asyncio
async def test_submit_job_round_trip_then_fanout_to_offloader_bus(
    paired_instances: PairedInstances,
) -> None:
    """``submit_job`` → ``JOB_STARTED`` on receiver → ``OFFLOADER_JOB_STATE_CHANGED`` on offloader.

    Extends the happy-path round-trip with the fan-out leg:
    once the receiver has queued the :class:`FirmwareJob`, the
    firmware controller's lifecycle events drive
    :class:`JobFanout` to push ``job_state_changed`` frames
    over the same peer-link session. Pins that the receiver-
    side ``JobFanout._remote_jobs`` correlation cache was
    populated by the ``JOB_QUEUED`` the queue fires after
    ``_enqueue`` lands (the real receiver-side flow; in this
    test we fire ``JOB_QUEUED`` + ``JOB_STARTED`` manually
    because ``_enqueue`` is stubbed to resolve immediately
    without driving the queue's own state machine).

    The existing ``test_submit_job_fanout.py`` covers
    ``JOB_STARTED`` → fan-out in isolation by seeding the
    correlation directly via ``make_and_seed_remote_peer_job``.
    Stitching the fan-out leg onto the real submit_job round-
    trip closes the gap end-to-end so a future regression in
    either half (submit_job extract OR JobFanout dispatch) can
    surface on this test.
    """
    await paired_instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    ack = await paired_instances.offloader._peer_link_clients[
        paired_instances.pin_sha256
    ].client.submit_job(
        job_id="off-job-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=_build_real_bundle(),
    )
    assert ack["accepted"] is True
    assert len(created_jobs) == 1
    job = created_jobs[0]

    # Fire the lifecycle events the real firmware queue would
    # have fired after _enqueue. JOB_QUEUED populates
    # JobFanout's correlation cache; JOB_STARTED triggers the
    # fan-out frame.
    paired_instances.receiver_bus.fire(EventType.JOB_QUEUED, JobLifecycleData(job=job))
    # JobFanout._on_queued is a sync bus listener — it runs
    # inline inside fire() and populates the correlation cache
    # before fire() returns. Pin that observable invariant here
    # so a regression that makes _on_queued async (or schedules
    # the cache update via a background task) trips this
    # assertion instead of producing flaky CI behaviour at the
    # JOB_STARTED fan-out check below.
    assert job.job_id in paired_instances.receiver._job_fanout._remote_jobs
    paired_instances.receiver_bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=job))

    # JOB_QUEUED now fans out as ``queued`` ahead of ``running``;
    # poll for the running transition rather than asserting on
    # whichever frame raced to the bus first.
    payload = await state_changes.wait_for_status("running")
    assert payload["job_id"] == "off-job-1"  # offloader's tag echoed back
    assert payload["pin_sha256"] == paired_instances.pin_sha256


@pytest.mark.asyncio
async def test_submit_job_round_trip_carries_display_strings_to_receiver_job(
    paired_instances: PairedInstances,
) -> None:
    """``submit_job``'s display-string header lands on the receiver-side FirmwareJob.

    Pins the wire contract from esphome/device-builder#587 end-to-end:
    the offloader-side frontend has the device's display strings
    (``device_name`` / ``device_friendly_name``) when it kicks the
    install off and threads them through ``client.submit_job(...)``;
    the receiver-side dispatch coerces them off the
    ``SubmitJobFrameData`` header and lands them on the queued
    :class:`FirmwareJob` so the receiver's own task-list dialog can
    show "AC Float Monitor 32 (kitchen)" from "offloader" without
    having to parse the YAML it just extracted. ``remote_peer_label``
    rides through the same path; it isn't a wire-header field (the
    receiver looks it up locally off its approved-peer registry at
    job-create time), but it's part of the same display-string
    triple; a regression in any one of the three would break it, so
    pin all three here.

    The unit tests cover each side in isolation
    (``test_remote_build_submit_job.py``, ``test_remote_runner.py``);
    this e2e wires them up over a real Noise session so a future
    rebase that swaps the wire header's spelling, drops a field
    from ``_coerce_display_field``'s coverage, or changes the
    ``approved_peer_label`` lookup site lands on this test instead
    of going silent until a frontend bug report.
    """
    await paired_instances.wait_until_session_opened()
    created_jobs = _wire_receiver_firmware_recorder(paired_instances)

    handle = paired_instances.offloader._peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-job-display",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=_build_real_bundle(),
        device_name="kitchen",
        device_friendly_name="AC Float Monitor 32",
    )

    assert ack["accepted"] is True
    assert len(created_jobs) == 1
    job = created_jobs[0]

    # device_name / device_friendly_name flow off the SubmitJobFrameData
    # header through ``_coerce_display_field`` onto the queued job.
    assert job.device_name == "kitchen"
    assert job.device_friendly_name == "AC Float Monitor 32"

    # remote_peer_label isn't on the wire — the receiver looks it
    # up at job-create time off its approved-peer registry, which
    # was populated by the pair_request flow in the conftest. The
    # pairing fixture stored "offloader" as the offloader-supplied
    # label; assert against whatever the registry currently
    # reports so a future fixture change doesn't pin a magic
    # string here.
    expected_label = paired_instances.receiver.approved_peer_label(
        paired_instances.offloader_dashboard_id
    )
    assert expected_label, (
        "test precondition: pairing fixture should leave the offloader "
        "approved with a non-empty label so the receiver-side lookup "
        "has something to find"
    )
    assert job.remote_peer_label == expected_label
