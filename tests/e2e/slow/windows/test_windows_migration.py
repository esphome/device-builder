r"""
Real-compile e2e: a compiled tree survives migration from the original layout across a restart.

Windows-only. First compiles a small esp8266 config with **no relocation** -- the original
never-relocated layout, build data under ``<config>/.esphome`` and the toolchain under
``~/.platformio``. Then it runs the relocation (a restart) so that tree migrates straight to the
nested ``C:\esphb\<id8>`` and recompiles, proving a real toolchain + build tree survive the
same-volume move to a new absolute path and still build. esp8266 keeps the compile fast (vs the
deep ESP-IDF MAX_PATH case in ``test_windows_short_paths``) while still moving a real xtensa
toolchain, unlike a host/native build. A short fixed config dir keeps the *original* (un-relocated)
build under MAX_PATH so step one can succeed before relocation is in play.
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
def test_compiled_tree_survives_migration_from_original_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An esp8266 build compiled in the original layout keeps building after migration to nested."""
    config_dir = Path("C:\\mig-cfg")  # short, so the un-relocated build stays under MAX_PATH
    old_esphome = config_dir / ".esphome"
    home_pio = Path.home() / ".platformio"
    shutil.rmtree(config_dir, ignore_errors=True)  # clear any leftover from a prior run
    config_dir.mkdir(parents=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    suffix = wbp._safe_suffix(get_or_create_dashboard_id(config_dir))
    nested = Path("C:\\esphb") / suffix
    job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)

    try:
        # Step 1: original layout -- no relocation. Build data -> <config>/.esphome, toolchain ->
        # ~/.platformio (the un-relocated state a never-updated user is in).
        monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
        monkeypatch.delenv("PLATFORMIO_CORE_DIR", raising=False)
        _compile(config, compose_subprocess_env(job), "original compile")
        assert (old_esphome / "build" / _NAME).is_dir()
        assert home_pio.is_dir()

        # Step 2 (restart): relocation migrates the original tree straight to the nested root (no
        # flat esphb-<id8> intermediate); the recompile at the new absolute path must still work.
        with windows_short_build_paths(config_dir):
            assert os.environ["ESPHOME_DATA_DIR"] == str(nested)
            assert os.environ["PLATFORMIO_CORE_DIR"] == str(nested / "pio")
            assert not old_esphome.exists()  # original build data migrated in
            assert not home_pio.exists()  # original toolchain migrated in
            assert (nested / "build" / _NAME).is_dir()
            _compile(config, compose_subprocess_env(job), "recompile after migration")
    finally:
        # Throwaway runner, but keep it tidy so reruns start clean.
        shutil.rmtree(config_dir, ignore_errors=True)
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
