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

This test runs a real esphome compile, packs the result through
the production receiver-side packer, materialises it back into
the local config_dir, runs another real esphome compile, and
asserts SCons recompiles **zero** files.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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
"""

# Empirical floor. A minimal esp01_1m build with ``esphome:`` +
# ``esp8266:`` lands ~100 .o files (locally observed: 106).
# Anything below 30 almost certainly means the compile bailed
# before SCons did real work — fail loudly rather than let the
# "0 recompiled" assertion pass on an empty tree.
_MIN_EXPECTED_OBJECT_FILES = 30


def _run_esphome_compile(yaml_path: Path) -> subprocess.CompletedProcess[str]:
    """Run ``esphome compile`` on *yaml_path* and return the captured process."""
    return subprocess.run(  # noqa: S603 — fixed argv list, no shell, test-only invocation
        [sys.executable, "-m", "esphome", "compile", str(yaml_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _snapshot_object_ns_mtimes(pioenvs: Path) -> dict[Path, int]:
    """Return ``{relative-path: st_mtime_ns}`` for every ``*.o`` under *pioenvs*.

    Nanosecond precision so the cross-compile mtime equality
    check is exact regardless of filesystem timestamp resolution.
    """
    return {p.relative_to(pioenvs): p.stat().st_mtime_ns for p in pioenvs.rglob("*.o")}


def _compiling_lines(stdout: str) -> list[str]:
    r"""Return SCons ``Compiling ...`` log lines from *stdout*.

    PlatformIO emits ANSI colour resets (``\x1b[0m``) at the
    start of each line on Linux even when stdout isn't a TTY, so
    a literal ``startswith("Compiling ")`` misses every line —
    match the substring instead.
    """
    return [line for line in stdout.splitlines() if "Compiling " in line]


@pytest.mark.timeout(600)
def test_remote_local_round_trip_does_not_invalidate_pioenvs_cache() -> None:
    """
    Real ``esphome compile`` → materialise → real ``esphome compile`` recompiles 0 files.

    Sanity-checks that the first compile actually produced a
    full object set (otherwise the "0 recompiled" assertion
    would pass trivially on an empty tree). Snapshots every
    ``.o`` mtime after the first compile and asserts every one
    survives the second compile unchanged so a partial rebuild
    can't hide behind the ``Compiling ...`` log scrape.
    """
    workdir = Path(tempfile.mkdtemp(prefix="dbb-real-compile-"))
    try:
        # Set up a "receiver" config_dir, compile there, then pack the
        # result through the production packer. This pins build_path to
        # the receiver's tree so the materialiser exercises the same
        # remap path it does in production (rather than a
        # local==offloader shortcut).
        receiver_dir = workdir / "receiver"
        receiver_dir.mkdir()
        receiver_yaml = receiver_dir / "kitchen.yaml"
        receiver_yaml.write_text(_MINIMAL_YAML)

        first = _run_esphome_compile(receiver_yaml)
        assert first.returncode == 0, (
            f"receiver compile failed:\nstdout:\n{first.stdout[-4000:]}\n"
            f"stderr:\n{first.stderr[-4000:]}"
        )

        receiver_build_path = receiver_dir / ".esphome" / "build" / "kitchen"
        receiver_pioenvs = receiver_build_path / ".pioenvs" / "kitchen"
        assert (receiver_pioenvs / "firmware.bin").is_file(), (
            f"firmware.bin missing after receiver compile — the compile didn't run. "
            f"Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
        )
        receiver_object_count = sum(1 for _ in receiver_pioenvs.rglob("*.o"))
        assert receiver_object_count >= _MIN_EXPECTED_OBJECT_FILES, (
            f"receiver compile only produced {receiver_object_count} object files "
            f"(expected >= {_MIN_EXPECTED_OBJECT_FILES}); compile likely bailed "
            f"early. Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
        )
        first_compiling = _compiling_lines(first.stdout)
        assert len(first_compiling) >= _MIN_EXPECTED_OBJECT_FILES, (
            f"receiver compile only printed {len(first_compiling)} 'Compiling ...' "
            f"lines (expected >= {_MIN_EXPECTED_OBJECT_FILES}). "
            f"Last 2000 chars of stdout:\n{first.stdout[-2000:]}"
        )

        # Pack the receiver's build through production.
        receiver_sentinel = receiver_dir / "___DASHBOARD_SENTINEL___.yaml"
        with patch.object(CORE, "config_path", receiver_sentinel):
            packed = pack_build_artifacts("kitchen.yaml")

        # Now act as the offloader: separate config_dir, run a local
        # compile to populate .pioenvs, then materialise the receiver
        # tarball on top.
        offloader_dir = workdir / "offloader"
        offloader_dir.mkdir()
        offloader_yaml = offloader_dir / "kitchen.yaml"
        offloader_yaml.write_text(_MINIMAL_YAML)

        cold_local = _run_esphome_compile(offloader_yaml)
        assert cold_local.returncode == 0, (
            f"offloader cold compile failed:\nstdout:\n{cold_local.stdout[-4000:]}\n"
            f"stderr:\n{cold_local.stderr[-4000:]}"
        )

        offloader_pioenvs = (
            offloader_dir / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen"
        )
        assert (offloader_pioenvs / "firmware.bin").is_file(), (
            "firmware.bin missing after offloader cold compile."
        )
        # Pin the offloader's per-object cache state before the
        # remote-build step so we can prove materialise + the next
        # local compile didn't invalidate it. Snapshot in
        # nanoseconds so the post-compile equality check is exact
        # regardless of filesystem timestamp resolution.
        first_objects_ns = _snapshot_object_ns_mtimes(offloader_pioenvs)
        assert len(first_objects_ns) >= _MIN_EXPECTED_OBJECT_FILES, (
            f"offloader cold compile produced only {len(first_objects_ns)} object "
            f"files (expected >= {_MIN_EXPECTED_OBJECT_FILES})."
        )

        # Materialise the receiver tarball on top of the offloader's
        # tree — the production round-trip the user reported breaking.
        offloader_sentinel = offloader_dir / "___DASHBOARD_SENTINEL___.yaml"
        with patch.object(CORE, "config_path", offloader_sentinel):
            materialise_remote_artifacts(packed.tarball, "kitchen.yaml")

        warm_local = _run_esphome_compile(offloader_yaml)
        assert warm_local.returncode == 0, (
            f"offloader warm compile failed:\nstdout:\n{warm_local.stdout[-4000:]}\n"
            f"stderr:\n{warm_local.stderr[-4000:]}"
        )

        # Load-bearing assertions: SCons prints "Compiling <obj>" for
        # every object it rebuilds. Pre-fix this was 100+.
        recompiled = _compiling_lines(warm_local.stdout)
        assert recompiled == [], (
            f"local compile after materialise recompiled {len(recompiled)} "
            f"object(s) — the round-trip invalidated SCons's cache. "
            f"First few:\n  " + "\n  ".join(recompiled[:5])
        )

        # Cross-check: every .o snapshot before materialise still
        # exists with the exact same mtime afterwards. Catches a
        # partial rebuild the log scrape could miss (e.g. SCons
        # changes its log format) and pins that nothing got
        # deleted either. Nanosecond comparison so the equality is
        # exact across FS timestamp resolutions.
        second_objects_ns = _snapshot_object_ns_mtimes(offloader_pioenvs)
        missing = sorted(set(first_objects_ns) - set(second_objects_ns))
        assert not missing, (
            f"warm compile dropped {len(missing)} object file(s) the cold "
            f"compile had built. First few: {missing[:5]}"
        )
        bumped = sorted(
            obj for obj, mtime_ns in first_objects_ns.items() if second_objects_ns[obj] != mtime_ns
        )
        assert not bumped, (
            f"warm compile rebuilt {len(bumped)} object file(s) — the cache "
            f"was invalidated despite zero 'Compiling' log lines. "
            f"First few: {bumped[:5]}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
