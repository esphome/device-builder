"""
End-to-end benchmark: firmware-artifact download over loopback Noise WS.

Stands up two paired :class:`DeviceBuilder` instances on
``127.0.0.1`` (the same shape
:func:`tests.manual._mock_remote_e2e.paired_dashboards` uses for
the wet-test scripts), lays down an incompressible random
``firmware.bin`` on the receiver, seeds a ``COMPLETED``
:class:`FirmwareJob`, and times one offloader-side
``download_artifacts`` round-trip.

The single timed call exercises every wire-path cost on a real
Noise session: :func:`pack_build_artifacts` (gzipped tar render
streaming the random payload), per-chunk ``artifacts_chunk``
frames over Noise AEAD, the offloader-side
:class:`BundleAssembler` reassemble + SHA-256 verify, and the
final tarball unpack into the WS response shape.

Payload size is 32 KiB — small enough that the Python
dispatch / framing / await machinery dominates the
profile (rather than the C-extension crypto / gzip /
SHA-256 internals where CodSpeed can't produce a useful
flamegraph), while still exercising every wire step on
the artifact-download path.

``max_rounds=1`` keeps walltime mode to one measurement; the
simulation runner already runs each benchmark exactly once.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from esphome.core import CORE
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.helpers.storage_path import resolve_idedata_path
from esphome_device_builder.models import (
    FirmwareJob,
    JobStatus,
    JobType,
)
from tests._storage_fixtures import write_storage_json
from tests.manual._mock_remote_e2e import MockPair, paired_dashboards

_FIRMWARE_BLOB_BYTES = 32 * 1024


@pytest.fixture(scope="module")
def _firmware_blob() -> bytes:
    """Generate an incompressible 32 KiB payload once per session."""
    return secrets.token_bytes(_FIRMWARE_BLOB_BYTES)


def _stage_receiver_artifacts(blob: bytes, configuration: str = "kitchen.yaml") -> None:
    """Lay down storage / idedata / firmware.bin / platformio.ini."""
    name = "kitchen"
    data_dir = Path(CORE.data_dir)
    build_path = data_dir / "build" / name
    pioenvs = build_path / ".pioenvs" / name
    pioenvs.mkdir(parents=True, exist_ok=True)

    firmware_bin = pioenvs / "firmware.bin"
    firmware_bin.write_bytes(blob)
    (build_path / "platformio.ini").write_text("[env:e2e]\nplatform = espressif32\n")

    # ``CORE.data_dir`` is ``<config_dir>/.esphome`` in default
    # mode; the helper appends ``.esphome/storage/`` to its
    # ``tmp_path`` arg, so passing the parent lines it up with
    # ``resolve_storage_path``.
    write_storage_json(
        data_dir.parent,
        configuration,
        build_path=build_path,
        firmware_bin_path=firmware_bin,
        overrides={"target_platform": "esp32"},
    )

    idedata_path = resolve_idedata_path(configuration, name=name)
    idedata_path.parent.mkdir(parents=True, exist_ok=True)
    idedata_path.write_text("{}\n", encoding="utf-8")


def _seed_receiver_firmware_job(pair: MockPair) -> None:
    """Drop a ``COMPLETED`` :class:`FirmwareJob` on the receiver's ``_jobs`` map.

    :meth:`ArtifactsDownloadSender._find_remote_job` linear-scans
    ``firmware._jobs`` for a matching
    ``(remote_peer, remote_job_id)``; seeding directly bypasses
    the firmware queue (which would otherwise try to run a real
    ``esphome compile`` on submit).
    """
    job = FirmwareJob(
        job_id="rcv-job-1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        remote_peer=pair.offloader_dashboard_id,
        remote_job_id="off-job-1",
    )
    assert pair.receiver.firmware is not None
    pair.receiver.firmware._jobs[job.job_id] = job


@asynccontextmanager
async def _bench_pair(tmp_path: Path, blob: bytes) -> AsyncIterator[MockPair]:
    """Stand up the loopback pair, stage 128 MiB artifacts, seed the job."""
    async with paired_dashboards(root_dir=tmp_path) as pair:
        _stage_receiver_artifacts(blob)
        _seed_receiver_firmware_job(pair)
        yield pair


@pytest.fixture
def _benchmark_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """One asyncio loop that owns the paired servers across setup → bench → teardown."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.mark.benchmark(max_rounds=1)
def test_download_artifacts_over_loopback_noise(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
    _benchmark_loop: asyncio.AbstractEventLoop,
    _firmware_blob: bytes,
) -> None:
    """One ``download_artifacts`` round-trip carrying a multi-MiB firmware.bin."""
    loop = _benchmark_loop
    pair_cm = _bench_pair(tmp_path, _firmware_blob)
    pair = loop.run_until_complete(pair_cm.__aenter__())
    try:

        async def _download() -> dict[str, Any]:
            return await pair.offloader.remote_build_offloader.download_artifacts(
                pin_sha256=pair.pin_sha256,
                job_id="off-job-1",
            )

        @benchmark
        def run() -> None:
            result = loop.run_until_complete(_download())
            # Sanity gate: the 128 MiB blob actually transited.
            # ``total_bytes`` sums every image's declared size;
            # ``firmware.bin`` alone is 128 MiB so the lower
            # bound here would catch a regression that silently
            # truncated the payload.
            assert result["total_bytes"] >= _FIRMWARE_BLOB_BYTES
    finally:
        loop.run_until_complete(pair_cm.__aexit__(None, None, None))
