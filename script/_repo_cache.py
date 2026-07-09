"""Shared shallow clone/pull cache for the sync scripts' upstream git repos."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def ensure_shallow_git_repo(
    url: str,
    target: Path,
    branch: str,
    *,
    label: str,
    pull: bool = True,
    pull_timeout: int = 120,
    clone_timeout: int = 300,
    allow_existing_non_git: bool = False,
    clone_fail_level: int = logging.WARNING,
    pull_fail_level: int = logging.WARNING,
) -> Path | None:
    """
    Clone *url* into *target* (shallow) or refresh it; return the path or None.

    First run does ``git clone --depth=1 --single-branch --branch=<branch>``;
    a later run with an existing ``.git`` does ``git pull -q --ff-only`` unless
    *pull* is False. Every git failure is non-fatal: a failed pull keeps the
    on-disk snapshot, a failed clone returns None. With *allow_existing_non_git*
    a pre-existing non-git *target* is used as-is rather than cloned into.

    *clone_fail_level* / *pull_fail_level* are the log levels for a failed
    clone / pull; pass ``logging.ERROR`` when the repo is a primary data source
    whose absence or stale refresh yields a result the operator must notice.
    """
    if (target / ".git").exists():
        if pull:
            # Keep the pull non-fatal end to end: a non-zero exit, a timeout, or
            # git missing all fall back to the on-disk snapshot rather than
            # aborting the sync.
            try:
                result = subprocess.run(
                    ["git", "-C", str(target), "pull", "-q", "--ff-only"],
                    check=False,
                    timeout=pull_timeout,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                _LOGGER.log(
                    pull_fail_level,
                    "git pull in %s failed: %s; using existing snapshot",
                    target,
                    exc,
                )
            else:
                if result.returncode != 0:
                    _LOGGER.log(
                        pull_fail_level, "git pull failed in %s; using existing snapshot", target
                    )
        return target
    if allow_existing_non_git and target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("Cloning %s (shallow) to %s", label, target)
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--depth=1",
                "--single-branch",
                f"--branch={branch}",
                url,
                str(target),
            ],
            check=True,
            timeout=clone_timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        _LOGGER.log(clone_fail_level, "Could not clone %s: %s", label, exc)
        return None
    return target
