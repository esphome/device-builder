"""Version history seeds and lists devices below ``devices/`` like top-level ones."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from esphome_device_builder.controllers.version_history.git_repo import GitRepo

_GIT = shutil.which("git") or "git"


def _tracked(cwd: Path) -> list[str]:
    result = subprocess.run(  # noqa: S603
        [_GIT, "ls-files"], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.split()


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_init_seed_takes_every_device_and_no_package_or_secrets(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "kitchen.yaml": "k\n",
            "devices/lights/desk.yaml": "d\n",
            "devices/garage/secrets.yaml": "s\n",
            "secrets.ci.yaml": "s\n",
            "common/base.yaml": "b\n",
        },
    )

    GitRepo(config_dir=tmp_path).discover_or_init()

    assert _tracked(tmp_path) == [".gitignore", "devices/lights/desk.yaml", "kitchen.yaml"]


def test_deleted_files_lists_a_deleted_nested_device_but_not_a_package(tmp_path: Path) -> None:
    repo = GitRepo(config_dir=tmp_path)
    repo.discover_or_init()
    nested = tmp_path / "devices" / "lights" / "desk.yaml"
    package = tmp_path / "common" / "base.yaml"
    for path in (nested, package):
        path.parent.mkdir(parents=True)
        path.write_text("x\n", encoding="utf-8")
        repo.commit_paths([path], f"Create {path.name}")
        path.unlink()
        repo.commit_paths([path], f"Delete {path.name}")

    assert repo.deleted_files() == ["devices/lights/desk.yaml"]
