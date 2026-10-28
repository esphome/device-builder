"""Tests for ``files/rename`` and the include rewriting behind it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.devices import file_change
from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.controllers.files import controller as controller_module
from esphome_device_builder.controllers.files.rename import plan_rename, rewrite_includes
from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.async_ import run_in_executor
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import ErrorCode, UserPreferences

# Captured before the ``setup`` fixture stubs it out.
_real_commit_history_many = file_change._commit_history_many


@pytest.mark.parametrize(
    ("text", "folder", "expected"),
    [
        # a device at the top level
        ("base: !include common/base.yaml\n", "", "base: !include common/main.yaml\n"),
        ('base: !include "common/base.yaml"\n', "", 'base: !include "common/main.yaml"\n'),
        (
            "base: !include {file: common/base.yaml, vars: {a: 1}}\n",
            "",
            "base: !include {file: common/main.yaml, vars: {a: 1}}\n",
        ),
        # block form: the path sits under ``file:`` on the next, deeper line
        (
            "b: !include\n  file: common/base.yaml\n  vars: {a: 1}\n",
            "",
            "b: !include\n  file: common/main.yaml\n  vars: {a: 1}\n",
        ),
        ("b: !include\nfile: common/base.yaml\n", "", "b: !include\nfile: common/base.yaml\n"),
        # a sibling package refers to it relative to its own folder
        ("<<: !include ./base.yaml\n", "common", "<<: !include ./main.yaml\n"),
        # untouched: other files, comments, substitutions, strings, longer names
        ("x: !include common/other.yaml\n", "", "x: !include common/other.yaml\n"),
        ("# x: !include common/base.yaml\n", "", "# x: !include common/base.yaml\n"),
        ("x: !include ${d}/base.yaml\n", "", "x: !include ${d}/base.yaml\n"),
        ('note: "!include common/base.yaml"\n', "", 'note: "!include common/base.yaml"\n'),
        ('m: "Use !include common/base.yaml"\n', "", 'm: "Use !include common/base.yaml"\n'),
        ("m: 'Use !include common/base.yaml'\n", "", "m: 'Use !include common/base.yaml'\n"),
        ("x: !include common/base.yaml.bak\n", "", "x: !include common/base.yaml.bak\n"),
    ],
)
def test_rewrite_includes(text: str, folder: str, expected: str) -> None:
    rewritten = rewrite_includes(
        text, folder=folder, old="common/base.yaml", new="common/main.yaml"
    )
    assert rewritten == expected


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_plan_rename_covers_devices_and_packages(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
            "garage.yaml": "packages:\n  base: !include ${dir}/base.yaml\n",
            "named.yaml": "packages:\n  base: !include common/${name}.yaml\n",
            "other.yaml": "logger:  # base.yaml\n",
            "secrets.yaml": "x: !include common/base.yaml\n",
            "common/base.yaml": "wifi: !include wifi.yaml\n",
            "common/extra.yaml": "<<: !include base.yaml\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts == {
        "kitchen.yaml": "packages:\n  base: !include common/main.yaml\n",
        "common/extra.yaml": "<<: !include main.yaml\n",
    }
    assert plan.originals["kitchen.yaml"] == "packages:\n  base: !include common/base.yaml\n"
    assert plan.remaining == {"garage.yaml", "named.yaml"}


@pytest.fixture
def files(tmp_path: Path, setup: tuple[FilesController, object, list]) -> FilesController:
    controller, _, _ = setup
    _tree(
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


def test_plan_rename_keeps_crlf_line_endings(tmp_path: Path) -> None:
    (tmp_path / "kitchen.yaml").write_bytes(b"packages:\r\n  base: !include common/base.yaml\r\n")

    plan = plan_rename(tmp_path, "common/base.yaml", "common/main.yaml")

    assert plan.texts["kitchen.yaml"] == "packages:\r\n  base: !include common/main.yaml\r\n"
    assert plan.originals["kitchen.yaml"].count("\r\n") == 2


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
    _tree(tmp_path, {"common/base #1.yaml": "a: 1\n"})
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
    _tree(
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


async def test_rename_records_one_real_history_commit(
    tmp_path: Path,
    files: FilesController,
    setup: tuple[FilesController, Any, list],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices, _ = setup
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
    # as if created through files/create; the start-up baseline covers devices only
    await history.commit([tmp_path / "common/base.yaml"], "Create common/base.yaml")
    devices._db.version_history = history

    with patch.object(devices, "get_devices", return_value=_users()):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")
    await history.stop()

    # one commit: the old path removed, the new one added, the referrer rewritten
    repo = history._repo
    heads = {
        rel: (await run_in_executor(repo.log_file, tmp_path / rel))[0]
        for rel in ("common/base.yaml", "common/main.yaml", "kitchen.yaml")
    }
    assert {c.message for c in heads.values()} == {"Rename common/base.yaml to common/main.yaml"}
    assert len({c.sha for c in heads.values()}) == 1
    sha = heads["kitchen.yaml"].sha
    assert await run_in_executor(repo.file_at, tmp_path / "common/base.yaml", sha) is None
    main = await run_in_executor(repo.file_at, tmp_path / "common/main.yaml", sha)
    assert main == "wifi: !include wifi.yaml\n"


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


async def test_rename_refuses_when_a_new_referrer_appears_before_the_locks(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, _, commits = setup
    calls = 0

    def _plan_then_add_referrer(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        plan = plan_rename(*args)
        if calls == 1:
            _tree(tmp_path, {"late.yaml": "packages:\n  b: !include common/base.yaml\n"})
        return plan

    with (
        patch(f"{controller_module.__name__}.plan_rename", _plan_then_add_referrer),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert exc_info.value.code is ErrorCode.PRECONDITION_FAILED
    assert (tmp_path / "common/base.yaml").exists()
    assert commits == []
