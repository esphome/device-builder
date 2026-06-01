"""
A real native ESP-IDF compile round-trips through the offload session (#1102).

The native-IDF toolchain (``esp32: toolchain: esp-idf``) builds into
``build/`` not ``.pioenvs/<name>/``, so it stresses the offloader's
artifact enumeration differently than the LibreTiny e2e. Skipped on
esphome without the toolchain (< 2026.5.0); runs for real on the e2e
CI job's ``dev`` channel. ``timeout(900)`` covers a cold IDF install.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers.firmware.download import (
    collect_download_entries,
    get_binaries,
)
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
    run_esphome_compile,
    wire_receiver_firmware_recorder,
)

try:
    from esphome.const import CONF_TOOLCHAIN  # noqa: F401

    _HAS_NATIVE_IDF = True
except ImportError:
    _HAS_NATIVE_IDF = False

pytestmark = pytest.mark.skipif(
    not _HAS_NATIVE_IDF, reason="esphome lacks the native ESP-IDF toolchain (< 2026.5.0)"
)

_DEVICE = "esp-idf-e2e"
_CONFIGURATION_FILENAME = f"{_DEVICE}.yaml"
_ESP_IDF_YAML = f"""\
esphome:
  name: {_DEVICE}
esp32:
  board: esp32-c3-devkitm-1
  toolchain: esp-idf
  framework:
    type: esp-idf
logger:
""".encode()


def _local_download_set(data_dir: Path) -> set[str]:
    """Return the download filenames a *local* build of this device offers.

    Runs the production selection (:func:`collect_download_entries`)
    against the receiver's own storage + build dir, so the parity check
    shares one source of truth with the offloader side.
    """
    [storage_path] = list((data_dir / "storage").glob("*.json"))
    storage = StorageJSON.load(storage_path)
    assert storage is not None
    return {entry["file"] for entry in collect_download_entries(storage)}


@pytest.mark.timeout(900)
async def test_esp_idf_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A native-IDF compile lands the same downloads offloader-side as a local build (#1102)."""
    await paired_instances.wait_until_session_opened()
    created_jobs = wire_receiver_firmware_recorder(paired_instances)
    state_changes = capture_events(
        paired_instances.offloader_bus, EventType.OFFLOADER_JOB_STATE_CHANGED
    )

    # 1. submit the native-IDF bundle; the receiver extracts the YAML to
    #    its remote-build subtree and dispatches a queued job.
    handle = paired_instances.offloader.state.peer_link_clients[paired_instances.pin_sha256]
    ack = await handle.client.submit_job(
        job_id="off-idf-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        target="compile",
        bundle_bytes=make_real_bundle(
            configuration_filename=_CONFIGURATION_FILENAME, yaml_body=_ESP_IDF_YAML
        ),
    )
    assert ack["accepted"] is True
    receiver_job = created_jobs[0]

    # 2. real compile into the receiver's remote-build data dir — the exact
    #    ESPHOME_DATA_DIR ``compose_subprocess_env`` pins for a remote job.
    remote_build_path = parse_remote_build_path(receiver_job.configuration)
    assert remote_build_path is not None
    data_dir = remote_build_path.data_dir(Path(CORE.data_dir))
    config_dir = Path(paired_instances.receiver._db.settings.config_dir)
    yaml_path = config_dir / receiver_job.configuration
    result = await asyncio.to_thread(
        run_esphome_compile, yaml_path, env={**os.environ, "ESPHOME_DATA_DIR": str(data_dir)}
    )
    assert result.returncode == 0, (
        f"native-IDF compile failed:\nstdout:\n{result.stdout[-4000:]}\n"
        f"stderr:\n{result.stderr[-2000:]}"
    )

    # The set a local build of this device would offer for download.
    expected = _local_download_set(data_dir)
    assert expected, "compile produced no downloadable artifacts"
    assert "firmware.factory.bin" in expected, expected

    # 3. drive the receiver queue lifecycle so the download side accepts the job.
    await drive_remote_job_to_completed(paired_instances, receiver_job, state_changes)

    # 4. pull the artifacts back over the same session and materialise locally.
    packed = await handle.client.download_artifacts(job_id="off-idf-1")
    await asyncio.to_thread(materialise_remote_artifacts, packed.tarball, _CONFIGURATION_FILENAME)

    # 5. the offloader's Download picker offers exactly what a local build
    #    offers — including build/firmware.elf, which only rides back once
    #    BUILD_FILES lists the native-IDF ELF path (#1102).
    firmware = paired_instances.offloader._db.firmware
    firmware._validate_configuration_boundary = AsyncMock()
    binaries = await get_binaries(firmware, configuration=_CONFIGURATION_FILENAME)
    offered = {entry["file"] for entry in binaries}
    assert offered == expected
