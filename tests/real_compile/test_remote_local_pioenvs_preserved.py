"""
End-to-end pin: a remote-build round-trip preserves the local SCons cache.

User report (2026-05-14, after PR #874): ``local → remote → local``
still triggers a full rebuild on the second local compile because
PR #874's fix only addressed esphome's ``storage_should_clean``
gate, not PlatformIO/SCons's per-object decider. The materialiser
was bumping ``platformio.ini``'s mtime forward (extract +
``_force_idedata_cache_hit``), which SCons treats as "every
object built before that timestamp is stale" — every
``.pioenvs/<name>/src/*.o`` got recompiled.

This test runs a real esphome compile twice with a synthetic
remote-build round-trip in between and asserts that SCons
recompiles **zero** files on the second run.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    BUILD_INFO_MEMBER_NAME,
    IDEDATA_MEMBER_NAME,
    PLATFORMIO_INI_MEMBER_NAME,
    STORAGE_MEMBER_NAME,
)
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    materialise_remote_artifacts,
)

_MINIMAL_YAML = """\
esphome:
  name: kitchen
esp8266:
  board: esp01_1m
"""

_FAKE_RECEIVER_DATA_DIR = Path("/fake/receiver/.esphome")


def _build_synthetic_receiver_tarball(local_build_path: Path, local_storage_path: Path) -> bytes:
    """
    Pack the local build dir into a tarball that *looks* receiver-shipped.

    Rewrites every absolute path under ``local_build_path`` to a
    fake receiver root so the materialiser exercises the same
    remap path it would in production. Mirrors what
    ``pack_build_artifacts`` ships for ESP32 / ESP8266 — it can't
    pack the ``host`` platform (no ``artifact_platforms`` module),
    so we hand-build the equivalent for this test.
    """
    receiver_build_path = _FAKE_RECEIVER_DATA_DIR / "build" / "kitchen"
    local_build_str = str(local_build_path)
    receiver_build_str = str(receiver_build_path)

    storage_data = json.loads(local_storage_path.read_text())
    storage_data["build_path"] = receiver_build_str
    if storage_data.get("firmware_bin_path"):
        storage_data["firmware_bin_path"] = storage_data["firmware_bin_path"].replace(
            local_build_str, receiver_build_str
        )
    storage_bytes = (json.dumps(storage_data, indent=2) + "\n").encode("utf-8")

    idedata_path = local_build_path / ".pioenvs" / "kitchen" / "idedata.json"
    idedata_data = json.loads(idedata_path.read_text()) if idedata_path.is_file() else {}

    def _remap(value: object) -> object:
        if isinstance(value, str) and local_build_str in value:
            return value.replace(local_build_str, receiver_build_str)
        return value

    if "prog_path" in idedata_data:
        idedata_data["prog_path"] = _remap(idedata_data["prog_path"])
    extra = idedata_data.get("extra")
    if isinstance(extra, dict):
        for image in extra.get("flash_images", []) or []:
            if isinstance(image, dict) and "path" in image:
                image["path"] = _remap(image["path"])
    idedata_bytes = (json.dumps(idedata_data) + "\n").encode("utf-8")

    members: list[tuple[str, bytes]] = [
        (STORAGE_MEMBER_NAME, storage_bytes),
        (IDEDATA_MEMBER_NAME, idedata_bytes),
        (PLATFORMIO_INI_MEMBER_NAME, (local_build_path / "platformio.ini").read_bytes()),
    ]
    build_info_path = local_build_path / BUILD_INFO_MEMBER_NAME
    if build_info_path.is_file():
        members.append((BUILD_INFO_MEMBER_NAME, build_info_path.read_bytes()))
    # Ship the firmware binary at the path the storage sidecar
    # claims (varies per platform: esp32/esp8266 → firmware.bin,
    # libretiny → firmware.uf2, host → program).
    firmware_bin_path = Path(json.loads(local_storage_path.read_text())["firmware_bin_path"])
    if firmware_bin_path.is_file():
        arcname = str(firmware_bin_path.relative_to(local_build_path))
        members.append((arcname, firmware_bin_path.read_bytes()))

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for arcname, payload in members:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _run_esphome_compile(yaml_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``esphome compile`` and return the captured process."""
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell, test-only invocation
        [sys.executable, "-m", "esphome", "compile", str(yaml_path)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.timeout(300)
def test_remote_local_round_trip_does_not_invalidate_pioenvs_cache() -> None:
    """
    Real ``esphome compile`` → materialise → real ``esphome compile`` recompiles 0 files.

    This is the user-reported regression after PR #874:
    ``local → remote → local`` was rebuilding from scratch on
    every remote→local transition because the materialiser
    bumped ``platformio.ini`` mtime forward, invalidating
    every preserved ``.pioenvs/<name>/*.o`` for SCons.
    """
    workdir = Path(tempfile.mkdtemp(prefix="dbb-slow-"))
    try:
        config_dir = workdir / "config"
        config_dir.mkdir()
        yaml_path = config_dir / "kitchen.yaml"
        yaml_path.write_text(_MINIMAL_YAML)

        first = _run_esphome_compile(yaml_path)
        assert first.returncode == 0, (
            f"local compile #1 failed:\nstdout:\n{first.stdout[-2000:]}\n"
            f"stderr:\n{first.stderr[-2000:]}"
        )

        local_build_path = config_dir / ".esphome" / "build" / "kitchen"
        storage_path = config_dir / ".esphome" / "storage" / "kitchen.yaml.json"
        assert local_build_path.is_dir(), "build dir missing after local compile #1"

        # Build the receiver-form tarball from the freshly-built local tree.
        tarball = _build_synthetic_receiver_tarball(local_build_path, storage_path)

        sentinel = config_dir / "___DASHBOARD_SENTINEL___.yaml"
        with patch.object(CORE, "config_path", sentinel):
            materialise_remote_artifacts(tarball, "kitchen.yaml")

        second = _run_esphome_compile(yaml_path)
        assert second.returncode == 0, (
            f"local compile #2 failed:\nstdout:\n{second.stdout[-2000:]}\n"
            f"stderr:\n{second.stderr[-2000:]}"
        )

        # SCons prints "Compiling <obj>" for every object it
        # rebuilds. Zero "Compiling " lines on the second run is
        # the load-bearing assertion.
        recompiled = [line for line in second.stdout.splitlines() if line.startswith("Compiling ")]
        assert recompiled == [], (
            f"local compile #2 recompiled {len(recompiled)} object(s) — "
            f"PR #874's preservation broke. First few:\n  " + "\n  ".join(recompiled[:5])
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
