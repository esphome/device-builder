"""Unit tests for ``ensure_shallow_git_repo`` in ``script/_repo_cache.py``."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

from script._repo_cache import ensure_shallow_git_repo  # type: ignore[import-not-found]


class _FakeRun:
    """Record ``subprocess.run`` calls and return a canned result."""

    def __init__(self, returncode: int = 0, raises: Exception | None = None) -> None:
        self.returncode = returncode
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append(cmd)
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode)


def _make_git_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    (target / ".git").mkdir(parents=True)
    return target


def test_pulls_existing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _make_git_repo(tmp_path)
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    result = ensure_shallow_git_repo("url", target, "main", label="x")
    assert result == target
    assert fake.calls[0][:3] == ["git", "-C", str(target)]
    assert "pull" in fake.calls[0]


def test_pull_nonzero_exit_keeps_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _make_git_repo(tmp_path)
    fake = _FakeRun(returncode=1)
    monkeypatch.setattr(subprocess, "run", fake)
    assert ensure_shallow_git_repo("url", target, "main", label="x") == target


def test_pull_timeout_is_non_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _make_git_repo(tmp_path)
    fake = _FakeRun(raises=subprocess.TimeoutExpired("git", 1))
    monkeypatch.setattr(subprocess, "run", fake)
    assert ensure_shallow_git_repo("url", target, "main", label="x") == target


def test_pull_failure_honours_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    target = _make_git_repo(tmp_path)
    fake = _FakeRun(returncode=1)
    monkeypatch.setattr(subprocess, "run", fake)
    with caplog.at_level(logging.DEBUG, logger="script._repo_cache"):
        ensure_shallow_git_repo("url", target, "main", label="x", pull_fail_level=logging.ERROR)
    pull_records = [r for r in caplog.records if "git pull" in r.getMessage()]
    assert [r.levelno for r in pull_records] == [logging.ERROR]


def test_skips_pull_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _make_git_repo(tmp_path)
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    result = ensure_shallow_git_repo("url", target, "main", label="x", pull=False)
    assert result == target
    assert fake.calls == []


def test_clones_when_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "repo"
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    result = ensure_shallow_git_repo("url", target, "current", label="x")
    assert result == target
    assert fake.calls[0][:2] == ["git", "clone"]
    assert "--branch=current" in fake.calls[0]
    assert fake.calls[0][-2:] == ["url", str(target)]


def test_clone_failure_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "repo"
    fake = _FakeRun(raises=subprocess.CalledProcessError(1, "git"))
    monkeypatch.setattr(subprocess, "run", fake)
    assert ensure_shallow_git_repo("url", target, "main", label="x") is None


def test_clone_failure_honours_log_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "repo"
    fake = _FakeRun(raises=subprocess.CalledProcessError(1, "git"))
    monkeypatch.setattr(subprocess, "run", fake)
    with caplog.at_level(logging.DEBUG, logger="script._repo_cache"):
        ensure_shallow_git_repo("url", target, "main", label="x", clone_fail_level=logging.ERROR)
    clone_records = [r for r in caplog.records if "Could not clone" in r.getMessage()]
    assert [r.levelno for r in clone_records] == [logging.ERROR]


def test_existing_non_git_used_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    fake = _FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    result = ensure_shallow_git_repo("url", target, "main", label="x", allow_existing_non_git=True)
    assert result == target
    assert fake.calls == []
