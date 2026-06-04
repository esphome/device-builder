"""Pins that the Windows relocation compiles a deep, spaced ESP-IDF config under MAX_PATH."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.cli import compose_subprocess_env
from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths
from esphome_device_builder.models import FirmwareJob, JobType

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows MAX_PATH only")

_MAX_PATH = 260

# Deliberately long AND space-bearing config dir: proves the relocation handles both the MAX_PATH
# overflow and the pioarduino whitespace guard / -fdebug-prefix-map in a single real compile.
_PAD = "padding-" * 9  # 72 chars
_PROFILE = "First Last"

_CONFIG = textwrap.dedent(
    """\
    esphome:
      name: maxpath-probe-esp32-idf
    esp32:
      board: esp32dev
      framework:
        type: esp-idf
    logger:
    wifi:
      ssid: "probe-ssid"
      password: "probe-password"
    api:
      encryption:
        key: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    """
)


@pytest.mark.timeout(2400)
def test_windows_relocated_build_compiles_deep_spaced_idf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep + spaced ESP-IDF compile succeeds after relocation and stays under MAX_PATH."""
    config_dir = tmp_path / _PAD / _PROFILE / "esphome"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    assert " " in str(config_dir)  # the case the relocation must neutralize

    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    with windows_short_build_paths(config_dir) as pio_core_dir:
        assert pio_core_dir is not None
        root = Path(os.environ["ESPHOME_DATA_DIR"])
        assert " " not in str(root)  # relocated to a short, space-free root

        # Drive the real subprocess-env composition (a local COMPILE job).
        job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)
        env = compose_subprocess_env(job, pio_core_dir)

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "esphome", "compile", str(config)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            close_fds=False,
        )
        assert result.returncode == 0, (
            f"deep + spaced ESP-IDF compile failed after relocation:\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-2000:]}"
        )

        deepest = _deepest(root)
        assert deepest < _MAX_PATH, f"deepest relocated path is {deepest} (>= {_MAX_PATH})"


def _deepest(root: Path) -> int:
    """Return the longest full file-path string length under *root* (0 if empty)."""
    longest = 0
    for current, _dirs, files in os.walk(root):
        longest = max((longest, *(len(current) + 1 + len(name) for name in files)))
    return longest
