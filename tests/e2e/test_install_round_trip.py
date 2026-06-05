"""
End-to-end: transparent install — submit_job + fan-out + download_artifacts on one session.

#568 wired the offloader-side ``firmware/install`` through
:func:`helpers.build_scheduler.pick_build_path` and extended
:func:`remote_runner.run_remote_job` to run both sides of a
transparent install on one paired session:

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
What transparent install introduced is the **combination**: the same paired
Noise session has to carry submit_job, then the lifecycle
fan-out, then download_artifacts, all keyed on the same
``(offloader dashboard_id, offloader-side job_id)`` correlation
through to the receiver's
``ArtifactsDownloadSender._find_remote_job`` linear scan over
``firmware.state.jobs``. A regression that breaks the correlation
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
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    BUILD_INFO_MEMBER_NAME,
    IDEDATA_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.build_scheduler import (
    BuildPath,
    pick_build_path,
)
from esphome_device_builder.helpers.config_hash import read_build_info_hash
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    materialise_remote_artifacts,
)
from esphome_device_builder.helpers.remote_build_layout import (
    parse_from_configuration as parse_remote_build_path,
)
from esphome_device_builder.helpers.storage_path import resolve_storage_path
from esphome_device_builder.helpers.version_compat import VersionMatchPolicy
from esphome_device_builder.models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
    QueueStatus,
)
from esphome_device_builder.models.api import ErrorCode

from .._storage_fixtures import write_storage_json
from ..conftest import capture_events
from .conftest import (
    PairedInstances,
    drive_remote_job_to_completed,
    make_real_bundle,
    wire_receiver_firmware_recorder,
)


def _write_build_artifacts_on_disk(tmp_path: Path, *, configuration: str) -> dict[str, bytes]:
    """Lay down a real StorageJSON sidecar + idedata.json + per-image binaries.

    Models the on-disk layout the receiver-side compile
    subprocess produces: ``ESPHOME_DATA_DIR`` is pinned to
    ``<CORE.data_dir>/.remote_builds/<dashboard_id>/.esphome``
    so esphome writes storage / idedata / build under that
    one ``dashboard_id``-keyed directory shared across every
    device an offloader submits. Production has the real
    ``esphome compile`` invocation produce these files there;
    the e2e variant short-circuits the build.

    The autouse ``_core_config_path_in_tmp`` fixture pins
    ``CORE.config_path`` to a sentinel inside *tmp_path*, so
    ``CORE.data_dir`` resolves to ``tmp_path/.esphome`` (default
    mode). :meth:`RemoteBuildPath.data_dir` anchors on
    ``CORE.data_dir`` exactly the way the writer-side env
    override does, so passing ``Path(CORE.data_dir)`` here lands
    the test sidecars where the production read path will look
    for them.
    """
    remote_build_path = parse_remote_build_path(configuration)
    assert remote_build_path is not None, (
        f"configuration {configuration!r} must be a remote-build path; "
        "the helper is e2e-specific and not meant for bare-basename inputs"
    )
    data_dir = remote_build_path.data_dir(Path(CORE.data_dir))
    device_name = remote_build_path.device_name
    build_dir = data_dir / "build" / device_name
    pioenvs = build_dir / ".pioenvs" / device_name
    pioenvs.mkdir(parents=True, exist_ok=True)
    # platformio.ini is now part of the packer's required set.
    (build_dir / "platformio.ini").write_bytes(b"[env:e2e]\nplatform = espressif32\n")
    images: dict[str, bytes] = {
        "firmware.bin": b"firmware-bin-bytes",
        "bootloader.bin": b"bootloader-bytes",
        "partitions.bin": b"partitions-bytes",
        # Download artefacts a real esp32 compile also emits: the two
        # downloadable images plus the ELF the stack trace decoder needs.
        # They ride back through the BUILD_FILES tarball but are not flash
        # images, so they don't appear in the ``result["images"]`` set.
        "firmware.factory.bin": b"factory-bin-bytes",
        "firmware.ota.bin": b"ota-bin-bytes",
        "firmware.elf": b"elf-debug-symbols",
    }
    image_paths: dict[str, Path] = {}
    for name, payload in images.items():
        path = pioenvs / name
        path.write_bytes(payload)
        image_paths[name] = path

    # Mirrors ESPHome's post-codegen build_info.json (#654).
    (build_dir / BUILD_INFO_MEMBER_NAME).write_text(
        json.dumps({"config_hash": 0x5A94A12D}), encoding="utf-8"
    )

    # The receiver-side compile writes
    # ``<data_dir>/storage/<basename>.json`` (esphome's
    # ``storage_path()`` keys on ``CORE.config_filename`` — the
    # YAML's basename — and ``CORE.data_dir`` resolves to the
    # ``ESPHOME_DATA_DIR`` we pinned). The shared helper carries
    # the full schema; pass ``data_dir`` so the sidecar lands in
    # the per-build subtree.
    write_storage_json(
        tmp_path,
        configuration,
        data_dir=data_dir,
        build_path=build_dir,
        firmware_bin_path=image_paths["firmware.bin"],
        overrides={"target_platform": "esp32"},
    )

    stem = Path(configuration).stem
    idedata_dir = data_dir / "idedata"
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


async def test_cold_connect_offloader_observes_initial_queue_status_then_picks_remote(
    paired_instances: PairedInstances,
) -> None:
    """Cold-connect offloader gets an idle entry via the receiver's auto-push.

    The bug this test pins: previously the receiver only
    broadcast ``queue_status`` on its own firmware queue
    transitions (``_on_firmware_queue_transition``). A
    cold-connected offloader that paired before the receiver
    built anything never observed an idle entry, and the
    install scheduler's ``pick_build_path`` requires an entry
    in ``_peer_queue_status`` to consider a pairing eligible —
    so ``firmware/install`` silently fell back to LOCAL on
    every paired receiver. The fix has the receiver send a
    one-shot ``queue_status`` to a freshly-registered session
    inside :meth:`register_peer_link_session`.

    The previous shape of this test pre-seeded
    ``offloader.state.peer_queue_status[pin]`` to model the
    transition-driven path, masking the cold-connect gap. The
    new test asserts the offloader observes the idle entry by
    waiting on its ``OFFLOADER_QUEUE_STATUS_CHANGED`` event
    (fired by the receive loop after parsing the receiver's
    one-shot frame) — no manual seeding. A regression that
    removes the initial push or otherwise breaks the cold-
    connect path will surface here.

    Once the cache entry has landed,
    :func:`build_scheduler_snapshot` + :func:`pick_build_path`
    resolve to ``BuildPath.REMOTE`` against the same pin.
    """
    queue_status_landed = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_QUEUE_STATUS_CHANGED
    )
    await paired_instances.wait_until_session_opened()
    # Wait for the offloader to observe the receiver's initial
    # ``queue_status`` push. Cold-connect contract:
    # the offloader's ``_peer_queue_status`` must populate from
    # the wire without any local-side seeding. The push is a
    # fire-and-forget background task scheduled at session
    # registration, so under ``-n auto`` CI contention it can
    # lag a couple event-loop turns behind session-open; a 2s
    # ceiling tipped over on a loaded runner. The global
    # ``--timeout=120`` still catches a truly broken push.
    await asyncio.wait_for(queue_status_landed.received.wait(), timeout=10.0)
    payload = queue_status_landed[-1]
    assert payload["pin_sha256"] == paired_instances.pin_sha256
    assert payload["idle"] is True
    assert payload["running"] is False
    assert payload["queue_depth"] == 0

    # Now that the offloader-side cache reflects the receiver's
    # signal, the scheduler routes REMOTE.
    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    decision = pick_build_path(snapshot)
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == paired_instances.pin_sha256


async def test_version_match_policy_filters_mismatched_peer_end_to_end(
    paired_instances: PairedInstances,
) -> None:
    """End-to-end policy matrix on a live paired session (ANY / RELEASE / EXACT)."""
    await paired_instances.wait_until_session_opened()
    pin = paired_instances.pin_sha256
    pairing = paired_instances.offloader.state.pairings[pin]

    # Pin both ends explicitly so the gate sees the mismatch
    # regardless of the bundled ``esphome.const.__version__``.
    pairing.esphome_version = "2026.6.1"
    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    base = replace(snapshot, offloader_esphome_version="2026.6.0")

    # ANY (default) — peer survives any drift.
    decision = pick_build_path(replace(base, version_match_policy=VersionMatchPolicy.ANY))
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == pin

    # RELEASE — patch diff OK.
    decision = pick_build_path(replace(base, version_match_policy=VersionMatchPolicy.RELEASE))
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == pin

    # EXACT — patch diff filtered, falls back to LOCAL.
    decision = pick_build_path(replace(base, version_match_policy=VersionMatchPolicy.EXACT))
    assert decision.path is BuildPath.LOCAL
    assert decision.pin_sha256 is None

    # Peer flips to matching version — EXACT picks it up.
    pairing.esphome_version = "2026.6.0"
    matched = paired_instances.offloader.build_scheduler_snapshot()
    decision = pick_build_path(
        replace(
            matched,
            offloader_esphome_version="2026.6.0",
            version_match_policy=VersionMatchPolicy.EXACT,
        )
    )
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == pin


async def test_exact_required_raises_no_compatible_peer_end_to_end(
    paired_instances: PairedInstances,
) -> None:
    """``EXACT_REQUIRED`` over a live session raises instead of LOCAL-fallback."""
    await paired_instances.wait_until_session_opened()
    pin = paired_instances.pin_sha256
    pairing = paired_instances.offloader.state.pairings[pin]
    pairing.esphome_version = "2026.6.1"
    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    snapshot = replace(
        snapshot,
        offloader_esphome_version="2026.6.0",
        version_match_policy=VersionMatchPolicy.EXACT_REQUIRED,
    )
    with pytest.raises(CommandError) as exc:
        pick_build_path(snapshot)
    assert exc.value.code is ErrorCode.NO_COMPATIBLE_PEER

    # And the same policy lets the install through when the
    # peer's version comes back into line — confirming the
    # filter is "no eligible peer" not "policy turned on".
    pairing.esphome_version = "2026.6.0"
    matched = paired_instances.offloader.build_scheduler_snapshot()
    matched = replace(
        matched,
        offloader_esphome_version="2026.6.0",
        version_match_policy=VersionMatchPolicy.EXACT_REQUIRED,
    )
    decision = pick_build_path(matched)
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == pin


async def test_remote_install_submit_then_lifecycle_then_download_on_one_session(
    paired_instances: PairedInstances,
    tmp_path: Path,
) -> None:
    """Transparent install: submit + fan-out + download on one Noise session.

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
    * ``OFFLOADER_JOB_STATE_CHANGED`` events land for the
      ``running`` then ``completed`` transitions (a leading
      ``queued`` from JOB_QUEUED may precede them; this test
      polls for the named statuses rather than asserting count).
      Both echo the offloader-supplied ``job_id`` and the live
      ``pin_sha256`` from the harness's handshake.
    * ``download_artifacts`` returns the StorageJSON +
      ``idedata`` + base64-enveloped image bytes the receiver
      packed; the artifact set survives the round-trip on the
      same Noise channel that just carried submit_job + the
      fan-out frames.
    """
    await paired_instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(paired_instances)
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
    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    bundle_bytes = make_real_bundle()
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
    running_payload = await state_changes.wait_for_status("running")
    assert running_payload["job_id"] == "off-job-1"
    assert running_payload["pin_sha256"] == paired_instances.pin_sha256

    paired_instances.receiver_bus.fire(EventType.JOB_COMPLETED, JobLifecycleData(job=receiver_job))
    completed_payload = await state_changes.wait_for_status("completed")
    assert completed_payload["job_id"] == "off-job-1"
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


async def test_remote_compile_materialises_for_local_firmware_download(
    paired_instances: PairedInstances,
    tmp_path: Path,
) -> None:
    """#624: compile remote → materialise → firmware/download reads staged bytes."""
    await paired_instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    # Fail loud if the scheduler would have picked LOCAL — silent
    # local fallback masks the whole point of this test. The
    # receiver's queue_status push can land before or after the
    # paired_instances fixture returns (event-vs-fixture race);
    # poll the cache + scheduler decision instead of waiting on
    # a one-shot event we might have missed.
    deadline = asyncio.get_event_loop().time() + 2.0
    decision = pick_build_path(paired_instances.offloader.build_scheduler_snapshot())
    while decision.path is not BuildPath.REMOTE and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
        decision = pick_build_path(paired_instances.offloader.build_scheduler_snapshot())
    assert decision.path is BuildPath.REMOTE, (
        f"scheduler picked {decision.path} — expected REMOTE for the e2e to be meaningful"
    )

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-compile-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=make_real_bundle(),
    )
    assert ack["accepted"] is True
    receiver_job = created_jobs[0]
    images = _write_build_artifacts_on_disk(tmp_path, configuration=receiver_job.configuration)

    await drive_remote_job_to_completed(paired_instances, receiver_job, state_changes)

    packed = await handle.client.download_artifacts(job_id="off-compile-1")
    build_path = await asyncio.to_thread(
        materialise_remote_artifacts, packed.tarball, "kitchen.yaml"
    )
    assert (build_path / ".pioenvs" / "kitchen" / "firmware.bin").is_file()

    def _load_staged() -> StorageJSON | None:
        return StorageJSON.load(resolve_storage_path("kitchen.yaml"))

    staged_storage = await asyncio.to_thread(_load_staged)
    assert staged_storage is not None
    assert staged_storage.firmware_bin_path is not None
    download_dir = staged_storage.firmware_bin_path.parent
    assert (download_dir / "firmware.bin").read_bytes() == images["firmware.bin"]
    assert (download_dir / "bootloader.bin").read_bytes() == images["bootloader.bin"]
    assert (download_dir / "partitions.bin").read_bytes() == images["partitions.bin"]
    # The download artefacts (factory/OTA + ELF) ride back from the remote
    # builder into the same dir ``get_binaries`` reads, so the Download picker
    # offers them with no local recompile. (get_binaries' filter + ELF-append
    # is covered against this layout in test_get_binaries.py.)
    assert (download_dir / "firmware.elf").read_bytes() == images["firmware.elf"]
    assert (download_dir / "firmware.factory.bin").read_bytes() == images["firmware.factory.bin"]
    assert (download_dir / "firmware.ota.bin").read_bytes() == images["firmware.ota.bin"]

    # #654: read_build_info_hash resolves post-materialise.
    assert (build_path / BUILD_INFO_MEMBER_NAME).is_file()
    yaml_path = Path(CORE.config_path).parent / "kitchen.yaml"
    hex_hash = await asyncio.to_thread(read_build_info_hash, yaml_path)
    assert hex_hash == "5a94a12d"


_WIN_BUILD_PATH = (
    r"C:\Users\receiver\esphome\.esphome\.remote_builds"
    r"\_pin\.esphome\build\kitchen"
)


def _rewrite_tarball_to_windows_receiver(tarball: bytes, *, posix_build: str) -> bytes:
    """Repack *tarball* with metadata paths rewritten under :data:`_WIN_BUILD_PATH`.

    Lets a POSIX test host stand in for a Windows receiver: the
    real ``pack_build_artifacts`` ran on Linux (so build-tree
    files exist), then we munge ``storage.json`` /
    ``idedata.json`` to look like a Windows receiver's wire
    shape. Build-tree arcnames stay POSIX, which is what
    production ships regardless of receiver OS.
    """
    buf_in = io.BytesIO(tarball)
    buf_out = io.BytesIO()
    with (
        tarfile.open(fileobj=buf_in, mode="r:gz") as tar_in,
        tarfile.open(fileobj=buf_out, mode="w:gz") as tar_out,
    ):
        for member in tar_in.getmembers():
            payload = tar_in.extractfile(member).read()  # type: ignore[union-attr]
            if member.name in (STORAGE_MEMBER_NAME, IDEDATA_MEMBER_NAME):
                payload = _swap_metadata_paths_to_windows(payload, posix_build=posix_build)
            info = tarfile.TarInfo(name=member.name)
            info.size = len(payload)
            tar_out.addfile(info, io.BytesIO(payload))
    return buf_out.getvalue()


def _swap_metadata_paths_to_windows(payload: bytes, *, posix_build: str) -> bytes:
    """Rewrite every absolute path field under *posix_build* into :data:`_WIN_BUILD_PATH`."""
    data = json.loads(payload)
    for field in ("build_path", "firmware_bin_path", "prog_path"):
        value = data.get(field)
        if isinstance(value, str) and value.startswith(posix_build):
            data[field] = _swap_prefix(value, posix_build, _WIN_BUILD_PATH)
    for image in data.get("extra", {}).get("flash_images", []):
        path = image.get("path")
        if isinstance(path, str) and path.startswith(posix_build):
            image["path"] = _swap_prefix(path, posix_build, _WIN_BUILD_PATH)
    return json.dumps(data).encode("utf-8")


def _swap_prefix(absolute_posix: str, posix_prefix: str, win_prefix: str) -> str:
    """Replace *posix_prefix* with *win_prefix*, converting trailing separators to backslashes."""
    suffix = absolute_posix[len(posix_prefix) :].lstrip("/").replace("/", "\\")
    return win_prefix + ("\\" + suffix if suffix else "")


async def test_windows_receiver_tarball_materialises_for_local_firmware_download(
    paired_instances: PairedInstances,
    tmp_path: Path,
) -> None:
    """Windows-receiver tarball over a real Noise session lands a readable factory binary."""
    await paired_instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-windows-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=make_real_bundle(),
    )
    assert ack["accepted"] is True
    receiver_job = created_jobs[0]
    images = _write_build_artifacts_on_disk(tmp_path, configuration=receiver_job.configuration)

    def _seed_factory_binary() -> str:
        # resolve_storage_path + StorageJSON.load + write_bytes all
        # block, so bundle them into one thread hop.
        receiver_storage = StorageJSON.load(resolve_storage_path(receiver_job.configuration))
        assert receiver_storage is not None
        factory_path = Path(receiver_storage.firmware_bin_path).parent / "firmware.factory.bin"
        factory_path.write_bytes(b"FACTORY-BIN-BYTES")
        return str(receiver_storage.build_path)

    receiver_build = await asyncio.to_thread(_seed_factory_binary)
    images["firmware.factory.bin"] = b"FACTORY-BIN-BYTES"

    await drive_remote_job_to_completed(paired_instances, receiver_job, state_changes)

    packed = await handle.client.download_artifacts(job_id="off-windows-1")
    munged = await asyncio.to_thread(
        _rewrite_tarball_to_windows_receiver, packed.tarball, posix_build=receiver_build
    )

    build_path = await asyncio.to_thread(materialise_remote_artifacts, munged, "kitchen.yaml")

    def _load_staged() -> StorageJSON | None:
        return StorageJSON.load(resolve_storage_path("kitchen.yaml"))

    staged_storage = await asyncio.to_thread(_load_staged)
    assert staged_storage is not None
    assert staged_storage.firmware_bin_path is not None
    # firmware_bin_path was a Windows absolute on the wire; the
    # offloader's staged copy must point into its own build tree.
    assert str(staged_storage.firmware_bin_path).startswith(str(build_path))
    download_dir = Path(staged_storage.firmware_bin_path).parent
    # The #945 bug: this lookup raised "Binary not found" because
    # firmware_bin_path stayed a Windows string and .parent
    # collapsed to the dashboard cwd.
    assert (download_dir / "firmware.factory.bin").read_bytes() == images["firmware.factory.bin"]
    assert (download_dir / "firmware.bin").read_bytes() == images["firmware.bin"]


def _drive_receiver_lifecycle(
    paired_instances: PairedInstances,
    job: FirmwareJob,
    *,
    terminal: EventType,
) -> None:
    """Fire one queue lifecycle (QUEUED → STARTED → terminal) on the receiver bus.

    Models the receiver-side firmware queue's three-event
    transition for a single job. ``compile_queue_status`` is
    pinned ahead of each fire so the
    :meth:`ReceiverController._on_firmware_queue_transition`
    listener captures the matching ``(idle, running, depth)``
    tuple synchronously inside the broadcast — same shape
    production's :meth:`FirmwareController._finalize_terminal`
    would land on (slot release + idle snapshot *before* the
    terminal fire reaches subscribers).

    A regression on the slot-release ordering would surface
    here as the offloader's ``_peer_queue_status`` ending up
    ``running=True`` after the terminal — the cache state the
    install scheduler rejects on the next install.
    """
    firmware = paired_instances.receiver._db.firmware
    bus = paired_instances.receiver_bus

    # JOB_QUEUED: queue_depth bumped, runner not yet picking up.
    firmware.compile_queue_status = MagicMock(
        return_value=QueueStatus(idle=False, running=False, queue_depth=1)
    )
    bus.fire(EventType.JOB_QUEUED, JobLifecycleData(job=job))

    # JOB_STARTED: runner picked up, queue_depth back to 0.
    firmware.compile_queue_status = MagicMock(
        return_value=QueueStatus(idle=False, running=True, queue_depth=0)
    )
    bus.fire(EventType.JOB_STARTED, JobLifecycleData(job=job))

    # Terminal: post-``_finalize_terminal`` state — slot
    # released, nothing queued. The fix this test pins is that
    # this snapshot is what the broadcast carries.
    firmware.compile_queue_status = MagicMock(
        return_value=QueueStatus(idle=True, running=False, queue_depth=0)
    )
    bus.fire(terminal, JobLifecycleData(job=job))


async def _wait_for_offloader_idle(
    paired_instances: PairedInstances,
    queue_status_events: Any,
    *,
    timeout: float = 2.0,
) -> None:
    """
    Block until the offloader's ``_peer_queue_status`` reports idle for this pin.

    Event-driven: the caller installs *queue_status_events*
    (a :func:`capture_events` handle on
    ``OFFLOADER_QUEUE_STATUS_CHANGED``) before driving any
    lifecycle so no broadcast races the listener-attach. Each
    iteration clears the capture's ``received`` flag *first*
    and then re-reads the cache — order matters: clearing
    after the read would discard a signal that arrived
    between the read and the clear, leaving the coroutine
    parked on a ``wait_for`` that times out even though the
    cache was already idle. The bounded deadline turns a
    regression that breaks the broadcast into a clean
    ``TimeoutError`` rather than the previous fixed-iteration
    spin that could pass under CI load if the background
    ``queue_status`` send/receive happened to take more than
    a few event-loop turns (per Copilot review on #576).
    """
    pin_sha256 = paired_instances.pin_sha256
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        # Clear before the cache read so an event landing in the
        # cache-read → clear → wait window can't be dropped on the
        # floor. Any concurrent ``bus.fire`` writes the cache AND
        # re-sets the flag, so a clear-then-read sequence either
        # observes the post-fire cache state below or wakes from
        # the wait_for on the re-set flag.
        queue_status_events.received.clear()
        entry = paired_instances.offloader.state.peer_queue_status.get(pin_sha256)
        if entry is not None and entry["idle"]:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            row = paired_instances.offloader.state.peer_queue_status.get(pin_sha256)
            msg = f"offloader's peer_queue_status never reached idle within {timeout}s: {row!r}"
            raise TimeoutError(msg)
        await asyncio.wait_for(queue_status_events.received.wait(), timeout=remaining)


async def test_back_to_back_successful_jobs_keep_scheduler_routing_remote(
    paired_instances: PairedInstances,
) -> None:
    """Two completed remote jobs in a row both leave the cache idle.

    Pins the second-install bug user-reported after #575 landed:
    the first install routed REMOTE, the second silently fell
    back to LOCAL. Root cause was the firmware controller
    firing the terminal event *before* releasing the runner
    slot — the receiver's ``queue_status`` broadcast captured a
    stale ``running=True`` snapshot at JOB_COMPLETED time,
    froze the offloader's ``_peer_queue_status`` there, and
    :func:`pick_build_path`'s idle gate rejected the pairing
    on every subsequent install.

    The fix routes every terminal fire through
    :meth:`FirmwareController._finalize_terminal`, which
    releases the slot before firing. This test simulates the
    production fire order via the test helper and asserts both
    cycles round-trip through to idle on the offloader-side
    cache.
    """
    # Install the capture before opening the session — the
    # cold-connect ``queue_status`` push fires inside
    # ``wait_until_session_opened`` and the helper needs to see
    # it without racing.
    queue_status_events = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_QUEUE_STATUS_CHANGED
    )
    await paired_instances.wait_until_session_opened()
    receiver_jobs = wire_receiver_firmware_recorder(paired_instances)
    # Initial cold-connect push lands; cache should already be
    # idle before we drive any traffic.
    await _wait_for_offloader_idle(paired_instances, queue_status_events)

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    bundle_bytes = make_real_bundle()

    for cycle in range(2):
        job_tag = f"off-job-{cycle}"
        ack = await handle.client.submit_job(
            job_id=job_tag,
            configuration_filename="kitchen.yaml",
            target="compile",
            bundle_bytes=bundle_bytes,
        )
        assert ack["accepted"] is True
        assert receiver_jobs[-1].remote_job_id == job_tag

        _drive_receiver_lifecycle(
            paired_instances, receiver_jobs[-1], terminal=EventType.JOB_COMPLETED
        )
        # After the terminal broadcast lands on the offloader,
        # the cache must reflect idle — proves the slot release
        # happened before the fire and the scheduler will pick
        # REMOTE for the next install. A regression that froze
        # the snapshot at ``running=True`` would hang this wait
        # past the timeout.
        await _wait_for_offloader_idle(paired_instances, queue_status_events)

        snapshot = paired_instances.offloader.build_scheduler_snapshot()
        decision = pick_build_path(snapshot)
        assert decision.path is BuildPath.REMOTE, (
            f"cycle {cycle}: scheduler fell back to LOCAL after a completed "
            f"remote job; cache entry: "
            f"{paired_instances.offloader.state.peer_queue_status[paired_instances.pin_sha256]!r}"
        )
        assert decision.pin_sha256 == paired_instances.pin_sha256


async def test_failed_first_job_still_routes_remote_on_second_install(
    paired_instances: PairedInstances,
) -> None:
    """A failed remote job leaves the scheduler eligible for the next install.

    Same shape as the back-to-back-success test, but cycle 1
    fires ``JOB_FAILED`` instead of ``JOB_COMPLETED``. The
    user-visible regression mode after #575 was the same — a
    receiver-side compile failure (anything the runner finishes
    with non-zero exit / matching error patterns) used to leave
    the offloader's cache stuck at ``running=True`` because the
    JOB_FAILED fire happened before the slot was released, and
    cycle 2 silently fell back to LOCAL. After the
    :meth:`_finalize_terminal` consolidation the FAILED path
    fires the same idle snapshot the COMPLETED path does, so
    the next install routes REMOTE again.
    """
    queue_status_events = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_QUEUE_STATUS_CHANGED
    )
    await paired_instances.wait_until_session_opened()
    receiver_jobs = wire_receiver_firmware_recorder(paired_instances)
    await _wait_for_offloader_idle(paired_instances, queue_status_events)

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    bundle_bytes = make_real_bundle()

    # Cycle 1: fail.
    ack = await handle.client.submit_job(
        job_id="off-job-fail",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle_bytes,
    )
    assert ack["accepted"] is True
    _drive_receiver_lifecycle(paired_instances, receiver_jobs[-1], terminal=EventType.JOB_FAILED)
    await _wait_for_offloader_idle(paired_instances, queue_status_events)
    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    assert pick_build_path(snapshot).path is BuildPath.REMOTE

    # Cycle 2: success on the same paired session.
    ack = await handle.client.submit_job(
        job_id="off-job-ok",
        configuration_filename="kitchen.yaml",
        target="compile",
        bundle_bytes=bundle_bytes,
    )
    assert ack["accepted"] is True
    _drive_receiver_lifecycle(paired_instances, receiver_jobs[-1], terminal=EventType.JOB_COMPLETED)
    await _wait_for_offloader_idle(paired_instances, queue_status_events)
    snapshot = paired_instances.offloader.build_scheduler_snapshot()
    decision = pick_build_path(snapshot)
    assert decision.path is BuildPath.REMOTE
    assert decision.pin_sha256 == paired_instances.pin_sha256


async def test_remote_clean_round_trip_lands_clean_job_and_fans_state_back(
    paired_instances: PairedInstances,
) -> None:
    """``submit_job(target="clean")`` lands a JobType.CLEAN on the receiver + fans state back.

    Pins the wire shape end-to-end for the fan-out side of
    ``FirmwareController.clean``: the operator clicks "Clean
    build files", the offloader-side runner sends
    ``submit_job(target="clean")`` over the paired Noise
    session for each connected peer, the receiver dispatches it
    to its firmware queue as a ``JobType.CLEAN`` job, and the
    lifecycle events fan back to the offloader through the same
    :class:`JobFanout` plumbing as ``compile`` /
    ``upload`` / ``install``.

    The compile-target path is covered above; this test exists
    so a regression that special-cases the receiver-side
    dispatch on ``target=="compile"`` (rejecting clean by
    accident, or routing it to the wrong ``JobType``) lands here
    rather than silently shipping. **No** ``download_artifacts``
    step — clean produces no firmware to flash. Single
    ``JOB_COMPLETED`` is the whole terminal.
    """
    await paired_instances.wait_until_session_opened()
    receiver_jobs = wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-clean-1",
        configuration_filename="kitchen.yaml",
        target="clean",
        bundle_bytes=make_real_bundle(),
    )

    # Receiver accepted the clean target on the same wire path
    # compile uses.
    assert ack["accepted"] is True
    assert len(receiver_jobs) == 1
    receiver_job = receiver_jobs[0]
    assert receiver_job.job_type is JobType.CLEAN
    assert receiver_job.remote_peer == paired_instances.offloader_dashboard_id
    assert receiver_job.remote_job_id == "off-clean-1"

    # Drive the receiver's queue lifecycle. The fan-out test
    # helper bakes in the slot-release-then-fire ordering the
    # transparent-install fix established; reusing it here proves CLEAN gets
    # the same idle snapshot the COMPLETED path emits for
    # compile / install.
    _drive_receiver_lifecycle(paired_instances, receiver_job, terminal=EventType.JOB_COMPLETED)

    # Three state changes land on the offloader's bus: queued,
    # running, completed; each carries the offloader-supplied
    # ``job_id`` and the live pin_sha256. Poll for the terminal
    # one and then check every captured entry rather than racing
    # ``wait_for(received)`` against the lifecycle.
    await state_changes.wait_for_status("completed")
    statuses = [payload["status"] for payload in state_changes]
    assert "queued" in statuses
    assert "running" in statuses
    assert "completed" in statuses
    for payload in state_changes:
        assert payload["job_id"] == "off-clean-1"
        assert payload["pin_sha256"] == paired_instances.pin_sha256
