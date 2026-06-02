"""Tests for :class:`VersionHistoryController` (async wrapper over GitRepo)."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers.version_history import VersionHistoryController


def _make_controller(config_dir: Path) -> VersionHistoryController:
    """Build a controller against a stub DeviceBuilder rooted at *config_dir*."""
    db = SimpleNamespace(
        settings=SimpleNamespace(
            config_dir=config_dir,
            rel_path=lambda configuration: config_dir / configuration,
        )
    )
    return VersionHistoryController(db)  # type: ignore[arg-type]


async def test_start_enables_and_commits(tmp_path: Path) -> None:
    """After start the controller commits a config by its dashboard name."""
    controller = _make_controller(tmp_path)
    await controller.start()
    assert controller.enabled

    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    sha = await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")

    assert sha
    versions = controller._repo.log_file(tmp_path / "kitchen.yaml")
    assert versions[0].message == "Create kitchen.yaml"


async def test_disabled_when_no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No git binary → controller disabled, commit is a quiet no-op."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    controller = _make_controller(tmp_path)
    await controller.start()

    assert not controller.enabled
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    assert await controller.record_configuration("kitchen.yaml", "msg") is None


async def test_commit_swallows_unexpected_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blow-up inside the git layer never propagates to the caller."""
    controller = _make_controller(tmp_path)
    await controller.start()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("git exploded")

    # An unexpected (non-CalledProcessError) blow-up in the git layer.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run",
        _boom,
    )
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    # Must return None, not raise — history can't break a save.
    assert await controller.commit([tmp_path / "kitchen.yaml"], "msg") is None
