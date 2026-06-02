"""Tests for the subprocess ``git`` wrapper behind version history.

The load-bearing guarantees these pin:
- A pre-existing repo is adopted, not re-initialised, and its
  ``.gitignore`` is left untouched.
- Commits are pathspec-scoped, so the user's unrelated staged edits
  never get folded into our automatic commit.
- A missing ``git`` binary disables the feature instead of crashing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from esphome_device_builder.controllers.version_history.git_repo import GitRepo

_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    """Run git in *cwd* with a throwaway identity; return stdout."""
    result = subprocess.run(  # noqa: S603
        [_GIT, "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _make_repo(path: Path) -> None:
    """Init a git repo at *path* with one committed file."""
    _git(path, "init")
    (path / "seed.yaml").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.yaml")
    _git(path, "commit", "-m", "seed")


# ---------------------------------------------------------------------------
# discovery / init
# ---------------------------------------------------------------------------


def test_init_creates_repo_and_gitignore(tmp_path: Path) -> None:
    """A non-repo config dir gets a fresh repo + committed .gitignore."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled
    assert repo.toplevel == tmp_path
    assert (tmp_path / ".git").is_dir()
    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    assert ".esphome/" in gitignore.read_text()
    # The .gitignore landed as a real commit, not just on disk.
    assert "Initialize version history" in _git(tmp_path, "log", "--format=%s")


def test_adopts_existing_repo_without_touching_gitignore(tmp_path: Path) -> None:
    """A pre-existing work tree is adopted; the user's .gitignore is untouched."""
    _make_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("my-rules/\n", encoding="utf-8")

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled
    assert repo.toplevel == tmp_path
    # Our default ignore content never overwrote the user's.
    assert (tmp_path / ".gitignore").read_text() == "my-rules/\n"


def test_adopts_repo_when_config_dir_is_subdir(tmp_path: Path) -> None:
    """Config dir nested inside a repo (``/config`` root, ``esphome/`` subdir)."""
    _make_repo(tmp_path)
    sub = tmp_path / "esphome"
    sub.mkdir()

    repo = GitRepo(config_dir=sub)
    repo.discover_or_init()

    assert repo.enabled
    # Toplevel resolves to the outer repo root, not the subdir.
    assert repo.toplevel == tmp_path


def test_missing_git_binary_disables_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git on PATH → disabled, every op a no-op, no exception."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert not repo.enabled
    assert repo.commit_paths([tmp_path / "x.yaml"], "msg") is None
    assert repo.log_file(tmp_path / "x.yaml") == []
    assert not (tmp_path / ".git").exists()


# ---------------------------------------------------------------------------
# commits
# ---------------------------------------------------------------------------


def test_commit_paths_records_new_and_edited_files(tmp_path: Path) -> None:
    """A create and a subsequent edit each land as their own commit."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    sha1 = repo.commit_paths([yaml], "Create kitchen.yaml")
    assert sha1
    yaml.write_text("v2\n", encoding="utf-8")
    sha2 = repo.commit_paths([yaml], "Update kitchen.yaml")
    assert sha2 and sha2 != sha1

    versions = repo.log_file(yaml)
    assert [c.message for c in versions] == ["Update kitchen.yaml", "Create kitchen.yaml"]
    assert repo.file_at(yaml, sha1) == "v1\n"
    assert repo.file_at(yaml, sha2) == "v2\n"


def test_commit_handles_flag_like_message_and_dashed_path(tmp_path: Path) -> None:
    """A flag-like message / leading-dash filename can't smuggle git options.

    Everything goes through argv (no shell): the message is the value of
    ``-m`` and the path sits after ``--``, so neither is reparsed as a
    git flag.
    """
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    dashed = tmp_path / "-weird.yaml"
    dashed.write_text("x\n", encoding="utf-8")

    sha = repo.commit_paths([dashed], "--amend is not actually a flag here")

    assert sha
    versions = repo.log_file(dashed)
    assert versions[0].message == "--amend is not actually a flag here"
    assert repo.file_at(dashed, sha) == "x\n"


def test_commit_paths_no_change_returns_none(tmp_path: Path) -> None:
    """Re-committing an unchanged file is a no-op (no empty commit)."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    repo.commit_paths([yaml], "Create kitchen.yaml")

    assert repo.commit_paths([yaml], "Update kitchen.yaml") is None


def test_commit_paths_does_not_sweep_unrelated_staged_edits(tmp_path: Path) -> None:
    """Pathspec scoping: our commit must not fold in the user's staged work.

    The dominant safety case for a pre-existing repo — a user with
    an in-progress ``git add`` of an unrelated file must not find it
    silently swept into our automatic commit.
    """
    _make_repo(tmp_path)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    # User stages an unrelated edit they're not ready to commit.
    user_file = tmp_path / "user_wip.yaml"
    user_file.write_text("user work in progress\n", encoding="utf-8")
    _git(tmp_path, "add", "user_wip.yaml")

    # We commit our own file.
    ours = tmp_path / "kitchen.yaml"
    ours.write_text("ours\n", encoding="utf-8")
    repo.commit_paths([ours], "Create kitchen.yaml")

    # The HEAD commit touched only our file.
    changed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split()
    assert changed == ["kitchen.yaml"]
    # The user's staged edit is still staged, never committed.
    assert "user_wip.yaml" in _git(tmp_path, "diff", "--cached", "--name-only")


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def test_log_file_preserves_messages_with_special_chars(tmp_path: Path) -> None:
    """Field/record separators survive a commit subject (no tab/newline confusion)."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    repo.commit_paths([yaml], "Restore kitchen.yaml to abc1234")

    versions = repo.log_file(yaml)
    assert versions[0].message == "Restore kitchen.yaml to abc1234"
    assert versions[0].short_sha and len(versions[0].short_sha) >= 7
    assert versions[0].timestamp > 0


def test_diff_file_shows_working_tree_change(tmp_path: Path) -> None:
    """diff_file returns a unified diff between a commit and the working copy."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    sha = repo.commit_paths([yaml], "Create kitchen.yaml")
    assert sha
    yaml.write_text("v2\n", encoding="utf-8")

    diff = repo.diff_file(yaml, sha)
    assert "-v1" in diff
    assert "+v2" in diff


def test_deleted_files_lists_configs_absent_from_work_tree(tmp_path: Path) -> None:
    """A committed YAML removed from disk shows up as restorable; a live one doesn't."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    gone = tmp_path / "gone.yaml"
    gone.write_text("bye\n", encoding="utf-8")
    repo.commit_paths([gone], "Create gone.yaml")
    live = tmp_path / "live.yaml"
    live.write_text("here\n", encoding="utf-8")
    repo.commit_paths([live], "Create live.yaml")

    # Delete one through git so it's recorded as removed.
    gone.unlink()
    repo.commit_paths([gone], "Delete gone.yaml")

    deleted = repo.deleted_files()
    assert "gone.yaml" in deleted
    assert "live.yaml" not in deleted
