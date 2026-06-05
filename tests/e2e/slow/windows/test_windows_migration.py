r"""
Real-compile e2e: a compiled tree survives the build-root migration across a restart.

Windows-only. Compiles a small esp8266 config (fast) into the legacy flat ``C:\esphb-<id8>`` layout
of the first relocation release, then runs the relocation again so it migrates to the nested
``C:\esphb\<id8>`` and recompiles. This proves a real toolchain + build tree survive the same-volume
move to a new absolute path and still build incrementally. The deep ESP-IDF MAX_PATH case is covered
separately by ``test_windows_short_paths``; here esp8266 keeps the compile fast while still moving a
real toolchain (its ``pio`` holds the xtensa toolchain, unlike a host/native build).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.cli import compose_subprocess_env
from esphome_device_builder.helpers import windows_build_paths as wbp
from esphome_device_builder.helpers.dashboard_identity import get_or_create_dashboard_id
from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths
from esphome_device_builder.models import FirmwareJob, JobType

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows relocation only")

_NAME = "migration-probe-esp8266"

_CONFIG = textwrap.dedent(
    f"""\
    esphome:
      name: {_NAME}
    esp8266:
      board: d1_mini
    logger:
      baud_rate: 0
    """
)


@pytest.mark.timeout(1800)
def test_compiled_tree_survives_migration_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real build compiled at the legacy flat root keeps building after migration to nested."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    monkeypatch.delenv("PLATFORMIO_CORE_DIR", raising=False)

    suffix = wbp._safe_suffix(get_or_create_dashboard_id(config_dir))
    legacy = Path("C:\\") / f"esphb-{suffix}"  # first-relocation flat layout
    nested = Path("C:\\esphb") / suffix  # current nested layout
    job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)

    try:
        # Run 1: simulate a first-release user -- compile into the flat legacy root, then seed the
        # completion markers the first-release code wrote.
        monkeypatch.setenv("ESPHOME_DATA_DIR", str(legacy))
        monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(legacy / "pio"))
        (legacy / "pio").mkdir(parents=True, exist_ok=True)
        _compile(config, compose_subprocess_env(job), "legacy compile")
        (legacy / wbp._RELOCATED_MARKER).write_text("{}", encoding="utf-8")
        (legacy / "pio" / wbp._RELOCATED_MARKER).write_text("{}", encoding="utf-8")
        assert (legacy / "build" / _NAME).is_dir()
        monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
        monkeypatch.delenv("PLATFORMIO_CORE_DIR", raising=False)

        # Run 2 (restart): relocation migrates flat -> nested; the recompile at the new absolute
        # path must still succeed (build tree + toolchain survived the move).
        with windows_short_build_paths(config_dir):
            assert os.environ["ESPHOME_DATA_DIR"] == str(nested)
            assert os.environ["PLATFORMIO_CORE_DIR"] == str(nested / "pio")
            assert not legacy.exists()  # migrated away, not left behind
            assert (nested / "build" / _NAME).is_dir()  # build tree moved into the nested root
            _compile(config, compose_subprocess_env(job), "recompile after migration")
    finally:
        # Throwaway runner, but keep it tidy so reruns start clean.
        shutil.rmtree(legacy, ignore_errors=True)
        shutil.rmtree(nested, ignore_errors=True)


def _compile(config: Path, env: dict[str, str], label: str) -> None:
    """Run ``esphome compile`` under *env*; fail with captured output on non-zero exit."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "esphome", "compile", str(config)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
    )
    assert result.returncode == 0, (
        f"esphome {label} failed:\n"
        f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
