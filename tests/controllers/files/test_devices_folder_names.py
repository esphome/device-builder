"""Devices under ``devices/``: unique file names, the build state they key, other spellings."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers._device_scanner import DeviceScanner
from esphome_device_builder.controllers.version_history import VersionHistoryController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.helpers.storage_path import resolve_storage_path
from esphome_device_builder.models import ErrorCode, UserPreferences

from .conftest import DESK, PLUG_LAMP, SWITCH_LAMP, TREE, Changes, Setup, write_tree

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges")

DESK_TAKEN = f"A device named desk.yaml already exists ({DESK}); device file names must be unique"


def _sidecar(configuration: str) -> Path:
    """Write the file-name keyed StorageJSON of *configuration* and return its path. Blocking."""
    path = resolve_storage_path(configuration)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


async def _refused(call: object) -> str:
    """Await *call*, which must raise ``INVALID_ARGS``; return its message."""
    with pytest.raises(CommandError) as exc_info:
        await call  # type: ignore[misc]
    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    return str(exc_info.value)


@pytest.mark.parametrize("overwrite", [False, True])
async def test_creating_a_device_named_like_a_nested_one_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], overwrite: bool
) -> None:
    (_, devices, _), _, _ = repo

    call = devices.create_device(name="desk", file_content="logger:\n", overwrite=overwrite)

    assert await _refused(call) == DESK_TAKEN
    assert not (tmp_path / "desk.yaml").exists()


async def test_cloning_onto_a_nested_device_name_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo

    call = devices.clone_device(configuration="kitchen.yaml", new_name="desk")

    assert await _refused(call) == DESK_TAKEN
    assert not (tmp_path / "desk.yaml").exists()


async def test_adopting_a_device_named_like_a_nested_one_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo
    devices.state.import_result = {}

    call = devices.import_device(
        name="desk",
        project_name="acme.desk",
        package_import_url="github://acme/desk.yaml@main",
        friendly_name=None,
        encryption=None,
    )

    assert await _refused(call) == DESK_TAKEN
    assert not (tmp_path / "desk.yaml").exists()


def _bundle(config_filename: str, text: str) -> bytes:
    """Build a one-file ``esphome bundle`` archive holding *config_filename*."""
    manifest = {
        "manifest_version": 1,
        "esphome_version": "2026.6.0",
        "config_filename": config_filename,
        "files": [config_filename],
        "has_secrets": False,
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("manifest.json", json.dumps(manifest)), (config_filename, text)):
            info = tarfile.TarInfo(name)
            info.size = len(data.encode())
            tar.addfile(info, io.BytesIO(data.encode()))
    return buf.getvalue()


async def test_importing_a_bundle_named_like_a_nested_device_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo

    call = devices.import_bundle(bundle_bytes=_bundle("desk.yaml", "esphome:\n  name: desk\n"))

    assert await _refused(call) == DESK_TAKEN
    assert not (tmp_path / "desk.yaml").exists()


@pytest.mark.parametrize("config_only", [False, True])
async def test_renaming_onto_a_nested_device_name_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], config_only: bool
) -> None:
    (_, devices, _), _, _ = repo

    call = devices.rename_device(
        configuration="kitchen.yaml", new_name="desk", config_only=config_only
    )

    assert await _refused(call) == DESK_TAKEN
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == TREE["kitchen.yaml"]
    assert not (tmp_path / "desk.yaml").exists()


@pytest.mark.parametrize(
    "configuration",
    [
        "./devices/lights/desk.yaml",
        "common/../devices/lights/desk.yaml",
        "devices//lights/desk.yaml",
        "devices/lights/./desk.yaml",
        "DEVICES/lights/desk.yaml",
        # Absolute: the test's config dir is prepended.
        "/devices/lights/desk.yaml",
    ],
)
async def test_other_spellings_of_a_nested_device_are_refused_archive_and_rename(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], configuration: str
) -> None:
    (_, devices, _), _, _ = repo
    if configuration.startswith("/"):
        configuration = f"{tmp_path.as_posix()}{configuration}"

    renamed = devices.rename_device(configuration=configuration, new_name="desk2", config_only=True)
    assert "Renaming is not supported yet" in await _refused(renamed)
    assert "Archiving is not supported yet" in await _refused(
        devices.archive_device(configuration=configuration)
    )

    assert (tmp_path / DESK).read_text(encoding="utf-8") == TREE[DESK]
    assert not (tmp_path / "desk2.yaml").exists()
    assert not (tmp_path / "archive" / "desk.yaml").exists()


@posix_only
async def test_a_folder_symlink_onto_a_nested_device_is_refused_archive(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """The firmware rename twin is in test_nested_devices (devices/rename resolves on the loop)."""
    (_, devices, _), _, _ = repo
    (tmp_path / "lights").symlink_to("devices/lights", target_is_directory=True)

    archived = devices.archive_device(configuration="lights/desk.yaml")
    assert "Archiving is not supported yet" in await _refused(archived)

    assert (tmp_path / DESK).read_text(encoding="utf-8") == TREE[DESK]
    assert not (tmp_path / "archive" / "desk.yaml").exists()


@pytest.mark.parametrize(("configuration", "message"), [(None, "empty"), ("", "empty")])
async def test_archiving_without_a_configuration_is_invalid_args(
    repo: tuple[Setup, DeviceScanner, Changes], configuration: str | None, message: str
) -> None:
    (_, devices, _), _, _ = repo

    assert message in await _refused(devices.archive_device(configuration=configuration))


@pytest.mark.parametrize("new_name", ["devices/lights/pantry", "devices\\lights\\pantry", "a/b"])
async def test_a_new_name_carrying_a_path_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], new_name: str
) -> None:
    (_, devices, _), _, _ = repo
    expected = f"new_name must be a device name, not a path: {new_name!r}"

    assert (
        await _refused(devices.clone_device(configuration="kitchen.yaml", new_name=new_name))
        == expected
    )
    for config_only in (False, True):
        renamed = devices.rename_device(
            configuration="kitchen.yaml", new_name=new_name, config_only=config_only
        )
        assert await _refused(renamed) == expected

    assert not (tmp_path / "devices/lights/pantry.yaml").exists()
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == TREE["kitchen.yaml"]


@pytest.mark.parametrize(
    "configuration", ["desk.yaml", "./desk.yaml", "devices/other/kitchen.yaml"]
)
async def test_saving_a_new_device_file_named_like_another_device_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], configuration: str
) -> None:
    (_, devices, commits), _, _ = repo

    saved = devices.update_config(configuration=configuration, content="logger:\n")
    message = await _refused(saved)

    assert "device file names must be unique" in message
    assert not (tmp_path / configuration).exists()
    assert commits == []


async def test_saving_existing_files_and_new_unique_devices_still_works(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """Only creating a name twin is refused; an existing device or secrets.yaml saves as before."""
    (_, devices, _), _, _ = repo

    await devices.update_config(configuration=DESK, content="logger:\n")
    await devices.update_config(configuration="secrets.yaml", content="wifi_ssid: x\n")
    await devices.update_config(configuration="pantry.yaml", content="logger:\n")

    assert (tmp_path / DESK).read_text(encoding="utf-8") == "logger:\n"
    assert (tmp_path / "pantry.yaml").read_text(encoding="utf-8") == "logger:\n"


async def test_restoring_a_deleted_top_level_twin_of_a_nested_device_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """Version history can't bring back ``desk.yaml`` while ``devices/lights/desk.yaml`` exists."""
    (_, devices, _), _, _ = repo
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
    twin = tmp_path / "desk.yaml"
    twin.write_text("esphome:\n  name: old-desk\n", encoding="utf-8")
    sha = await history.record_configuration("desk.yaml", "Create desk.yaml")
    twin.unlink()
    await history.record_configuration("desk.yaml", "Delete desk.yaml")

    message = await _refused(history.restore(configuration="desk.yaml", sha=sha))

    assert message == DESK_TAKEN
    assert not twin.exists()


async def test_deleting_a_device_spelled_otherwise_wipes_its_own_build_state(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """An absolute spelling is the device itself, not a second file sharing its name."""
    (_, devices, _), scanner, _ = repo
    sidecar = await asyncio.to_thread(_sidecar, "kitchen.yaml")
    await scanner.scan()

    await devices.delete_device(configuration=str(tmp_path / "kitchen.yaml"))

    assert not (tmp_path / "kitchen.yaml").exists()
    assert not sidecar.exists()


async def test_archiving_a_path_outside_the_config_dir_is_refused_as_before(
    repo: tuple[Setup, DeviceScanner, Changes],
) -> None:
    (_, devices, _), _, _ = repo

    message = await _refused(devices.archive_device(configuration="../devices/x/desk.yaml"))

    assert "plain filename" in message


async def test_renaming_a_top_level_device_spelled_otherwise_still_works(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """Only a spelling that reaches a nested device is refused; a top-level alias is as before."""
    (_, devices, _), _, _ = repo

    await devices.rename_device(
        configuration="common/../kitchen.yaml", new_name="pantry", config_only=True
    )

    assert (tmp_path / "pantry.yaml").is_file()
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_deleting_one_of_two_devices_sharing_a_name_keeps_the_build_state(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), scanner, _ = repo
    sidecar = await asyncio.to_thread(_sidecar, "lamp.yaml")
    await scanner.scan()

    await devices.delete_device(configuration=SWITCH_LAMP)

    assert not (tmp_path / SWITCH_LAMP).exists()
    assert sidecar.is_file()
    survivor = scanner.get_by_configuration(PLUG_LAMP)
    assert survivor is not None
    assert survivor.load_error == ""

    # The last device with the name takes its build state with it.
    await devices.delete_device(configuration=PLUG_LAMP)

    assert not sidecar.exists()


async def test_archiving_a_device_whose_name_a_nested_one_shares(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """Archive leaves the shared build state, unarchive refuses, delete leaves it too."""
    (_, devices, _), _, _ = repo
    write_tree(tmp_path, {"devices/other/kitchen.yaml": "esphome:\n  name: kitchen2\n"})
    sidecar = await asyncio.to_thread(_sidecar, "kitchen.yaml")

    await devices.archive_device(configuration="kitchen.yaml")

    assert (tmp_path / "archive" / "kitchen.yaml").is_file()
    assert sidecar.is_file()

    message = await _refused(devices.unarchive_device(configuration="kitchen.yaml"))
    assert message.startswith("A device named kitchen.yaml already exists (devices/other/")
    assert not (tmp_path / "kitchen.yaml").exists()

    await devices.delete_archived(configuration="kitchen.yaml")

    assert not (tmp_path / "archive" / "kitchen.yaml").exists()
    assert sidecar.is_file()
