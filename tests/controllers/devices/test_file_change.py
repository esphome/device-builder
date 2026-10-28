"""``commit_file_change``: the multi-file write + history commit behind ``files/*``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.controllers.devices.file_change import commit_file_change

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


async def test_git_failure_keeps_the_change_and_the_pending_entry(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    commit = AsyncMock(side_effect=subprocess.CalledProcessError(1, "git commit"))
    discard = MagicMock()
    controller._db.version_history = SimpleNamespace(  # type: ignore[assignment]
        commit=commit, discard_pending=discard
    )

    await commit_file_change(
        controller,
        ["kitchen.yaml"],
        lambda: _write(tmp_path / "kitchen.yaml"),
        message="Edit kitchen.yaml",
    )

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == "a: 1\n"
    # both the pre-change snapshot and the change commit were attempted
    assert [c.args[1] for c in commit.await_args_list] == [
        "Before: Edit kitchen.yaml",
        "Edit kitchen.yaml",
    ]
    # the debounced catch-all still records the save
    discard.assert_not_called()
