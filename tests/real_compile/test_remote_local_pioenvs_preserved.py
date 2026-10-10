"""Real-compile pin: a remote-build round-trip must not invalidate the per-object cache."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build.artifacts_tarball import (
    pack_build_artifacts,
)
from esphome_device_builder.helpers.remote_artifacts_materialise import (
    materialise_remote_artifacts,
)

_MINIMAL_YAML = """\
esphome:
  name: kitchen
esp8266:
  board: esp01_1m
  toolchain: {toolchain}
"""

# One compile line per object: SCons prints ``Compiling <path>.o`` (PlatformIO;
# esphome's own "Compiling with ccache" notes must not count), ninja prints
# ``[n/m] CXX ...`` (native Arduino).
_COMPILE_LINE = {
    "platformio": re.compile(r"Compiling \S+\.o\b"),
    "arduino": re.compile(r"^\[\d+/\d+\] (CC|CXX|AS) "),
}

# Conservative floor against the "no .o files at all" failure mode
# (locally observed: 106 .o on a minimal esp01_1m). If esphome ever
# ships a base smaller than this, *lower* the floor; raising would
# convert a real-world simplification into a misleading test failure.
_MIN_EXPECTED_OBJECT_FILES = 30


def _run_esphome_compile(yaml_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``esphome compile`` on *yaml_path* and return the captured process."""
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell, test-only invocation
        [sys.executable, "-m", "esphome", "compile", str(yaml_path)],
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
    )


def _snapshot_object_ns_mtimes(pioenvs: Path) -> dict[Path, int]:
    """Return ``{relative-path: st_mtime_ns}`` for every ``*.o`` under *pioenvs*."""
    return {p.relative_to(pioenvs): p.stat().st_mtime_ns for p in pioenvs.rglob("*.o")}


def _compiling_lines(stdout: str, toolchain: str) -> list[str]:
    r"""Return the per-object compile log lines from *stdout*.

    PlatformIO emits an ANSI reset (``\x1b[0m``) at the start of
    each line on Linux even off a TTY, so its pattern is searched,
    not anchored.
    """
    pattern = _COMPILE_LINE[toolchain]
    return [line for line in stdout.splitlines() if pattern.search(line)]


@pytest.mark.timeout(600)
@pytest.mark.parametrize("toolchain", ["platformio", "arduino"])
def test_remote_local_round_trip_does_not_invalidate_pioenvs_cache(
    tmp_path: Path, toolchain: str
) -> None:
    """A pack → materialise round-trip leaves the offloader's per-object cache valid."""
    yaml_text = _MINIMAL_YAML.format(toolchain=toolchain)
    receiver_dir = tmp_path / "receiver"
    receiver_dir.mkdir()
    receiver_yaml = receiver_dir / "kitchen.yaml"
    receiver_yaml.write_text(yaml_text)

    first = _run_esphome_compile(receiver_yaml)
    assert first.returncode == 0, (
        f"receiver compile failed:\nstdout:\n{first.stdout[-4000:]}\n"
        f"stderr:\n{first.stderr[-4000:]}"
    )

    receiver_pioenvs = receiver_dir / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    assert (receiver_pioenvs / "firmware.bin").is_file(), (
        f"firmware.bin missing after receiver compile.\n"
        f"Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
    )
    receiver_object_count = sum(1 for _ in receiver_pioenvs.rglob("*.o"))
    assert receiver_object_count >= _MIN_EXPECTED_OBJECT_FILES, (
        f"receiver compile only produced {receiver_object_count} object files "
        f"(expected >= {_MIN_EXPECTED_OBJECT_FILES}); compile likely bailed early.\n"
        f"Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
    )
    first_compiling = _compiling_lines(first.stdout, toolchain)
    assert len(first_compiling) >= _MIN_EXPECTED_OBJECT_FILES, (
        f"receiver compile only printed {len(first_compiling)} 'Compiling ...' "
        f"lines (expected >= {_MIN_EXPECTED_OBJECT_FILES}).\n"
        f"Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
    )

    receiver_sentinel = receiver_dir / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", receiver_sentinel):
        packed = pack_build_artifacts("kitchen.yaml")

    offloader_dir = tmp_path / "offloader"
    offloader_dir.mkdir()
    offloader_yaml = offloader_dir / "kitchen.yaml"
    offloader_yaml.write_text(yaml_text)

    cold_local = _run_esphome_compile(offloader_yaml)
    assert cold_local.returncode == 0, (
        f"offloader cold compile failed:\nstdout:\n{cold_local.stdout[-4000:]}\n"
        f"stderr:\n{cold_local.stderr[-4000:]}"
    )

    offloader_pioenvs = offloader_dir / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
    assert (offloader_pioenvs / "firmware.bin").is_file()
    first_objects_ns = _snapshot_object_ns_mtimes(offloader_pioenvs)
    assert len(first_objects_ns) >= _MIN_EXPECTED_OBJECT_FILES

    offloader_sentinel = offloader_dir / "___DASHBOARD_SENTINEL___.yaml"
    with patch.object(CORE, "config_path", offloader_sentinel):
        materialise_remote_artifacts(packed.tarball, "kitchen.yaml")

    warm_local = _run_esphome_compile(offloader_yaml)
    assert warm_local.returncode == 0, (
        f"offloader warm compile failed:\nstdout:\n{warm_local.stdout[-4000:]}\n"
        f"stderr:\n{warm_local.stderr[-4000:]}"
    )

    recompiled = _compiling_lines(warm_local.stdout, toolchain)
    assert recompiled == [], (
        f"warm compile recompiled {len(recompiled)} object(s). "
        f"First few:\n  " + "\n  ".join(recompiled[:5])
    )

    # mtime cross-check catches a partial rebuild the log scrape would miss
    # (e.g. a build tool changes its log format).
    second_objects_ns = _snapshot_object_ns_mtimes(offloader_pioenvs)
    missing = sorted(set(first_objects_ns) - set(second_objects_ns))
    assert not missing, f"warm compile dropped {len(missing)} object(s): {missing[:5]}"
    bumped = sorted(
        obj for obj, mtime_ns in first_objects_ns.items() if second_objects_ns[obj] != mtime_ns
    )
    assert not bumped, f"warm compile rebuilt {len(bumped)} object(s): {bumped[:5]}"
