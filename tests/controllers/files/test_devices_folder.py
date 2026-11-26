"""Devices under ``devices/``: the real scanner and controllers over a repo-shaped tree."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers._device_scanner import DeviceScanner, ScanChange
from esphome_device_builder.controllers.onboarding import _has_device_configs
from esphome_device_builder.helpers import config_layout
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import Device, ErrorCode

from .conftest import DESK, DOOR, PLUG_LAMP, SWITCH_LAMP, TREE, Changes, Setup, write_tree

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")


def _by_configuration(scanner: DeviceScanner) -> dict[str, Device]:
    return {d.configuration: d for d in scanner.devices}


async def test_scan_finds_top_level_and_nested_devices(
    repo: tuple[Setup, DeviceScanner, Changes],
) -> None:
    _, scanner, _ = repo

    await scanner.scan()

    devices = _by_configuration(scanner)
    assert list(devices) == [DOOR, DESK, PLUG_LAMP, SWITCH_LAMP, "kitchen.yaml"]
    desk = devices[DESK]
    assert (desk.name, desk.friendly_name, desk.load_error) == ("desk", "Desk", "")
    assert desk.dependencies == ("common/base.yaml",)
    assert devices["kitchen.yaml"].dependencies == ("common/base.yaml",)
    door = devices[DOOR]
    assert (door.name, door.address, door.load_error) == ("door", "door.local", "")
    assert scanner.get_by_configuration(DESK) is desk


async def test_every_device_sharing_a_file_name_gets_a_load_error(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    _, scanner, changes = repo

    await scanner.scan()

    devices = _by_configuration(scanner)
    assert SWITCH_LAMP in devices[PLUG_LAMP].load_error
    assert PLUG_LAMP in devices[SWITCH_LAMP].load_error
    assert devices[PLUG_LAMP].name_shared_with == (SWITCH_LAMP,)
    # Neither reads the file-name keyed state: both stay the placeholder.
    assert devices[PLUG_LAMP].name == devices[SWITCH_LAMP].name == "lamp"
    assert devices[PLUG_LAMP].dependencies is None
    # A reload keeps the placeholder and fires nothing.
    changes.clear()
    assert await scanner.reload(PLUG_LAMP)
    assert changes == []

    # The clash going away re-loads the survivor even though its file didn't change.
    (tmp_path / SWITCH_LAMP).unlink()
    await scanner.scan()

    survivor = _by_configuration(scanner)[PLUG_LAMP]
    assert (survivor.name, survivor.load_error) == ("plug-lamp", "")
    assert (ScanChange.UPDATED, PLUG_LAMP) in changes
    assert (ScanChange.REMOVED, SWITCH_LAMP) in changes


async def test_a_new_clash_turns_a_loaded_device_into_a_placeholder(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    _, scanner, _ = repo
    await scanner.scan()
    assert _by_configuration(scanner)["kitchen.yaml"].load_error == ""

    write_tree(tmp_path, {"devices/kitchen.yaml": "esphome:\n  name: kitchen2\n"})
    await scanner.scan()

    devices = _by_configuration(scanner)
    assert "devices/kitchen.yaml" in devices["kitchen.yaml"].load_error
    assert "kitchen.yaml" in devices["devices/kitchen.yaml"].load_error


async def test_files_commands_over_nested_devices(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (files, _, _), scanner, _ = repo
    await scanner.scan()

    assert await files.list_packages() == [{"path": "common/base.yaml"}]
    usage = await files.usage(path="common/base.yaml")
    assert usage["used_by"] == [DESK, "kitchen.yaml"]
    assert usage["referenced_by"] == [DESK, "kitchen.yaml"]
    assert usage["unknown"] == [PLUG_LAMP, SWITCH_LAMP]
    assert await files.dependencies(configuration=DESK) == {
        "files": ["common/base.yaml"],
        "complete": True,
    }


@pytest.mark.parametrize(
    "call",
    [
        {"op": "create", "path": "devices/lights/new.yaml", "content": "logger:\n"},
        {"op": "create", "path": "common/secrets.ci.yaml", "content": "x: 1\n"},
        {"op": "delete", "path": DESK},
        {"op": "rename", "path": DESK, "new_path": "devices/lights/desk2.yaml"},
        {"op": "rename", "path": "common/base.yaml", "new_path": "devices/base.yaml"},
    ],
)
async def test_files_commands_refuse_device_paths(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], call: dict[str, Any]
) -> None:
    (files, _, commits), _, _ = repo
    op = call.pop("op")

    with pytest.raises(CommandError) as exc_info:
        await getattr(files, op)(**call)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert (tmp_path / DESK).read_text(encoding="utf-8") == TREE[DESK]
    assert commits == []


# A folder symlink inside the config dir, and what a file below it really is.
_LINKED_FOLDERS = [
    pytest.param("devices/lights", "desk.yaml", id="nested-devices"),
    pytest.param("devices", "lights/desk.yaml", id="devices"),
    pytest.param("archive", "old.yaml", id="archive"),
    pytest.param(".esphome", "build.yaml", id="hidden"),
]


def _link_folder(tmp_path: Path, target: str) -> None:
    (tmp_path / ".esphome").mkdir(exist_ok=True)
    (tmp_path / ".esphome/build.yaml").write_text("logger:\n", encoding="utf-8")
    (tmp_path / "pkgs").symlink_to(target, target_is_directory=True)


@posix_only
@pytest.mark.parametrize(("target", "existing"), _LINKED_FOLDERS)
async def test_files_commands_refuse_a_folder_symlink_onto_a_guarded_folder(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], target: str, existing: str
) -> None:
    (files, _, commits), _, _ = repo
    _link_folder(tmp_path, target)
    before = (tmp_path / "pkgs" / existing).read_text(encoding="utf-8")
    calls: list[Any] = [
        files.create(path="pkgs/new.yaml", content="logger:\n"),
        files.delete(path=f"pkgs/{existing}"),
        files.rename(path=f"pkgs/{existing}", new_path=f"pkgs/{existing}.yaml"),
    ]

    for call in calls:
        with pytest.raises(CommandError) as exc_info:
            await call
        assert exc_info.value.code is ErrorCode.INVALID_ARGS

    assert (tmp_path / "pkgs" / existing).read_text(encoding="utf-8") == before
    assert not (tmp_path / target / "new.yaml").exists()
    assert not (tmp_path / target / f"{existing}.yaml").exists()
    assert commits == []


@posix_only
async def test_files_commands_refuse_a_file_symlink_onto_a_secrets_file(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (files, _, commits), _, _ = repo
    (tmp_path / "common/s.yaml").symlink_to("../secrets.yaml")

    for call in (
        files.delete(path="common/s.yaml"),
        files.rename(path="common/s.yaml", new_path="common/t.yaml"),
        files.rename(path="common/base.yaml", new_path="common/s.yaml"),
    ):
        with pytest.raises(CommandError) as exc_info:
            await call
        assert exc_info.value.code is ErrorCode.INVALID_ARGS

    assert (tmp_path / "common/s.yaml").is_symlink()
    assert (tmp_path / "secrets.yaml").read_text(encoding="utf-8") == TREE["secrets.yaml"]
    assert (tmp_path / "common/base.yaml").is_file()
    assert commits == []


@posix_only
async def test_files_commands_follow_a_folder_symlink_onto_another_package_folder(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (files, _, _), _, _ = repo
    _link_folder(tmp_path, "common")

    await files.create(path="pkgs/new.yaml", content="logger:\n")
    await files.rename(path="pkgs/new.yaml", new_path="pkgs/newer.yaml")
    await files.delete(path="pkgs/newer.yaml")

    assert sorted(p.name for p in (tmp_path / "common").iterdir()) == ["base.yaml", "secrets.yaml"]


async def test_a_secrets_like_referrer_is_only_checked_by_a_package_rename(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """``secrets.ci.yaml`` stays out of version history, so a rename never rewrites it."""
    (files, _, commits), _, _ = repo
    text = "b: !include base.yaml\n"
    write_tree(tmp_path, {"common/secrets.ci.yaml": text})

    usage = await files.usage(path="common/base.yaml")
    result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert "common/secrets.ci.yaml" not in usage["referenced_by"]
    assert "common/secrets.ci.yaml" in result["unresolved"]
    assert "common/secrets.ci.yaml" not in result["rewritten"]
    assert (tmp_path / "common/secrets.ci.yaml").read_text(encoding="utf-8") == text
    assert all(rel != "common/secrets.ci.yaml" for rel, _ in commits)


@posix_only
async def test_a_symlinked_referrer_is_only_checked_by_a_package_rename(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """Rewriting would swap the link for a regular file holding the secrets in plain text."""
    (files, _, commits), _, _ = repo
    text = "wifi_ssid: home\nb: !include common/base.yaml\n"
    write_tree(tmp_path, {"secrets.yaml": text})
    (tmp_path / "common/link.yaml").symlink_to("../secrets.yaml")

    usage = await files.usage(path="common/base.yaml")
    result = await files.rename(path="common/base.yaml", new_path="common/main.yaml")

    assert "common/link.yaml" not in usage["rewritable"]
    assert "common/link.yaml" in result["unresolved"]
    assert "common/link.yaml" not in result["rewritten"]
    assert (tmp_path / "common/link.yaml").is_symlink()
    assert (tmp_path / "secrets.yaml").read_text(encoding="utf-8") == text
    assert all(rel != "common/link.yaml" for rel, _ in commits)


@posix_only  # Windows can't create these names at all
async def test_the_scanner_skips_device_folders_no_command_takes(
    tmp_path: Path,
    repo: tuple[Setup, DeviceScanner, Changes],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``devices/v1./x.yaml`` would be listed as a device every command then refuses."""
    _, scanner, _ = repo
    monkeypatch.setattr(config_layout, "_WARNED_FOLDERS", set())
    odd = ["devices/v1./x.yaml", "devices/rack /y.yaml", "devices/a:b/z.yaml"]
    write_tree(tmp_path, dict.fromkeys(odd, "esphome:\n  name: odd\n"))

    await scanner.scan()
    await scanner.scan()

    assert not set(odd) & set(_by_configuration(scanner))
    assert DESK in _by_configuration(scanner)
    for folder in ("v1.", "rack ", "a:b"):
        assert sum(repr(folder) in r.getMessage() for r in caplog.records) == 1


async def test_a_case_variant_of_the_devices_folder_is_not_a_package(
    tmp_path: Path, setup: Setup
) -> None:
    """files/* refuses ``Devices/x.yaml`` (it folds case), so files/list_packages leaves it out."""
    files, _, _ = setup
    # Not TREE: beside its devices/, a case-insensitive filesystem would put x.yaml in it.
    write_tree(tmp_path, {"Devices/x.yaml": "logger:\n", "common/base.yaml": "logger:\n"})

    assert await files.list_packages() == [{"path": "common/base.yaml"}]


async def test_package_rename_rewrites_a_nested_device_include(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (files, _, _), scanner, _ = repo
    await scanner.scan()

    result = await files.rename(path="common/base.yaml", new_path="common/core.yaml")

    assert result["rewritten"] == [DESK, "kitchen.yaml"]
    assert "!include ../../common/core.yaml" in (tmp_path / DESK).read_text(encoding="utf-8")


async def test_device_commands_resolve_a_nested_configuration(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], monkeypatch: pytest.MonkeyPatch
) -> None:
    (_, devices, _), scanner, changes = repo
    await scanner.scan()

    assert await devices.get_config(configuration=DESK) == TREE[DESK]

    edited = TREE[DESK].replace("friendly_name: Desk", "friendly_name: Desk Lamp")
    await devices.update_config(configuration=DESK, content=edited)
    assert await scanner.reload(DESK)
    assert _by_configuration(scanner)[DESK].friendly_name == "Desk Lamp"
    assert (ScanChange.RELOADED, DESK) in changes

    commands: list[list[str]] = []

    async def _capture(cmd: list[str], *_args: Any, **_kwargs: Any) -> None:
        commands.append(cmd)

    monkeypatch.setattr(devices, "_stream_subprocess", _capture)
    devices.state.esphome_cmd = ["esphome"]
    await devices.validate_config(configuration=DESK, client=None, message_id="m")
    assert str(tmp_path / DESK) in commands[0]

    await devices.delete_device(configuration=DOOR)
    assert not (tmp_path / DOOR).exists()
    assert DOOR not in _by_configuration(scanner)
    assert (ScanChange.REMOVED, DOOR) in changes


def test_onboarding_counts_nested_devices(tmp_path: Path) -> None:
    write_tree(tmp_path, {"secrets.ci.yaml": "x: 1\n", "common/base.yaml": "a: 1\n"})
    assert not _has_device_configs(tmp_path)

    write_tree(tmp_path, {DESK: "esphome:\n  name: desk\n"})
    assert _has_device_configs(tmp_path)
