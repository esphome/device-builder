"""
Unit contract for the Windows build-data relocation helper.

The off-Windows no-op runs everywhere. The Windows branch is driven off Windows by faking the
platform gate, the root base, and the toolchain source dir, so relocation / migration /
idempotence / env restore / fallback get fast-matrix coverage without a Windows runner.
"""

from __future__ import annotations

import os
import shutil
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
    with windows_short_build_paths(tmp_path) as ret:
        assert ret is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ


@pytest.fixture
def fake_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Drive the Windows branch off Windows; return the (real, space-free) drive base.

    ``_ROOT_BASE`` is the nested parent (``<drive>/esphb``) and ``_LEGACY_ROOT_BASE`` the flat
    drive root, so new roots are ``<drive>/esphb/<id8>`` and legacy ones ``<drive>/esphb-<id8>``.
    """
    root_base = tmp_path / "drive"
    root_base.mkdir()
    monkeypatch.setattr(wbp, "_is_windows", lambda: True)
    monkeypatch.setattr(wbp, "_ROOT_BASE", root_base / "esphb")
    monkeypatch.setattr(wbp, "_LEGACY_ROOT_BASE", root_base)
    monkeypatch.setattr(wbp, "get_or_create_dashboard_id", lambda _config_dir: _ID)
    monkeypatch.setattr(wbp, "_platformio_dir", lambda: tmp_path / "home_platformio")
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    monkeypatch.delenv("PLATFORMIO_CORE_DIR", raising=False)
    return root_base


def test_relocates_env_to_short_root_and_restores(tmp_path: Path, fake_windows: Path) -> None:
    """Inside the block both env vars point at the short root; both are cleared on exit."""
    config_dir = tmp_path / "First Last" / "esphome"
    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)
        assert os.environ["PLATFORMIO_CORE_DIR"] == str(root / "pio")
        assert root.is_dir()
        assert (root / "pio").is_dir()
    assert "ESPHOME_DATA_DIR" not in os.environ
    assert "PLATFORMIO_CORE_DIR" not in os.environ


def test_root_uses_first_8_chars_of_dashboard_id(tmp_path: Path, fake_windows: Path) -> None:
    """The root is the esphb parent plus exactly the first 8 chars of the dashboard_id."""
    assert len(_ID) > 8, "stub id must exceed 8 chars to prove truncation"
    with windows_short_build_paths(tmp_path / "cfg"):
        root = Path(os.environ["ESPHOME_DATA_DIR"])
        assert root.name == _ID[:8]
        assert root.parent.name == "esphb"


def test_corrupt_dashboard_id_cannot_escape_the_root_segment(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-corrupted id with separators / a drive prefix is sanitized to one safe segment."""
    monkeypatch.setattr(wbp, "get_or_create_dashboard_id", lambda _config_dir: "..\\C:/evil")
    with windows_short_build_paths(tmp_path / "cfg"):
        root = Path(os.environ["ESPHOME_DATA_DIR"])
        assert root.parent == fake_windows / "esphb"  # stays under the esphb parent, no traversal
        assert root.name == "Cevil"  # separators, dots, colon stripped; safe chars kept


def test_fully_corrupt_dashboard_id_falls_back_to_noop(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""An id sanitizing to empty must not collapse the root onto the shared C:\esphb parent."""
    monkeypatch.setattr(wbp, "get_or_create_dashboard_id", lambda _config_dir: "../..")
    with windows_short_build_paths(tmp_path / "cfg"):
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert not (fake_windows / "esphb").exists()


def test_skips_relocation_when_user_set_data_dir(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-set ESPHOME_DATA_DIR is a deliberate choice: relocation is skipped and it stands."""
    monkeypatch.setenv("ESPHOME_DATA_DIR", str(tmp_path / "chosen"))
    with windows_short_build_paths(tmp_path / "First Last" / "esphome"):
        assert os.environ["ESPHOME_DATA_DIR"] == str(tmp_path / "chosen")
        assert not (fake_windows / "esphb" / _ID8).exists()
    assert os.environ["ESPHOME_DATA_DIR"] == str(tmp_path / "chosen")


def test_respects_user_set_platformio_core_dir(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user-set PLATFORMIO_CORE_DIR is left alone (data still relocates); toolchain untouched."""
    chosen = tmp_path / "chosen_pio"
    home_pio = tmp_path / "home_platformio"
    home_pio.mkdir()  # would be swept if we relocated; must stay put
    monkeypatch.setenv("PLATFORMIO_CORE_DIR", str(chosen))
    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(tmp_path / "First Last" / "esphome"):
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)  # data dir still relocated
        assert os.environ["PLATFORMIO_CORE_DIR"] == str(chosen)  # user choice respected
        assert not (root / "pio").exists()  # we did not create/override the toolchain dir
    assert os.environ["PLATFORMIO_CORE_DIR"] == str(chosen)
    assert home_pio.is_dir()  # user's toolchain left untouched


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

    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        assert (root / "marker.txt").read_text(encoding="utf-8") == "data"
        assert (root / "pio" / "tool.txt").read_text(encoding="utf-8") == "toolchain"
    assert not (config_dir / ".esphome").exists()
    assert not home_pio.exists()


def test_migrates_from_legacy_flat_root(tmp_path: Path, fake_windows: Path) -> None:
    r"""The flat ``C:\esphb-<id8>`` of the first relocation is moved under ``C:\esphb\<id8>``."""
    legacy = fake_windows / f"esphb-{_ID8}"
    (legacy / "pio").mkdir(parents=True)
    (legacy / wbp._RELOCATED_MARKER).write_text("{}", encoding="utf-8")
    (legacy / "pio" / wbp._RELOCATED_MARKER).write_text("{}", encoding="utf-8")
    (legacy / "data.txt").write_text("built", encoding="utf-8")
    (legacy / "pio" / "tool.txt").write_text("toolchain", encoding="utf-8")
    # A migrated user's original config_dir/.esphome is already gone.
    config_dir = tmp_path / "First Last" / "esphome"
    config_dir.mkdir(parents=True)

    new_root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(new_root)
        assert os.environ["PLATFORMIO_CORE_DIR"] == str(new_root / "pio")
    assert (new_root / "data.txt").read_text(encoding="utf-8") == "built"  # build data moved
    assert (new_root / "pio" / "tool.txt").read_text(encoding="utf-8") == "toolchain"  # toolchain
    assert not legacy.exists()  # flat layout gone


def test_legacy_migration_failure_falls_back_to_legacy_root(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the flat root cannot be moved to the nested one, keep using it (its data is intact)."""
    legacy = fake_windows / f"esphb-{_ID8}"
    legacy.mkdir(parents=True)
    (legacy / wbp._RELOCATED_MARKER).write_text("{}", encoding="utf-8")
    (legacy / "data.txt").write_text("built", encoding="utf-8")
    config_dir = tmp_path / "First Last" / "esphome"
    config_dir.mkdir(parents=True)

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "denied"
        raise OSError(msg)

    monkeypatch.setattr(wbp.shutil, "move", _boom)
    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(legacy)  # fell back to the intact flat root
    assert (legacy / "data.txt").read_text(encoding="utf-8") == "built"  # data untouched


def test_second_run_reuses_root_without_remigrating(tmp_path: Path, fake_windows: Path) -> None:
    """Once the root exists, a later run reuses it and does not move freshly-written data in."""
    config_dir = tmp_path / "First Last" / "esphome"
    config_dir.mkdir(parents=True)
    with windows_short_build_paths(config_dir):
        pass

    # New data written to the old location after the first relocation must NOT be swept in.
    (config_dir / ".esphome").mkdir(parents=True, exist_ok=True)
    (config_dir / ".esphome" / "new.txt").write_text("x", encoding="utf-8")
    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        pass
    assert (config_dir / ".esphome" / "new.txt").exists()
    assert not (root / "new.txt").exists()


def test_failed_data_move_stays_on_old_dir(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``.esphome`` move leaves data in place and does NOT relocate (no silent miss)."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)

    def _boom(*_args: object, **_kwargs: object) -> None:
        msg = "denied"
        raise OSError(msg)

    monkeypatch.setattr(wbp.shutil, "move", _boom)
    with windows_short_build_paths(config_dir):
        assert "ESPHOME_DATA_DIR" not in os.environ  # env points nowhere -> reads hit old data
    assert (config_dir / ".esphome").is_dir()  # data left untouched at the original location


def test_failed_toolchain_move_still_relocates(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed toolchain move never aborts relocation: the build data still moves to the root."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    (config_dir / ".esphome" / "marker.txt").write_text("data", encoding="utf-8")
    (tmp_path / "home_platformio").mkdir()

    real_move = wbp.shutil.move

    def _move(src: str, dst: str, *args: object, **kwargs: object) -> object:
        if "platformio" in str(src):
            msg = "denied"
            raise OSError(msg)
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(wbp.shutil, "move", _move)
    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)
        assert (root / "marker.txt").read_text(encoding="utf-8") == "data"
    assert "ESPHOME_DATA_DIR" not in os.environ


def test_retries_toolchain_move_when_pio_absent(tmp_path: Path, fake_windows: Path) -> None:
    """A crash before the toolchain landed (pio absent) self-heals: the next run re-sweeps it."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)

    # First run relocates the build data; no toolchain exists yet.
    with windows_short_build_paths(config_dir):
        pass
    root = fake_windows / "esphb" / _ID8
    shutil.rmtree(root / "pio", ignore_errors=True)  # simulate a crash before pio was populated

    # Toolchain now present and pio absent: the next run must still sweep it into the root.
    home_pio = tmp_path / "home_platformio"
    home_pio.mkdir()
    (home_pio / "tool.txt").write_text("toolchain", encoding="utf-8")
    with windows_short_build_paths(config_dir):
        pass
    assert (root / "pio" / "tool.txt").read_text(encoding="utf-8") == "toolchain"
    assert not home_pio.exists()


def test_failed_toolchain_relocation_uses_default_core_dir(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted toolchain move leaves PLATFORMIO_CORE_DIR at the default, not corrupt."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    (tmp_path / "home_platformio").mkdir()

    real_move = wbp.shutil.move

    def _interrupted(src: str, dst: str, *args: object, **kwargs: object) -> object:
        if "platformio" in str(src):
            # A cross-volume copy that got partway then died: dst half-written, src left behind.
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "half.txt").write_text("partial", encoding="utf-8")
            msg = "interrupted"
            raise OSError(msg)
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(wbp.shutil, "move", _interrupted)
    root = fake_windows / "esphb" / _ID8
    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)  # build data still relocated
        assert "PLATFORMIO_CORE_DIR" not in os.environ  # corrupt toolchain not adopted
    assert "PLATFORMIO_CORE_DIR" not in os.environ


def test_corrupt_partial_toolchain_not_trusted_across_runs(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-copied toolchain left by one run is discarded next run, never trusted (the marker)."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    home_pio = tmp_path / "home_platformio"
    home_pio.mkdir()
    (home_pio / "tool.txt").write_text("toolchain", encoding="utf-8")
    root = fake_windows / "esphb" / _ID8

    real_move = wbp.shutil.move

    def _interrupted(src: str, dst: str, *args: object, **kwargs: object) -> object:
        if "platformio" in str(src):
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "half.txt").write_text("partial", encoding="utf-8")
            msg = "interrupted"
            raise OSError(msg)
        return real_move(src, dst, *args, **kwargs)

    # Run N: the toolchain move is interrupted, leaving a half-copied pio and the source intact.
    monkeypatch.setattr(wbp.shutil, "move", _interrupted)
    with windows_short_build_paths(config_dir):
        assert "PLATFORMIO_CORE_DIR" not in os.environ  # corrupt partial not adopted
    assert (root / "pio" / "half.txt").exists()  # partial left behind this run

    # Run N+1: the move works now; the stale, marker-less partial must be discarded, not trusted.
    monkeypatch.setattr(wbp.shutil, "move", real_move)
    with windows_short_build_paths(config_dir):
        assert os.environ["PLATFORMIO_CORE_DIR"] == str(root / "pio")
    assert (root / "pio" / "tool.txt").read_text(encoding="utf-8") == "toolchain"
    assert not (root / "pio" / "half.txt").exists()  # stale partial discarded


def test_partial_root_is_discarded_and_retried(tmp_path: Path, fake_windows: Path) -> None:
    """A partial root from an interrupted first move is discarded so the retry relocates cleanly."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    (config_dir / ".esphome" / "real.txt").write_text("real", encoding="utf-8")
    root = fake_windows / "esphb" / _ID8
    root.mkdir(parents=True)  # leftover partial root, no completion marker
    (root / "half.txt").write_text("partial", encoding="utf-8")

    with windows_short_build_paths(config_dir):
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)
    assert (root / "real.txt").read_text(encoding="utf-8") == "real"  # real data moved in
    assert not (root / "half.txt").exists()  # partial discarded
    assert (root / wbp._RELOCATED_MARKER).is_file()
    assert not (config_dir / ".esphome").exists()


def test_partial_root_discard_failure_stays_on_old_dir(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a partial root cannot be cleared, env is not set: stay on the original data dir."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    root = fake_windows / "esphb" / _ID8
    root.mkdir(parents=True)
    (root / "half.txt").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(wbp.shutil, "rmtree", lambda *_a, **_k: None)  # discard fails to remove

    with windows_short_build_paths(config_dir):
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert (config_dir / ".esphome").is_dir()  # original data untouched


def test_marker_loss_does_not_destroy_relocated_root(tmp_path: Path, fake_windows: Path) -> None:
    """A lost completion marker (write crashed) must not trigger a destructive re-relocation."""
    config_dir = tmp_path / "First Last" / "esphome"
    (config_dir / ".esphome").mkdir(parents=True)
    with windows_short_build_paths(config_dir):  # first run relocates + writes the marker
        pass

    root = fake_windows / "esphb" / _ID8
    (root / "data.txt").write_text("built", encoding="utf-8")  # real build output now under root
    (root / wbp._RELOCATED_MARKER).unlink()  # simulate a marker write that never landed
    with windows_short_build_paths(config_dir):  # source is gone; root must be trusted as-is
        assert os.environ["ESPHOME_DATA_DIR"] == str(root)
    assert (root / "data.txt").read_text(encoding="utf-8") == "built"  # preserved, not wiped
    assert (root / wbp._RELOCATED_MARKER).is_file()  # marker rewritten


def test_dashboard_id_io_failure_falls_back_to_noop(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError minting the dashboard_id degrades to a no-op, not a startup-aborting raise."""

    def _boom(_config_dir: Path) -> str:
        msg = "sidecar unwritable"
        raise OSError(msg)

    monkeypatch.setattr(wbp, "get_or_create_dashboard_id", _boom)
    with windows_short_build_paths(tmp_path / "First Last" / "esphome"):
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ


def test_root_creation_failure_falls_back_to_noop(
    tmp_path: Path, fake_windows: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the root dir cannot be created, the block yields and never sets the override env."""
    config_dir = tmp_path / "First Last" / "esphome"
    config_dir.mkdir(parents=True)  # no .esphome, so the stranded-data guard cannot fire first
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(wbp, "_ROOT_BASE", blocker)  # mkdir under a file raises OSError
    with windows_short_build_paths(config_dir):
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ
