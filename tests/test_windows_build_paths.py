"""Unit contract for the Windows build-data relocation helper.

The off-Windows no-op runs everywhere. The Windows branch is driven off Windows by faking the
platform gate, the root base, and the toolchain source dir, so relocation / migration /
idempotence / env restore / fallback get fast-matrix coverage without a Windows runner.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from esphome_device_builder.helpers import windows_build_paths as wbp
from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths

_ID = "abcd1234wxyz"  # dashboard_id stand-in; [:8] -> "abcd1234"
_ID8 = "abcd1234"


@pytest.mark.skipif(sys.platform == "win32", reason="pins the off-Windows no-op contract")
def test_context_manager_is_noop_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows the context manager touches nothing and yields ``None``."""
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    with windows_short_build_paths(tmp_path) as pio:
        assert pio is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ


@pytest.fixture
def fake_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drive the Windows branch off Windows; return the (real, space-free) root base."""
    root_base = tmp_path / "drive"
    root_base.mkdir()
    monkeypatch.setattr(wbp, "_is_windows", lambda: True)
    monkeypatch.setattr(wbp, "_ROOT_BASE", root_base)
    monkeypatch.setattr(wbp, "get_or_create_dashboard_id", lambda _config_dir: _ID)
    monkeypatch.setattr(wbp, "_platformio_dir", lambda: tmp_path / "home_platformio")
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    return root_base


def test_relocates_env_to_short_root_and_restores(tmp_path: Path, fake_windows: Path) -> None:
    """Inside the block ESPHOME_DATA_DIR is the short root and the pio dir is yielded."""
    config_dir = tmp_path / "First Last" / "esphome"
    root = fake_windows / f"esphb-{_ID8}"
    with windows_short_build_paths(config_dir) as pio:
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)
        assert pio == root / "pio"
        assert root.is_dir()
        assert pio.is_dir()
    assert "ESPHOME_DATA_DIR" not in os.environ


def test_restores_prior_env_var(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing ESPHOME_DATA_DIR is restored verbatim on exit."""
    monkeypatch.setenv("ESPHOME_DATA_DIR", str(tmp_path / "prior"))
    with windows_short_build_paths(tmp_path / "First Last" / "esphome") as pio:
        assert pio is not None
        assert os.environ["ESPHOME_DATA_DIR"] != str(tmp_path / "prior")
    assert os.environ["ESPHOME_DATA_DIR"] == str(tmp_path / "prior")


def test_platformio_dir_defaults_under_home() -> None:
    """The toolchain-source seam points at ~/.platformio by default."""
    assert wbp._platformio_dir() == Path.home() / ".platformio"


def test_migrates_existing_data_and_toolchain(tmp_path: Path, fake_windows: Path) -> None:
    """Existing ``<config>/.esphome`` and ``~/.platformio`` are moved into the root once."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    (config_dir / ".esphome" / "marker.txt").write_text("data", encoding="utf-8")
    home_pio = tmp_path / "home_platformio"
    home_pio.mkdir()
    (home_pio / "tool.txt").write_text("toolchain", encoding="utf-8")

    root = fake_windows / f"esphb-{_ID8}"
    with windows_short_build_paths(config_dir) as pio:
        assert (root / "marker.txt").read_text(encoding="utf-8") == "data"
        assert (pio / "tool.txt").read_text(encoding="utf-8") == "toolchain"
    assert not (config_dir / ".esphome").exists()
    assert not home_pio.exists()


def test_second_run_reuses_root_without_remigrating(tmp_path: Path, fake_windows: Path) -> None:
    """Once the root exists, a later run reuses it and does not move freshly-written data in."""
    config_dir = tmp_path / "First Last" / "esphome"
    config_dir.mkdir(parents=True)
    with windows_short_build_paths(config_dir):
        pass

    # New data written to the old location after the first relocation must NOT be swept in.
    (config_dir / ".esphome").mkdir(parents=True, exist_ok=True)
    (config_dir / ".esphome" / "new.txt").write_text("x", encoding="utf-8")
    root = fake_windows / f"esphb-{_ID8}"
    with windows_short_build_paths(config_dir):
        pass
    assert (config_dir / ".esphome" / "new.txt").exists()
    assert not (root / "new.txt").exists()


def test_migration_failure_falls_back_and_leaves_env(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If relocation raises, the block yields ``None`` and never sets the override."""

    def _boom(*_args: object) -> None:
        msg = "denied"
        raise OSError(msg)

    monkeypatch.setattr(wbp, "_migrate", _boom)
    with windows_short_build_paths(tmp_path / "First Last" / "esphome") as pio:
        assert pio is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ
