"""Tests for the ``files/rename`` command: locking, history, refusals and reporting."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from esphome_device_builder.controllers.devices import archive, file_change
from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.controllers.files import controller as controller_module
from esphome_device_builder.controllers.files.rename import plan_rename
from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.async_ import run_in_executor
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import ErrorCode, UserPreferences

from .conftest import UNREWRITABLE_INCLUDES, write_tree

# Captured before the ``setup`` fixture stubs it out.
_real_commit_history_many = file_change._commit_history_many


@pytest.fixture
def files(tmp_path: Path, setup: tuple[FilesController, object, list]) -> FilesController:
    controller, _, _ = setup
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
            "garage.yaml": "packages:\n  base: !include ${dir}/base.yaml\n",
            "common/base.yaml": "wifi: !include wifi.yaml\n",
            "common/wifi.yaml": "ssid: x\n",
        },
    )
    return controller


def _users() -> list[SimpleNamespace]:
    deps = ("common/base.yaml", "common/wifi.yaml")
    return [
        SimpleNamespace(configuration="kitchen.yaml", dependencies=deps),
        SimpleNamespace(configuration="garage.yaml", dependencies=deps),
    ]


async def test_rename_renames_rewrites_and_reports(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["garage.yaml"],
    }
    assert not (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "common/main.yaml").read_text(encoding="utf-8") == (
        "wifi: !include wifi.yaml\n"
    )
    assert "common/main.yaml" in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # one history commit covering every touched file
    assert {c for c, _ in commits} == {"common/base.yaml", "common/main.yaml", "kitchen.yaml"}
    assert {m for _, m in commits} == {"Rename common/base.yaml to common/main.yaml"}


@pytest.mark.parametrize(
    ("new_path", "code"),
    [
        ("common/wifi.yaml", ErrorCode.ALREADY_EXISTS),
        ("common/base.yaml", ErrorCode.INVALID_ARGS),
        ("shared/base.yaml", ErrorCode.INVALID_ARGS),
        ("common/sub/base.yaml", ErrorCode.INVALID_ARGS),
        ("base.yaml", ErrorCode.INVALID_ARGS),
        ("../x/base.yaml", ErrorCode.INVALID_ARGS),
        ("common/base #1.yaml", ErrorCode.INVALID_ARGS),
        ("common/@main.yaml", ErrorCode.INVALID_ARGS),
    ],
)
async def test_rename_refusals_change_nothing(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    new_path: str,
    code: ErrorCode,
) -> None:
    _, devices, commits = setup
    before = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")

    with (
        patch.object(devices, "get_devices", return_value=_users()),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path=new_path)

    assert exc_info.value.code is code
    assert (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == before
    assert commits == []


async def test_rename_refuses_a_source_name_that_needs_quoting(
    tmp_path: Path, files: FilesController
) -> None:
    write_tree(tmp_path, {"common/base #1.yaml": "a: 1\n"})
    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/base #1.yaml", new_path="common/main.yaml")
    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert (tmp_path / "common/base #1.yaml").exists()


async def test_rename_missing_source(files: FilesController) -> None:
    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/nope.yaml", new_path="common/new.yaml")
    assert exc_info.value.code is ErrorCode.NOT_FOUND


async def test_rename_flags_a_device_with_a_missed_reference_despite_a_rewrite(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "garage.yaml": (
                "packages:\n  a: !include common/base.yaml\n  b: !include common/${name}.yaml\n"
            ),
        },
    )
    users = [SimpleNamespace(configuration="garage.yaml", dependencies=("common/base.yaml",))]

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert "garage.yaml" in result["rewritten"]
    assert result["unresolved"] == ["garage.yaml"]


async def _real_history(devices: Any, monkeypatch: pytest.MonkeyPatch) -> VersionHistoryController:
    """Attach a started, real version history; its baseline holds the devices only."""
    monkeypatch.setattr(file_change, "_commit_history_many", _real_commit_history_many)
    prefs = UserPreferences(version_history_enabled=True)
    history = VersionHistoryController(
        SimpleNamespace(  # type: ignore[arg-type]
            bus=EventBus(),
            devices=devices,
            settings=devices._db.settings,
            config=SimpleNamespace(prefs=SimpleNamespace(snapshot=lambda: prefs)),
        )
    )
    await history.start()
    devices._db.version_history = history
    return history


async def test_rename_records_one_real_history_commit(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})
    history = await _real_history(devices, monkeypatch)

    with patch.object(devices, "get_devices", return_value=_users()):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")
    await history.stop()

    repo = history._repo
    # the untracked package was recorded first
    base_log = await run_in_executor(repo.log_file, tmp_path / "common/base.yaml")
    assert [c.message for c in base_log] == [
        "Rename common/base.yaml to common/main.yaml",
        "Before: Rename common/base.yaml to common/main.yaml",
    ]
    # then one commit: the old path removed, the new one added, the referrer rewritten
    heads = {
        rel: (await run_in_executor(repo.log_file, tmp_path / rel))[0]
        for rel in ("common/base.yaml", "common/main.yaml", "kitchen.yaml", "archive/old.yaml")
    }
    assert {c.message for c in heads.values()} == {"Rename common/base.yaml to common/main.yaml"}
    assert len({c.sha for c in heads.values()}) == 1
    sha = heads["kitchen.yaml"].sha
    assert await run_in_executor(repo.file_at, tmp_path / "common/base.yaml", sha) is None
    main = await run_in_executor(repo.file_at, tmp_path / "common/main.yaml", sha)
    assert main == "wifi: !include wifi.yaml\n"


async def test_delete_keeps_an_untracked_package_in_history(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
    history = await _real_history(devices, monkeypatch)

    await files.delete(path="common/wifi.yaml")
    await history.stop()

    assert not (tmp_path / "common/wifi.yaml").exists()
    latest = await run_in_executor(history._repo.latest_content, tmp_path / "common/wifi.yaml")
    assert latest is not None
    assert latest[1] == "ssid: x\n"


async def test_rename_failed_move_restores_the_referrers(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup
    before = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")

    with (
        patch.object(devices, "get_devices", return_value=_users()),
        patch.object(Path, "rename", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == before
    assert (tmp_path / "common/base.yaml").exists()
    assert not (tmp_path / "common/main.yaml").exists()
    assert commits == []


@pytest.mark.parametrize(
    ("path", "new_path"),
    [("common/base.yaml", "common/wifi.yaml"), ("common/nope.yaml", "common/new.yaml")],
)
async def test_refused_rename_records_nothing_in_history(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    new_path: str,
) -> None:
    _, devices, _ = setup
    history = await _real_history(devices, monkeypatch)

    with pytest.raises(CommandError):
        await files.rename(path=path, new_path=new_path)
    await history.stop()

    for rel in ("common/base.yaml", "common/wifi.yaml"):
        assert await run_in_executor(history._repo.log_file, tmp_path / rel) == []


async def test_rename_is_refused_when_the_snapshot_fails(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
    monkeypatch.setattr(file_change, "_commit_history_many", _real_commit_history_many)
    devices._db.version_history = SimpleNamespace(
        commit=AsyncMock(side_effect=OSError("disk full")), discard_pending=MagicMock()
    )
    before = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert exc_info.value.code is ErrorCode.UNAVAILABLE
    assert (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == before


async def test_rename_names_a_referrer_it_could_not_restore(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"lounge.yaml": "packages:\n  base: !include common/base.yaml\n"})
    real_write = controller_module.write_user_yaml

    def _write(path: Path, text: str) -> None:
        if path.name == "kitchen.yaml" and "common/base.yaml" in text:
            raise OSError("read-only")  # the restore
        real_write(path, text)

    with (
        patch.object(devices, "get_devices", return_value=_users()),
        patch.object(controller_module, "write_user_yaml", _write),
        patch.object(Path, "rename", side_effect=OSError("disk full")),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert exc_info.value.code is ErrorCode.INTERNAL_ERROR
    assert "kitchen.yaml" in str(exc_info.value)
    assert "lounge.yaml" not in str(exc_info.value)
    assert str(exc_info.value.__cause__) == "disk full"
    # every other referrer was still put back
    lounge = (tmp_path / "lounge.yaml").read_text(encoding="utf-8")
    assert "common/base.yaml" in lounge


async def test_rename_refuses_when_a_new_referrer_appears_before_the_locks(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, _, commits = setup
    calls = 0

    def _plan_then_add_referrer(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        plan = plan_rename(*args, **kwargs)
        if calls == 1:
            write_tree(tmp_path, {"late.yaml": "packages:\n  b: !include common/base.yaml\n"})
        return plan

    with (
        patch(f"{controller_module.__name__}.plan_rename", _plan_then_add_referrer),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert exc_info.value.code is ErrorCode.PRECONDITION_FAILED
    assert (tmp_path / "common/base.yaml").exists()
    assert commits == []


async def test_rename_skips_the_files_the_user_unticked(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup
    write_tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(
            path="common/base.yaml", new_path="common/main.yaml", skip=["archive/old.yaml"]
        )

    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["garage.yaml"],
    }
    old = (tmp_path / "archive/old.yaml").read_text(encoding="utf-8")
    assert old == "base: !include common/base.yaml\n"
    assert "archive/old.yaml" not in {c for c, _ in commits}


async def test_rename_warns_through_unskipped_files_only(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": "b: !include common/base.yaml\n",
            "garage.yaml": "b: !include bridge/b.yaml\n",
            "shed.yaml": "b: !include bridge/b.yaml\nd: !include common/dyn.yaml\n",
            "bridge/b.yaml": "b: !include ../common/base.yaml\n",
            "common/dyn.yaml": "x: !include ${d}/base.yaml\n",
        },
    )
    users = [
        SimpleNamespace(configuration="kitchen.yaml", dependencies=("common/base.yaml",)),
        SimpleNamespace(
            configuration="garage.yaml", dependencies=("bridge/b.yaml", "common/base.yaml")
        ),
        SimpleNamespace(
            configuration="shed.yaml",
            dependencies=("bridge/b.yaml", "common/base.yaml", "common/dyn.yaml"),
        ),
    ]

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(
            path="common/base.yaml",
            new_path="common/main.yaml",
            skip=["kitchen.yaml", "bridge/b.yaml"],
        )

    # skipped files are no evidence; shed still reaches the old name through common/dyn.yaml
    assert result["rewritten"] == []
    assert result["unresolved"] == ["shed.yaml"]


async def test_rename_reports_an_archived_file_it_could_not_rewrite(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"archive/old.yaml": "base: !include ${d}/base.yaml\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["unresolved"] == ["archive/old.yaml", "garage.yaml"]


@pytest.mark.parametrize("skip", ["archive/old.yaml", ["a.yaml", 1], {"a.yaml": 1}])
async def test_rename_refuses_a_malformed_skip(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    skip: Any,
) -> None:
    _, _, commits = setup

    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/base.yaml", new_path="common/main.yaml", skip=skip)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert (tmp_path / "common/base.yaml").exists()
    assert commits == []


@pytest.mark.parametrize("operation", [archive.unarchive_single, archive.delete_archived_single])
async def test_archive_operations_wait_for_a_rename_rewriting_the_archived_file(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    operation: Any,
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"archive/old.yaml": "base: !include common/base.yaml\n"})
    replanning, resume = threading.Event(), threading.Event()
    calls = 0

    def _pause_under_the_locks(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            replanning.set()
            resume.wait(5)
        return plan_rename(*args, **kwargs)

    with patch(f"{controller_module.__name__}.plan_rename", _pause_under_the_locks):
        rename = asyncio.create_task(
            files.rename(path="common/base.yaml", new_path="common/main.yaml")
        )
        await asyncio.to_thread(replanning.wait, 5)
        other = asyncio.create_task(operation(devices, "old.yaml"))
        done, _ = await asyncio.wait({other}, timeout=0.2)
        resume.set()
        await rename
        await other

    assert not done
    assert not (tmp_path / "archive/old.yaml").exists()
    if operation is archive.unarchive_single:
        restored = (tmp_path / "old.yaml").read_text(encoding="utf-8")
        assert restored == "base: !include common/main.yaml\n"


@pytest.mark.parametrize("rel", ["garage.yaml", "archive/old.yaml"])
async def test_rename_reports_a_multiline_flow_include(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    rel: str,
) -> None:
    _, devices, _ = setup
    write_tree(
        tmp_path,
        {"garage.yaml": "x: 1\n", rel: 'b: !include {\n  file: "common/${name}.yaml"\n}\n'},
    )

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["rewritten"] == ["kitchen.yaml"]
    assert rel in result["unresolved"]


@pytest.mark.parametrize("text", UNREWRITABLE_INCLUDES)
async def test_rename_reports_a_package_it_could_not_rewrite(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, object, list],
    text: str,
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"common/wrapper.yaml": text})

    # no device uses the file, so only the direct scan can flag the package
    with patch.object(devices, "get_devices", return_value=[]):
        usage = await files.usage(path="common/base.yaml")
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert usage["referenced_by"] == ["common/wrapper.yaml", "kitchen.yaml"]
    assert usage["rewritable"] == ["kitchen.yaml"]
    assert result["rewritten"] == ["kitchen.yaml"]
    # garage.yaml's substitution-built include: no loaded device maps it either
    assert result["unresolved"] == ["common/wrapper.yaml", "garage.yaml"]


async def test_rename_reports_files_no_loaded_device_reaches(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"common/extra.yaml": "b: !include ${dir}/base.yaml\n"})
    # garage.yaml's config isn't loaded yet, kitchen.yaml's doesn't use extra.yaml
    users = [
        SimpleNamespace(configuration="kitchen.yaml", dependencies=("common/base.yaml",)),
        SimpleNamespace(configuration="garage.yaml", dependencies=None),
    ]

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["unresolved"] == ["common/extra.yaml", "garage.yaml"]


async def test_usage_and_rename_skip_an_unrelated_incomplete_file(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, _ = setup
    write_tree(tmp_path, {"common/broken.yaml": "b: !include { ?\n"})

    with patch.object(devices, "get_devices", return_value=_users()):
        usage = await files.usage(path="common/wifi.yaml")
        result = await files.rename(path="common/wifi.yaml", new_path="common/net.yaml")

    assert usage["referenced_by"] == ["common/base.yaml"]
    assert result["rewritten"] == ["common/base.yaml"]
