"""Tests for the ``config_only`` rename path (rewrite name + file, no flash)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers.devices import mutations_simple
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import read_yaml_scalar
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json
from tests.conftest import make_device

from .conftest import MakeControllerFactory, wifi_ap_block

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
    assert controller._scanner.calls == [("reload", "livingroom.yaml"), ("scan", False)]


_UNDERSCORE_YAML = _YAML.replace("name: kitchen", "name: test_1")


@pytest.mark.parametrize(
    ("configuration", "text", "new_name", "saved_meanwhile", "code"),
    [
        pytest.param(
            "kitchen.yaml", _YAML, "livingroom", True, ErrorCode.PRECONDITION_FAILED, id="saved"
        ),
        pytest.param("kitchen.yaml", _YAML, "livingroom", False, ErrorCode.NOT_FOUND, id="deleted"),
        pytest.param(
            "test-1.yaml",
            _UNDERSCORE_YAML,
            "test-1",
            True,
            ErrorCode.PRECONDITION_FAILED,
            id="in_place",
        ),
    ],
)
async def test_config_only_rename_refuses_when_the_file_changed_during_validation(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    configuration: str,
    text: str,
    new_name: str,
    saved_meanwhile: bool,
    code: ErrorCode,
) -> None:
    controller = make_controller(tmp_path)
    old = tmp_path / configuration
    old.write_text(text, encoding="utf-8")
    on_disk = text + "logger:\n" if saved_meanwhile else None

    async def _file_moves_on_meanwhile(*_args: object, **_kwargs: object) -> None:
        if on_disk is None:
            await asyncio.to_thread(old.unlink)
        else:
            await controller.update_config(configuration=configuration, content=on_disk)

    monkeypatch.setattr(controller, "_schedule_storage_regenerate", lambda _configuration: None)
    monkeypatch.setattr(controller, "_validate_rewritten_yaml_or_raise", _file_moves_on_meanwhile)

    with pytest.raises(CommandError) as err:
        await controller.rename_device(
            configuration=configuration, new_name=new_name, config_only=True
        )

    assert err.value.code == code
    assert not (tmp_path / "livingroom.yaml").exists()
    assert (old.read_text(encoding="utf-8") if old.exists() else None) == on_disk


async def test_config_only_rename_never_replaces_a_target_created_during_validation(
    tmp_path: Path, make_controller: MakeControllerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    other = _YAML.replace("name: kitchen", "name: livingroom")

    async def _another_device_takes_the_name(*_args: object, **_kwargs: object) -> None:
        await asyncio.to_thread((tmp_path / "livingroom.yaml").write_text, other, "utf-8")

    monkeypatch.setattr(
        controller, "_validate_rewritten_yaml_or_raise", _another_device_takes_the_name
    )

    with pytest.raises(CommandError) as err:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    assert err.value.code == ErrorCode.INVALID_ARGS
    assert (tmp_path / "livingroom.yaml").read_text(encoding="utf-8") == other
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == _YAML


async def test_config_only_rename_holds_both_filenames_only_while_the_metadata_moves(
    tmp_path: Path, make_controller: MakeControllerFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    held: dict[str, tuple[bool, bool]] = {}

    def _locks() -> tuple[bool, bool]:
        return (
            controller._yaml_write_lock("kitchen.yaml").locked(),
            controller._yaml_write_lock("livingroom.yaml").locked(),
        )

    async def _migrate(_controller: object, _old: str, _new: str) -> None:
        held["migrate"] = _locks()

    async def _rescan(_controller: object, _new: str) -> None:
        held["rescan"] = _locks()

    monkeypatch.setattr(mutations_simple, "migrate_metadata", _migrate)
    monkeypatch.setattr(mutations_simple, "rescan_renamed", _rescan)

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert held == {"migrate": (True, True), "rescan": (False, False)}


async def test_config_only_rename_lands_as_one_executor_job(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")

    with patch.object(
        mutations_simple, "run_in_executor", wraps=mutations_simple.run_in_executor
    ) as spy:
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="livingroom", config_only=True
        )

    jobs = [call.args[0].__name__ for call in spy.await_args_list]
    assert jobs == ["_read_and_probe", "_land"]


async def test_config_only_rename_retargets_name_labelled_ap_ssid(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Without a friendly name the generated fallback-AP ssid tracks the rename."""
    controller = make_controller(tmp_path)
    yaml_text = "esphome:\n  name: kitchen\n\nesp32:\n  board: esp32dev\n\n" + wifi_ap_block(
        "kitchen Fallback Hotspot"
    )
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    new_content = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert read_yaml_scalar(new_content, ("wifi", "ap", "ssid")) == "livingroom Fallback Hotspot"


async def test_config_only_rename_leaves_friendly_labelled_ap_ssid(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """With a friendly name the ssid is friendly-derived; rename doesn't touch it."""
    controller = make_controller(tmp_path)
    yaml_text = _YAML + "\n" + wifi_ap_block("Kitchen Light Fallback Hotspot")
    (tmp_path / "kitchen.yaml").write_text(yaml_text, encoding="utf-8")

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    new_content = (tmp_path / "livingroom.yaml").read_text(encoding="utf-8")
    assert read_yaml_scalar(new_content, ("wifi", "ap", "ssid")) == "Kitchen Light Fallback Hotspot"


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
    assert controller._scanner.calls == [("reload", "livingroom.yaml"), ("scan", False)]


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
    # The OTA chain uses the same rewriter and refuses the same shapes,
    # so the remedy points at editing the name, not bringing it online.
    assert "Edit esphome.name" in excinfo.value.message
    assert (tmp_path / "kitchen.yaml").exists()
    assert not (tmp_path / "livingroom.yaml").exists()


async def test_in_place_rename_non_retargetable_name_points_to_editing(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An in-place non-retargetable name can't fall back to the OTA rename.

    ``esphome rename`` won't keep the same filename, so the refusal must
    point at editing the name, not at bringing the device online. The
    embedded ``${suffix}`` resolves to ``kitchen_a1`` (not a pure ref, so it
    differs from the slugified ``kitchen-a1`` stem and clears the same-name
    guard) but still isn't retargetable in place.
    """
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen-a1.yaml").write_text(
        "substitutions:\n  suffix: a1\nesphome:\n  name: kitchen_${suffix}\n",
        encoding="utf-8",
    )

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen-a1.yaml", new_name="kitchen-a1")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Edit esphome.name" in excinfo.value.message
    assert "online" not in excinfo.value.message
    assert (tmp_path / "kitchen-a1.yaml").exists()


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

    assert excinfo.value.code == ErrorCode.NOT_FOUND


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


# ----------------------------------------------------------------------
# deployed_name: the firmware keeps its old hostname until a flash (#2730)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "configuration", "renames", "expected_filename", "expected_deployed"),
    [
        pytest.param(_YAML, "kitchen.yaml", ["livingroom"], "livingroom.yaml", "kitchen", id="one"),
        # ``esphome.name`` is recorded, which the filename stem can differ from.
        pytest.param(
            _UNDERSCORE_YAML, "test-1.yaml", ["test-1"], "test-1.yaml", "test_1", id="in_place"
        ),
        # A second flash-free rename still points at what the firmware has.
        pytest.param(
            _YAML, "kitchen.yaml", ["livingroom", "hallway"], "hallway.yaml", "kitchen", id="chain"
        ),
        # Renaming back to it leaves nothing to redirect.
        pytest.param(
            _YAML, "kitchen.yaml", ["livingroom", "kitchen"], "kitchen.yaml", None, id="back"
        ),
    ],
)
async def test_config_only_rename_records_the_deployed_name(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    text: str,
    configuration: str,
    renames: list[str],
    expected_filename: str,
    expected_deployed: str | None,
) -> None:
    """The hostname the firmware still answers to is recorded under the new filename."""
    controller = make_controller(tmp_path)
    (tmp_path / configuration).write_text(text, encoding="utf-8")

    current = configuration
    for new_name in renames:
        result = await controller.rename_device(
            configuration=current, new_name=new_name, config_only=True
        )
        current = result["configuration"]

    assert current == expected_filename
    assert controller._metadata_store.get(current).get("deployed_name") == expected_deployed


async def test_in_place_rename_back_survives_the_rescan_stamp(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The rescan re-enters the scan-change stamp with the pre-rename name (#2730)."""
    controller = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(
        _YAML.replace("name: kitchen", "name: livingroom"), encoding="utf-8"
    )
    # The state a hand-edit leaves: the firmware still answers to kitchen.
    controller._metadata_store.update("kitchen.yaml", deployed_name="kitchen", delay=0.0)

    async def _rescan_fires_scan_change(_controller: object, configuration: str) -> None:
        controller._on_scan_change(
            ScanChange.RELOADED,
            make_device(configuration=configuration, name="kitchen", loaded_integrations=["api"]),
            make_device(configuration=configuration, name="livingroom"),
        )

    with patch.object(mutations_simple, "rescan_renamed", _rescan_fires_scan_change):
        await controller.rename_device(
            configuration="kitchen.yaml", new_name="kitchen", config_only=True
        )

    assert "deployed_name" not in controller._metadata_store.get("kitchen.yaml")


async def test_config_only_rename_records_the_name_even_if_the_migration_fails(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``migrate_metadata`` logs and continues, so the stamp is re-asserted under the new file."""
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    controller._migrate_device_metadata = AsyncMock(side_effect=OSError("disk"))

    await controller.rename_device(
        configuration="kitchen.yaml", new_name="livingroom", config_only=True
    )

    assert controller._metadata_store.get("livingroom.yaml")["deployed_name"] == "kitchen"
