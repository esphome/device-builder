"""
End-to-end: ``download_artifacts`` round-trip across the live peer-link.

Exercises the 6a wire surface (#547) all the way through both
halves of the pair. The unit tests in
``tests/test_remote_build_artifacts_download.py`` cover the
receiver-side :class:`ArtifactsDownloadSender` branches in
isolation; the unit tests in
``tests/test_remote_build_peer_link_client.py`` cover the
offloader-side :meth:`PeerLinkClient.download_artifacts` send +
receive-loop dispatchers; the e2e variant pins the contract
between them, so a wire-shape regression on either side
surfaces here rather than slipping past two unit suites that
pass on the same drift.

The chain (happy path):

  offloader-side ``RemoteBuildController.download_artifacts``
                       →  ``PeerLinkClient.download_artifacts``
                       →  peer-link ``download_artifacts`` frame
                          (real Noise AEAD)
                       →  receiver-side ``_run_session_loops``
                          receive loop
                       →  ``ArtifactsDownloadSender.handle_download_artifacts``
                          resolves ``(remote_peer, remote_job_id)``
                          via linear scan over ``firmware._jobs``,
                          packs the artifacts, streams
                          ``artifacts_start`` → ``artifacts_chunk``
                          → ``artifacts_end{accepted: true}``
                       →  offloader-side dispatchers (one per
                          frame type) fill the per-job future
                          the awaiter is parked on
                       →  WS command unpacks the tarball into
                          ``{job_id, idedata, images, total_bytes}``

The receiver-side :func:`_pack_build_artifacts` is stubbed with
a synthetic tarball: building a real one would require a
StorageJSON sidecar + cached ``idedata.json`` + a real
firmware binary on disk, none of which the e2e harness needs to
stand up. The tarball still carries the canonical layout the
production packer emits (idedata.json first, firmware.bin,
then every ``extra.flash_images[].path`` basename) so the
offloader-side unpack runs against the real wire bytes.

The receiver's ``db.firmware._jobs`` map is seeded with a
synthetic :class:`FirmwareJob` whose
``(remote_peer, remote_job_id)`` matches the dialogue —
``ArtifactsDownloadSender._find_remote_job`` walks that map
directly (the production controller's queue isn't running in
the harness), and the cardinality is bounded by retention so
the linear scan is the same shape as production.
"""

from __future__ import annotations

import base64
import io
import json
import tarfile
from typing import Any

import pytest

from esphome_device_builder.controllers.remote_build.artifacts_download import (
    _PackedArtifacts,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)

from .conftest import PairedInstances


def _seed_firmware_job(
    instances: PairedInstances,
    *,
    status: JobStatus = JobStatus.COMPLETED,
    remote_job_id: str = "off-job-1",
    job_id: str = "rcv-job-1",
    configuration: str = "kitchen.yaml",
) -> FirmwareJob:
    """Put a remote-peer :class:`FirmwareJob` on the receiver's firmware map.

    :meth:`ArtifactsDownloadSender._find_remote_job` linear-
    scans ``firmware._jobs`` for a matching
    ``(remote_peer, remote_job_id)``; seeding here lets the
    e2e flow proceed past the ``unknown_job`` soft-reject
    without standing up a real firmware queue. The same
    primitive the existing receiver-side fan-out tests use
    for ``JobFanout``'s cache; that path subscribes to
    ``JOB_QUEUED``, the download path reads ``_jobs``
    directly — different cache.
    """
    job = FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=JobType.COMPILE,
        status=status,
        remote_peer=instances.offloader_dashboard_id,
        remote_job_id=remote_job_id,
    )
    instances.receiver._db.firmware._jobs = {job_id: job}
    return job


def _build_artifacts_tarball() -> bytes:
    """Build the canonical artifacts-tarball layout for stubbing the packer.

    Mirrors :func:`_pack_build_artifacts`'s production layout:
    ``idedata.json`` first (so the offloader-side unpack can
    parse the manifest before walking the binaries), then
    ``firmware.bin``, then every ``extra.flash_images[].path``
    basename. Receiver-side paths in ``idedata`` are absolute
    (matching what upstream esphome writes) so the offloader-
    side basename rewrite is exercised end-to-end.
    """
    idedata: dict[str, Any] = {
        "extra": {
            "flash_images": [
                {"path": "/r/build/bootloader.bin", "offset": "0x1000"},
                {"path": "/r/build/partitions.bin", "offset": "0x8000"},
            ]
        }
    }
    idedata_bytes = json.dumps(idedata).encode("utf-8")
    members: list[tuple[str, bytes]] = [
        ("idedata.json", idedata_bytes),
        ("firmware.bin", b"firmware-bin-bytes"),
        ("bootloader.bin", b"bootloader-bytes"),
        ("partitions.bin", b"partitions-bytes"),
    ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_download_artifacts_round_trip_returns_unpacked_images(
    paired_instances: PairedInstances,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``download_artifacts`` → real wire stream → unpacked ``{idedata, images, …}``.

    Pins the happy-path round-trip. The receiver packs the
    artifacts (stubbed packer, but the result rides through
    every other production step — Noise AEAD encrypt, frame
    chunking via :func:`chunk_bundle`, post-assembly SHA-256
    verification on the offloader, basename rewrite of
    ``extra.flash_images[].path``). Assertions cover the wire-
    shape contract end-to-end:

    * ``idedata.extra.flash_images[].path`` rewritten from
      receiver-absolute to bare basenames the in-tarball
      entries match.
    * ``images`` is ``firmware.bin`` first (with the
      ``firmware_offset`` the receiver placed on the start
      frame), then every extra in declared order.
    * Per-image bytes round-trip verbatim through the base64
      wire envelope.
    * ``total_bytes`` is the sum of every image's ``size``.
    """
    await paired_instances.wait_until_session_opened()
    job = _seed_firmware_job(paired_instances)

    tarball = _build_artifacts_tarball()

    def _fake_pack(_configuration: str) -> _PackedArtifacts:
        return _PackedArtifacts(tarball=tarball, firmware_offset="0x10000")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_download._pack_build_artifacts",
        _fake_pack,
    )

    result = await paired_instances.offloader.download_artifacts(
        pin_sha256=paired_instances.pin_sha256,
        job_id=job.remote_job_id,
    )

    assert result["job_id"] == job.remote_job_id
    assert result["idedata"] == {
        "extra": {
            "flash_images": [
                {"path": "bootloader.bin", "offset": "0x1000"},
                {"path": "partitions.bin", "offset": "0x8000"},
            ]
        }
    }
    images = result["images"]
    assert [img["name"] for img in images] == [
        "firmware.bin",
        "bootloader.bin",
        "partitions.bin",
    ]
    assert images[0]["offset"] == "0x10000"
    assert images[1]["offset"] == "0x1000"
    assert images[2]["offset"] == "0x8000"
    assert base64.b64decode(images[0]["data_b64"]) == b"firmware-bin-bytes"
    assert base64.b64decode(images[1]["data_b64"]) == b"bootloader-bytes"
    assert base64.b64decode(images[2]["data_b64"]) == b"partitions-bytes"
    assert result["total_bytes"] == sum(int(img["size"]) for img in images)


@pytest.mark.asyncio
async def test_download_artifacts_unknown_job_surfaces_not_found(
    paired_instances: PairedInstances,
) -> None:
    """A ``job_id`` with no matching ``FirmwareJob`` surfaces ``NOT_FOUND``.

    Pins the soft-reject round-trip for the first of the
    receiver's five structured reject reasons (``unknown_job``
    / ``build_dir_missing`` / ``job_not_completed`` /
    ``duplicate_download`` / ``pack_failed``). The receiver-
    side :meth:`_find_remote_job` returns ``None`` when the
    ``(remote_peer, remote_job_id)`` correlation isn't in
    ``firmware._jobs``; the sender replies with a single
    ``artifacts_end{accepted: false, reason: "unknown_job"}``
    frame (no preceding ``artifacts_start``); the offloader-
    side WS layer maps that reason to
    :attr:`ErrorCode.NOT_FOUND` via
    :data:`_DOWNLOAD_ARTIFACTS_REASON_TO_ERROR_CODE`.
    """
    await paired_instances.wait_until_session_opened()
    # Deliberately don't seed the firmware map; the linear
    # scan finds nothing and the sender's first branch trips.
    paired_instances.receiver._db.firmware._jobs = {}

    with pytest.raises(CommandError) as exc_info:
        await paired_instances.offloader.download_artifacts(
            pin_sha256=paired_instances.pin_sha256,
            job_id="off-job-never-existed",
        )

    assert exc_info.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_download_artifacts_job_not_completed_surfaces_precondition_failed(
    paired_instances: PairedInstances,
) -> None:
    """A still-running job's download surfaces ``PRECONDITION_FAILED``.

    Pins the second soft-reject mapping. The receiver refuses
    to pack artifacts for a non-terminal job — the build dir's
    contents are partial during a running compile, and a half-
    rendered ``firmware.bin`` isn't flashable. The wire reply
    is ``artifacts_end{accepted: false, reason:
    "job_not_completed"}``; the offloader-side WS layer
    maps that to :attr:`ErrorCode.PRECONDITION_FAILED` so the
    frontend can rerender as "wait for the build to finish."
    """
    await paired_instances.wait_until_session_opened()
    job = _seed_firmware_job(paired_instances, status=JobStatus.RUNNING)

    with pytest.raises(CommandError) as exc_info:
        await paired_instances.offloader.download_artifacts(
            pin_sha256=paired_instances.pin_sha256,
            job_id=job.remote_job_id,
        )

    assert exc_info.value.code == ErrorCode.PRECONDITION_FAILED
