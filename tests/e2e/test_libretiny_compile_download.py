"""
End-to-end: a real LibreTiny (bk7231n) compile round-trips through the offload session (#1102).

The synthetic install round-trip (``test_install_round_trip.py``)
writes fake esp32-shaped binaries; nothing exercised the LibreTiny
artifact set. A bk7231n build's ``firmware_bin_path`` is
``firmware.uf2`` (not ``firmware.bin``), it emits no esp-style flash
images, and the downloadable Beken / cloudcutter images are listed in
a ``firmware.json`` the offloader must re-read. #1102 was the
offloader's Download picker offering only ``firmware.uf2`` after a
remote build because ``firmware.json`` never shipped in the artifact
tarball.

This test runs a real ``esphome compile`` of a bk7231n config through
the full paired session (submit_job → receiver compile →
download_artifacts → materialise) and asserts the offloader can
enumerate every public chip image, matching what a local build offers.
It is slow (cold runs clone the LibreTiny SDK), hence
``@pytest.mark.timeout(600)``; it runs in the dedicated e2e CI jobs.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.firmware.download import get_binaries
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    materialise_remote_artifacts,
)
from esphome_device_builder.helpers.remote_build_layout import (
    parse_from_configuration as parse_remote_build_path,
)
from esphome_device_builder.models import EventType

from ..conftest import capture_events
from .conftest import (
    PairedInstances,
    drive_remote_job_to_completed,
    make_real_bundle,
    wire_receiver_firmware_recorder,
)

_DEVICE = "bk7231n-e2e"
_CONFIGURATION_FILENAME = f"{_DEVICE}.yaml"
_BK7231N_YAML = f"""\
esphome:
  name: {_DEVICE}
bk72xx:
  board: generic-bk7231n-qfn32-tuya
logger:
""".encode()


def _run_esphome_compile(
    yaml_path: Path, *, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``esphome compile`` on *yaml_path* with *env*'s ESPHOME_DATA_DIR override."""
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell, test-only invocation
        [sys.executable, "-m", "esphome", "compile", str(yaml_path)],
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
        env=env,
    )


@pytest.mark.timeout(600)
async def test_libretiny_bk7231n_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A real bk7231n compile lands every public Beken/cloudcutter image offloader-side (#1102)."""
    await paired_instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    # 1. submit the bk7231n bundle; the receiver extracts the YAML to its
    #    remote-build subtree and dispatches a queued job.
    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-bk-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        target="compile",
        bundle_bytes=make_real_bundle(
            configuration_filename=_CONFIGURATION_FILENAME, yaml_body=_BK7231N_YAML
        ),
    )
    assert ack["accepted"] is True
    receiver_job = created_jobs[0]

    # 2. real compile into the receiver's remote-build data dir — the exact
    #    ESPHOME_DATA_DIR ``compose_subprocess_env`` pins for a remote job, so
    #    ``pack_build_artifacts`` reads the produced storage/build/idedata back.
    remote_build_path = parse_remote_build_path(receiver_job.configuration)
    assert remote_build_path is not None
    data_dir = remote_build_path.data_dir(Path(CORE.data_dir))
    config_dir = Path(paired_instances.receiver._db.settings.config_dir)
    yaml_path = config_dir / receiver_job.configuration
    result = await asyncio.to_thread(
        _run_esphome_compile, yaml_path, env={**os.environ, "ESPHOME_DATA_DIR": str(data_dir)}
    )
    assert result.returncode == 0, (
        f"bk7231n compile failed:\nstdout:\n{result.stdout[-4000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )
    receiver_pioenvs = data_dir / "build" / _DEVICE / ".pioenvs" / _DEVICE
    assert (receiver_pioenvs / "firmware.uf2").is_file()
    assert (receiver_pioenvs / "firmware.json").is_file()

    # 3. drive the receiver queue lifecycle so the download side accepts the job.
    await drive_remote_job_to_completed(paired_instances, receiver_job, state_changes)

    # 4. pull the artifacts back over the same session and materialise locally.
    packed = await handle.client.download_artifacts(job_id="off-bk-1")
    build_path = await asyncio.to_thread(
        materialise_remote_artifacts, packed.tarball, _CONFIGURATION_FILENAME
    )
    pioenvs = build_path / ".pioenvs" / _DEVICE
    assert (pioenvs / "firmware.uf2").is_file()
    # The #1102 fix: firmware.json rides back so get_download_types can
    # re-enumerate the chip images on the offloader.
    assert (pioenvs / "firmware.json").is_file()

    # 5. the offloader's Download picker offers firmware.uf2 plus the public
    #    Beken (.rbl) and cloudcutter (.ug.bin) images — not uf2 alone (#1102).
    firmware = paired_instances.offloader._db.firmware
    firmware._validate_configuration_boundary = AsyncMock()
    binaries = await get_binaries(firmware, configuration=_CONFIGURATION_FILENAME)
    offered = {entry["file"] for entry in binaries}
    assert "firmware.uf2" in offered
    assert any(name.endswith(".rbl") for name in offered), offered
    assert any(name.endswith(".ug.bin") for name in offered), offered
