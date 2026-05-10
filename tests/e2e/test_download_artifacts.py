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
                          calls real :func:`_pack_build_artifacts`
                          (StorageJSON sidecar + ``idedata.json``
                          + per-image bytes read off the autouse
                          fixture's ``tmp_path / .esphome / ...``
                          tree), streams ``artifacts_start`` →
                          ``artifacts_chunk`` →
                          ``artifacts_end{accepted: true}``
                       →  offloader-side dispatchers (one per
                          frame type) fill the per-job future
                          the awaiter is parked on
                       →  WS command unpacks the tarball into
                          ``{job_id, idedata, images, total_bytes}``

The happy-path test runs the real packer (no monkeypatch) by
writing a real :class:`StorageJSON` sidecar +
``idedata/<name>.json`` + per-image binaries under
``tmp_path / .esphome / ...`` (the layout the autouse
``_core_config_path_in_tmp`` fixture in ``tests/conftest.py``
pins ``CORE.data_dir`` to). Soft-reject tests don't need the
packer to run; they short-circuit on the receiver's
``_find_remote_job`` / status gate.

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
import json
from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)

from .._storage_fixtures import write_storage_json
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


def _write_build_artifacts_on_disk(tmp_path: Path) -> dict[str, bytes]:
    """Lay down a real StorageJSON sidecar + idedata.json + per-image binaries.

    The autouse ``_core_config_path_in_tmp`` fixture pins
    ``CORE.config_path = tmp_path / ___DASHBOARD_SENTINEL___.yaml``
    so ``CORE.data_dir`` resolves to ``tmp_path / .esphome``;
    this helper writes:

    * ``tmp_path/.esphome/storage/kitchen.yaml.json`` — the
      :class:`StorageJSON` sidecar
      :func:`StorageJSON.load` reads on the receiver-side
      packer's first move. ``target_platform=esp32`` so
      :func:`_firmware_offset_for_platform` lands on
      ``0x10000``; ``firmware_bin_path`` points at the real
      file written below.
    * ``tmp_path/.esphome/idedata/kitchen.json`` — the
      :class:`IDEData`-shaped manifest, with
      ``extra.flash_images`` carrying absolute paths to the
      bootloader / partitions binaries.
    * The per-image binaries themselves
      (``firmware.bin`` / ``bootloader.bin`` /
      ``partitions.bin``) under ``tmp_path / build /``.

    Returns ``{basename: bytes}`` so the test can assert the
    real-disk bytes round-tripped through the wire envelope
    verbatim.
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

    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=image_paths["firmware.bin"],
        # Sidecar's ``target_platform`` drives
        # ``_firmware_offset_for_platform``; force esp32 so the
        # asserted ``firmware_offset == "0x10000"`` is the
        # receiver-resolved value rather than the ESP8266 / RP2040
        # fallback.
        overrides={"target_platform": "esp32"},
    )

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
    # Idedata filename uses ``StorageJSON.name`` (stem), not the
    # full configuration filename. ``write_storage_json`` defaults
    # ``name`` to ``Path(configuration).stem`` so we mirror that
    # here.
    (idedata_dir / "kitchen.json").write_text(json.dumps(idedata), encoding="utf-8")
    return images


@pytest.mark.asyncio
async def test_download_artifacts_round_trip_returns_unpacked_images(
    paired_instances: PairedInstances,
    tmp_path: Path,
) -> None:
    """``download_artifacts`` → real wire stream → unpacked ``{idedata, images, …}``.

    Pins the happy-path round-trip. Every production step runs:
    receiver-side :func:`_pack_build_artifacts` reads the real
    StorageJSON sidecar + ``idedata.json`` + per-image bytes
    off ``tmp_path / .esphome / ...`` (laid down by
    :func:`_write_build_artifacts_on_disk`), packs them into a
    real gzipped tarball, the receiver chunks + streams it
    via real Noise AEAD frames, the offloader's
    :class:`BundleAssembler` reassembles + SHA-256 verifies,
    the WS command unpacks the tarball into the structured
    response with ``extra.flash_images[].path`` rewritten from
    receiver-absolute to bare basenames.

    Assertions cover the wire-shape contract end-to-end:

    * ``idedata.extra.flash_images[].path`` rewritten from
      receiver-absolute to bare basenames the in-tarball
      entries match.
    * ``images`` is ``firmware.bin`` first (with the
      receiver-resolved ``firmware_offset`` from
      :func:`_firmware_offset_for_platform`), then every
      extra in declared order.
    * Per-image bytes round-trip verbatim through the base64
      wire envelope.
    * ``total_bytes`` is the sum of every image's ``size``.
    """
    await paired_instances.wait_until_session_opened()
    job = _seed_firmware_job(paired_instances)
    images = _write_build_artifacts_on_disk(tmp_path)

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
    response_images = result["images"]
    assert [img["name"] for img in response_images] == [
        "firmware.bin",
        "bootloader.bin",
        "partitions.bin",
    ]
    assert response_images[0]["offset"] == "0x10000"
    assert response_images[1]["offset"] == "0x1000"
    assert response_images[2]["offset"] == "0x8000"
    for img in response_images:
        assert base64.b64decode(img["data_b64"]) == images[img["name"]]
    assert result["total_bytes"] == sum(int(img["size"]) for img in response_images)


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
