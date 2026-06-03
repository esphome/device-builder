"""Mutation sites record a rich-message version-history commit."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from .conftest import MakeControllerFactory


def _vh_stub(record: Any) -> SimpleNamespace:
    """Build a version_history stand-in: the given record + a tracking discard_pending."""
    return SimpleNamespace(record_configuration=record, discard_pending=MagicMock())


async def test_update_config_commits_edit_message(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """``devices/update_config`` records an "Edit <file>" commit."""
    controller = make_controller(tmp_path)
    record = AsyncMock(return_value="sha")
    controller._db.version_history = _vh_stub(record)

    await controller.update_config(configuration="kitchen.yaml", content="esphome:\n  name: k\n")

    record.assert_awaited_once_with("kitchen.yaml", "Edit kitchen.yaml")
    # On success the now-redundant catch-all entry is dropped.
    controller._db.version_history.discard_pending.assert_called_once_with("kitchen.yaml")


async def test_disabled_version_history_is_a_noop(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """With version_history None the write still succeeds (history is optional)."""
    controller = make_controller(tmp_path)
    assert controller._db.version_history is None

    await controller.update_config(configuration="kitchen.yaml", content="esphome:\n  name: k\n")

    assert (tmp_path / "kitchen.yaml").read_text() == "esphome:\n  name: k\n"


async def test_commit_failure_does_not_break_the_save(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """A genuine git error is swallowed — the save still lands on disk."""
    controller = make_controller(tmp_path)
    record = AsyncMock(side_effect=subprocess.CalledProcessError(1, "git commit"))
    controller._db.version_history = _vh_stub(record)

    await controller.update_config(configuration="kitchen.yaml", content="esphome:\n  name: k\n")

    record.assert_awaited_once()
    assert (tmp_path / "kitchen.yaml").read_text() == "esphome:\n  name: k\n"
    # On failure the queued catch-all entry is preserved so the debounced
    # flush still records this save — not discarded.
    controller._db.version_history.discard_pending.assert_not_called()


async def test_programming_bug_in_commit_propagates(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """A non-git exception isn't mislabelled as a recoverable hiccup — it propagates."""
    controller = make_controller(tmp_path)
    record = AsyncMock(side_effect=AttributeError("real bug"))
    controller._db.version_history = _vh_stub(record)

    with pytest.raises(AttributeError):
        await controller.update_config(
            configuration="kitchen.yaml", content="esphome:\n  name: k\n"
        )


async def test_concurrent_same_file_saves_each_get_committed(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """Write+commit is serialized per file, so no concurrent save loses its version.

    The commit records on-disk content; without per-file serialization a
    second writer could slip between the first's write and commit and win
    the history slot, dropping a version.
    """
    controller = make_controller(tmp_path)
    committed: list[str] = []

    async def _record(configuration: str, _message: str) -> None:
        # Mirror commit_paths: capture whatever is on disk at commit time.
        # Read off-loop — this runs from inside the (package-frame) commit
        # path, so a blocking read here trips the blockbuster guard.
        content = await asyncio.to_thread(
            lambda: (tmp_path / configuration).read_text(encoding="utf-8")
        )
        committed.append(content)

    controller._db.version_history = _vh_stub(_record)

    await asyncio.gather(
        controller.update_config(configuration="kitchen.yaml", content="A\n"),
        controller.update_config(configuration="kitchen.yaml", content="B\n"),
    )

    # Each commit captured the content its own save wrote — both survive.
    assert sorted(committed) == ["A\n", "B\n"]


def _attach_recorder(controller: object) -> AsyncMock:
    """Attach a recordable version_history stub; return its record mock."""
    record = AsyncMock(return_value="sha")
    controller._db.version_history = _vh_stub(record)  # type: ignore[attr-defined]
    return record


async def test_delete_records_removal(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """``_delete_single`` records a "Delete <file>" commit so it stays restorable."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: k\n", encoding="utf-8")
    record = _attach_recorder(controller)

    await controller._delete_single("kitchen.yaml")

    record.assert_awaited_once_with("kitchen.yaml", "Delete kitchen.yaml")


async def test_archive_records_removal(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """``_archive_single`` records an "Archive <file>" commit."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: k\n", encoding="utf-8")
    record = _attach_recorder(controller)

    await controller._archive_single("kitchen.yaml")

    record.assert_awaited_once_with("kitchen.yaml", "Archive kitchen.yaml")


async def test_apply_restored_yaml_writes_and_records_restore(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """``apply_restored_yaml`` writes the content back and records a "Restore" commit."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("old\n", encoding="utf-8")
    record = _attach_recorder(controller)

    await controller.apply_restored_yaml("kitchen.yaml", "new\n", restored_from="abc1234")

    assert (tmp_path / "kitchen.yaml").read_text() == "new\n"
    record.assert_awaited_once_with("kitchen.yaml", "Restore kitchen.yaml to abc1234")
