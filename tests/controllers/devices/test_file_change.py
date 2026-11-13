"""``commit_file_change``: the multi-file write + history commit behind ``files/*``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome_device_builder.controllers.devices.file_change import commit_file_change
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory


def _write(path: Path) -> None:
    path.write_text("a: 1\n", encoding="utf-8")


async def test_disabled_version_history_still_applies_the_change(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    assert controller._db.version_history is None

    await commit_file_change(
        controller,
        ["kitchen.yaml"],
        lambda: _write(tmp_path / "kitchen.yaml"),
        message="Edit kitchen.yaml",
    )

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == "a: 1\n"


def _failing_history(controller: Any) -> AsyncMock:
    commit = AsyncMock(side_effect=subprocess.CalledProcessError(1, "git commit"))
    controller._db.version_history = SimpleNamespace(commit=commit, discard_pending=MagicMock())
    return commit


async def test_failed_snapshot_refuses_the_change(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    commit = _failing_history(controller)

    with pytest.raises(CommandError) as exc_info:
        await commit_file_change(
            controller,
            ["kitchen.yaml"],
            lambda: _write(tmp_path / "kitchen.yaml"),
            message="Edit kitchen.yaml",
        )

    assert exc_info.value.code is ErrorCode.UNAVAILABLE
    assert not (tmp_path / "kitchen.yaml").exists()
    assert [c.args[1] for c in commit.await_args_list] == ["Before: Edit kitchen.yaml"]


async def test_git_failure_without_a_snapshot_keeps_the_change_and_the_pending_entry(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    commit = _failing_history(controller)

    await commit_file_change(
        controller,
        ["kitchen.yaml"],
        lambda: _write(tmp_path / "kitchen.yaml"),
        message="Edit kitchen.yaml",
        snapshot=False,
    )

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == "a: 1\n"
    assert [c.args[1] for c in commit.await_args_list] == ["Edit kitchen.yaml"]
    # the debounced catch-all still records the save
    controller._db.version_history.discard_pending.assert_not_called()


async def test_a_refused_precheck_records_nothing(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    commit = AsyncMock()
    controller._db.version_history = SimpleNamespace(commit=commit, discard_pending=MagicMock())

    def _refuse() -> None:
        raise CommandError(ErrorCode.NOT_FOUND, "gone")

    with (
        patch.object(controller, "_after_yaml_write") as after_write,
        pytest.raises(CommandError),
    ):
        await commit_file_change(
            controller,
            ["kitchen.yaml"],
            lambda: _write(tmp_path / "kitchen.yaml"),
            message="Edit kitchen.yaml",
            precheck=_refuse,
        )

    commit.assert_not_awaited()
    after_write.assert_not_called()
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_a_change_failing_part_way_still_refreshes_the_caches(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)

    def _half() -> None:
        _write(tmp_path / "kitchen.yaml")
        raise OSError("disk full")

    with (
        patch.object(controller, "_after_yaml_write") as after_write,
        pytest.raises(OSError, match="disk full"),
    ):
        await commit_file_change(
            controller, ["kitchen.yaml", "garage.yaml"], _half, message="Edit both"
        )

    assert [c.args[0] for c in after_write.call_args_list] == ["kitchen.yaml", "garage.yaml"]
