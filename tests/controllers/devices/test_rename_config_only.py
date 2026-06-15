"""Tests for the ``config_only`` rename path (rewrite name + file, no flash)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from esphome.storage_json import StorageJSON

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import read_yaml_scalar
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json

from .conftest import MakeControllerFactory

_YAML = """\
esphome:
  name: kitchen
  friendly_name: Kitchen Light

esp32:
  board: esp32dev
"""


async def test_config_only_rename_rewrites_name_and_renames_file(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The YAML name is rewritten, the file moves, nothing is queued."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")

    result = await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    assert not (tmp_path / "kitchen.yaml").exists()
    new_content = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert read_yaml_scalar(new_content, ("esphome", "name")) == "livingroom"
    # Untouched siblings survive the rewrite.
    assert read_yaml_scalar(new_content, ("esphome", "friendly_name")) == "Kitchen Light"
    assert controller._scanner.calls == [("scan",)]


async def test_config_only_rename_migrates_sidecar_metadata(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Labels / comment follow the file to the new name."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    await controller._shared_sidecar.update("kitchen.yaml", labels=["a", "b"], comment="downstairs")

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert await controller._shared_sidecar.get("kitchen.yaml") == {}
    moved = await controller._shared_sidecar.get("livingroom.yaml")
    assert moved.get("labels") == ["a", "b"]
    assert moved.get("comment") == "downstairs"


async def test_config_only_rename_migrates_storage_json(
    tmp_path: Path, make_controller: MakeControllerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The StorageJSON sidecar moves with the file, retargeting name/address."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_simple.resolve_storage_path",
        lambda configuration: storage_dir / f"{configuration}.json",
    )
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={"name": "kitchen", "friendly_name": "kitchen", "address": "kitchen.local"},
    )

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert not (storage_dir / "kitchen.yaml.json").exists()
    moved = StorageJSON.load(storage_dir / "livingroom.yaml.json")
    assert moved is not None
    assert moved.name == "livingroom"
    assert moved.friendly_name == "livingroom"
    assert moved.address == "livingroom.local"


async def test_config_only_rename_survives_storage_migration_failure(
    tmp_path: Path, make_controller: MakeControllerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A StorageJSON-migration error still completes the rename and rescans."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_simple.resolve_storage_path",
        lambda configuration: storage_dir / f"{configuration}.json",
    )
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"name": "kitchen"})

    def _boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_simple.save_device_storage", _boom
    )

    result = await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    assert not (tmp_path / "kitchen.yaml").exists()
    assert (tmp_path / "livingroom.yaml").exists()
    assert controller._scanner.calls == [("scan",)]


async def test_config_only_rename_rejects_invalid_rewrite(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A rewrite that fails validation refuses and leaves disk untouched."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    controller._db.editor.validate_yaml = AsyncMock(
        return_value={"yaml_errors": [{"message": "boom"}], "validation_errors": []}
    )

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    # Old file stays, new file never lands.
    assert (tmp_path / "kitchen.yaml").exists()
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_config_only_rename_refuses_non_literal_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No literal ``esphome.name`` leaf (packages / !include) → clean refusal."""
    controller = make_controller(tmp_path)
    # ``name`` is supplied elsewhere (e.g. a package); this file has no leaf.
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  friendly_name: Kitchen Light\n", encoding="utf-8"
    )

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "literal" in excinfo.value.message
    assert (tmp_path / "kitchen.yaml").exists()
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_config_only_rename_rewrites_local_substitution(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A pure ``${var}`` name rewrites the substitution def, keeping the indirection."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(
        "substitutions:\n  devicename: kitchen\nesphome:\n  name: ${devicename}\n",
        encoding="utf-8",
    )

    result = await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert result == {"configuration": "livingroom.yaml", "job": None}
    assert not (tmp_path / "kitchen.yaml").exists()
    content = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    # Indirection preserved: the ``${devicename}`` leaf stays, the sub def moves.
    assert "name: ${devicename}" in content
    assert "devicename: livingroom" in content


async def test_config_only_rename_refuses_nonlocal_substitution_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``${var}`` with no local ``substitutions:`` def is refused, not flattened."""
    controller = make_controller(tmp_path)
    # ``devicename`` would come from a package / !include, not this file.
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: ${devicename}\n", encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "${devicename}" in content
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_config_only_rename_refuses_embedded_substitution_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An embedded ``${var}`` (``kitchen_${suffix}``) is refused, not flattened."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(
        "substitutions:\n  suffix: a1\nesphome:\n  name: kitchen_${suffix}\n",
        encoding="utf-8",
    )

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "${suffix}" in content
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_config_only_rename_missing_file_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A vanished source config refuses with a typed error, not a traceback."""
    controller = make_controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_config_only_rename_still_rejects_collision(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The collision guard runs before the config-only branch."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    (tmp_path / "livingroom.yaml").write_text(_YAML, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "already exists" in excinfo.value.message
