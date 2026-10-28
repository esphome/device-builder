"""Tests for ``files/rename`` and the include rewriting behind it."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.controllers.files.rename import plan_rename, rewrite_includes
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


@pytest.mark.parametrize(
    ("text", "read_dir", "write_dir", "expected"),
    [
        # a device at the top level
        ("base: !include common/base.yaml\n", "", "", "base: !include shared/base.yaml\n"),
        ('base: !include "common/base.yaml"\n', "", "", 'base: !include "shared/base.yaml"\n'),
        (
            "base: !include {file: common/base.yaml, vars: {a: 1}}\n",
            "",
            "",
            "base: !include {file: shared/base.yaml, vars: {a: 1}}\n",
        ),
        # a sibling package refers to it relative to its own folder
        ("<<: !include base.yaml\n", "common", "common", "<<: !include ../shared/base.yaml\n"),
        # untouched: other files, comments, substitutions
        ("x: !include common/other.yaml\n", "", "", "x: !include common/other.yaml\n"),
        ("# x: !include common/base.yaml\n", "", "", "# x: !include common/base.yaml\n"),
        ("x: !include ${d}/base.yaml\n", "", "", "x: !include ${d}/base.yaml\n"),
        # the moved file rebases its own relative includes
        ("wifi: !include wifi.yaml\n", "common", "shared", "wifi: !include ../common/wifi.yaml\n"),
    ],
)
def test_rewrite_includes(text: str, read_dir: str, write_dir: str, expected: str) -> None:
    assert (
        rewrite_includes(
            text,
            read_dir=read_dir,
            write_dir=write_dir,
            old="common/base.yaml",
            new="shared/base.yaml",
        )
        == expected
    )


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_plan_rename_covers_devices_packages_and_the_moved_file(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include common/base.yaml\n",
            "other.yaml": "logger:\n",
            "secrets.yaml": "x: !include common/base.yaml\n",
            "common/base.yaml": "wifi: !include wifi.yaml\n",
            "common/wifi.yaml": "ssid: x\n",
            "common/extra.yaml": "<<: !include base.yaml\n",
        },
    )

    plan = plan_rename(tmp_path, "common/base.yaml", "shared/base.yaml")

    assert plan == {
        "kitchen.yaml": "packages:\n  base: !include shared/base.yaml\n",
        "common/extra.yaml": "<<: !include ../shared/base.yaml\n",
        "common/base.yaml": "wifi: !include ../common/wifi.yaml\n",
    }


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


async def test_rename_moves_rewrites_and_reports(
    tmp_path: Path, files: FilesController, setup: tuple[FilesController, object, list]
) -> None:
    _, devices, commits = setup

    with patch.object(devices, "get_devices", return_value=_users()):
        result = await files.rename(path="common/base.yaml", new_path="shared/base.yaml")

    assert result == {
        "path": "shared/base.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["garage.yaml"],
    }
    assert not (tmp_path / "common/base.yaml").exists()
    assert (tmp_path / "shared/base.yaml").read_text(encoding="utf-8") == (
        "wifi: !include ../common/wifi.yaml\n"
    )
    assert "shared/base.yaml" in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # one history commit covering every touched file
    assert {c for c, _ in commits} == {"common/base.yaml", "shared/base.yaml", "kitchen.yaml"}
    assert {m for _, m in commits} == {"Rename common/base.yaml to shared/base.yaml"}


@pytest.mark.parametrize(
    ("new_path", "code"),
    [
        ("common/wifi.yaml", ErrorCode.ALREADY_EXISTS),
        ("common/base.yaml", ErrorCode.INVALID_ARGS),
        ("base.yaml", ErrorCode.INVALID_ARGS),
        ("../x/base.yaml", ErrorCode.INVALID_ARGS),
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


async def test_rename_missing_source(files: FilesController) -> None:
    with pytest.raises(CommandError) as exc_info:
        await files.rename(path="common/nope.yaml", new_path="shared/nope.yaml")
    assert exc_info.value.code is ErrorCode.NOT_FOUND
