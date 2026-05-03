"""End-to-end coverage for the file-level rename path (``_manual_rename``).

The existing ``test_rename.py`` mocks ``_manual_rename`` out and
asserts the dispatch wiring (invalid-yaml → manual, valid-yaml →
firmware queue, error mapping). That leaves the body of
``_manual_rename`` itself uncovered: the YAML rewrite, the
``StorageJSON`` move, and the metadata-sidecar move all run
against real on-disk state.

These tests drive through the public ``rename_device`` API but
let ``_manual_rename`` execute for real against ``tmp_path``,
so a regression in any of the four file-ops (YAML rewrite,
YAML rename, StorageJSON load+rewrite+save, sidecar metadata
move) surfaces here. We patch ``_yaml_validates`` to return
False so dispatch routes to the manual path; everything else
runs end-to-end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import get_device_metadata
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import SeedDeviceFactory


def _make_controller(tmp_path: Path) -> DevicesController:
    """Build a controller wired to ``tmp_path`` for real file ops.

    Same shape as the in-process helpers in ``test_archive.py`` /
    ``test_get_update_config.py``: bypass ``__init__`` and attach
    a ``_db.settings`` whose ``rel_path`` joins onto ``tmp_path``.
    The scanner is mocked because ``_manual_rename`` doesn't talk
    to it; ``rename_device`` does (post-rename ``await
    self._scanner.scan()``) so we satisfy that with an
    ``AsyncMock``.
    """
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = tmp_path
    controller._db.settings.rel_path = lambda configuration: tmp_path / configuration
    controller._scanner = MagicMock()
    controller._scanner.scan = AsyncMock()
    controller._esphome_cmd = ["esphome"]
    return controller


async def _route_through_manual(
    controller: DevicesController,
    monkeypatch: pytest.MonkeyPatch,
    *,
    configuration: str,
    new_name: str,
) -> dict[str, Any]:
    """Drive ``rename_device`` with ``_yaml_validates`` forced to False.

    That's the only stub — ``_manual_rename`` itself runs for real
    against the on-disk YAML / storage / sidecar set up by
    ``_seed_device``. Returns the API command's response so callers
    can assert the wire shape too.
    """

    async def _fake_validates(self: Any, config_path: str) -> bool:
        return False

    monkeypatch.setattr(DevicesController, "_yaml_validates", _fake_validates)
    return await controller.rename_device(configuration=configuration, new_name=new_name)


# ---------------------------------------------------------------------------
# YAML rewrite + on-disk file ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_rename_writes_new_yaml_with_rewritten_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """``esphome.name`` inside the YAML is rewritten and the file is moved."""
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml")

    result = await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    # New YAML lands with the rewritten ``esphome.name``.
    new_yaml = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert "name: livingroom" in new_yaml
    # Old YAML is gone.
    assert not (tmp_path / "kitchen.yaml").exists()
    # Scanner was kicked so the device list refreshes.
    controller._scanner.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_rename_preserves_unrelated_yaml_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """Only ``esphome.name`` is touched — substitutions, packages, comments stay.

    The legacy dashboard's manual rename used a regex that
    chewed through anything matching ``name:`` anywhere in the
    YAML. ``rewrite_esphome_name`` is scoped to the ``esphome:``
    block; this test pins that scoping end-to-end so a refactor
    that loosened the rewrite would surface.
    """
    controller = _make_controller(tmp_path)
    yaml_text = (
        "esphome:\n"
        "  name: kitchen\n"
        "  friendly_name: Kitchen\n"
        "\n"
        "# A wifi block names the network — must NOT get rewritten\n"
        "wifi:\n"
        "  ssid: !secret wifi_ssid\n"
        "\n"
        "sensor:\n"
        "  - platform: dht\n"
        "    name: kitchen-temp  # device label, also must stay\n"
    )
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    new_yaml = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    # esphome.name flipped …
    assert "  name: livingroom\n" in new_yaml
    # … but the sensor's ``name: kitchen-temp`` is unchanged …
    assert "name: kitchen-temp" in new_yaml
    # … and the wifi/secret/comments survive verbatim.
    assert "ssid: !secret wifi_ssid" in new_yaml
    assert "# A wifi block names the network" in new_yaml


# ---------------------------------------------------------------------------
# StorageJSON sidecar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_rename_moves_and_rewrites_storage_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """StorageJSON moves to the new filename with name + address rewritten.

    The dashboard's address resolution leans on ``StorageJSON.address``
    (``<name>.local``) and the file's logical ``name`` for mDNS
    correlation; both must reflect the rename or the device's
    online indicator goes UNKNOWN until the next compile.
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml")

    storage_dir = tmp_path / ".esphome" / "storage"
    old_storage = storage_dir / "kitchen.yaml.json"
    new_storage = storage_dir / "livingroom.yaml.json"
    assert old_storage.exists()  # sanity

    await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    assert not old_storage.exists()
    assert new_storage.exists()
    parsed = json.loads(new_storage.read_text(encoding="utf-8"))
    assert parsed["name"] == "livingroom"
    # When the friendly_name in the storage equals the old esphome
    # name, ``_manual_rename`` rewrites it to the new name too —
    # consistent with how a fresh wizard run wires the two together.
    assert parsed["friendly_name"] == "livingroom"
    assert parsed["address"] == "livingroom.local"


@pytest.mark.asyncio
async def test_manual_rename_keeps_unrelated_friendly_name_in_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """Storage ``friendly_name`` only flips when it equalled the *old* name.

    A user who set "My Kitchen Sensor" as the friendly name shouldn't
    have it overwritten when the YAML file is renamed.
    Pin the conditional rewrite end-to-end.
    """
    controller = _make_controller(tmp_path)
    await seed_device(
        tmp_path,
        "kitchen.yaml",
        storage_friendly="My Kitchen Sensor",
    )

    await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    parsed = json.loads(
        (tmp_path / ".esphome" / "storage" / "livingroom.yaml.json").read_text(encoding="utf-8")
    )
    assert parsed["name"] == "livingroom"
    # Custom friendly-name is preserved verbatim.
    assert parsed["friendly_name"] == "My Kitchen Sensor"


@pytest.mark.asyncio
async def test_manual_rename_succeeds_when_storage_json_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """A device without a StorageJSON (never compiled) renames cleanly.

    Exercises the ``if old_storage.exists()`` guard. The YAML rename
    is the load-bearing operation; the storage-move is best-effort
    and shouldn't blow up the rename when there's nothing to move.
    """
    controller = _make_controller(tmp_path)
    # Plain YAML, no storage / sidecar.
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    result = await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    assert result["configuration"] == "livingroom.yaml"
    assert (tmp_path / "livingroom.yaml").exists()
    # No new storage file should have been created from nothing.
    assert not (tmp_path / ".esphome" / "storage" / "livingroom.yaml.json").exists()


@pytest.mark.asyncio
async def test_manual_rename_swallows_storage_load_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """A StorageJSON.load that raises doesn't abort the rename.

    The except-Exception in ``_manual_rename`` is what keeps the
    user able to recover from a partially-written ``StorageJSON``
    (interrupted compile, rsync mid-flight). The YAML rename still
    has to land; the storage move just logs and moves on.

    Patch ``StorageJSON.load`` to raise — that's the exception
    surface the code's wrapping. (A garbage JSON file alone
    doesn't trip it because ``load`` returns ``None`` for invalid
    payloads rather than raising; we want to pin the
    *exception* path here.)
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml")

    def _raise(_path: Path) -> None:
        raise RuntimeError("simulated load failure")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.StorageJSON.load",
        staticmethod(_raise),
    )

    result = await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    assert result["configuration"] == "livingroom.yaml"
    assert (tmp_path / "livingroom.yaml").exists()
    assert not (tmp_path / "kitchen.yaml").exists()


@pytest.mark.asyncio
async def test_manual_rename_swallows_metadata_move_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """A failure during the sidecar-metadata move doesn't abort the rename.

    Pin the second except-Exception in ``_manual_rename``. Same
    motivation as the storage one: the YAML rename has already
    succeeded; an issue with the metadata sidecar must not strand
    the user with mismatched on-disk state.
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml")

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated metadata write failure")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.set_device_metadata",
        _raise,
    )

    result = await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    # Rename still completed at the YAML level.
    assert result["configuration"] == "livingroom.yaml"
    assert (tmp_path / "livingroom.yaml").exists()
    assert not (tmp_path / "kitchen.yaml").exists()


# ---------------------------------------------------------------------------
# Sidecar metadata (.device-builder.json)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_rename_moves_sidecar_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """Sidecar metadata moves to the new filename, preserving ``board_id``.

    The drawer's "Generic ESP32-C3" label and the install
    flow's pio_board lookup both key off ``board_id`` from
    ``.device-builder.json``; if the rename forgot to move it the
    new device would show up unidentified until the next
    StorageJSON regenerate.
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", friendly_name="kitchen")

    await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    # Old entry gone.
    assert await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml") == {}
    # New entry carries the same board_id and the rewritten
    # friendly_name (since it equalled the old name).
    new_meta = await asyncio.to_thread(get_device_metadata, tmp_path, "livingroom.yaml")
    assert new_meta != {}
    assert new_meta["board_id"] == "generic-esp32c3"
    assert new_meta["friendly_name"] == "livingroom"


@pytest.mark.asyncio
async def test_manual_rename_keeps_unrelated_metadata_friendly_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """Sidecar ``friendly_name`` survives if it didn't match the old name.

    Companion to the StorageJSON test above. Two parallel
    "rewrite only when it matches old_name" branches in
    ``_manual_rename`` — pin both.
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", friendly_name="My Kitchen Sensor")

    await _route_through_manual(
        controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
    )

    new_meta = await asyncio.to_thread(get_device_metadata, tmp_path, "livingroom.yaml")
    assert new_meta != {}
    assert new_meta["friendly_name"] == "My Kitchen Sensor"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_rename_raises_when_source_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """A typo'd source filename surfaces as ``CommandError(INTERNAL_ERROR)``.

    ``_manual_rename`` raises ``FileNotFoundError`` which the
    public ``rename_device`` handler maps to a generic
    ``INTERNAL_ERROR`` (``FileNotFoundError`` isn't a typed
    user-correctable error).
    """
    controller = _make_controller(tmp_path)
    # No YAML on disk at all.

    with pytest.raises(CommandError) as excinfo:
        await _route_through_manual(
            controller, monkeypatch, configuration="ghost.yaml", new_name="livingroom"
        )

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_manual_rename_does_not_clobber_existing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """Target filename collision is rejected before any file ops run.

    The public handler's pre-check fires here; verify the OLD
    YAML survives intact (no half-rename) so the user can recover.
    """
    controller = _make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    (tmp_path / "livingroom.yaml").write_text("esphome:\n  name: livingroom\n", encoding="utf-8")
    livingroom_orig = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")

    with pytest.raises(CommandError):
        await _route_through_manual(
            controller, monkeypatch, configuration="kitchen.yaml", new_name="livingroom"
        )

    # Both files survive untouched.
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8").startswith("esphome:")
    assert (tmp_path / "livingroom.yaml").read_text(encoding="utf-8") == livingroom_orig


# ---------------------------------------------------------------------------
# End-to-end round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_rename_full_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_device: SeedDeviceFactory,
    redirect_storage_path: None,
) -> None:
    """One assertion-rich pass that walks every observable side effect.

    Belt-and-suspenders coverage: even if a future refactor
    splits ``_manual_rename`` into smaller pieces, this test
    verifies the *contract* (file moves + content rewrites +
    metadata move) the public ``rename_device`` API delivers.
    """
    controller = _make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", friendly_name="kitchen")

    # Exercise via the executor path the API actually uses
    # (``rename_device`` calls ``run_in_executor`` to keep the
    # sync ``_manual_rename`` off the event loop).
    result = await asyncio.wait_for(
        _route_through_manual(
            controller,
            monkeypatch,
            configuration="kitchen.yaml",
            new_name="livingroom",
        ),
        timeout=5.0,
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    # YAML moved + rewritten.
    assert not (tmp_path / "kitchen.yaml").exists()
    new_yaml = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert "  name: livingroom\n" in new_yaml
    # StorageJSON moved + rewritten.
    storage = json.loads(
        (tmp_path / ".esphome" / "storage" / "livingroom.yaml.json").read_text(encoding="utf-8")
    )
    assert storage["name"] == "livingroom"
    assert storage["address"] == "livingroom.local"
    # Sidecar metadata moved.
    assert await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml") == {}
    assert await asyncio.to_thread(get_device_metadata, tmp_path, "livingroom.yaml") != {}
    # Scanner kicked.
    controller._scanner.scan.assert_awaited_once()
