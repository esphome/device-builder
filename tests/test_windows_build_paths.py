"""Unit contract for the Windows short-build-paths helper.

The nt-only branches are driven off Windows by faking the platform gate + junction syscall
(a posix symlink stands in), so env save/restore, reuse, collision, and the fallbacks get
fast-matrix coverage; skipped on Windows, where the real e2e exercises the native junction.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from esphome_device_builder.helpers import windows_build_paths as wbp
from esphome_device_builder.helpers.windows_build_paths import windows_short_build_paths

_not_windows = pytest.mark.skipif(sys.platform == "win32", reason="posix symlink stands in")


@pytest.mark.skipif(sys.platform == "win32", reason="pins the off-Windows no-op contract")
def test_context_manager_is_noop_off_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off Windows the context manager touches nothing and yields ``None``."""
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    with windows_short_build_paths(tmp_path) as pio_core_dir:
        assert pio_core_dir is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ


@pytest.fixture
def fake_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Run the helper's Windows branch off Windows; fake the junction as a posix symlink."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(wbp, "_is_windows", lambda: True)
    monkeypatch.setattr(wbp, "_JUNCTION_ROOT", root)
    created: list[tuple[Path, Path]] = []

    def _fake_create(link: Path, target: Path) -> None:
        created.append((Path(link), Path(target)))
        Path(link).symlink_to(target, target_is_directory=True)

    monkeypatch.setattr(wbp, "_create_junction", _fake_create)
    monkeypatch.delenv("ESPHOME_DATA_DIR", raising=False)
    return created


@_not_windows
def test_sets_short_env_inside_block_and_restores_after(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]]
) -> None:
    """Inside the block ESPHOME_DATA_DIR is the junction; after, it's gone again."""
    config_dir = tmp_path / "cfg"
    expected_junction = wbp._JUNCTION_ROOT / f"esphb-{wbp._suffix(config_dir / '.esphome')}"
    with windows_short_build_paths(config_dir) as pio:
        assert os.environ["ESPHOME_DATA_DIR"] == str(expected_junction)
        assert pio == Path(f"{expected_junction}-pio")
        assert pio.is_dir()
        assert len(fake_windows) == 1
    assert "ESPHOME_DATA_DIR" not in os.environ


@_not_windows
def test_restores_prior_env_var(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing ESPHOME_DATA_DIR is restored verbatim on exit."""
    prior = tmp_path / "prior"
    prior.mkdir()
    monkeypatch.setenv("ESPHOME_DATA_DIR", str(prior))
    with windows_short_build_paths(tmp_path / "cfg") as pio:
        assert pio is not None
        assert os.environ["ESPHOME_DATA_DIR"] != str(prior)
    assert os.environ["ESPHOME_DATA_DIR"] == str(prior)


@_not_windows
def test_oserror_fallback_yields_none_and_leaves_env(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If junction creation fails the block yields ``None`` and never sets the override."""

    def _boom(link: Path, target: Path) -> None:
        msg = "denied"
        raise OSError(msg)

    monkeypatch.setattr(wbp, "_create_junction", _boom)
    with windows_short_build_paths(tmp_path / "cfg") as pio:
        assert pio is None
        assert "ESPHOME_DATA_DIR" not in os.environ
    assert "ESPHOME_DATA_DIR" not in os.environ


@_not_windows
def test_reuses_existing_junction_without_recreating(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]]
) -> None:
    """A second run reuses the junction already pointing at the target, no recreate."""
    config_dir = tmp_path / "cfg"
    with windows_short_build_paths(config_dir):
        pass
    assert len(fake_windows) == 1
    with windows_short_build_paths(config_dir):
        pass
    assert len(fake_windows) == 1


@_not_windows
def test_name_collision_falls_back_without_stealing(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]]
) -> None:
    """A junction at our name pointing elsewhere is left alone; we fall back to long paths."""
    config_dir = tmp_path / "cfg"
    data = config_dir / ".esphome"
    data.mkdir(parents=True)
    junction = wbp._JUNCTION_ROOT / f"esphb-{wbp._suffix(data)}"
    other = tmp_path / "other"
    other.mkdir()
    Path(junction).symlink_to(other, target_is_directory=True)

    with windows_short_build_paths(config_dir) as pio:
        assert pio is None
    # The colliding junction is untouched (not stolen / repointed).
    assert os.path.realpath(junction) == os.path.realpath(other)


@_not_windows
def test_dangling_junction_is_replaced(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]]
) -> None:
    """A junction whose target was deleted is cleared and recreated, not left to fail setup."""
    config_dir = tmp_path / "cfg"
    data = config_dir / ".esphome"
    data.mkdir(parents=True)
    junction = wbp._JUNCTION_ROOT / f"esphb-{wbp._suffix(data)}"
    gone = tmp_path / "gone"  # never created → dangling
    Path(junction).symlink_to(gone, target_is_directory=True)
    assert not junction.exists()  # dangling: exists() follows the link and is False

    with windows_short_build_paths(config_dir) as pio:
        assert pio is not None
        assert os.path.realpath(junction) == os.path.realpath(data)


@_not_windows
def test_space_bearing_target_passed_through_unmangled(
    tmp_path: Path, fake_windows: list[tuple[Path, Path]]
) -> None:
    """A space-bearing target (Windows profile dir) reaches the junction call intact.

    The native ``_winapi.CreateJunction`` takes the path as a direct argument, so unlike
    ``cmd /c mklink`` a space can't split it. (A full ESP-IDF *compile* with a space still
    fails upstream via ``-fdebug-prefix-map``; that's orthogonal to junction creation.)
    """
    config_dir = tmp_path / "First Last" / "esphome"
    with windows_short_build_paths(config_dir) as pio:
        assert pio is not None
        assert len(fake_windows) == 1
        _link, target = fake_windows[0]
        assert target == config_dir / ".esphome"
        assert " " in str(target)


def test_remove_link_rmdirs_a_plain_directory(tmp_path: Path) -> None:
    """The non-symlink branch (a real junction on Windows) is removed with rmdir."""
    junction = tmp_path / "j"
    junction.mkdir()
    wbp._remove_link(junction)
    assert not junction.exists()


def test_create_junction_calls_the_native_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_create_junction`` calls ``_winapi.CreateJunction(target, link)`` (faked off Windows)."""
    calls: list[tuple[str, str]] = []
    fake_winapi = types.ModuleType("_winapi")
    fake_winapi.CreateJunction = lambda src, dst: calls.append((src, dst))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_winapi", fake_winapi)

    link = tmp_path / "link"
    target = tmp_path / "target"
    wbp._create_junction(link, target)
    assert calls == [(str(target), str(link))]
