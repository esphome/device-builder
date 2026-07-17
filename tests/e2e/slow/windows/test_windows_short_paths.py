"""
Pins the Windows build-data relocation against a real native ESP-IDF toolchain.

One deep + spaced ESP-IDF compile (shared via a module-scoped fixture) lands its artifacts under
the relocated root, proving MAX_PATH + ESP-IDF's no-spaces install requirement are both
neutralised. Three separately-reported tests then assert that the compile, ``esphome clean``, and
``esphome clean-all`` all target the *relocated* dirs (``ESPHOME_DATA_DIR`` build tree +
``ESPHOME_ESP_IDF_PREFIX`` toolchain), never the original config dir.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from esphome_device_builder.controllers.firmware.cli import compose_subprocess_env
from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths
from esphome_device_builder.models import FirmwareJob, JobType

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows MAX_PATH only")

_MAX_PATH = 260
_NAME = "maxpath-probe-esp32-idf"

# Deliberately long AND space-bearing config dir: proves the relocation handles both the MAX_PATH
# overflow and ESP-IDF's no-spaces install requirement in a single real compile.
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


class _Relocated(NamedTuple):
    config_dir: Path
    config: Path
    root: Path  # relocated ESPHOME_DATA_DIR
    idf: Path  # relocated ESPHOME_ESP_IDF_PREFIX
    env: dict[str, str]


@pytest.fixture(scope="module")
def relocated_compile(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Relocated]:
    """Relocate, compile a deep + spaced ESP-IDF config once, and share the result module-wide."""
    config_dir = tmp_path_factory.mktemp("win") / _PAD / _PROFILE / "esphome"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "probe.yaml"
    config.write_text(_CONFIG, encoding="utf-8")
    assert " " in str(config_dir)  # the case the relocation must neutralize

    prev_data = os.environ.pop("ESPHOME_DATA_DIR", None)
    prev_pio = os.environ.pop("PLATFORMIO_CORE_DIR", None)
    prev_idf = os.environ.pop("ESPHOME_ESP_IDF_PREFIX", None)
    try:
        with windows_short_build_paths(config_dir):
            root = Path(os.environ["ESPHOME_DATA_DIR"])
            idf = Path(os.environ["ESPHOME_ESP_IDF_PREFIX"])
            assert " " not in str(root)  # relocated to a short, space-free root
            assert " " not in str(idf)

            # ESPHOME_ESP_IDF_PREFIX flows in through os.environ, so the env carries it without a
            # threaded argument.
            job = FirmwareJob(job_id="probe", configuration="probe.yaml", job_type=JobType.COMPILE)
            env = compose_subprocess_env(job)
            assert env["ESPHOME_ESP_IDF_PREFIX"] == str(idf)

            _run(["compile", str(config)], env, "compile")
            yield _Relocated(config_dir=config_dir, config=config, root=root, idf=idf, env=env)
    finally:
        for name, value in (
            ("ESPHOME_DATA_DIR", prev_data),
            ("PLATFORMIO_CORE_DIR", prev_pio),
            ("ESPHOME_ESP_IDF_PREFIX", prev_idf),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_compile_lands_under_relocated_root(relocated_compile: _Relocated) -> None:
    """The compile's artifacts land under the relocated root, under MAX_PATH, not the config dir."""
    r = relocated_compile
    assert (r.root / "build" / _NAME / "build").is_dir(), "build tree not under relocated root"
    assert not (r.config_dir / ".esphome").exists(), "nothing should build under the config dir"
    assert r.idf.is_dir(), "toolchain not under the relocated ESPHOME_ESP_IDF_PREFIX"
    deepest = _deepest(r.root)
    assert deepest < _MAX_PATH, f"deepest relocated path is {deepest}"


def test_clean_clears_relocated_build_tree(relocated_compile: _Relocated) -> None:
    """``esphome clean`` removes the build tree under the relocated build path."""
    r = relocated_compile
    build_path = r.root / "build" / _NAME
    assert (build_path / "build").is_dir()  # present before clean
    _run(["clean", str(r.config)], r.env, "clean")
    assert not build_path.is_dir()


def test_clean_all_clears_relocated_data_and_toolchain(relocated_compile: _Relocated) -> None:
    """``esphome clean-all`` clears the relocated data dir + toolchain, keeping storage/ + .json."""
    r = relocated_compile
    # A .json sidecar + a storage dir under the root prove clean-all preserves them.
    (r.root / "keep.json").write_text("{}", encoding="utf-8")
    (r.root / "storage").mkdir(exist_ok=True)
    (r.root / "storage" / "probe.json").write_text("{}", encoding="utf-8")
    assert r.idf.is_dir()  # toolchain present (clean leaves it; clean-all removes it)

    _run(["clean-all", str(r.config_dir)], r.env, "clean-all")
    assert not r.idf.is_dir(), "clean-all did not remove the relocated ESPHOME_ESP_IDF_PREFIX"
    assert not (r.root / "build").exists(), "clean-all did not clear the relocated build tree"
    assert (r.root / "keep.json").is_file(), "clean-all must preserve .json files"
    assert (r.root / "storage" / "probe.json").is_file(), "clean-all must preserve storage/"


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
