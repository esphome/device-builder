"""Devices under ``devices/``: data-dir pinning, secrets, archive and rename refusals."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome import yaml_util
from esphome.core import CORE

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers._device_scanner import DeviceScanner, ScanChange
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.device_yaml import run_esphome_config
from esphome_device_builder.models import Device, ErrorCode

from .conftest import DESK, PLUG_LAMP, TREE, Changes, Setup, write_tree

# Stands in for the esphome CLI: records its argv and data-dir pin, prints a mapping.
_RECORD = (
    "import json, os, sys\n"
    "with open(sys.argv[1], 'w') as out:\n"
    "    env = os.environ\n"
    "    json.dump({'argv': sys.argv[2:], 'data_dir': env.get('ESPHOME_DATA_DIR'),\n"
    "               'nogitignore': env.get('ESPHOME_NOGITIGNORE')}, out)\n"
    "print('logger: {}')\n"
)
ALARM = "devices/other/alarm.yaml"


def _recorder(out: Path) -> list[str]:
    return [sys.executable, "-c", _RECORD, str(out)]


def _recorded(out: Path) -> dict[str, Any]:
    return json.loads(out.read_text(encoding="utf-8"))


def _by_configuration(scanner: DeviceScanner) -> dict[str, Device]:
    return {d.configuration: d for d in scanner.devices}


async def test_storage_regen_of_a_nested_device_builds_into_the_dashboard_data_dir(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo
    out = tmp_path / "record.json"
    devices.state.esphome_cmd = _recorder(out)

    assert await devices._spawn_only_generate(DESK) is None

    recorded = _recorded(out)
    assert recorded["argv"][-1] == str(tmp_path / DESK)
    assert recorded["data_dir"] == str(Path(CORE.data_dir))
    # esphome would otherwise write a ``.gitignore`` into ``devices/lights/``.
    assert recorded["nogitignore"] == "1"


async def test_a_real_esphome_run_leaves_the_devices_folder_clean(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """The real CLI, not a stub: the pins are what keep esphome's output out of ``devices/``."""
    (_, devices, _), _, _ = repo
    devices.state.esphome_cmd = [sys.executable, "-m", "esphome"]
    real = "devices/lights/real.yaml"
    include = "packages:\n  base: !include ../../common/base.yaml\n"
    write_tree(tmp_path, {real: f"esphome:\n  name: real\nhost:\n{include}"})

    assert await devices._spawn_only_generate(real) is None

    data_dir = Path(CORE.data_dir)
    assert (data_dir / "storage" / "real.yaml.json").is_file()
    assert sorted(p.name for p in (tmp_path / "devices").rglob(".*")) == []


async def test_logs_of_a_nested_device_run_with_the_dashboard_data_dir(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo
    out = tmp_path / "record.json"
    devices.state.esphome_cmd = _recorder(out)
    client = WebSocketClient(MagicMock(), MagicMock(), authenticated=True)
    events: list[tuple[str, Any]] = []

    async def _capture(_message_id: str, event: str, data: Any = None) -> None:
        events.append((event, data))

    client.send_event = _capture  # type: ignore[method-assign]

    await devices.stream_logs(configuration=DESK, client=client, message_id="m")

    recorded = _recorded(out)
    assert recorded["argv"][recorded["argv"].index("logs") + 1] == str(tmp_path / DESK)
    assert recorded["data_dir"] == str(Path(CORE.data_dir))
    assert events[-1] == ("result", {"success": True, "code": 0})


async def test_esphome_config_of_a_nested_device_runs_with_the_dashboard_data_dir(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    out = tmp_path / "record.json"

    assert await run_esphome_config(_recorder(out), tmp_path / DESK) == {"logger": {}}

    assert _recorded(out)["data_dir"] == str(Path(CORE.data_dir))


async def test_archiving_a_nested_device_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    (_, devices, _), _, _ = repo

    with pytest.raises(CommandError) as exc_info:
        await devices.archive_device(configuration=DESK)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert "Archiving is not supported yet" in str(exc_info.value)
    assert DESK in str(exc_info.value)
    assert (tmp_path / DESK).is_file()
    assert not (tmp_path / "archive" / "desk.yaml").exists()


@pytest.mark.parametrize("config_only", [False, True])
async def test_renaming_a_nested_device_is_refused(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes], config_only: bool
) -> None:
    (_, devices, _), _, _ = repo

    with pytest.raises(CommandError) as exc_info:
        await devices.rename_device(configuration=DESK, new_name="desk2", config_only=config_only)

    assert exc_info.value.code is ErrorCode.INVALID_ARGS
    assert "Renaming is not supported yet" in str(exc_info.value)
    assert (tmp_path / DESK).read_text(encoding="utf-8") == TREE[DESK]
    assert not (tmp_path / "desk2.yaml").exists()


async def test_a_nested_device_without_its_secrets_file_gets_a_load_error(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    _, scanner, changes = repo
    write_tree(
        tmp_path,
        {
            ALARM: "esphome:\n  name: alarm\nwifi:\n  password: !secret wifi_pw\n",
            # The top-level and garage secrets files exist, so these load cleanly.
            "top.yaml": "esphome:\n  name: top\nwifi:\n  password: !secret wifi_pw\n",
            "devices/garage/opener.yaml": "esphome:\n  name: opener\napi:\n  key: !secret k\n",
        },
    )
    await scanner.scan()

    devices = _by_configuration(scanner)
    assert devices[ALARM].load_error == (
        "Uses !secret, but devices/other/secrets.yaml does not exist"
    )
    # Advisory: only a shared file name refuses firmware jobs.
    assert devices[ALARM].name_shared_with == ()
    assert devices["top.yaml"].load_error == ""
    assert devices["devices/garage/opener.yaml"].load_error == ""
    assert devices[DESK].load_error == ""

    # Nothing changed on disk: the next scan reloads nothing, placeholders included.
    changes.clear()
    await scanner.scan()
    assert changes == []
    assert "lamp.yaml" in devices[PLUG_LAMP].load_error

    # Adding the secrets file beside it refreshes the device on the next scan.
    write_tree(tmp_path, {"devices/other/secrets.yaml": "wifi_pw: hunter2\n"})
    await scanner.scan()

    assert _by_configuration(scanner)[ALARM].load_error == ""
    assert changes == [(ScanChange.UPDATED, ALARM)]


async def test_a_sibling_secrets_yml_is_not_read_like_esphome(
    tmp_path: Path, repo: tuple[Setup, DeviceScanner, Changes]
) -> None:
    """ESPHome's ``!secret`` reads only ``secrets.yaml`` beside the config, so we do too."""
    _, scanner, _ = repo
    write_tree(
        tmp_path,
        {
            ALARM: "esphome:\n  name: alarm\nwifi:\n  password: !secret wifi_pw\n",
            "devices/other/secrets.yml": "wifi_pw: hunter2\n",
        },
    )

    await scanner.scan()

    assert yaml_util.SECRET_YAML == "secrets.yaml"
    assert _by_configuration(scanner)[ALARM].load_error == (
        "Uses !secret, but devices/other/secrets.yaml does not exist"
    )
