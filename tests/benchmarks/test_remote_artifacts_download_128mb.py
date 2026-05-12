"""
End-to-end benchmark: 128 MB firmware-artifact download over loopback Noise WS.

Stands up two paired :class:`DeviceBuilder` instances on
``127.0.0.1`` (the same shape
:func:`tests.manual._mock_remote_e2e.paired_dashboards` uses for
the wet-test scripts), lays down a 128 MiB random
``firmware.bin`` on the receiver, seeds a ``COMPLETED``
:class:`FirmwareJob`, and times one offloader-side
``download_artifacts`` round-trip.

The single timed call exercises every wire-path cost on a real
Noise session: :func:`pack_build_artifacts` (gzipped tar render
streaming an incompressible 128 MiB payload), per-chunk
``artifacts_chunk`` frames over Noise AEAD, the offloader-side
:class:`BundleAssembler` reassemble + SHA-256 verify, and the
final tarball unpack into the WS response shape.

Single-round benchmark — at 128 MiB the wall-clock per round is
high enough that re-running in CodSpeed walltime mode would
dominate the test budget. ``max_rounds=1`` keeps it to one
measurement; CodSpeed callgraph mode runs once regardless.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from esphome.core import CORE
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.helpers.storage_path import (
    resolve_idedata_path,
    resolve_storage_path,
)
from esphome_device_builder.models import (
    FirmwareJob,
    JobStatus,
    JobType,
)
from tests.manual._mock_remote_e2e import MockPair, paired_dashboards

_FIRMWARE_BLOB_BYTES = 128 * 1024 * 1024


@pytest.fixture(scope="module")
def _firmware_blob() -> bytes:
    """Generate an incompressible 128 MiB payload once per session.

    ``secrets.token_bytes`` lands on ``os.urandom``; on macOS / Linux
    that's ~100-300 ms for 128 MiB. Module scope so we don't pay
    that cost on every benchmark invocation.
    """
    return secrets.token_bytes(_FIRMWARE_BLOB_BYTES)


def _stage_receiver_artifacts(blob: bytes, configuration: str = "kitchen.yaml") -> None:
    """Lay down storage / idedata / firmware.bin / platformio.ini.

    Anchors on ``CORE.data_dir`` (which
    :func:`paired_dashboards` pins to the offloader's sentinel)
    so the same paths :func:`resolve_storage_path` and
    :func:`resolve_idedata_path` look at on the pack side are
    where these files land. The receiver-side packer reads
    through those same helpers, so both DeviceBuilders in this
    one-process pair see one canonical artifact tree.
    """
    name = "kitchen"
    data_dir = Path(CORE.data_dir)
    build_path = data_dir / "build" / name
    pioenvs = build_path / ".pioenvs" / name
    pioenvs.mkdir(parents=True, exist_ok=True)

    firmware_bin = pioenvs / "firmware.bin"
    firmware_bin.write_bytes(blob)
    (build_path / "platformio.ini").write_text("[env:e2e]\nplatform = espressif32\n")

    storage_path = resolve_storage_path(configuration)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "storage_version": 1,
                "name": name,
                "esp_platform": "esp32",
                "build_path": str(build_path),
                "firmware_bin_path": str(firmware_bin),
                "loaded_integrations": [],
                "loaded_platforms": [],
                "no_mdns": False,
                "framework": "arduino",
                "core_platform": "esp32",
                "target_platform": "esp32",
            }
        )
        + "\n",
        encoding="utf-8",
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
def test_download_artifacts_128mb_over_loopback_noise(
    benchmark: BenchmarkFixture,
    tmp_path: Path,
    _benchmark_loop: asyncio.AbstractEventLoop,
    _firmware_blob: bytes,
) -> None:
    """One ``download_artifacts`` round-trip carrying a 128 MiB firmware.bin."""
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
