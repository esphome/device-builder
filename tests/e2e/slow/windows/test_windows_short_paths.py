"""Real-compile pin: the Windows short-path layout builds a deep ESP-IDF config under MAX_PATH.

Windows-only. Exercises the production
:func:`helpers.windows_build_paths.apply_windows_short_build_paths` +
:func:`controllers.firmware.cli.compose_subprocess_env` path, then runs a real
``esphome compile`` of an ESP-IDF + ``api: encryption:`` config (the deep mbedtls /
tf-psa-crypto tree that overflows ``MAX_PATH`` on the long default layout). Asserts the build
succeeds, the deepest emitted path stays under 260, and the same path *without* the junction
would exceed 260 -- so the junction is provably load-bearing, not incidental.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.cli import compose_subprocess_env
from esphome_device_builder.helpers.windows_build_paths import (
    apply_windows_short_build_paths,
    remove_windows_short_build_paths,
    windows_pio_core_dir,
)
from esphome_device_builder.models import FirmwareJob, JobType

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows MAX_PATH only")

_MAX_PATH = 260

# Pad the config dir so the *real* (canonical) build paths are long: without the junction the
# deepest object path overflows MAX_PATH; through the junction it stays short.
_PAD = "padding-" * 9  # 72 chars

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
def test_windows_short_paths_compile_deep_idf(tmp_path: Path) -> None:
    """A deep ESP-IDF compile succeeds through the short-path layout and stays under MAX_PATH."""
    config_dir = tmp_path / _PAD / "esphome"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    saved = {k: os.environ.get(k) for k in ("ESPHOME_DATA_DIR", "PLATFORMIO_CORE_DIR")}
    os.environ.pop("ESPHOME_DATA_DIR", None)
    try:
        apply_windows_short_build_paths(config_dir)
        junction = Path(os.environ["ESPHOME_DATA_DIR"])
        real_data = config_dir / ".esphome"
        assert windows_pio_core_dir() is not None, "PLATFORMIO_CORE_DIR not set up"

        # Drive the real subprocess-env composition (a local COMPILE job).
        job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)
        env = compose_subprocess_env(job)

        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "esphome", "compile", str(config)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            close_fds=False,
        )
        assert result.returncode == 0, (
            f"deep ESP-IDF compile failed under the short-path layout:\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-2000:]}"
        )

        deepest_junction = _deepest(junction)
        deepest_canonical = deepest_junction - len(str(junction)) + len(str(real_data))
        assert deepest_junction < _MAX_PATH, (
            f"deepest path through the junction is {deepest_junction} (>= {_MAX_PATH})"
        )
        # The fix is load-bearing: the same tree at the real (un-junctioned) path overflows.
        assert deepest_canonical > _MAX_PATH, (
            f"control check weak: canonical depth {deepest_canonical} did not exceed "
            f"{_MAX_PATH}; the test no longer proves the junction is required"
        )
    finally:
        remove_windows_short_build_paths()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _deepest(root: Path) -> int:
    """Return the longest full file-path string length under *root* (0 if empty)."""
    longest = 0
    for current, _dirs, files in os.walk(root):
        longest = max((longest, *(len(current) + 1 + len(name) for name in files)))
    return longest
