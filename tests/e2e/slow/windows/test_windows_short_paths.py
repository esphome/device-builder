"""
Pins the Windows build-data relocation against a real ESP-IDF toolchain.

One deep + spaced ESP-IDF compile lands its artifacts under the relocated root (proving MAX_PATH
+ the pioarduino whitespace guard are both neutralised), then ``esphome clean`` and
``esphome clean-all`` are run against that same tree to prove they target the *relocated* dirs
(``ESPHOME_DATA_DIR`` build tree + ``PLATFORMIO_CORE_DIR`` toolchain), not the original config dir.
"""

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
_NAME = "maxpath-probe-esp32-idf"

# Deliberately long AND space-bearing config dir: proves the relocation handles both the MAX_PATH
# overflow and the pioarduino whitespace guard / -fdebug-prefix-map in a single real compile.
_PAD = "padding-" * 9  # 72 chars
_PROFILE = "First Last"

_CONFIG = textwrap.dedent(
    f"""\
    esphome:
      name: {_NAME}
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


@pytest.mark.timeout(3000)
def test_relocated_build_compiles_then_clean_and_clean_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compile under MAX_PATH, then clean / clean-all empirically clear the relocated dirs."""
    config_dir = tmp_path / _PAD / _PROFILE / "esphome"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    assert " " in str(config_dir)  # the case the relocation must neutralize

    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    monkeypatch.delenv("PLATFORMIO_CORE_DIR", raising=False)
    with windows_short_build_paths(config_dir):
        root = Path(os.environ["ESPHOME_DATA_DIR"])
        pio = Path(os.environ["PLATFORMIO_CORE_DIR"])
        assert " " not in str(root)  # relocated to a short, space-free root
        assert " " not in str(pio)

        # PLATFORMIO_CORE_DIR flows in through os.environ, so the env carries it without a
        # threaded argument.
        job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)
        env = compose_subprocess_env(job)
        assert env["PLATFORMIO_CORE_DIR"] == str(pio)

        # 1) Compile: artifacts must land under the relocated root, never the spaced config dir.
        _run(["compile", str(config)], env, "compile")
        build_path = root / "build" / _NAME
        pioenvs = build_path / ".pioenvs"
        assert pioenvs.is_dir(), "build tree not under the relocated root"
        assert not (config_dir / ".esphome").exists(), "nothing should build under the config dir"
        assert pio.is_dir(), "toolchain not under the relocated PLATFORMIO_CORE_DIR"
        assert _deepest(root) < _MAX_PATH, f"deepest relocated path is {_deepest(root)}"

        # A .json sidecar + a storage dir under the root prove clean-all preserves them.
        (root / "keep.json").write_text("{}", encoding="utf-8")
        (root / "storage").mkdir(exist_ok=True)
        (root / "storage" / "probe.json").write_text("{}", encoding="utf-8")

        # 2) esphome clean: the build trees under the relocated build path go away.
        _run(["clean", str(config)], env, "clean")
        assert not pioenvs.is_dir()
        assert not (build_path / ".piolibdeps").is_dir()
        assert not (build_path / "build").is_dir()

        # 3) esphome clean-all: the relocated data dir is cleared (json + storage kept) and the
        # relocated PlatformIO toolchain dir is removed.
        assert pio.is_dir()  # still present after a plain clean
        _run(["clean-all", str(config_dir)], env, "clean-all")
        assert not pio.is_dir(), "clean-all did not remove the relocated PLATFORMIO_CORE_DIR"
        assert not build_path.exists(), "clean-all did not clear the relocated build tree"
        assert (root / "keep.json").is_file(), "clean-all must preserve .json files"
        assert (root / "storage" / "probe.json").is_file(), "clean-all must preserve storage/"


def _run(esphome_args: list[str], env: dict[str, str], label: str) -> None:
    """Run an ``esphome`` subcommand under *env*; fail with captured output on non-zero exit."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "esphome", *esphome_args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
    )
    assert result.returncode == 0, (
        f"esphome {label} failed after relocation:\n"
        f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-2000:]}"
    )


def _deepest(root: Path) -> int:
    """Return the longest full file-path string length under *root* (0 if empty)."""
    longest = 0
    for current, _dirs, files in os.walk(root):
        longest = max((longest, *(len(current) + 1 + len(name) for name in files)))
    return longest
