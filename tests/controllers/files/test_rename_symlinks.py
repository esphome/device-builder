"""``files/rename`` and ``files/usage`` against symlinks, hidden referrers and odd extensions."""

from __future__ import annotations

import sys
from collections.abc import Coroutine
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from .conftest import Setup, write_tree

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")

KITCHEN = "packages:\n  base: !include common/base.yaml\n"


def _device(configuration: str, *dependencies: str) -> SimpleNamespace:
    return SimpleNamespace(configuration=configuration, dependencies=dependencies)


@posix_only
@pytest.mark.parametrize("loaded", [True, False])
async def test_rename_reports_the_users_of_a_symlink_alias_it_leaves_dangling(
    tmp_path: Path, setup: Setup, caplog: pytest.LogCaptureFixture, loaded: bool
) -> None:
    files, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": KITCHEN,
            "office.yaml": "packages:\n  base: !include common/alias.yaml\n",
            "common/base.yaml": "logger:\n",
        },
    )
    (tmp_path / "common/alias.yaml").symlink_to("base.yaml")
    users = [_device("kitchen.yaml", "common/base.yaml")]
    if loaded:
        users.append(_device("office.yaml", "common/alias.yaml"))

    with patch.object(devices, "get_devices", return_value=users):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    # The link is reported, not retargeted, and the device including it is flagged.
    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["common/alias.yaml", "office.yaml"],
    }
    assert (tmp_path / "common/alias.yaml").readlink() == Path("base.yaml")
    assert not (tmp_path / "common/alias.yaml").exists()
    assert (tmp_path / "office.yaml").read_text() == (
        "packages:\n  base: !include common/alias.yaml\n"
    )
    assert "common/alias.yaml links to common/base.yaml" in caplog.text


@posix_only
async def test_rename_leaves_a_reference_that_leaves_a_symlinked_folder_alone(
    tmp_path: Path, setup: Setup
) -> None:
    files, devices, _ = setup
    office = "packages:\n  a: !include common/link/../base.yaml\n  b: !include common/base.yaml\n"
    write_tree(
        tmp_path,
        {
            "office.yaml": office,
            "common/base.yaml": "logger:\n",
            "other/base.yaml": "wifi:\n",
            "other/sub/keep.yaml": "x: 1\n",
        },
    )
    # ``common/link/..`` is ``other``, so the first include loads other/base.yaml.
    (tmp_path / "common/link").symlink_to("../other/sub", target_is_directory=True)

    with patch.object(devices, "get_devices", return_value=[]):
        common = await files.rename(path="common/base.yaml", new_path="common/main.yaml")
        other = await files.rename(path="other/base.yaml", new_path="other/main.yaml")

    assert common["rewritten"] == ["office.yaml"]
    assert other["rewritten"] == ["office.yaml"]
    assert (tmp_path / "office.yaml").read_text() == (
        "packages:\n  a: !include common/link/../main.yaml\n  b: !include common/main.yaml\n"
    )
    # Still naming a base.yaml, office.yaml is reported until the second rename.
    assert (common["unresolved"], other["unresolved"]) == (["office.yaml"], [])


@posix_only
async def test_rename_rewrites_a_reference_through_a_symlinked_folder_onto_the_source(
    tmp_path: Path, setup: Setup
) -> None:
    files, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": "packages:\n  base: !include pkgs/base.yaml\n",
            # Out of the tree: never the source, whatever its name.
            "office.yaml": "packages:\n  base: !include ext/base.yaml\n",
            "common/base.yaml": "logger:\n",
        },
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "base.yaml").write_text("logger:\n")
    (tmp_path / "pkgs").symlink_to("common", target_is_directory=True)
    (tmp_path / "ext").symlink_to(outside, target_is_directory=True)

    with patch.object(devices, "get_devices", return_value=[]):
        usage = await files.usage(path="common/base.yaml")
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert usage["referenced_by"] == usage["rewritable"] == ["kitchen.yaml"]
    # office.yaml still names a base.yaml, so it is reported for a look, but left alone.
    assert result == {
        "path": "common/main.yaml",
        "rewritten": ["kitchen.yaml"],
        "unresolved": ["office.yaml"],
    }
    assert (tmp_path / "kitchen.yaml").read_text() == "packages:\n  base: !include pkgs/main.yaml\n"
    assert (tmp_path / "office.yaml").read_text() == "packages:\n  base: !include ext/base.yaml\n"


@posix_only
async def test_rename_reports_a_reference_through_a_symlink_loop_as_unresolved(
    tmp_path: Path, setup: Setup
) -> None:
    files, devices, _ = setup
    office = "packages:\n  a: !include common/loop/../base.yaml\n"
    write_tree(tmp_path, {"office.yaml": office, "common/base.yaml": "logger:\n"})
    (tmp_path / "common/loop").symlink_to("loop")

    with patch.object(devices, "get_devices", return_value=[]):
        usage = await files.usage(path="common/loop/base.yaml")
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert usage["referenced_by"] == []
    # Lexically the source, but the loop can't be resolved: left alone and reported.
    assert result == {"path": "common/main.yaml", "rewritten": [], "unresolved": ["office.yaml"]}
    assert (tmp_path / "office.yaml").read_text() == office


async def test_rename_rewrites_nested_hidden_and_upper_case_extension_referrers(
    tmp_path: Path, setup: Setup
) -> None:
    files, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "shared.YAML": KITCHEN,
            "common/.wrapper.yaml": "base: !include base.yaml\n",
            "common/base.yaml": "logger:\n",
            # A hidden folder stays unwalked.
            "common/.cache/x.yaml": "base: !include ../base.yaml\n",
        },
    )

    with patch.object(devices, "get_devices", return_value=[]):
        packages = await files.list_packages()
        usage = await files.usage(path="common/base.yaml")
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    # Hidden files are referrers only, never packages.
    assert packages == [{"path": "common/base.yaml"}]
    assert usage["referenced_by"] == ["common/.wrapper.yaml", "shared.YAML"]
    assert result["rewritten"] == ["common/.wrapper.yaml", "shared.YAML"]
    assert (tmp_path / "common/.wrapper.yaml").read_text() == "base: !include main.yaml\n"
    assert (tmp_path / "shared.YAML").read_text() == KITCHEN.replace("base.yaml", "main.yaml")
    assert (tmp_path / "common/.cache/x.yaml").read_text() == "base: !include ../base.yaml\n"


async def test_rename_of_a_hidden_referrer_schedules_no_regeneration(
    tmp_path: Path, setup: Setup
) -> None:
    files, devices, _ = setup
    write_tree(
        tmp_path,
        {
            "kitchen.yaml": KITCHEN,
            ".shared.yaml": "base: !include common/base.yaml\n",
            "common/base.yaml": "logger:\n",
        },
    )
    devices.state.esphome_cmd = ["esphome"]

    def _drop(coro: Coroutine[object, object, object]) -> None:
        coro.close()  # the spawn itself isn't under test; the scheduling is

    devices._db.create_background_task = _drop

    with patch.object(devices, "get_devices", return_value=[]):
        result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert result["rewritten"] == [".shared.yaml", "kitchen.yaml"]
    # Only the device regenerates; the hidden fragment and the packages don't.
    assert devices.state.regen.pending == {"kitchen.yaml"}
