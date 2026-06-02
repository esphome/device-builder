"""Tests for :class:`VersionHistoryController` (async wrapper over GitRepo)."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import Device, DeviceEventData, EventType


def _make_controller(config_dir: Path) -> VersionHistoryController:
    """Build a controller against a stub DeviceBuilder rooted at *config_dir*."""
    db = SimpleNamespace(
        bus=EventBus(),
        settings=SimpleNamespace(
            config_dir=config_dir,
            rel_path=lambda configuration: config_dir / configuration,
        ),
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


async def test_external_edit_committed_via_scanner_catch_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scanner DEVICE_UPDATED commits the externally-edited YAML (debounced)."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")

    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    # Let the debounced flush task run.
    assert controller._flush_task is not None
    await controller._flush_task

    versions = controller._repo.log_file(tmp_path / "kitchen.yaml")
    assert [c.message for c in versions] == ["Edit kitchen.yaml"]


async def test_dashboard_commit_makes_catch_all_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashboard rich-message commit means the later catch-all adds nothing."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")

    # Dashboard commits immediately with its own message.
    await controller.record_configuration("kitchen.yaml", "Edit kitchen.yaml via editor")
    # Scanner then fires for the same on-disk change.
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    assert controller._flush_task is not None
    await controller._flush_task

    versions = controller._repo.log_file(tmp_path / "kitchen.yaml")
    # Only the dashboard commit — the catch-all found nothing to commit.
    assert [c.message for c in versions] == ["Edit kitchen.yaml via editor"]


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
