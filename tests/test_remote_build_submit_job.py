"""
Tests for the receiver-side ``submit_job`` flow (issue #106 phase 5c-2).

Two layers, mirroring :mod:`tests.test_remote_build_peer_link`'s
shape so the seam between this module's unit tests and the
e2e harness in :mod:`tests.e2e` stays visible:

* Unit tests against a stubbed
  :class:`PeerLinkSession` + :class:`FirmwareController` — pin
  the per-branch reject reasons and the happy-path enqueue.
* End-to-end flow against a real
  :class:`PeerLinkSession` + a real bundle goes in the
  ``tests/e2e`` harness in 5c-2b once the firmware-event
  fan-out lands; the submit-and-ack contract here is enough
  for 5c-2a.
"""

from __future__ import annotations

import hashlib
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from esphome.bundle import EsphomeError

from esphome_device_builder.controllers.remote_build_submit_job import (
    SubmitJobReceiver,
    _validate_configuration_filename,
)
from esphome_device_builder.helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
    chunk_bundle,
    compute_bundle_sha256,
    encode_chunk,
)
from esphome_device_builder.models import (
    JobType,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)


def _make_tar_bundle(yaml_filename: str, yaml_body: bytes) -> bytes:
    """Build a minimal gzipped-tar bundle containing one YAML.

    The receiver-side ``prepare_bundle_for_compile`` helper
    expects an esphome-shaped bundle with a manifest and a
    handful of validated members. For the unit tests here we
    sidestep extraction by stubbing ``prepare_bundle_for_compile``;
    a syntactically-valid tarball is enough for the
    ``write_and_extract`` executor hop to take the happy path.
    """
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=yaml_filename)
        info.size = len(yaml_body)
        tar.addfile(info, BytesIO(yaml_body))
    return buf.getvalue()


def _make_session(*, dashboard_id: str = "alpha") -> Any:
    """Stub ``PeerLinkSession`` capturing send_app_frame + terminate calls."""
    session = MagicMock()
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    session.terminate = AsyncMock()
    return session


def _make_firmware_controller() -> Any:
    """Stub ``FirmwareController`` recording ``_create_job`` / ``_enqueue`` calls."""
    firmware = MagicMock()
    created_jobs: list[Any] = []

    def _create_job(
        configuration: str, job_type: JobType, *, remote_peer: str = "", **_: Any
    ) -> Any:
        job = MagicMock()
        job.job_id = f"local-{len(created_jobs)}"
        job.configuration = configuration
        job.job_type = job_type
        job.remote_peer = remote_peer
        created_jobs.append(job)
        return job

    firmware._create_job = MagicMock(side_effect=_create_job)
    firmware._enqueue = AsyncMock(side_effect=lambda job: job)
    firmware.created_jobs = created_jobs
    return firmware


def _make_receiver(tmp_path: Path, firmware: Any | None = None) -> SubmitJobReceiver:
    return SubmitJobReceiver(
        config_dir=tmp_path,
        firmware_controller=firmware or _make_firmware_controller(),
    )


def _header(
    *,
    job_id: str = "job-1",
    configuration_filename: str = "kitchen.yaml",
    target: str = "compile",
    bundle: bytes = b"\x00" * 100,
) -> SubmitJobFrameData:
    return SubmitJobFrameData(
        type="submit_job",
        job_id=job_id,
        configuration_filename=configuration_filename,
        target=target,  # type: ignore[typeddict-item]
        total_bundle_bytes=len(bundle),
        num_chunks=max(1, (len(bundle) + BUNDLE_CHUNK_SIZE_BYTES - 1) // BUNDLE_CHUNK_SIZE_BYTES),
        bundle_sha256=compute_bundle_sha256(bundle),
    )


def _ack_payload(session: Any) -> dict[str, Any]:
    """Pull the most recent ``send_app_frame`` payload off *session*."""
    payload: dict[str, Any] = session.send_app_frame.call_args.args[0]
    return payload


def _frame_chunks(job_id: str, bundle: bytes) -> list[SubmitJobChunkFrameData]:
    """Wrap :func:`chunk_bundle` output in :class:`SubmitJobChunkFrameData` dicts.

    The bundle helper produces raw ``(index, bytes, is_last)``
    tuples; the wire format wraps each as a JSON-encoded
    ``SubmitJobChunkFrameData`` (5c-3 will do this on the
    offloader side). Tests here drive the receiver, so they
    need the wrapped form to feed
    :meth:`SubmitJobReceiver.handle_submit_job_chunk` the same
    shape the live wire would land.
    """
    return [
        SubmitJobChunkFrameData(
            type="submit_job_chunk",
            job_id=job_id,
            chunk_index=index,
            data_b64=encode_chunk(raw),
            is_last=is_last,
        )
        for index, raw, is_last in chunk_bundle(bundle)
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("kitchen.yaml", "kitchen"),
        ("kitchen.yml", "kitchen"),
        ("KITCHEN.YAML", "KITCHEN"),
        ("multi.dot.yaml", "multi.dot"),
    ],
)
def test_validate_configuration_filename_accepts_clean_yaml_leaves(
    filename: str, expected: str
) -> None:
    assert _validate_configuration_filename(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "",  # empty
        "no-extension",  # no .yaml / .yml
        "kitchen.txt",  # wrong extension
        ".yaml",  # extension only — empty stem
        "..yaml",  # leading-dot escape attempt
        "../etc/passwd.yaml",  # path traversal via ..
        "../../escape.yaml",  # nested path traversal
        "sub/kitchen.yaml",  # forward slash separator
        "sub\\kitchen.yaml",  # Windows-style separator
        "kitchen\x00.yaml",  # NUL byte
        "/abs/kitchen.yaml",  # absolute path
    ],
)
def test_validate_configuration_filename_rejects_malicious_inputs(filename: str) -> None:
    assert _validate_configuration_filename(filename) is None


# ---------------------------------------------------------------------------
# handle_submit_job — header validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_job_duplicate_header_rejected(tmp_path: Path) -> None:
    """A second header on a session with one in-flight rejects ``duplicate_submit``."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()

    await receiver.handle_submit_job(session, _header(job_id="first"))
    session.send_app_frame.reset_mock()

    await receiver.handle_submit_job(session, _header(job_id="second"))

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "duplicate_submit"
    assert payload["job_id"] == "second"


@pytest.mark.asyncio
async def test_submit_job_invalid_target_rejected(tmp_path: Path) -> None:
    """A header with ``target`` outside compile/upload rejects ``invalid_header``."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()

    await receiver.handle_submit_job(session, _header(target="install"))

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "invalid_header"


@pytest.mark.asyncio
async def test_submit_job_path_traversal_filename_rejected(tmp_path: Path) -> None:
    """A ``configuration_filename`` with path traversal rejects ``invalid_header``."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()

    await receiver.handle_submit_job(session, _header(configuration_filename="../etc/passwd.yaml"))

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "invalid_header"


@pytest.mark.asyncio
async def test_submit_job_oversized_bundle_rejected(tmp_path: Path) -> None:
    """A header announcing a bundle past ``BUNDLE_MAX_TOTAL_BYTES`` rejects via the assembler."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    huge = SubmitJobFrameData(
        type="submit_job",
        job_id="job-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        total_bundle_bytes=10 * 1024 * 1024,  # 10 MiB > 4 MiB cap
        num_chunks=1,
        bundle_sha256="0" * 64,
    )

    await receiver.handle_submit_job(session, huge)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "oversized"


# ---------------------------------------------------------------------------
# handle_submit_job_chunk — chunk dispatch + finalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_job_chunk_no_inflight_rejected(tmp_path: Path) -> None:
    """A chunk frame without a preceding header rejects ``no_inflight_submit``."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()

    chunk = SubmitJobChunkFrameData(
        type="submit_job_chunk",
        job_id="job-1",
        chunk_index=0,
        data_b64="AAAA",  # any valid base64
        is_last=True,
    )
    await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "no_inflight_submit"
    session.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_submit_job_chunk_job_id_mismatch_rejected(tmp_path: Path) -> None:
    """A chunk with the wrong ``job_id`` rejects ``job_id_mismatch``."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    bundle = b"hello"
    await receiver.handle_submit_job(session, _header(job_id="real", bundle=bundle))

    chunk = _frame_chunks("real", bundle)[0]
    chunk["job_id"] = "wrong"  # type: ignore[typeddict-unknown-key]

    await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "job_id_mismatch"


@pytest.mark.asyncio
async def test_submit_job_chunk_decode_failure_terminates_session(tmp_path: Path) -> None:
    """Garbage base64 in a chunk rejects ``chunk_decode_failed`` AND terminates the session."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    await receiver.handle_submit_job(session, _header(bundle=b"hello"))

    bad_chunk = SubmitJobChunkFrameData(
        type="submit_job_chunk",
        job_id="job-1",
        chunk_index=0,
        data_b64="!!!not-valid-base64!!!",
        is_last=True,
    )
    await receiver.handle_submit_job_chunk(session, bad_chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "chunk_decode_failed"
    session.terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_job_chunk_out_of_order_terminates_session(tmp_path: Path) -> None:
    """An out-of-order chunk index rejects + terminates (wire-level misbehaviour)."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    bundle = b"x" * (BUNDLE_CHUNK_SIZE_BYTES * 3)  # three chunks
    await receiver.handle_submit_job(session, _header(bundle=bundle))

    chunks = _frame_chunks("job-1", bundle)
    # Skip chunk 0; feed chunk 1 first.
    await receiver.handle_submit_job_chunk(session, chunks[1])

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "out_of_order"
    session.terminate.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_job_chunk_hash_mismatch_recoverable(tmp_path: Path) -> None:
    """A bundle whose assembled hash mismatches rejects ``hash_mismatch`` *without* terminating."""
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    bundle = b"hello world"

    # Build a header announcing the WRONG sha256 so finalise()
    # raises HASH_MISMATCH after the chunks land cleanly.
    bad_sha = hashlib.sha256(b"different bytes").hexdigest()
    header = SubmitJobFrameData(
        type="submit_job",
        job_id="job-1",
        configuration_filename="kitchen.yaml",
        target="compile",
        total_bundle_bytes=len(bundle),
        num_chunks=1,
        bundle_sha256=bad_sha,
    )
    await receiver.handle_submit_job(session, header)

    chunk = _frame_chunks("job-1", bundle)[0]
    await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "hash_mismatch"
    # ``hash_mismatch`` is a recoverable error — no terminate.
    session.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — bundle reception, extract, queue, ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_job_happy_path_extracts_and_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final chunk → write tarball → extract → queue job → ack accepted."""
    firmware = _make_firmware_controller()
    receiver = _make_receiver(tmp_path, firmware)
    session = _make_session(dashboard_id="alpha-dashboard")
    bundle = _make_tar_bundle("kitchen.yaml", b"esphome:\n  name: kitchen\n")

    # Stub ``prepare_bundle_for_compile`` because the real one
    # validates a manifest + esphome-shaped layout. The test
    # here pins the receive-loop → write → extract → queue
    # plumbing; the real extraction is covered by esphome's own
    # tests + the e2e harness later.
    expected_yaml = (
        tmp_path / ".esphome" / ".remote_builds" / "alpha-dashboard" / "kitchen" / "kitchen.yaml"
    )

    def _stub_prepare(bundle_path: Path, target_dir: Path) -> Path:
        # Ensure the bundle was written to disk first (we're
        # checking the executor hop ran the write step).
        assert bundle_path.exists()
        assert bundle_path.read_bytes() == bundle
        target_dir.mkdir(parents=True, exist_ok=True)
        expected_yaml.parent.mkdir(parents=True, exist_ok=True)
        expected_yaml.write_bytes(b"esphome:\n  name: kitchen\n")
        return expected_yaml

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_submit_job.prepare_bundle_for_compile",
        _stub_prepare,
    )

    await receiver.handle_submit_job(session, _header(bundle=bundle))
    for chunk in _frame_chunks("job-1", bundle):
        await receiver.handle_submit_job_chunk(session, chunk)

    # Ack accepted, reason omitted on the success path.
    payload = _ack_payload(session)
    assert payload["accepted"] is True
    assert payload["job_id"] == "job-1"
    assert "reason" not in payload

    # Job created with remote_peer set + the relative
    # configuration path under the per-peer subtree.
    assert len(firmware.created_jobs) == 1
    job = firmware.created_jobs[0]
    assert job.remote_peer == "alpha-dashboard"
    assert job.job_type is JobType.COMPILE
    assert job.configuration == str(expected_yaml.relative_to(tmp_path))
    firmware._enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_job_intermediate_chunk_no_ack(tmp_path: Path) -> None:
    """A non-final chunk feeds the assembler silently; no ack until ``is_last``.

    Pins the streaming contract: while chunks are arriving the
    receiver stays quiet — the offloader's submit caller waits
    on the single ack frame at end-of-stream rather than
    counting per-chunk responses.
    """
    receiver = _make_receiver(tmp_path)
    session = _make_session()
    bundle = b"x" * (BUNDLE_CHUNK_SIZE_BYTES * 2 + 100)  # three chunks
    await receiver.handle_submit_job(session, _header(bundle=bundle))
    chunks = _frame_chunks("job-1", bundle)
    assert len(chunks) >= 2
    session.send_app_frame.reset_mock()

    # Feed only the non-final chunks; no ack should fire.
    for chunk in chunks[:-1]:
        await receiver.handle_submit_job_chunk(session, chunk)

    session.send_app_frame.assert_not_called()
    session.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_submit_job_path_traversal_dashboard_id_caught_at_extract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malicious ``dashboard_id`` shape escapes the filename validator but is caught at extract.

    Pins the defence-in-depth resolve-and-stay-under-root check
    inside ``_extract_and_queue``. The filename validator
    catches separators / ``..`` in ``configuration_filename``,
    but ``dashboard_id`` flows through unvalidated from the
    Noise handshake / receiver-side registration. A future
    regression there would hit this gate.
    """
    firmware = _make_firmware_controller()
    receiver = _make_receiver(tmp_path, firmware)
    # Climbing dashboard_id; the resolve-and-relative-to defense
    # rejects because the resulting target_dir resolves outside
    # ``<config>/.esphome/.remote_builds/``.
    session = _make_session(dashboard_id="../../escape")
    bundle = b"hello"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_submit_job.prepare_bundle_for_compile",
        lambda _bundle, _target: tmp_path / "kitchen.yaml",
    )

    await receiver.handle_submit_job(session, _header(bundle=bundle))
    for chunk in _frame_chunks("job-1", bundle):
        await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "invalid_header"
    firmware._enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_submit_job_enqueue_failure_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside ``_enqueue`` rejects ``queue_rejected``; session stays."""
    firmware = _make_firmware_controller()
    firmware._enqueue = AsyncMock(side_effect=RuntimeError("queue full"))
    receiver = _make_receiver(tmp_path, firmware)
    session = _make_session()
    bundle = b"hello"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_submit_job.prepare_bundle_for_compile",
        lambda _bundle, target: target / "kitchen.yaml",
    )

    await receiver.handle_submit_job(session, _header(bundle=bundle))
    for chunk in _frame_chunks("job-1", bundle):
        await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "queue_rejected"
    session.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_submit_job_extract_failure_rejects_without_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside ``prepare_bundle_for_compile`` rejects ``extract_failed``; session stays."""
    firmware = _make_firmware_controller()
    receiver = _make_receiver(tmp_path, firmware)
    session = _make_session()
    bundle = b"\x1f\x8b" + b"\x00" * 50  # gzip magic + filler — not a valid tar

    def _failing_prepare(bundle_path: Path, target_dir: Path) -> Path:
        raise EsphomeError("bundle invalid: missing manifest")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.remote_build_submit_job.prepare_bundle_for_compile",
        _failing_prepare,
    )

    await receiver.handle_submit_job(session, _header(bundle=bundle))
    for chunk in _frame_chunks("job-1", bundle):
        await receiver.handle_submit_job_chunk(session, chunk)

    payload = _ack_payload(session)
    assert payload["accepted"] is False
    assert payload["reason"] == "extract_failed"
    # Receiver-side problem; the wire is still good.
    session.terminate.assert_not_called()
    # No job was queued.
    firmware._enqueue.assert_not_called()


# ---------------------------------------------------------------------------
# discard_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discard_session_drops_inflight(tmp_path: Path) -> None:
    """A discarded session forgets its in-flight upload."""
    receiver = _make_receiver(tmp_path)
    session = _make_session(dashboard_id="alpha")
    await receiver.handle_submit_job(session, _header(bundle=b"x" * 100))

    receiver.discard_session("alpha")

    # A subsequent chunk now lands as "no in-flight" rather than
    # "out-of-order" / "hash mismatch" — proves the dict entry
    # was actually dropped.
    chunk = _frame_chunks("job-1", b"x" * 100)[0]
    await receiver.handle_submit_job_chunk(session, chunk)
    payload = _ack_payload(session)
    assert payload["reason"] == "no_inflight_submit"


def test_discard_session_unknown_is_noop(tmp_path: Path) -> None:
    """Discarding a session that never registered is a no-op."""
    receiver = _make_receiver(tmp_path)
    receiver.discard_session("never-seen")  # should not raise
