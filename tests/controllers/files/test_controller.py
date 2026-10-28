"""Tests for ``FilesController``: create / delete / usage / dependencies."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from ...conftest import MakeSettingsFactory
from ..devices.conftest import MakeControllerFactory


@pytest.fixture
def setup(
    tmp_path: Path, make_controller: MakeControllerFactory, make_settings: MakeSettingsFactory
) -> tuple[FilesController, DevicesController, list[tuple[str, str]]]:
    """Build a FilesController over a real DevicesController and real path validation."""
    devices = make_controller(tmp_path)
    devices._db.settings = make_settings()
    commits: list[tuple[str, str]] = []

    async def _commit(configuration: str, message: str) -> None:
        commits.append((configuration, message))

    devices._commit_history = _commit  # type: ignore[method-assign]
    files = FilesController.__new__(FilesController)
    files._db = SimpleNamespace(settings=devices._db.settings, devices=devices)  # type: ignore[assignment]
    return files, devices, commits


Setup = tuple[FilesController, DevicesController, list[tuple[str, str]]]


async def test_list_packages(tmp_path: Path, setup: Setup) -> None:
    files, _, _ = setup
    (tmp_path / "kitchen.yaml").write_text("esphome:\n", encoding="utf-8")
    (tmp_path / "common").mkdir()
    (tmp_path / "common/base.yaml").write_text("a: 1\n", encoding="utf-8")

    assert await files.list_packages() == [{"path": "common/base.yaml"}]


async def test_create_makes_parents_and_records_history(tmp_path: Path, setup: Setup) -> None:
    files, devices, commits = setup

    with patch.object(devices._db, "invalidate_editor_cache") as invalidate:
        result = await files.create(path="common/sub/new.yaml", content="logger:\n")

    assert result == {"path": "common/sub/new.yaml"}
    assert (tmp_path / "common/sub/new.yaml").read_text(encoding="utf-8") == "logger:\n"
    assert commits == [("common/sub/new.yaml", "Create common/sub/new.yaml")]
    invalidate.assert_called_once()


async def test_create_refuses_existing(tmp_path: Path, setup: Setup) -> None:
    files, _, commits = setup
    (tmp_path / "common").mkdir()
    (tmp_path / "common/base.yaml").write_text("a: 1\n", encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        await files.create(path="common/base.yaml", content="b: 2\n")

    assert exc_info.value.code is ErrorCode.ALREADY_EXISTS
    assert (tmp_path / "common/base.yaml").read_text(encoding="utf-8") == "a: 1\n"
    assert commits == []


async def test_create_refuses_file_as_parent(tmp_path: Path, setup: Setup) -> None:
    files, _, _ = setup
    (tmp_path / "common").write_text("not a dir", encoding="utf-8")

    with pytest.raises(CommandError) as exc_info:
        await files.create(path="common/base.yaml", content="a: 1\n")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("kitchen.yaml", "a: 1\n"),
        ("common/base.yaml", "  \n"),
        ("common/base.yaml", None),
        ("../outside/x.yaml", "a: 1\n"),
    ],
)
async def test_create_refuses_bad_input(
    tmp_path: Path, setup: Setup, path: str, content: Any
) -> None:
    files, _, _ = setup

    with pytest.raises(CommandError) as exc_info:
        await files.create(path=path, content=content)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert not (tmp_path / "kitchen.yaml").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")
async def test_create_refuses_symlink_escape(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, setup: Setup
) -> None:
    files, _, _ = setup
    outside = tmp_path_factory.mktemp("outside")
    (tmp_path / "common").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CommandError) as exc_info:
        await files.create(path="common/evil.yaml", content="a: 1\n")

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert not (outside / "evil.yaml").exists()


async def test_delete_removes_and_records_history(tmp_path: Path, setup: Setup) -> None:
    files, _, commits = setup
    (tmp_path / "common").mkdir()
    (tmp_path / "common/base.yaml").write_text("a: 1\n", encoding="utf-8")

    await files.delete(path="common/base.yaml")

    assert not (tmp_path / "common/base.yaml").exists()
    assert commits == [("common/base.yaml", "Delete common/base.yaml")]


async def test_delete_missing_is_not_found(setup: Setup) -> None:
    files, _, commits = setup

    with pytest.raises(CommandError) as exc_info:
        await files.delete(path="common/base.yaml")

    assert exc_info.value.code is ErrorCode.NOT_FOUND
    assert commits == []


async def test_delete_refuses_device_yaml(tmp_path: Path, setup: Setup) -> None:
    files, _, _ = setup
    (tmp_path / "kitchen.yaml").write_text("esphome:\n", encoding="utf-8")

    with pytest.raises(CommandError):
        await files.delete(path="kitchen.yaml")

    assert (tmp_path / "kitchen.yaml").exists()


def _devices(*rows: tuple[str, tuple[str, ...] | None]) -> list[SimpleNamespace]:
    return [SimpleNamespace(configuration=c, dependencies=d) for c, d in rows]


async def test_usage(setup: Setup) -> None:
    files, devices, _ = setup
    rows = _devices(
        ("b.yaml", ("common/base.yaml", "secrets.yaml")),
        ("a.yaml", ("common/wifi.yaml", "common/base.yaml")),
        ("c.yaml", ()),
        ("d.yaml", None),
    )

    with patch.object(devices, "get_devices", return_value=rows):
        result = await files.usage(path="common/base.yaml")

    assert result == {"used_by": ["a.yaml", "b.yaml"], "unknown": ["d.yaml"]}


async def test_usage_of_top_level_include(setup: Setup) -> None:
    files, devices, _ = setup
    rows = _devices(("a.yaml", ("_shared.yaml",)))

    with patch.object(devices, "get_devices", return_value=rows):
        result = await files.usage(path="_shared.yaml")

    assert result == {"used_by": ["a.yaml"], "unknown": []}


async def test_dependencies(setup: Setup) -> None:
    files, devices, _ = setup
    device = SimpleNamespace(
        configuration="a.yaml", dependencies=("common/base.yaml", "secrets.yaml", "/abs/x.yaml")
    )

    with patch.object(devices, "get_by_configuration", return_value=device):
        result = await files.dependencies(configuration="a.yaml")

    assert result == {"files": ["common/base.yaml", "/abs/x.yaml"], "complete": True}


async def test_dependencies_unknown_list(setup: Setup) -> None:
    files, devices, _ = setup
    device = SimpleNamespace(configuration="a.yaml", dependencies=None)

    with patch.object(devices, "get_by_configuration", return_value=device):
        result = await files.dependencies(configuration="a.yaml")

    assert result == {"files": [], "complete": False}


async def test_dependencies_missing_device(setup: Setup) -> None:
    files, devices, _ = setup

    with (
        patch.object(devices, "get_by_configuration", return_value=None),
        pytest.raises(CommandError) as exc_info,
    ):
        await files.dependencies(configuration="nope.yaml")

    assert exc_info.value.code is ErrorCode.NOT_FOUND
