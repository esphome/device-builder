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

from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.firmware.download import get_binaries

from ...conftest import PairedInstances, run_offload_compile_round_trip

_DEVICE = "bk7231n-e2e"
_CONFIGURATION_FILENAME = f"{_DEVICE}.yaml"
_BK7231N_YAML = f"""\
esphome:
  name: {_DEVICE}
bk72xx:
  board: generic-bk7231n-qfn32-tuya
logger:
""".encode()


@pytest.mark.timeout(600)
async def test_libretiny_bk7231n_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A real bk7231n compile lands every public Beken/cloudcutter image offloader-side (#1102)."""
    _data_dir, build_path = await run_offload_compile_round_trip(
        paired_instances,
        job_id="off-bk-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        yaml_body=_BK7231N_YAML,
    )

    # The #1102 fix: firmware.json rides back beside firmware.uf2 so
    # get_download_types can re-enumerate the chip images on the offloader.
    pioenvs = build_path / ".pioenvs" / _DEVICE
    assert (pioenvs / "firmware.uf2").is_file()
    assert (pioenvs / "firmware.json").is_file()

    # The offloader's Download picker offers firmware.uf2 plus the public
    # Beken (.rbl) and cloudcutter (.ug.bin) images — not uf2 alone (#1102).
    firmware = paired_instances.offloader._db.firmware
    firmware._validate_configuration_boundary = AsyncMock()
    binaries = await get_binaries(firmware, configuration=_CONFIGURATION_FILENAME)
    offered = {entry["file"] for entry in binaries}
    assert "firmware.uf2" in offered
    assert any(name.endswith(".rbl") for name in offered), offered
    assert any(name.endswith(".ug.bin") for name in offered), offered
