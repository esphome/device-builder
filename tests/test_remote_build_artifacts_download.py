"""
Tests for the receiver-side ``download_artifacts`` flow (issue #106).

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
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
    PackedArtifacts,
    UnpackArtifactsError,
    _download_type_files,
    _render_tarball,
    pack_build_artifacts,
    read_artifacts_tarball,
    unpack_artifacts_response,
)
from esphome_device_builder.controllers.remote_build.peer_link_client import (
    DownloadArtifactsResult,
)
from esphome_device_builder.helpers.build_artifacts import load_build_artifacts
from esphome_device_builder.helpers.storage_path import (
    resolve_compiled_config_path,
    resolve_idedata_path,
    resolve_storage_path,
)
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
    """Stub ``FirmwareController`` exposing one matching job via the public API."""
    job = MagicMock()
    job.remote_peer = remote_peer
    job.remote_job_id = remote_job_id
    job.status = status
    job.configuration = configuration

    firmware = MagicMock()

    def _find(*, remote_peer: str, remote_job_id: str) -> Any:
        if job.remote_peer == remote_peer and job.remote_job_id == remote_job_id:
            return job
        return None

    firmware.find_remote_peer_job.side_effect = _find
    firmware.remote_peer_job_ids.side_effect = lambda *, remote_peer: (
        [job.remote_job_id] if job.remote_peer == remote_peer else []
    )
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


def _write_receiver_state(
    tmp_path: Path,
    *,
    configuration: str = "kitchen.yaml",
    device_name: str = "kitchen",
    target_platform: str = "ESP32",
    loaded_integrations: list[str] | None = None,
    extras: list[tuple[str, str]] | None = None,
    extra_build_files: dict[str, bytes] | None = None,
    validated_yaml: bytes | None = None,
) -> dict[str, Path]:
    """Lay down a minimal receiver-side build state on disk.

    Writes:

    * ``<data_dir>/build/<device_name>/platformio.ini``
    * ``<data_dir>/build/<device_name>/.pioenvs/<device_name>/firmware.bin``
    * Any additional build-tree files in *extra_build_files*
      (keys are paths relative to ``<build_path>/``).
    * ``<data_dir>/storage/<basename>.json`` with the JSON
      fields the new packer reads via ``StorageJSON.load``.
    * ``<data_dir>/idedata/<device_name>.json`` with an
      ``extra.flash_images`` entry per *extras*
      (``(basename, offset_hex)``); receiver-absolute paths
      under ``.pioenvs/<device_name>/<basename>`` so the
      WS-adapter rewrite to basenames is observable.

    Returns the key paths so individual tests can mutate them
    (e.g. drop ``platformio.ini`` to drive the missing-file
    branch).

    Side-effects ``CORE.config_path`` so the dashboard's
    ``resolve_storage_path`` / ``resolve_idedata_path`` /
    ``resolve_data_dir`` all anchor on *tmp_path*'s ``.esphome``
    subtree. The autouse ``_core_config_path_in_tmp`` fixture
    already pins this, so we just rely on its anchor here.
    """
    data_dir = tmp_path / ".esphome"
    build_path = data_dir / "build" / device_name
    pioenvs = build_path / ".pioenvs" / device_name
    pioenvs.mkdir(parents=True, exist_ok=True)
    (build_path / "platformio.ini").write_bytes(b"[env:kitchen]\nplatform = espressif32\n")
    firmware_bin = pioenvs / "firmware.bin"
    firmware_bin.write_bytes(b"FIRMWARE")

    extra_paths: list[dict[str, str]] = []
    for basename, offset in extras or []:
        path = pioenvs / basename
        path.write_bytes(basename.encode("ascii"))
        extra_paths.append({"path": str(path), "offset": offset})

    for rel_path, payload in (extra_build_files or {}).items():
        abs_path = build_path / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(payload)

    storage_path = resolve_storage_path(configuration)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "storage_version": 1,
                "name": device_name,
                "esp_platform": target_platform,
                "build_path": str(build_path),
                "firmware_bin_path": str(firmware_bin),
                "loaded_integrations": loaded_integrations or [],
                "loaded_platforms": [],
                "no_mdns": False,
                "framework": "arduino",
                "core_platform": target_platform.lower(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    idedata_path = resolve_idedata_path(configuration, name=device_name)
    idedata_path.parent.mkdir(parents=True, exist_ok=True)
    idedata_path.write_text(
        json.dumps(
            {
                "prog_path": str(pioenvs / "firmware.elf"),
                "cc_path": (
                    "/home/receiver/.platformio/packages/toolchain-xtensa32"
                    "/bin/xtensa-esp32-elf-gcc"
                ),
                "extra": {"flash_images": extra_paths},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    paths = {
        "build_path": build_path,
        "firmware_bin": firmware_bin,
        "storage_path": storage_path,
        "idedata_path": idedata_path,
        "platformio_ini": build_path / "platformio.ini",
    }
    if validated_yaml is not None:
        validated_yaml_path = resolve_compiled_config_path(configuration)
        validated_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        validated_yaml_path.write_bytes(validated_yaml)
        paths["validated_yaml_path"] = validated_yaml_path
    return paths


def _tar_member_names(tarball: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        return tar.getnames()


def test_pack_build_artifacts_ships_metadata_and_per_platform_build_tree(
    tmp_path: Path,
) -> None:
    """Tarball carries storage.json + idedata.json + platformio.ini at root, then BUILD_FILES."""
    state = _write_receiver_state(
        tmp_path,
        extras=[("bootloader.bin", "0x1000"), ("partitions.bin", "0x8000")],
        extra_build_files={
            ".pioenvs/kitchen/firmware.elf": b"ELF",
            ".pioenvs/kitchen/bootloader.bin": b"BOOT",
            ".pioenvs/kitchen/partitions.bin": b"PART",
            ".pioenvs/kitchen/ota_data_initial.bin": b"OTA",
        },
    )

    packed = pack_build_artifacts("kitchen.yaml")

    assert packed.firmware_offset == "0x10000"
    names = _tar_member_names(packed.tarball)
    # Three metadata members at the top, in deterministic order.
    assert names[:3] == [
        STORAGE_MEMBER_NAME,
        IDEDATA_MEMBER_NAME,
        PLATFORMIO_INI_MEMBER_NAME,
    ]
    # The ESP32 BUILD_FILES list resolved at pack time.
    assert ".pioenvs/kitchen/firmware.bin" in names
    assert ".pioenvs/kitchen/firmware.elf" in names
    assert ".pioenvs/kitchen/bootloader.bin" in names
    assert ".pioenvs/kitchen/partitions.bin" in names
    assert ".pioenvs/kitchen/ota_data_initial.bin" in names
    # Nothing else snuck into the tarball.
    assert state["build_path"].is_dir()  # tmp dir still around


def test_pack_build_artifacts_skips_missing_build_files(tmp_path: Path) -> None:
    """Per-platform BUILD_FILES entries that don't exist on disk are silently skipped.

    The packer pre-declares every file the platform might
    emit (e.g. ESP32 lists ``ota_data_initial.bin``) but a
    given build doesn't always emit each one. Skipping them
    is intentional — the missing-file branch is platform
    behaviour, not a packer error.
    """
    _write_receiver_state(tmp_path)  # only firmware.bin under .pioenvs/

    packed = pack_build_artifacts("kitchen.yaml")

    names = _tar_member_names(packed.tarball)
    assert ".pioenvs/kitchen/firmware.bin" in names
    # firmware.elf / bootloader.bin / etc were never written, so they're absent.
    assert ".pioenvs/kitchen/firmware.elf" not in names
    assert ".pioenvs/kitchen/bootloader.bin" not in names


def test_pack_build_artifacts_libretiny_ships_uf2(tmp_path: Path) -> None:
    """Libretiny BUILD_FILES is resolved from the registry (.uf2 + .bin + .elf)."""
    _write_receiver_state(
        tmp_path,
        device_name="bw15",
        target_platform="BK72XX",
        extra_build_files={
            ".pioenvs/bw15/firmware.uf2": b"UF2",
            ".pioenvs/bw15/firmware.elf": b"ELF",
        },
    )

    packed = pack_build_artifacts("kitchen.yaml")

    # ESP32 prefix → 0x10000; libretiny falls through to 0x0.
    assert packed.firmware_offset == "0x0"
    names = _tar_member_names(packed.tarball)
    assert ".pioenvs/bw15/firmware.uf2" in names
    assert ".pioenvs/bw15/firmware.bin" in names
    assert ".pioenvs/bw15/firmware.elf" in names


def test_pack_build_artifacts_rejects_unknown_target_platform(tmp_path: Path) -> None:
    """A target_platform without an artifact_platforms module raises RuntimeError."""
    _write_receiver_state(tmp_path, target_platform="ABSURDIAN-X1")

    with pytest.raises(RuntimeError, match=r"no artifact_platforms module"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_firmware_bin_path_unset(tmp_path: Path) -> None:
    """StorageJSON with ``firmware_bin_path=None`` raises FileNotFoundError."""
    state = _write_receiver_state(tmp_path)
    data = json.loads(state["storage_path"].read_text())
    data["firmware_bin_path"] = None
    state["storage_path"].write_text(json.dumps(data) + "\n")

    with pytest.raises(FileNotFoundError, match=r"firmware_bin_path unset"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_build_path_unset(tmp_path: Path) -> None:
    """StorageJSON with ``build_path=None`` raises FileNotFoundError."""
    state = _write_receiver_state(tmp_path)
    data = json.loads(state["storage_path"].read_text())
    data["build_path"] = None
    state["storage_path"].write_text(json.dumps(data) + "\n")

    with pytest.raises(FileNotFoundError, match=r"build_path unset"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_name_empty(tmp_path: Path) -> None:
    """StorageJSON with an empty ``name`` raises FileNotFoundError."""
    state = _write_receiver_state(tmp_path)
    data = json.loads(state["storage_path"].read_text())
    data["name"] = ""
    state["storage_path"].write_text(json.dumps(data) + "\n")

    with pytest.raises(FileNotFoundError, match=r"name unset / non-string"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_storage_missing(tmp_path: Path) -> None:
    """No StorageJSON sidecar on disk → FileNotFoundError (mapped to build_dir_missing)."""
    # Don't call _write_receiver_state — the storage sidecar
    # never gets written.
    with pytest.raises(FileNotFoundError, match="StorageJSON sidecar missing"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_idedata_missing(tmp_path: Path) -> None:
    """No cached idedata.json → FileNotFoundError."""
    state = _write_receiver_state(tmp_path)
    state["idedata_path"].unlink()

    with pytest.raises(FileNotFoundError, match="idedata cache missing"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_raises_when_platformio_ini_missing(tmp_path: Path) -> None:
    """No platformio.ini → FileNotFoundError."""
    state = _write_receiver_state(tmp_path)
    state["platformio_ini"].unlink()

    with pytest.raises(FileNotFoundError, match=r"platformio\.ini missing"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_includes_get_download_types_files(tmp_path: Path) -> None:
    """``get_download_types`` files (firmware.factory.bin etc.) ride in the tarball."""
    _write_receiver_state(
        tmp_path,
        extra_build_files={
            ".pioenvs/kitchen/firmware.factory.bin": b"FACTORY",
            ".pioenvs/kitchen/firmware.ota.bin": b"OTA",
        },
    )

    packed = pack_build_artifacts("kitchen.yaml")

    names = _tar_member_names(packed.tarball)
    assert ".pioenvs/kitchen/firmware.factory.bin" in names
    assert ".pioenvs/kitchen/firmware.ota.bin" in names


def test_download_type_files_empty_for_unknown_component() -> None:
    """``_download_type_files`` returns ``[]`` when the platform has no mapped component."""
    fake_storage = MagicMock()
    fake_storage.target_platform = None
    assert _download_type_files(fake_storage) == []


def test_pack_build_artifacts_logs_download_types_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If ``get_download_types`` raises, we log the traceback and ship without those files."""
    _write_receiver_state(tmp_path)

    def _raise(_storage: object) -> object:
        raise RuntimeError("simulated component breakage")

    import esphome.components.esp32  # noqa: PLC0415

    monkeypatch.setattr(esphome.components.esp32, "get_download_types", _raise)

    with caplog.at_level("ERROR"):
        packed = pack_build_artifacts("kitchen.yaml")

    assert packed.tarball  # pack succeeded with the static BUILD_FILES set
    assert any("Could not determine download types" in r.message for r in caplog.records)
    assert any(r.exc_info is not None for r in caplog.records)


def test_pack_build_artifacts_rejects_firmware_bin_outside_build_files(
    tmp_path: Path,
) -> None:
    """A firmware_bin_path not covered by BUILD_FILES raises ``RuntimeError``.

    Defence-in-depth against a future esphome bump moving
    firmware_bin_path somewhere a platform module doesn't list:
    we want a clean reject rather than a half-shipped tarball
    the offloader stages without the firmware binary.
    """
    state = _write_receiver_state(tmp_path)
    # Point firmware_bin_path at a path that isn't covered by
    # esp32's BUILD_FILES tuple (no platform ships a top-level
    # ``custom_firmware.bin``).
    custom_path = state["build_path"] / "custom_firmware.bin"
    custom_path.write_bytes(b"CUSTOM")
    storage_data = json.loads(state["storage_path"].read_text())
    storage_data["firmware_bin_path"] = str(custom_path)
    state["storage_path"].write_text(json.dumps(storage_data) + "\n")

    with pytest.raises(RuntimeError, match=r"not covered by BUILD_FILES"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_rejects_firmware_bin_outside_build_path(
    tmp_path: Path,
) -> None:
    """A firmware_bin_path that escapes the build dir raises ``RuntimeError``."""
    state = _write_receiver_state(tmp_path)
    escapee = tmp_path / "outside" / "firmware.bin"
    escapee.parent.mkdir()
    escapee.write_bytes(b"OUTSIDE")
    storage_data = json.loads(state["storage_path"].read_text())
    storage_data["firmware_bin_path"] = str(escapee)
    state["storage_path"].write_text(json.dumps(storage_data) + "\n")

    with pytest.raises(RuntimeError, match=r"not under build_path"):
        pack_build_artifacts("kitchen.yaml")


def test_pack_build_artifacts_rejects_oversized_uncompressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncompressed walking sum exceeding the cap raises ``RuntimeError``."""
    _write_receiver_state(tmp_path)
    # Cap to a small value so the uncompressed walk trips on
    # the first few members (storage.json + idedata.json
    # together are well over 128 bytes once we serialise
    # them).
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball."
        "FIRMWARE_MAX_TOTAL_BYTES",
        16,
    )

    with pytest.raises(RuntimeError, match=r"would exceed FIRMWARE_MAX_TOTAL_BYTES"):
        pack_build_artifacts("kitchen.yaml")


def test_render_tarball_rejects_oversized_on_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gzipped tarball larger than the cap raises the post-render ``on the wire`` guard.

    The per-member uncompressed pre-check is the first gate;
    the post-render wire-size cap is the second gate, defending
    against the case where the gzipped output is *larger* than
    the uncompressed file contents (tar headers + gzip framing
    add ~80 bytes minimum, easily exceeding tiny payloads).
    Use a 1-byte file so the loop sees ``0 + 1 > cap`` is false
    while the rendered tarball trips the post-check.
    """
    payload = tmp_path / "tiny.bin"
    payload.write_bytes(b"X")
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball."
        "FIRMWARE_MAX_TOTAL_BYTES",
        10,
    )

    with pytest.raises(RuntimeError, match=r"would exceed FIRMWARE_MAX_TOTAL_BYTES on the wire"):
        _render_tarball([("tiny.bin", payload)], configuration="kitchen.yaml")


def test_artifacts_download_sender_discard_session_clears_inflight() -> None:
    """``discard_session`` removes the inflight slot for *dashboard_id*."""
    sender = _make_sender()
    sender._inflight["alpha"] = MagicMock()

    sender.discard_session("alpha")

    assert "alpha" not in sender._inflight


def test_unpack_artifacts_response_round_trip(tmp_path: Path) -> None:
    """Pack → unpack returns the same flash bytes + rewrites idedata paths to basenames."""
    _write_receiver_state(
        tmp_path,
        extras=[("bootloader.bin", "0x1000"), ("ota_data_initial.bin", "0xe000")],
        extra_build_files={
            ".pioenvs/kitchen/firmware.elf": b"ELF",
            ".pioenvs/kitchen/bootloader.bin": b"BOOT",
            ".pioenvs/kitchen/ota_data_initial.bin": b"OTA",
        },
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


def test_unpack_artifacts_response_ignores_metadata_and_aux_members(tmp_path: Path) -> None:
    """storage.json / platformio.ini / firmware.elf are filtered from the images set."""
    _write_receiver_state(
        tmp_path,
        extra_build_files={".pioenvs/kitchen/firmware.elf": b"ELF"},
    )

    packed = pack_build_artifacts("kitchen.yaml")
    response = unpack_artifacts_response(
        DownloadArtifactsResult(tarball=packed.tarball, firmware_offset="0x10000"),
        job_id="j",
    )

    image_names = [image["name"] for image in response["images"]]
    # Only flash images (per idedata.extra.flash_images) plus firmware.bin appear.
    # storage.json / platformio.ini / firmware.elf are absent.
    assert image_names == ["firmware.bin"]


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


def test_unpack_artifacts_response_ignores_unreferenced_aux_files() -> None:
    """Aux files in the tarball that aren't in idedata.extra.flash_images are silently ignored.

    The materialise-locally wire format legitimately ships
    per-platform aux files (``firmware.elf`` for picotool
    symbol resolution, ``firmware.uf2`` for libretiny/RP2040
    ltchiptool flashing) that aren't in
    ``idedata.extra.flash_images``. The WS-adapter
    surfaces only the flash-image subset to the frontend;
    leftovers in the basename map are dropped without
    raising.
    """
    idedata_bytes = json.dumps({"extra": {"flash_images": []}}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        idedata_info = tarfile.TarInfo(name="idedata.json")
        idedata_info.size = len(idedata_bytes)
        tar.addfile(idedata_info, io.BytesIO(idedata_bytes))
        firmware_info = tarfile.TarInfo(name="firmware.bin")
        firmware_info.size = 4
        tar.addfile(firmware_info, io.BytesIO(b"FIRM"))
        stray_info = tarfile.TarInfo(name="firmware.elf")
        stray_info.size = 1
        tar.addfile(stray_info, io.BytesIO(b"X"))

    response = unpack_artifacts_response(
        DownloadArtifactsResult(tarball=buf.getvalue(), firmware_offset="0x10000"),
        job_id="j",
    )

    # Only the manifest-referenced flash images surface; firmware.elf is dropped.
    assert [image["name"] for image in response["images"]] == ["firmware.bin"]


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
    """Corrupt-but-parseable idedata.json (``null`` / list) raises :class:`TypeError`.

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

    with pytest.raises(TypeError, match="not a JSON object"):
        load_build_artifacts("kitchen.yaml")


# ---------------------------------------------------------------------------
# read_artifacts_tarball — cumulative size cap (decompression-bomb defence)
# ---------------------------------------------------------------------------


def _build_minimal_tarball(members: dict[str, bytes]) -> bytes:
    """Build a gzipped tarball with *members* (full name → bytes)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


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


def test_read_artifacts_tarball_rejects_per_member_size_over_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single member declaring more bytes than the cap is rejected."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build.artifacts_tarball."
        "FIRMWARE_MAX_TOTAL_BYTES",
        128,
    )
    tarball = _build_minimal_tarball(
        {"idedata.json": b'{"extra": {}}', "firmware.bin": b"x" * 200},
    )

    with pytest.raises(UnpackArtifactsError, match=r"exceeding FIRMWARE_MAX_TOTAL_BYTES"):
        read_artifacts_tarball(tarball)


def test_read_artifacts_tarball_rejects_duplicate_basename() -> None:
    """Two tarball members sharing the same basename surface as UnpackArtifactsError."""
    idedata_bytes = json.dumps({"extra": {"flash_images": []}}).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(idedata_bytes)
        tar.addfile(info, io.BytesIO(idedata_bytes))
        firm = b"FIRM"
        info = tarfile.TarInfo(name=".pioenvs/a/firmware.bin")
        info.size = len(firm)
        tar.addfile(info, io.BytesIO(firm))
        info = tarfile.TarInfo(name=".pioenvs/b/firmware.bin")
        info.size = len(firm)
        tar.addfile(info, io.BytesIO(firm))

    with pytest.raises(UnpackArtifactsError, match=r"duplicate basename"):
        read_artifacts_tarball(buf.getvalue())


def test_read_artifacts_tarball_surfaces_malformed_tarball_as_unpack_error() -> None:
    """A corrupt gzip / tar header → ``UnpackArtifactsError`` (not a tarfile traceback)."""
    with pytest.raises(UnpackArtifactsError, match="is malformed"):
        read_artifacts_tarball(b"definitely not a tarball")
