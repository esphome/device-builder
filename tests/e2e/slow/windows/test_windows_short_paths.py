"""Pins that the Windows short-path layout compiles a deep ESP-IDF config under MAX_PATH."""

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

# Pad the config dir so the *real* (canonical) build paths are long: without the junction the
# deepest object path overflows MAX_PATH; through the junction it stays short.
#
# No space in the path: junction creation handles spaces (native _winapi API, pinned by a unit
# test), but ESP-IDF passes the project dir unquoted into ``-fdebug-prefix-map``, so a space
# anywhere in the path breaks the *compile* regardless of the junction — an upstream issue
# orthogonal to MAX_PATH. A full-compile e2e therefore can't carry a space and stay green.
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
def test_windows_short_paths_compile_deep_idf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deep ESP-IDF compile succeeds through the short-path layout and stays under MAX_PATH."""
    config_dir = tmp_path / _PAD / "esphome"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    with windows_short_build_paths(config_dir) as pio_core_dir:
        junction = Path(os.environ["ESPHOME_DATA_DIR"])
        real_data = config_dir / ".esphome"
        assert pio_core_dir is not None, "PLATFORMIO_CORE_DIR not set up"

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


def _deepest(root: Path) -> int:
    """Return the longest full file-path string length under *root* (0 if empty)."""
    longest = 0
    for current, _dirs, files in os.walk(root):
        longest = max((longest, *(len(current) + 1 + len(name) for name in files)))
    return longest
