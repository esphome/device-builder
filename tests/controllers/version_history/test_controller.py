"""Tests for :class:`VersionHistoryController` (async wrapper over GitRepo)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import Device, DeviceEventData, ErrorCode, EventType


def _rel_path(config_dir: Path):
    def _resolve(configuration: str) -> Path:
        if "/" in configuration or ".." in configuration:
            raise CommandError(ErrorCode.INVALID_ARGS, "bad configuration")
        return config_dir / configuration

    return _resolve


def _make_controller(config_dir: Path) -> VersionHistoryController:
    """Build a controller against a stub DeviceBuilder rooted at *config_dir*."""
    db = SimpleNamespace(
        bus=EventBus(),
        devices=SimpleNamespace(apply_restored_yaml=AsyncMock()),
        settings=SimpleNamespace(
            config_dir=config_dir,
            rel_path=_rel_path(config_dir),
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
    versions = await controller.list_versions(configuration="kitchen.yaml")
    assert versions[0]["message"] == "Create kitchen.yaml"

    # stop() with no debounce flush pending is a clean no-op.
    await controller.stop()
    assert controller._flush_task is None


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

    versions = await controller.list_versions(configuration="kitchen.yaml")
    assert [v["message"] for v in versions] == ["Edit kitchen.yaml"]


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

    versions = await controller.list_versions(configuration="kitchen.yaml")
    # Only the dashboard commit — the catch-all found nothing to commit.
    assert [v["message"] for v in versions] == ["Edit kitchen.yaml via editor"]


async def test_flush_picks_up_edit_arriving_during_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An external edit that lands while the flush is committing isn't stranded.

    Without the drain loop, a change queued during a per-config commit
    would wait for the next scanner event (potentially forever).
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "a.yaml").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("b\n", encoding="utf-8")

    original = controller.record_configuration
    injected = False

    async def _wrapper(configuration: str, message: str) -> str | None:
        nonlocal injected
        result = await original(configuration, message)
        if configuration == "a.yaml" and not injected:
            injected = True
            # Simulate b.yaml being edited externally mid-flush.
            device = Device(name="b", friendly_name="b", configuration="b.yaml")
            controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
        return result

    monkeypatch.setattr(controller, "record_configuration", _wrapper)
    device = Device(name="a", friendly_name="a", configuration="a.yaml")
    controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    assert controller._flush_task is not None
    await controller._flush_task

    # Both got committed by the one flush task — b on the second drain pass.
    assert await controller.list_versions(configuration="a.yaml")
    assert await controller.list_versions(configuration="b.yaml")


async def test_list_and_get_version_round_trip(tmp_path: Path) -> None:
    """list_versions surfaces commits; get_version returns content at a sha."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    yaml.write_text("v2\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Edit kitchen.yaml")

    versions = await controller.list_versions(configuration="kitchen.yaml")
    assert [v["message"] for v in versions] == ["Edit kitchen.yaml", "Create kitchen.yaml"]

    first_sha = versions[1]["sha"]
    got = await controller.get_version(configuration="kitchen.yaml", sha=first_sha)
    assert got["content"] == "v1\n"


async def test_get_diff_returns_unified_diff(tmp_path: Path) -> None:
    """get_diff compares a commit against the working copy."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    sha = await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    yaml.write_text("v2\n", encoding="utf-8")

    result = await controller.get_diff(configuration="kitchen.yaml", sha=sha)
    assert "-v1" in result["diff"] and "+v2" in result["diff"]


async def test_restore_specific_sha_writes_through_devices(tmp_path: Path) -> None:
    """Restore fetches the old content and writes it via the devices persist path."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    sha = await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    yaml.write_text("v2\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Edit kitchen.yaml")

    result = await controller.restore(configuration="kitchen.yaml", sha=sha)

    assert result["content"] == "v1\n"
    controller._db.devices.apply_restored_yaml.assert_awaited_once()
    _, kwargs = controller._db.devices.apply_restored_yaml.call_args
    assert kwargs["restored_from"] == sha[:7]


async def test_restore_deleted_uses_latest_available_version(tmp_path: Path) -> None:
    """With no sha, a deleted config is restored from its last surviving version."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("final\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    yaml.unlink()
    await controller.record_configuration("kitchen.yaml", "Delete kitchen.yaml")

    # It now shows up as deletable and restores its last content.
    deleted = await controller.list_deleted()
    assert {"configuration": "kitchen.yaml"} in deleted

    result = await controller.restore(configuration="kitchen.yaml")
    assert result["content"] == "final\n"
    args, _ = controller._db.devices.apply_restored_yaml.call_args
    assert args[0] == "kitchen.yaml"
    assert args[1] == "final\n"


async def test_history_commands_raise_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With git unavailable, reads return empty and mutators raise NOT_FOUND."""
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    controller = _make_controller(tmp_path)
    await controller.start()

    assert await controller.list_versions(configuration="kitchen.yaml") == []
    assert await controller.list_deleted() == []
    with pytest.raises(CommandError) as exc:
        await controller.restore(configuration="kitchen.yaml", sha="abc1234")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_get_version_rejects_bad_sha(tmp_path: Path) -> None:
    """A non-hex sha is refused before reaching git."""
    controller = _make_controller(tmp_path)
    await controller.start()
    with pytest.raises(CommandError) as exc:
        await controller.get_version(configuration="kitchen.yaml", sha="; rm -rf /")
    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_stop_detaches_listeners_and_flushes_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() detaches listeners and flushes an edit queued in the debounce window."""
    # Long debounce so the flush timer is still pending when we stop —
    # the edit must be committed by stop()'s drain, not dropped.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        30.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    assert controller._flush_task is not None

    await controller.stop()

    assert controller._unsubs == []
    # The queued edit was flushed on shutdown rather than lost.
    assert await controller.list_versions(configuration="kitchen.yaml")
    # A post-stop event must not reach the (now detached) listener.
    controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    assert not controller._pending


async def test_read_commands_reject_path_traversal(tmp_path: Path) -> None:
    """A ``configuration`` escaping the config dir is refused before reaching git."""
    controller = _make_controller(tmp_path)
    await controller.start()
    traversal = "../secrets.yaml"

    for call in (
        controller.list_versions(configuration=traversal),
        controller.get_version(configuration=traversal, sha="abc1234"),
        controller.get_diff(configuration=traversal, sha="abc1234"),
        controller.restore(configuration=traversal),
    ):
        with pytest.raises(CommandError) as exc:
            await call
        assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_restore_unknown_sha_raises_not_found(tmp_path: Path) -> None:
    """Restoring a config to a commit that doesn't contain it raises NOT_FOUND."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    sha = await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    assert sha

    with pytest.raises(CommandError) as exc:
        await controller.restore(configuration="other.yaml", sha=sha)
    assert exc.value.code == ErrorCode.NOT_FOUND
    controller._db.devices.apply_restored_yaml.assert_not_awaited()


async def test_discard_pending_drops_a_queued_catch_all_entry(tmp_path: Path) -> None:
    """A specific commit supersedes a queued generic external-edit entry."""
    controller = _make_controller(tmp_path)
    await controller.start()
    controller._pending["kitchen.yaml"] = "Edit kitchen.yaml"

    controller.discard_pending("kitchen.yaml")

    assert "kitchen.yaml" not in controller._pending
    controller.discard_pending("kitchen.yaml")  # idempotent / unknown key is fine


async def test_restore_commits_pending_external_edit_first(tmp_path: Path) -> None:
    """A restore captures a queued external edit before overwriting it.

    Otherwise restoring over an edit that's still in the debounce window
    would drop that version from history entirely.
    """
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"
    yaml.write_text("v1\n", encoding="utf-8")
    create_sha = await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    assert create_sha
    # External edit detected by the scanner but not yet flushed.
    yaml.write_text("v2-external\n", encoding="utf-8")
    controller._pending["kitchen.yaml"] = "Edit kitchen.yaml"

    result = await controller.restore(configuration="kitchen.yaml", sha=create_sha)

    assert result["content"] == "v1\n"
    # The just-overwritten external edit is now in history (committed
    # before the restore wrote the old content back).
    versions = await controller.list_versions(configuration="kitchen.yaml")
    assert [v["message"] for v in versions] == ["Edit kitchen.yaml", "Create kitchen.yaml"]
    captured = await controller.get_version(configuration="kitchen.yaml", sha=versions[0]["sha"])
    assert captured["content"] == "v2-external\n"


async def test_flush_task_failure_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An error escaping the per-config guard is logged via the done-callback."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()

    async def _boom() -> None:
        raise RuntimeError("drain bug")

    monkeypatch.setattr(controller, "_flush_pending", _boom)
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    with caplog.at_level(logging.WARNING):
        controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
        task = controller._flush_task
        assert task is not None
        with suppress(RuntimeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    assert any("flush task failed" in rec.message for rec in caplog.records)


async def test_restore_without_history_raises_not_found(tmp_path: Path) -> None:
    """Restoring a config with no recorded history raises NOT_FOUND."""
    controller = _make_controller(tmp_path)
    await controller.start()

    with pytest.raises(CommandError) as exc:
        await controller.restore(configuration="ghost.yaml")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_get_version_unknown_sha_raises_not_found(tmp_path: Path) -> None:
    """get_version of a valid-but-nonexistent commit raises NOT_FOUND."""
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")

    with pytest.raises(CommandError) as exc:
        await controller.get_version(configuration="kitchen.yaml", sha="0" * 40)
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_catch_all_flush_survives_a_failing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git failure on one config doesn't strand the rest of the flush batch."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "good.yaml").write_text("ok\n", encoding="utf-8")

    original = controller.record_configuration

    async def _maybe_fail(configuration: str, message: str) -> str | None:
        if configuration == "bad.yaml":
            raise subprocess.CalledProcessError(1, "git commit")
        return await original(configuration, message)

    monkeypatch.setattr(controller, "record_configuration", _maybe_fail)
    for name in ("bad.yaml", "good.yaml"):
        device = Device(name=name, friendly_name=name, configuration=name)
        controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
    assert controller._flush_task is not None
    await controller._flush_task

    # The good config still committed despite bad.yaml's git failure.
    assert await controller.list_versions(configuration="good.yaml")


async def test_degraded_flips_after_repeated_failures_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated git failures flag the feature degraded; a success clears it."""
    controller = _make_controller(tmp_path)
    await controller.start()
    yaml = tmp_path / "kitchen.yaml"

    real_run = subprocess.run
    fail = True

    def _maybe_fail(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if fail and "commit" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run", _maybe_fail
    )

    # A one-off failure is not yet "degraded".
    yaml.write_text("v1\n", encoding="utf-8")
    with suppress(subprocess.CalledProcessError):
        await controller.record_configuration("kitchen.yaml", "Create kitchen.yaml")
    assert not controller.degraded

    # Cross the threshold of consecutive failures.
    for _ in range(2):
        with suppress(subprocess.CalledProcessError):
            await controller.record_configuration("kitchen.yaml", "Edit kitchen.yaml")
    assert controller.degraded

    # A subsequent successful commit clears the degraded flag.
    fail = False
    yaml.write_text("v2\n", encoding="utf-8")
    await controller.record_configuration("kitchen.yaml", "Edit kitchen.yaml")
    assert not controller.degraded


async def test_catch_all_programming_bug_propagates_to_done_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-git bug in the catch-all isn't masked — it reaches the done-callback."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()

    async def _bug(_configuration: str, _message: str) -> str | None:
        raise AttributeError("real bug")

    monkeypatch.setattr(controller, "record_configuration", _bug)
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    with caplog.at_level(logging.WARNING):
        controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
        task = controller._flush_task
        assert task is not None
        with suppress(AttributeError):
            await task
        await asyncio.sleep(0)  # let the done-callback run

    # Surfaced as a task failure, not swallowed as a routine "catch-all failed".
    assert any("flush task failed" in rec.message for rec in caplog.records)


async def test_commit_propagates_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """commit() raises on a real git error so callers can tell it from a no-op."""
    controller = _make_controller(tmp_path)
    await controller.start()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("git exploded")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run",
        _boom,
    )
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        await controller.commit([tmp_path / "kitchen.yaml"], "msg")


async def test_catch_all_warns_on_real_commit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A genuine git failure in the catch-all now reaches the WARNING (not dead code)."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.controller._DEBOUNCE_SECONDS",
        0.0,
    )
    controller = _make_controller(tmp_path)
    await controller.start()
    (tmp_path / "kitchen.yaml").write_text("v1\n", encoding="utf-8")

    real_run = subprocess.run

    def _fail_commit(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "commit" in cmd:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.version_history.git_repo.subprocess.run",
        _fail_commit,
    )
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    with caplog.at_level(logging.WARNING):
        controller._db.bus.fire(EventType.DEVICE_UPDATED, DeviceEventData(device=device))
        assert controller._flush_task is not None
        await controller._flush_task

    assert any("catch-all failed for kitchen.yaml" in rec.message for rec in caplog.records)
