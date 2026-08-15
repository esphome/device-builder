"""Tests for the sibling-CLI resolvers in ``helpers/sibling_cli.py``."""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from esphome_device_builder.helpers.sibling_cli import (
    _find_sibling_cli,
    find_esphome_cmd,
    find_esptool_cmd,
)


@pytest.fixture(autouse=True)
def _clear_sibling_cli_cache() -> Any:
    """Reset the ``_find_sibling_cli`` lru_cache before each test.

    The cache exists so async callers don't trip blockbuster on
    repeated ``os.stat`` calls, but it would carry state between
    tests that ``monkeypatch.setattr(sys, "executable", ...)`` —
    making the second test see the first test's resolved path.
    """
    _find_sibling_cli.cache_clear()
    yield
    _find_sibling_cli.cache_clear()


def test_find_esphome_cmd_prefers_sibling_binary_when_present(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone ``esphome`` next to ``sys.executable`` wins.

    A sibling script in the same bin directory is slightly cheaper
    than ``python -m esphome`` (one fewer import-system warmup)
    and surfaces a friendlier traceback when something goes wrong
    inside esphome — the wrapper's ``sys.exit(main())`` shape
    raises with a clear top frame, vs. the ``runpy`` shim that
    ``-m`` adds to a stack trace.

    Pin the preference so a refactor that swaps the order would
    surface here.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    # Match the helper's own ``os.name == "nt"`` check exactly.
    # ``sys.platform`` and ``os.name`` can disagree on MSYS / Cygwin
    # so basing the fixture on ``os.name`` keeps the test in lockstep
    # with whichever branch the helper is about to take.
    sibling = bin_dir / ("esphome.exe" if os.name == "nt" else "esphome")
    sibling.write_text("#!/bin/sh\necho fake-esphome\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = find_esphome_cmd()

    assert cmd == [str(sibling)]


def test_find_esphome_cmd_falls_back_to_python_dash_m(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sibling binary → ``[sys.executable, '-m', 'esphome']``.

    The fallback path is what runs in most production
    deployments — pip-installed esphome creates the sibling
    script in dev / venv installs but not in every package
    layout (e.g. some Docker images strip the script wrapper
    to keep the image small). ``python -m esphome`` always works
    when the package is importable, which is the same condition
    the dashboard's own startup already requires, so it's the
    safe default.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    # Deliberately don't create a sibling esphome script.

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = find_esphome_cmd()

    assert cmd == [str(fake_python), "-m", "esphome"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX-extension branch")
def test_find_esphome_cmd_picks_bare_esphome_on_posix(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX (``os.name != 'nt'``) the helper picks ``esphome`` (no extension).

    Layered with the Windows-only twin below so the CI matrix
    (Linux + macOS + Windows) covers both branches end-to-end.
    Faking ``os.name`` mid-process doesn't work — pathlib
    instantiates ``PosixPath`` / ``WindowsPath`` based on the
    real ``os.name`` at import time, and switching makes
    pathlib raise ``NotImplementedError`` — so we let the
    host OS pick.

    Skip predicate matches the helper's branch exactly
    (``os.name == "nt"``) rather than ``sys.platform`` so MSYS /
    Cygwin (where the two can disagree) routes to the
    correct test.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "esphome").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = find_esphome_cmd()

    assert cmd == [str(bin_dir / "esphome")]


@pytest.mark.skipif(os.name != "nt", reason="Windows-extension branch")
def test_find_esphome_cmd_picks_esphome_exe_on_windows(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the helper picks ``esphome.exe``, not ``esphome``.

    Companion to the POSIX test above. Pinned because the
    wrong-extension lookup is silent in production: a regression
    that hard-coded ``esphome`` (or flipped the ternary) would
    just fall through to ``python -m esphome``. The fallback
    works, but loses the perf + traceback wins the sibling
    path provides.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python.exe"
    fake_python.write_text("MZ\n", encoding="utf-8")
    (bin_dir / "esphome.exe").write_text("MZ\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = find_esphome_cmd()

    assert cmd == [str(bin_dir / "esphome.exe")]


def test_find_esptool_cmd_prefers_sibling_script(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling ``esptool`` next to ``sys.executable`` wins over ``-m esptool``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    sibling = bin_dir / ("esptool.exe" if os.name == "nt" else "esptool")
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))

    assert find_esptool_cmd() == [str(sibling)]


def test_find_esptool_cmd_falls_back_to_python_dash_m(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sibling esptool → ``[sys.executable, '-m', 'esptool']``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))

    assert find_esptool_cmd() == [str(fake_python), "-m", "esptool"]


def test_find_esphome_cmd_does_not_substitute_sibling_python(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling ``python`` next to a non-``python`` executable is irrelevant.

    Documents the deliberate non-feature: the helper anchors on
    ``sys.executable`` exactly and never tries to find "the
    python next door". A previous draft of this code looked for
    a sibling ``python`` interpreter and used *that* to run
    ``python -m esphome``; on Linux the running interpreter is
    sometimes ``/opt/python3.12/bin/python3.12`` while a system
    ``/usr/bin/python`` (no esphome) is on PATH — substituting
    silently produced "No module named esphome" at compile
    time. Anchoring on ``sys.executable`` only is the cure.

    Verify by pointing ``sys.executable`` at a non-``python``-named
    script with a sibling ``python``: the helper must use the
    weird name verbatim, not the sibling.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    weird = bin_dir / "python3.12"
    weird.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    # No sibling esphome → expect the fallback.

    monkeypatch.setattr(sys, "executable", str(weird))
    cmd = find_esphome_cmd()

    assert cmd == [str(weird), "-m", "esphome"]
    assert str(bin_dir / "python") not in cmd
