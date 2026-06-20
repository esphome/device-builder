"""Tests for the subprocess ``git`` wrapper behind version history.

The load-bearing guarantees these pin:
- A pre-existing repo is adopted, not re-initialised, and its
  ``.gitignore`` is left untouched.
- Commits are pathspec-scoped, so the user's unrelated staged edits
  never get folded into our automatic commit.
- A missing ``git`` binary disables the feature instead of crashing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from esphome_device_builder.controllers.version_history import git_repo as git_repo_mod
from esphome_device_builder.controllers.version_history.git_repo import GitRepo

_GIT = shutil.which("git") or "git"


def _index_lock(repo_root: Path, *, age_seconds: float) -> Path:
    """Write a ``.git/index.lock`` aged *age_seconds* in the past."""
    lock = repo_root / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(lock, (stamp, stamp))
    return lock


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


def test_adopts_enclosing_repo_that_lacks_our_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enclosing repo without our package source is still adopted (the ``/config`` case)."""
    _make_repo(tmp_path)
    sub = tmp_path / "esphome"
    sub.mkdir()
    # A normal pip / site-packages install: our package lives outside the
    # user's repo, so the enclosing repo is the genuine adoption target.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo._OWN_SOURCE_ROOT",
        Path("/opt/site-packages/esphome_device_builder"),
    )

    repo = GitRepo(config_dir=sub)
    repo.discover_or_init()

    assert repo.enabled
    assert repo.toplevel == tmp_path  # adopted the outer repo, not a nested one


def test_declines_to_adopt_own_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config dir inside the Device Builder source repo gets its own nested repo."""
    _make_repo(tmp_path)  # stands in for the device-builder source checkout
    pkg = tmp_path / "esphome_device_builder"
    pkg.mkdir()
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo._OWN_SOURCE_ROOT",
        pkg,
    )
    configs = tmp_path / "configs"
    configs.mkdir()

    repo = GitRepo(config_dir=configs)
    repo.discover_or_init()

    assert repo.enabled
    assert repo.toplevel == configs  # nested repo, not the enclosing source repo
    assert (configs / ".git").is_dir()


def test_init_keeps_a_preexisting_gitignore(tmp_path: Path) -> None:
    """A .gitignore already sitting in a non-repo dir is committed, not overwritten."""
    (tmp_path / ".gitignore").write_text("user-rules/\n", encoding="utf-8")

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled
    assert (tmp_path / ".gitignore").read_text() == "user-rules/\n"


# Machine state / secrets that must never reach git history, mirroring
# what Device Builder writes into a real config dir.
_SECRET_STATE_FILES = (
    ".device-builder-peer-link-key.bin",
    ".device-builder.json",
    ".device-builder.lock",
    ".receiver_peers.json",
    ".offloader_pairings.json",
    ".DS_Store",
)


def test_init_seed_never_commits_keys_or_state(tmp_path: Path) -> None:
    """The fresh-init seed commits configs but never our keys / state / OS noise.

    Even when a stock ESPHome ``.gitignore`` (which doesn't know about
    Device Builder's files) is already present, the seed must not sweep
    the peer-link key, receiver/offloader credentials, sidecars, or
    ``.DS_Store`` into the initial snapshot.
    """
    # A stock-style .gitignore that covers neither our state nor .DS_Store.
    (tmp_path / ".gitignore").write_text("/.esphome/\n/secrets.yaml\n", encoding="utf-8")
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    for name in _SECRET_STATE_FILES:
        (tmp_path / name).write_text("secret\n", encoding="utf-8")

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    tracked = _git(tmp_path, "ls-files").split()
    assert "kitchen.yaml" in tracked
    for name in _SECRET_STATE_FILES:
        assert name not in tracked, f"{name} must not be committed"


def test_init_seed_is_yaml_only_not_logs_or_binaries(tmp_path: Path) -> None:
    """The seed captures YAML configs but never large non-config files.

    A config dir often also holds logs / databases / media; ``git add -A``
    would bake those into history forever. The seed is YAML-scoped instead.
    """
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    (tmp_path / "home-assistant.log").write_text("X" * 5_000_000, encoding="utf-8")
    (tmp_path / "home-assistant_v2.db").write_bytes(b"\x00" * 1024)
    (tmp_path / "secrets.yaml").write_text("wifi_password: hunter2\n", encoding="utf-8")
    (tmp_path / "snapshot.jpg").write_bytes(b"\xff\xd8\xff")

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    tracked = _git(tmp_path, "ls-files").split()
    assert "kitchen.yaml" in tracked
    assert ".gitignore" in tracked
    assert "home-assistant.log" not in tracked
    assert "home-assistant_v2.db" not in tracked
    assert "snapshot.jpg" not in tracked
    assert "secrets.yaml" not in tracked  # credentials stay out of history


def test_adopt_writes_local_excludes_for_state(tmp_path: Path) -> None:
    """Adopting a repo installs local excludes so our key can't be staged later."""
    _make_repo(tmp_path)  # pre-existing repo, no device-builder ignores
    (tmp_path / ".device-builder-peer-link-key.bin").write_text("key\n", encoding="utf-8")

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    # git now treats the key as ignored (via .git/info/exclude), so a
    # later ``git add -A`` can't stage it.
    ignored = _git(tmp_path, "status", "--porcelain", "--ignored").splitlines()
    assert any(
        line.startswith("!!") and "device-builder-peer-link-key.bin" in line for line in ignored
    )


def test_local_excludes_are_idempotent(tmp_path: Path) -> None:
    """Re-running discovery doesn't duplicate our managed exclude block."""
    _make_repo(tmp_path)
    GitRepo(config_dir=tmp_path).discover_or_init()
    GitRepo(config_dir=tmp_path).discover_or_init()

    exclude = (tmp_path / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.count("ESPHome Device Builder (managed)") == 2  # one start + one end marker


def _patch_git_path(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Intercept ``git rev-parse --git-path`` calls; pass everything else through."""
    real_run = subprocess.run

    def _fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--git-path" in cmd:
            return handler(cmd)  # type: ignore[operator]
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run", _fake
    )


def test_local_excludes_noop_when_git_path_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``rev-parse --git-path`` leaves discovery working, just no excludes."""
    _make_repo(tmp_path)
    _patch_git_path(monkeypatch, lambda cmd: subprocess.CompletedProcess(cmd, 1, "", ""))

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled  # adoption still succeeded


def test_local_excludes_honor_absolute_git_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute path from ``rev-parse --git-path`` is used as-is."""
    _make_repo(tmp_path)
    abs_exclude = tmp_path / "abs_exclude"
    _patch_git_path(
        monkeypatch, lambda cmd: subprocess.CompletedProcess(cmd, 0, f"{abs_exclude}\n", "")
    )

    GitRepo(config_dir=tmp_path).discover_or_init()

    assert "ESPHome Device Builder (managed)" in abs_exclude.read_text(encoding="utf-8")


def test_local_excludes_tolerate_write_error(tmp_path: Path) -> None:
    """An unwritable info/exclude is logged, not fatal to discovery."""
    _make_repo(tmp_path)
    # Replace the exclude file with a directory so the read/append raises.
    exclude = tmp_path / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()
    exclude.mkdir()

    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled


def test_commit_index_noop_when_nothing_staged(tmp_path: Path) -> None:
    """The seed-commit helper is a no-op when there's nothing staged."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    before = _git(tmp_path, "rev-list", "--count", "HEAD").strip()

    repo._commit_index("nothing to do")

    assert _git(tmp_path, "rev-list", "--count", "HEAD").strip() == before


def test_deleted_files_empty_on_repo_without_commits(tmp_path: Path) -> None:
    """An adopted repo with no commits yet yields no deletable configs (git log fails)."""
    _git(tmp_path, "init")  # bare repo, no commits
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.enabled
    assert repo.deleted_files() == []


def test_file_at_path_outside_toplevel_falls_back_to_name(tmp_path: Path) -> None:
    """A path outside the work tree doesn't crash _rel_to_toplevel; returns None."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.file_at(Path("/nonexistent/elsewhere.yaml"), "0" * 40) is None


def test_missing_git_binary_disables_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git on PATH → disabled, every op a no-op, no exception."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert not repo.enabled
    p = tmp_path / "x.yaml"
    assert repo.commit_paths([p], "msg") is None
    assert repo.log_file(p) == []
    assert repo.file_at(p, "abc1234") is None
    assert repo.diff_file(p, "abc1234") == ""
    assert repo.deleted_files() == []
    assert not (tmp_path / ".git").exists()


def test_discover_or_init_tolerates_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError while probing / initialising leaves the feature disabled, not crashed."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("git exec failed")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run",
        _boom,
    )
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert not repo.enabled


# ---------------------------------------------------------------------------
# commits
# ---------------------------------------------------------------------------


def test_commit_paths_raises_on_git_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing ``git commit`` raises so the controller can tell it from a no-op."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")

    real_run = subprocess.run

    def _fail_commit(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "commit" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="commit blew up")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run",
        _fail_commit,
    )
    with pytest.raises(subprocess.CalledProcessError):
        repo.commit_paths([yaml], "Create kitchen.yaml")


def test_commit_clears_stale_index_lock_and_retries(tmp_path: Path) -> None:
    """A repo we created self-heals a stale ``index.lock`` left by a killed git."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    assert repo.managed
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    lock = _index_lock(tmp_path, age_seconds=3600)

    sha = repo.commit_paths([yaml], "Create kitchen.yaml")

    assert sha
    assert not lock.exists()


def test_commit_paths_skips_gitignored_secrets(tmp_path: Path) -> None:
    """A commit for a gitignored secrets.yaml is a clean no-op, not a git error."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()  # seeds a .gitignore that ignores secrets.yaml
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("wifi_password: hunter2\n", encoding="utf-8")

    assert repo.commit_paths([secrets], "Update secrets") is None
    assert "secrets.yaml" not in _git(tmp_path, "ls-files").split()


def test_commit_paths_commits_others_alongside_an_ignored_one(tmp_path: Path) -> None:
    """A mixed batch commits the trackable file and drops the ignored one."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    kitchen = tmp_path / "kitchen.yaml"
    kitchen.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("wifi_password: hunter2\n", encoding="utf-8")

    sha = repo.commit_paths([kitchen, secrets], "Add kitchen + secrets")

    assert sha
    tracked = _git(tmp_path, "ls-files").split()
    assert "kitchen.yaml" in tracked
    assert "secrets.yaml" not in tracked


def test_ignored_subset_treats_check_ignore_failure_as_nothing_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-0/1 check-ignore exit reports nothing ignored, never a crash."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()  # real _run; patch only the check-ignore call below
    monkeypatch.setattr(
        GitRepo,
        "_run",
        lambda self, *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="boom"),
    )

    assert repo._ignored_subset([tmp_path / "secrets.yaml"]) == []


def test_ignored_subset_skips_a_truncated_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short (malformed) check-ignore record is skipped, not unpacked into a crash."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()  # real _run; patch only the check-ignore call below
    monkeypatch.setattr(
        GitRepo,
        "_run",
        lambda self, *a, **k: subprocess.CompletedProcess(a, 0, stdout="src\0", stderr=""),
    )

    assert repo._ignored_subset([tmp_path / "x.yaml"]) == []


def test_managed_flag_survives_restart_and_heals(tmp_path: Path) -> None:
    """A repo we created is re-adopted as managed on restart, so the stale-lock heal fires."""
    GitRepo(config_dir=tmp_path).discover_or_init()  # first boot: initialises

    restarted = GitRepo(config_dir=tmp_path)
    restarted.discover_or_init()  # re-discovers the existing repo via adopt
    assert restarted.managed

    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    lock = _index_lock(tmp_path, age_seconds=3600)

    sha = restarted.commit_paths([yaml], "Create kitchen.yaml")

    assert sha
    assert not lock.exists()


def test_adopted_user_repo_caches_not_managed_verdict(tmp_path: Path) -> None:
    """An adopted user repo records a ``false`` verdict so the seed-root scan runs once."""
    _make_repo(tmp_path)  # root commit authored by someone other than us
    GitRepo(config_dir=tmp_path).discover_or_init()
    assert _git(tmp_path, "config", "--local", "--get", "device-builder.managed").strip() == "false"

    # A restart reads the cached verdict rather than re-scanning history.
    restarted = GitRepo(config_dir=tmp_path)
    restarted.discover_or_init()
    assert not restarted.managed


def test_pre_marker_repo_is_backfilled_as_managed(tmp_path: Path) -> None:
    """A pre-marker repo we created is recognised by its seed root commit and re-stamped."""
    GitRepo(config_dir=tmp_path).discover_or_init()
    _git(tmp_path, "config", "--local", "--unset", "device-builder.managed")

    upgraded = GitRepo(config_dir=tmp_path)
    upgraded.discover_or_init()

    assert upgraded.managed
    # The backfill re-stamped the marker, so the next restart is cheap.
    assert _git(tmp_path, "config", "--local", "--get", "device-builder.managed").strip() == "true"


def test_commit_keeps_fresh_index_lock(tmp_path: Path) -> None:
    """A young ``index.lock`` (a live git may hold it) is left alone; the write raises busy."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    lock = _index_lock(tmp_path, age_seconds=0)

    with pytest.raises(git_repo_mod.GitIndexLockBusyError):
        repo.commit_paths([yaml], "Create kitchen.yaml")
    assert lock.exists()


def test_adopted_repo_never_clears_index_lock(tmp_path: Path) -> None:
    """An adopted work tree is never auto-unlocked; a stale lock there propagates as-is."""
    _make_repo(tmp_path)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    assert not repo.managed
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    lock = _index_lock(tmp_path, age_seconds=3600)

    # Stale (won't free itself) and unclearable (adopted) → the original
    # failure surfaces, not a retryable busy signal.
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        repo.commit_paths([yaml], "Create kitchen.yaml")
    assert not isinstance(excinfo.value, git_repo_mod.GitIndexLockBusyError)
    assert lock.exists()


def test_adopted_repo_fresh_lock_is_retryable_busy(tmp_path: Path) -> None:
    """A live writer's fresh lock in an adopted repo is the retryable case (the PR's target)."""
    _make_repo(tmp_path)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    assert not repo.managed
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    lock = _index_lock(tmp_path, age_seconds=0)

    with pytest.raises(git_repo_mod.GitIndexLockBusyError):
        repo.commit_paths([yaml], "Create kitchen.yaml")
    assert lock.exists()


def _lock_error() -> subprocess.CalledProcessError:
    """Build a git failure whose stderr blames a live ``index.lock``."""
    return subprocess.CalledProcessError(
        128,
        ["git", "add"],
        stderr="fatal: Unable to create '.git/index.lock': File exists.",
    )


def _bare_repo(tmp_path: Path) -> GitRepo:
    """Wire a GitRepo just enough to drive ``_run_write`` with a stubbed ``_run``."""
    repo = GitRepo(config_dir=tmp_path)
    repo.git_bin = "git"
    repo.toplevel = tmp_path
    return repo


def test_run_write_raises_busy_on_a_fresh_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh lock (a live writer) becomes GitIndexLockBusyError for the async caller to retry."""
    repo = _bare_repo(tmp_path)
    monkeypatch.setattr(GitRepo, "_clear_stale_index_lock", lambda _self, _exc: False)
    monkeypatch.setattr(GitRepo, "_index_lock_is_fresh", lambda _self: True)
    calls = {"n": 0}

    def _fake_run(_self: GitRepo, args: list[str], *, check: bool) -> object:
        calls["n"] += 1
        raise _lock_error()

    monkeypatch.setattr(GitRepo, "_run", _fake_run)
    with pytest.raises(git_repo_mod.GitIndexLockBusyError):
        repo._run_write(["add", "--", "x"])
    assert calls["n"] == 1  # no in-thread retry; the wait belongs on the loop


def test_run_write_propagates_a_stale_unclearable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale lock that couldn't be cleared surfaces the original error, not a busy retry."""
    repo = _bare_repo(tmp_path)
    monkeypatch.setattr(GitRepo, "_clear_stale_index_lock", lambda _self, _exc: False)
    monkeypatch.setattr(GitRepo, "_index_lock_is_fresh", lambda _self: False)
    calls = {"n": 0}

    def _fake_run(_self: GitRepo, args: list[str], *, check: bool) -> object:
        calls["n"] += 1
        raise _lock_error()

    monkeypatch.setattr(GitRepo, "_run", _fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        repo._run_write(["add", "--", "x"])
    assert not isinstance(excinfo.value, git_repo_mod.GitIndexLockBusyError)
    assert calls["n"] == 1


def test_run_write_retries_in_place_after_clearing_a_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale lock is cleared and the write retried at once, no busy signal."""
    repo = _bare_repo(tmp_path)
    monkeypatch.setattr(GitRepo, "_clear_stale_index_lock", lambda _self, _exc: True)
    calls = {"n": 0}

    def _fake_run(_self: GitRepo, args: list[str], *, check: bool) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _lock_error()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(GitRepo, "_run", _fake_run)
    repo._run_write(["add", "--", "x"])
    assert calls["n"] == 2


def test_run_write_reclassifies_a_post_clear_recollision_as_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh writer grabbing the lock right after a stale-clear is still retryable busy."""
    repo = _bare_repo(tmp_path)
    monkeypatch.setattr(GitRepo, "_clear_stale_index_lock", lambda _self, _exc: True)
    monkeypatch.setattr(GitRepo, "_index_lock_is_fresh", lambda _self: True)
    calls = {"n": 0}

    def _fake_run(_self: GitRepo, args: list[str], *, check: bool) -> object:
        calls["n"] += 1
        raise _lock_error()  # the cleared lock is immediately re-taken

    monkeypatch.setattr(GitRepo, "_run", _fake_run)
    with pytest.raises(git_repo_mod.GitIndexLockBusyError):
        repo._run_write(["add", "--", "x"])
    assert calls["n"] == 2  # original attempt + one post-clear retry, then busy


def test_run_write_propagates_a_non_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git failure that isn't index.lock contention is not wrapped as busy."""
    repo = _bare_repo(tmp_path)
    calls = {"n": 0}

    def _fake_run(_self: GitRepo, args: list[str], *, check: bool) -> object:
        calls["n"] += 1
        raise subprocess.CalledProcessError(1, ["git", "commit"], stderr="fatal: nothing to do")

    monkeypatch.setattr(GitRepo, "_run", _fake_run)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        repo._run_write(["commit"])
    assert not isinstance(excinfo.value, git_repo_mod.GitIndexLockBusyError)
    assert calls["n"] == 1


def test_index_lock_is_fresh_classifies_by_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unresolvable / vanished / young locks count as fresh; only an aged one is stale."""
    repo = _bare_repo(tmp_path)
    lock = tmp_path / "index.lock"

    monkeypatch.setattr(GitRepo, "_index_lock_path", lambda _self: None)
    assert repo._index_lock_is_fresh() is True  # can't resolve → retry resolves it

    monkeypatch.setattr(GitRepo, "_index_lock_path", lambda _self: lock)
    assert repo._index_lock_is_fresh() is True  # vanished (stat raises) → fresh

    lock.write_text("", encoding="utf-8")
    assert repo._index_lock_is_fresh() is True  # young → a live writer may hold it

    stamp = time.time() - 3600
    os.utime(lock, (stamp, stamp))
    assert repo._index_lock_is_fresh() is False  # aged → won't free itself


def test_commit_propagates_a_stale_lock_it_cannot_remove(tmp_path: Path) -> None:
    """A stale lock we can't unlink (e.g. it's a directory) surfaces the original error."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    # A directory at the lock path: aged-stale, but unlink() raises OSError.
    lock_dir = tmp_path / ".git" / "index.lock"
    lock_dir.mkdir()
    stamp = time.time() - 3600
    os.utime(lock_dir, (stamp, stamp))

    # Stale (won't free itself) and unclearable → not a retryable busy signal.
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        repo.commit_paths([yaml], "Create kitchen.yaml")
    assert not isinstance(excinfo.value, git_repo_mod.GitIndexLockBusyError)
    assert lock_dir.exists()


def test_clear_stale_index_lock_noop_when_lock_already_gone(tmp_path: Path) -> None:
    """A race where the lock vanished before we looked is a clean no-op, not a crash."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    exc = subprocess.CalledProcessError(128, ["git", "add"], stderr="fatal: ... index.lock")

    assert repo._clear_stale_index_lock(exc) is False


def test_clear_stale_index_lock_handles_stat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat() failure between the exists check and age read is swallowed, not raised."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    class _BadLock:
        def exists(self) -> bool:
            return True

        def stat(self) -> object:
            raise OSError("stat failed")

    monkeypatch.setattr(GitRepo, "_index_lock_path", lambda _self: _BadLock())
    exc = subprocess.CalledProcessError(128, ["git", "add"], stderr="fatal: ... index.lock")

    assert repo._clear_stale_index_lock(exc) is False


def test_mark_managed_warns_when_config_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed managed-flag stamp is logged, not silently dropped."""
    real_run = subprocess.run

    def _fail_mark(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "config" in cmd and "device-builder.managed" in cmd and "true" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "config write failed")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run", _fail_mark
    )
    with caplog.at_level(logging.WARNING):
        GitRepo(config_dir=tmp_path).discover_or_init()

    assert any("Could not stamp managed flag" in rec.message for rec in caplog.records)


def test_index_lock_path_none_when_rev_parse_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``rev-parse --git-path`` yields no lock path rather than crashing."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    real_run = subprocess.run

    def _fail_git_path(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--git-path" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run", _fail_git_path
    )

    assert repo._index_lock_path() is None


def test_reads_pass_no_optional_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every git invocation carries ``--no-optional-locks`` so reads can't grab index.lock."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    calls: list[list[str]] = []
    real_run = subprocess.run

    def _record(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run", _record
    )
    repo.log_file(tmp_path / "kitchen.yaml")

    assert calls
    assert all(cmd[1] == "--no-optional-locks" for cmd in calls)


def test_run_surfaces_git_stderr_on_failure(tmp_path: Path) -> None:
    """A checked git failure raises with the ``fatal:`` stderr line in ``str``.

    Pins the triage path: a stale ``index.lock`` (or any fatal) must show
    *why* in the log, not a bare ``exit status 128``.
    """
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    # Simulate the killed-mid-commit leftover that breaks every add.
    (tmp_path / ".git" / "index.lock").write_text("", encoding="utf-8")
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        repo.commit_paths([yaml], "Create kitchen.yaml")

    assert "index.lock" in str(exc_info.value)


def test_file_at_returns_none_for_unknown_commit(tmp_path: Path) -> None:
    """Asking for a path at a commit that doesn't have it yields None, not a crash."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    repo.commit_paths([yaml], "Create kitchen.yaml")

    assert repo.file_at(yaml, "0" * 40) is None


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


def test_commit_paths_untracked_and_deleted_returns_none(tmp_path: Path) -> None:
    """Deleting a file the work tree/index never held is a no-op, not a git error."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    gone = tmp_path / "ghost.yaml"

    assert repo.commit_paths([gone], "Delete ghost.yaml") is None


def test_commit_paths_records_deletion_of_tracked_file(tmp_path: Path) -> None:
    """A gone-but-tracked file still records its deletion (file already unlinked)."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    repo.commit_paths([yaml], "Create kitchen.yaml")

    yaml.unlink()
    sha = repo.commit_paths([yaml], "Delete kitchen.yaml")

    assert sha
    assert repo.log_file(yaml)[0].message == "Delete kitchen.yaml"


def test_latest_content_walks_back_past_a_deletion(tmp_path: Path) -> None:
    """latest_content returns the newest commit where the file still had content."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    repo.commit_paths([yaml], "Create kitchen.yaml")
    yaml.write_text("v2\n", encoding="utf-8")
    repo.commit_paths([yaml], "Update kitchen.yaml")
    yaml.unlink()
    repo.commit_paths([yaml], "Delete kitchen.yaml")

    result = repo.latest_content(yaml)

    assert result is not None
    _, content = result
    assert content == "v2\n"  # skipped the contentless deletion commit


def test_latest_content_none_when_no_history(tmp_path: Path) -> None:
    """latest_content is None for a path git has never recorded."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()

    assert repo.latest_content(tmp_path / "ghost.yaml") is None


def test_index_lock_path_absolute_returned_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absolute ``--git-path index.lock`` (split git dir) is used as-is."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    abs_lock = tmp_path / ".git" / "index.lock"
    monkeypatch.setattr(
        GitRepo,
        "_run",
        lambda self, args, *, check, cwd=None: subprocess.CompletedProcess(
            args, 0, stdout=f"{abs_lock}\n", stderr=""
        ),
    )

    assert repo._index_lock_path() == abs_lock


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


def test_commit_never_modifies_the_working_tree(tmp_path: Path) -> None:
    """Auto-commit records history; it must not rewrite any working-tree file.

    The clobber concern: the feature only ever ``add``/``commit``s — never
    ``checkout``/``reset``/``stash`` — so a user's on-disk content (even
    uncommitted, unstaged edits to other files) is byte-for-byte preserved.
    """
    _make_repo(tmp_path)
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    # A user file with uncommitted, unstaged working-tree edits.
    user_file = tmp_path / "living_room.yaml"
    user_file.write_text("user edits not yet saved\n", encoding="utf-8")

    ours = tmp_path / "kitchen.yaml"
    ours.write_text("ours\n", encoding="utf-8")
    repo.commit_paths([ours], "Create kitchen.yaml")

    # The user's file on disk is untouched, and so is ours.
    assert user_file.read_text() == "user edits not yet saved\n"
    assert ours.read_text() == "ours\n"


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


def test_deleted_files_ignores_nested_and_non_config(tmp_path: Path) -> None:
    """Only top-level configs are restorable — not nested includes or stray files."""
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    top = tmp_path / "dev.yaml"
    nested = tmp_path / "pkg" / "inc.yaml"
    nested.parent.mkdir()
    other = tmp_path / "notes.txt"
    for path, body in ((top, "d\n"), (nested, "n\n"), (other, "x\n")):
        path.write_text(body, encoding="utf-8")
        repo.commit_paths([path], f"Create {path.name}")
    for path in (top, nested, other):
        path.unlink()
        repo.commit_paths([path], f"Delete {path.name}")

    deleted = repo.deleted_files()
    assert "dev.yaml" in deleted
    assert "pkg/inc.yaml" not in deleted  # nested include, not a device
    assert "notes.txt" not in deleted  # not a config
