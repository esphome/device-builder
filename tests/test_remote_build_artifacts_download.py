"""
Tests for the receiver-side ``download_artifacts`` flow (issue #106 phase 6a).

Two layers, mirroring :mod:`tests.test_remote_build_submit_job`'s
shape so the seam between this module's unit tests and the e2e
harness stays visible:

* Receiver-side :class:`ArtifactsDownloadSender` — pin the
  per-branch reject reasons (malformed frame / unknown job /
  job not completed / duplicate / build-dir-missing /
  pack-failed) and the happy-path stream
  (``artifacts_start`` → chunks →
  ``artifacts_end{accepted: true}``).
* Tarball pack/unpack contract — the receiver's
  ``pack_build_artifacts`` and the offloader's
  ``unpack_artifacts_response`` are wire-format mirrors;
  pin the round-trip + the basename rewrite of
  ``idedata.extra.flash_images[].path``.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.remote_build.artifacts_download import (
    ArtifactsDownloadSender,
)
from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    PackedArtifacts,
    UnpackArtifactsError,
    extract_firmware_bin,
    pack_build_artifacts,
    read_artifacts_tarball,
    unpack_artifacts_response,
)
from esphome_device_builder.controllers.remote_build.peer_link_client import (
    DownloadArtifactsResult,
)
from esphome_device_builder.helpers.build_artifacts import (
    BuildArtifacts,
    FlashArtifact,
    load_build_artifacts,
)
from esphome_device_builder.helpers.peer_link_bundle import FIRMWARE_MAX_TOTAL_BYTES
from esphome_device_builder.models import (
    DownloadArtifactsFrameData,
    JobStatus,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_session(*, dashboard_id: str = "alpha") -> Any:
    """Stub ``PeerLinkSession`` capturing send_app_frame + terminate calls."""
    session = MagicMock()
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    session.terminate = AsyncMock()
    return session


def _make_firmware_with_job(
    *,
    remote_peer: str = "alpha",
    remote_job_id: str = "remote-1",
    status: JobStatus = JobStatus.COMPLETED,
    configuration: str = "kitchen.yaml",
) -> Any:
    """Stub ``FirmwareController`` with a single matching FirmwareJob in ``_jobs``."""
    job = MagicMock()
    job.remote_peer = remote_peer
    job.remote_job_id = remote_job_id
    job.status = status
    job.configuration = configuration

    firmware = MagicMock()
    firmware._jobs = {"local-1": job}
    return firmware


def _make_sender(firmware: Any | None = None) -> ArtifactsDownloadSender:
    return ArtifactsDownloadSender(firmware_controller=firmware or _make_firmware_with_job())


def _last_app_frame(session: Any) -> dict[str, Any]:
    """Return the most recent ``send_app_frame`` payload."""
    return cast(dict[str, Any], session.send_app_frame.await_args.args[0])


def _all_app_frames(session: Any) -> list[dict[str, Any]]:
    """Every ``send_app_frame`` payload, in order."""
    return [cast(dict[str, Any], call.args[0]) for call in session.send_app_frame.await_args_list]


# ---------------------------------------------------------------------------
# handle_download_artifacts — frame validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken_frame",
    [
        # Missing required ``job_id``.
        {"type": "download_artifacts"},
        # Wrong type on ``job_id``.
        {"type": "download_artifacts", "job_id": 12345},
    ],
)
@pytest.mark.asyncio
async def test_download_artifacts_malformed_terminates(broken_frame: dict[str, Any]) -> None:
    """A malformed ``download_artifacts`` frame terminates the session, no end frame."""
    sender = _make_sender()
    session = _make_session()

    await sender.handle_download_artifacts(session, broken_frame)

    session.terminate.assert_awaited_once()
    session.send_app_frame.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_artifacts_unknown_job_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    An unknown job_id rejects ``unknown_job`` (no terminate).

    Receiver-side WARNING log carries the requested ``job_id``
    plus the list of ``remote_job_id`` values the receiver does
    have on file for this peer — keeps the failure debuggable
    when a frontend retry sends a stale id after a restart.
    """
    firmware = _make_firmware_with_job(remote_job_id="another")
    sender = _make_sender(firmware)
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "missing"}
    with caplog.at_level("WARNING"):
        await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    session.terminate.assert_not_awaited()
    payload = _last_app_frame(session)
    assert payload["type"] == "artifacts_end"
    assert payload["accepted"] is False
    assert payload["reason"] == "unknown_job"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "missing" in log_text  # requested job_id
    assert "another" in log_text  # the receiver's known job_ids


@pytest.mark.asyncio
async def test_download_artifacts_job_not_completed_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A job in ``RUNNING`` status rejects ``job_not_completed``.

    Receiver-side WARNING log carries the configuration string
    and the actual status so operators can see *why* the
    receiver refused the download (still compiling vs.
    cancelled vs. failed).
    """
    firmware = _make_firmware_with_job(status=JobStatus.RUNNING, configuration="kitchen.yaml")
    sender = _make_sender(firmware)
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    with caplog.at_level("WARNING"):
        await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    payload = _last_app_frame(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "job_not_completed"
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "kitchen.yaml" in log_text
    assert "running" in log_text.lower()


@pytest.mark.asyncio
async def test_download_artifacts_duplicate_rejected_without_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second concurrent download on the same session rejects ``duplicate_download``."""
    sender = _make_sender()
    session = _make_session()
    sender._inflight[session.dashboard_id] = MagicMock()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    session.terminate.assert_not_awaited()
    payload = _last_app_frame(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "duplicate_download"


@pytest.mark.asyncio
async def test_download_artifacts_build_dir_missing_rejected(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A ``FileNotFoundError`` from the packer rejects ``build_dir_missing``.

    Also pins the receiver-side log: the WARNING includes the
    configuration string and the actual missing-path text from
    the :class:`FileNotFoundError` so operators can see *which*
    file the receiver looked for. Without the path in the log
    the failure surfaces on the offloader as a bare
    ``build_dir_missing`` with no actionable detail.
    """

    def _raise_missing(_configuration: str) -> Any:
        raise FileNotFoundError("StorageJSON sidecar missing for kitchen.yaml")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_download.pack_build_artifacts",
        _raise_missing,
    )
    sender = _make_sender(_make_firmware_with_job(configuration="kitchen.yaml"))
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    with caplog.at_level("WARNING"):
        await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    payload = _last_app_frame(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "build_dir_missing"
    # Receiver-side log carries the configuration + the
    # FileNotFoundError detail (the actual missing path).
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "kitchen.yaml" in log_text
    assert "StorageJSON sidecar missing" in log_text


@pytest.mark.asyncio
async def test_download_artifacts_pack_failed_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected exception from the packer rejects ``pack_failed``."""

    def _raise(_configuration: str) -> Any:
        raise RuntimeError("size cap")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_download.pack_build_artifacts",
        _raise,
    )
    sender = _make_sender()
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    payload = _last_app_frame(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "pack_failed"


@pytest.mark.asyncio
async def test_download_artifacts_happy_path_streams_start_chunk_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path sends ``artifacts_start`` → chunk(s) → ``artifacts_end{accepted: true}``."""
    tarball = b"x" * 200

    def _fake_pack(_configuration: str) -> PackedArtifacts:
        return PackedArtifacts(tarball=tarball, firmware_offset="0x10000")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_download.pack_build_artifacts",
        _fake_pack,
    )
    sender = _make_sender()
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    frames = _all_app_frames(session)
    assert frames[0]["type"] == "artifacts_start"
    assert frames[0]["total_bytes"] == 200
    assert frames[0]["num_chunks"] == 1
    assert frames[0]["firmware_offset"] == "0x10000"
    assert len(frames[0]["artifacts_sha256"]) == 64
    assert frames[1]["type"] == "artifacts_chunk"
    assert frames[1]["chunk_index"] == 0
    assert frames[1]["is_last"] is True
    assert frames[-1]["type"] == "artifacts_end"
    assert frames[-1]["accepted"] is True
    # Inflight slot freed for the next download.
    assert session.dashboard_id not in sender._inflight


@pytest.mark.asyncio
async def test_download_artifacts_clears_inflight_on_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pack failure releases the inflight slot so the next request isn't a duplicate."""

    def _raise(_configuration: str) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_download.pack_build_artifacts",
        _raise,
    )
    sender = _make_sender()
    session = _make_session()

    frame: DownloadArtifactsFrameData = {"type": "download_artifacts", "job_id": "remote-1"}
    await sender.handle_download_artifacts(session, cast(dict[str, Any], frame))

    assert session.dashboard_id not in sender._inflight


# ---------------------------------------------------------------------------
# Pack / unpack round-trip
# ---------------------------------------------------------------------------


def _make_build_artifacts(
    tmp_path: Path,
    *,
    extra_offsets: list[tuple[str, str]] | None = None,
) -> BuildArtifacts:
    """Build a synthetic :class:`BuildArtifacts` on disk for round-trip tests.

    *extra_offsets* is a list of ``(basename, offset_hex)`` for
    additional flash images (bootloader / partitions /
    ota_data_initial). The matching idedata.json carries these
    under ``extra.flash_images`` with absolute receiver-side
    paths so the unpack can prove the basename rewrite.
    """
    extras = extra_offsets or []
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"FIRMWARE")
    flash_images = [FlashArtifact(path=firmware_path, offset="0x10000")]
    extra_entries: list[dict[str, str]] = []
    for name, offset in extras:
        path = tmp_path / name
        path.write_bytes(name.encode("ascii"))
        flash_images.append(FlashArtifact(path=path, offset=offset))
        extra_entries.append({"path": str(path), "offset": offset})
    idedata_payload = {"extra": {"flash_images": extra_entries}}
    idedata_bytes = json.dumps(idedata_payload).encode("utf-8")
    return BuildArtifacts(flash_images=flash_images, idedata_bytes=idedata_bytes)


def test_pack_build_artifacts_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tarball contains ``idedata.json`` first, then every flash image, flat."""
    artifacts = _make_build_artifacts(
        tmp_path,
        extra_offsets=[("bootloader.bin", "0x1000"), ("partitions.bin", "0x8000")],
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )

    packed = pack_build_artifacts("kitchen.yaml")

    assert packed.firmware_offset == "0x10000"
    with tarfile.open(fileobj=io.BytesIO(packed.tarball), mode="r:gz") as tar:
        names = tar.getnames()
    assert names == ["idedata.json", "firmware.bin", "bootloader.bin", "partitions.bin"]


def test_pack_build_artifacts_rejects_oversized_idedata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idedata.json alone exceeding ``FIRMWARE_MAX_TOTAL_BYTES`` raises ``RuntimeError``."""
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"FW")
    artifacts = BuildArtifacts(
        flash_images=[FlashArtifact(path=firmware_path, offset="0x10000")],
        idedata_bytes=b"x" * (FIRMWARE_MAX_TOTAL_BYTES + 1),
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )

    with pytest.raises(RuntimeError, match=r"already exceeds FIRMWARE_MAX_TOTAL_BYTES"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_rejects_oversized_compressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tarball that lands oversized after pack raises ``RuntimeError``.

    Pins the post-render cap that catches the
    incompressible-data + tar-header-overhead corner where
    the uncompressed walking sum slips under the limit but
    ``len(tarball)`` exceeds it. Constructs the corner case
    with a synthetic ``BuildArtifacts`` whose uncompressed
    total is tiny (passing the walking gates) but whose tar
    headers + gzip framing inflate ``len(tarball)`` past a
    monkey-patched cap.
    """
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"")  # 0 bytes — uncompressed walks pass with any cap >= 2
    artifacts = BuildArtifacts(
        flash_images=[FlashArtifact(path=firmware_path, offset="0x10000")],
        idedata_bytes=b"{}",  # 2 bytes
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )
    # Cap=10 lets uncompressed total (2 bytes idedata + 0
    # bytes firmware) clear the two walking gates but the
    # rendered tarball — gzip envelope (~20B) + two tar
    # headers (1024B compressed to ~30B) — easily exceeds.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball."
        "FIRMWARE_MAX_TOTAL_BYTES",
        10,
    )

    with pytest.raises(RuntimeError, match=r"on the wire"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_rejects_oversized_cumulative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative artifact bytes exceeding the cap raises ``RuntimeError``."""
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"x" * (FIRMWARE_MAX_TOTAL_BYTES + 1))
    artifacts = BuildArtifacts(
        flash_images=[FlashArtifact(path=firmware_path, offset="0x10000")],
        idedata_bytes=b"{}",
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )

    with pytest.raises(RuntimeError, match=r"would exceed FIRMWARE_MAX_TOTAL_BYTES"):
        pack_build_artifacts("kitchen.yaml")


def test_artifacts_download_sender_discard_session_clears_inflight() -> None:
    """``discard_session`` removes the inflight slot for *dashboard_id*."""
    sender = _make_sender()
    sender._inflight["alpha"] = MagicMock()

    sender.discard_session("alpha")

    assert "alpha" not in sender._inflight


def test_pack_build_artifacts_rejects_duplicate_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two extras with the same basename trigger a ``RuntimeError``."""
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"FW")
    extra_a = tmp_path / "a" / "img.bin"
    extra_a.parent.mkdir()
    extra_a.write_bytes(b"A")
    extra_b = tmp_path / "b" / "img.bin"
    extra_b.parent.mkdir()
    extra_b.write_bytes(b"B")
    artifacts = BuildArtifacts(
        flash_images=[
            FlashArtifact(path=firmware_path, offset="0x10000"),
            FlashArtifact(path=extra_a, offset="0x1000"),
            FlashArtifact(path=extra_b, offset="0x8000"),
        ],
        idedata_bytes=b"{}",
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )

    with pytest.raises(RuntimeError, match="duplicate flash image basename"):
        pack_build_artifacts("kitchen.yaml")


def test_unpack_artifacts_response_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pack → unpack returns the same flash bytes + rewrites idedata paths to basenames."""
    artifacts = _make_build_artifacts(
        tmp_path,
        extra_offsets=[("bootloader.bin", "0x1000"), ("ota_data_initial.bin", "0xe000")],
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball.load_build_artifacts",
        lambda _config: artifacts,
    )

    packed = pack_build_artifacts("kitchen.yaml")
    response = unpack_artifacts_response(
        DownloadArtifactsResult(tarball=packed.tarball, firmware_offset="0x10000"),
        job_id="remote-1",
    )

    assert response["job_id"] == "remote-1"
    image_names = [image["name"] for image in response["images"]]
    assert image_names == ["firmware.bin", "bootloader.bin", "ota_data_initial.bin"]
    # Firmware offset comes from the start frame (packed result), not idedata.
    assert response["images"][0]["offset"] == "0x10000"
    assert response["images"][1]["offset"] == "0x1000"
    # idedata.extra.flash_images[].path rewritten to basenames.
    rewritten_paths = [entry["path"] for entry in response["idedata"]["extra"]["flash_images"]]
    assert rewritten_paths == ["bootloader.bin", "ota_data_initial.bin"]
    assert response["total_bytes"] == sum(image["size"] for image in response["images"])


def test_unpack_artifacts_response_missing_idedata_raises() -> None:
    """A tarball without ``idedata.json`` raises :class:`UnpackArtifactsError`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="firmware.bin")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"FIRM"))

    with pytest.raises(UnpackArtifactsError, match=r"missing idedata\.json"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_missing_firmware_raises() -> None:
    """A tarball without ``firmware.bin`` raises :class:`UnpackArtifactsError`."""
    idedata_bytes = json.dumps({"extra": {"flash_images": []}}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(idedata_bytes)
        tar.addfile(info, io.BytesIO(idedata_bytes))

    with pytest.raises(UnpackArtifactsError, match=r"missing firmware\.bin"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_unreferenced_file_raises() -> None:
    """An extra file in the tarball not referenced by idedata raises."""
    idedata_bytes = json.dumps({"extra": {"flash_images": []}}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))
        stray_info = tarfile.TarInfo(name="stray.bin")
        stray_info.size = 1
        tar.addfile(stray_info, io.BytesIO(b"X"))

    with pytest.raises(UnpackArtifactsError, match="unexpected files not referenced"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_invalid_idedata_json_raises() -> None:
    """Malformed JSON in idedata.json raises :class:`UnpackArtifactsError`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="idedata.json")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"{bad"))

    with pytest.raises(UnpackArtifactsError, match="not valid JSON"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_non_dict_idedata_raises() -> None:
    """idedata.json that parses to a non-object raises :class:`UnpackArtifactsError`."""
    payload = b'["not", "an", "object"]'  # valid JSON, parses to list
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(UnpackArtifactsError, match="not a JSON object"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_directory_entry_raises() -> None:
    """A directory entry in the tarball (wire-format drift) raises ``UnpackArtifactsError``."""
    idedata_bytes = json.dumps({"extra": {"flash_images": []}}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        # Stray directory entry — the receiver-side packer is
        # flat by design, so a directory means a corrupted /
        # version-skewed peer.
        dir_info = tarfile.TarInfo(name="some_dir/")
        dir_info.type = tarfile.DIRTYPE
        tar.addfile(dir_info)

    with pytest.raises(UnpackArtifactsError, match="non-file tarball entry"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x0"),
            job_id="j",
        )


def test_unpack_artifacts_response_missing_flash_image_from_extras_raises() -> None:
    """An extra-flash-image entry whose tarball member is missing raises."""
    idedata_bytes = json.dumps(
        {"extra": {"flash_images": [{"path": "/build/bootloader.bin", "offset": "0x1000"}]}}
    ).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))
        # No bootloader.bin in the tarball even though idedata
        # declares it.

    with pytest.raises(UnpackArtifactsError, match=r"missing flash image 'bootloader\.bin'"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x10000"),
            job_id="j",
        )


def test_unpack_artifacts_response_non_dict_flash_image_entry_raises() -> None:
    """A non-dict ``extra.flash_images`` entry raises :class:`UnpackArtifactsError`."""
    idedata_bytes = json.dumps(
        {"extra": {"flash_images": ["not-a-dict"]}}  # malformed entry
    ).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))

    with pytest.raises(UnpackArtifactsError, match="entry is not an object"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x10000"),
            job_id="j",
        )


def test_unpack_artifacts_response_flash_image_entry_missing_fields_raises() -> None:
    """An ``extra.flash_images`` entry without path/offset raises."""
    idedata_bytes = json.dumps(
        {"extra": {"flash_images": [{"path": "/build/bootloader.bin"}]}}  # no offset
    ).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))

    with pytest.raises(UnpackArtifactsError, match="missing path/offset"):
        unpack_artifacts_response(
            DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x10000"),
            job_id="j",
        )


def test_unpack_artifacts_response_handles_non_dict_extra() -> None:
    """An ``extra`` field that isn't a dict yields no extras (treated as empty)."""
    idedata_bytes = json.dumps({"extra": None}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))

    response = unpack_artifacts_response(
        DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x10000"),
        job_id="j",
    )
    assert [image["name"] for image in response["images"]] == ["firmware.bin"]


# ---------------------------------------------------------------------------
# load_build_artifacts — defensive idedata shape check
# ---------------------------------------------------------------------------


def test_load_build_artifacts_rejects_non_dict_idedata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt-but-parseable idedata.json (``null`` / list) raises :class:`ValueError`.

    Pins the defensive check that surfaces "esphome wrote a
    weird idedata.json" as a clean error reachable through the
    receiver-side packer's ``except Exception`` arm — without
    it, ``idedata.get("extra", {})`` would blow up with
    ``AttributeError`` and bubble as an opaque traceback rather
    than a structured ``pack_failed`` reject.
    """
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"FW")
    idedata_path = tmp_path / "idedata.json"
    idedata_path.write_bytes(b"null")  # parses to None, not a dict

    fake_storage = MagicMock()
    fake_storage.firmware_bin_path = firmware_path
    fake_storage.target_platform = "esp32"
    fake_storage.name = "kitchen"

    monkeypatch.setattr(
        "esphome_device_builder.helpers.build_artifacts.StorageJSON.load",
        staticmethod(lambda _path: fake_storage),
    )
    monkeypatch.setattr(
        "esphome_device_builder.helpers.build_artifacts.resolve_idedata_path",
        lambda _configuration, *, name: idedata_path,
    )

    with pytest.raises(ValueError, match="not a JSON object"):
        load_build_artifacts("kitchen.yaml")


# ---------------------------------------------------------------------------
# extract_firmware_bin — runner-side single-image extractor (7a-3)
# ---------------------------------------------------------------------------


def _build_minimal_tarball(members: dict[str, bytes]) -> bytes:
    """Build a gzipped tarball with *members* (basename → bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_extract_firmware_bin_returns_firmware_bytes() -> None:
    """The happy path: a tarball with ``firmware.bin`` returns its payload."""
    expected = b"\xe9\x08\x02\x20RUNTIME-FIRMWARE-BYTES"
    tarball = _build_minimal_tarball(
        {"idedata.json": b"{}", "firmware.bin": expected, "extra.bin": b"x"},
    )

    assert extract_firmware_bin(tarball) == expected


def test_extract_firmware_bin_raises_when_firmware_missing() -> None:
    """No ``firmware.bin`` in the tarball → ``UnpackArtifactsError``."""
    tarball = _build_minimal_tarball(
        {"idedata.json": b"{}", "bootloader.bin": b"boot"},
    )

    with pytest.raises(UnpackArtifactsError, match=r"firmware\.bin missing"):
        extract_firmware_bin(tarball)


def test_extract_firmware_bin_raises_when_firmware_is_a_directory() -> None:
    """
    A directory entry named ``firmware.bin`` surfaces as ``UnpackArtifactsError``.

    Defensive — the receiver-side packer never writes a
    directory, so this is a wire-shape-drift / hostile-peer
    case. ``isfile()`` rejects non-regular members before we
    read any bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="firmware.bin")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    tarball = buf.getvalue()

    with pytest.raises(UnpackArtifactsError, match="not a regular file"):
        extract_firmware_bin(tarball)


def test_extract_firmware_bin_raises_when_firmware_is_a_symlink() -> None:
    """
    A symlink entry named ``firmware.bin`` surfaces as ``UnpackArtifactsError``.

    Defence against a hostile peer: ``tarfile.extractfile()``
    follows symlinks transparently and returns a readable
    stream pointing at whatever the link target resolves to
    on the receiver's filesystem. An ``is None`` guard alone
    would let the bytes through; the explicit
    ``member.isfile()`` check rejects every non-regular type
    before the read.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="firmware.bin")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../etc/passwd"
        tar.addfile(info)
    tarball = buf.getvalue()

    with pytest.raises(UnpackArtifactsError, match="not a regular file"):
        extract_firmware_bin(tarball)


def test_extract_firmware_bin_raises_on_malformed_tarball() -> None:
    """A non-gzipped / non-tar payload surfaces as ``UnpackArtifactsError``."""
    with pytest.raises(UnpackArtifactsError, match="malformed tarball"):
        extract_firmware_bin(b"this is not a gzipped tarball")


def test_extract_firmware_bin_rejects_oversized_member() -> None:
    """
    A tarball declaring a firmware.bin larger than the cap fails fast.

    Decompression-bomb defence: gzip can shrink huge zero-
    filled / sparse data to a tiny on-the-wire payload. The
    header-size gate trips before ``extractfile`` reads a
    single byte.
    """
    # Build a tarball whose header declares a huge size but
    # whose actual payload is tiny — mimic the
    # "decompression-bomb" shape: a TarInfo with an inflated
    # ``size`` field followed by the matching payload.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        oversized_payload = b"\x00" * (FIRMWARE_MAX_TOTAL_BYTES + 1)
        info = tarfile.TarInfo(name="firmware.bin")
        info.size = len(oversized_payload)
        tar.addfile(info, io.BytesIO(oversized_payload))
    tarball = buf.getvalue()

    with pytest.raises(UnpackArtifactsError, match="exceeding FIRMWARE_MAX_TOTAL_BYTES"):
        extract_firmware_bin(tarball)


# ---------------------------------------------------------------------------
# read_artifacts_tarball — cumulative size cap (decompression-bomb defence)
# ---------------------------------------------------------------------------


def test_read_artifacts_tarball_rejects_cumulative_size_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cumulative sum across members trips the cap even if every individual member fits.

    The packer enforces the same per-call ceiling on the way
    out, so a well-formed tarball never exceeds the cap. A
    peer-controlled / malformed stream that declares N
    just-under-cap members would still blow up the offloader
    without this gate.
    """
    # Patch the cap to a tiny value so the test stays cheap.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball."
        "FIRMWARE_MAX_TOTAL_BYTES",
        128,
    )
    members = {
        "idedata.json": b'{"extra": {}}',
        "firmware.bin": b"x" * 64,
        "bootloader.bin": b"x" * 64,
    }
    tarball = _build_minimal_tarball(members)

    with pytest.raises(UnpackArtifactsError, match="cumulative size"):
        read_artifacts_tarball(tarball)


def test_read_artifacts_tarball_surfaces_malformed_tarball_as_unpack_error() -> None:
    """A corrupt gzip / tar header → ``UnpackArtifactsError`` (not a tarfile traceback)."""
    with pytest.raises(UnpackArtifactsError, match="is malformed"):
        read_artifacts_tarball(b"definitely not a tarball")
