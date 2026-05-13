"""Tests for the automations controller WS commands.

Pins the catalog-loader path (the four ``get_*`` commands), the
context-scoping behaviour of ``get_available``, and the basic
parse / upsert / delete round-trips. The deep parser and writer
tests live in ``test_automations_parse.py`` and
``test_automations_writer.py`` respectively.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.automations import AutomationsController, catalog
from esphome_device_builder.helpers.api import CommandError


def _make_controller(config_dir: Path) -> AutomationsController:
    """Build a controller wired to a tmp config dir.

    The controller's only DeviceBuilder interaction is
    ``self._db.settings.rel_path(configuration)`` — wire it to the
    tmp path's joinpath so each test sees its own filesystem.
    """
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    return AutomationsController(db)


# ---------------------------------------------------------------------------
# Catalog list commands
# ---------------------------------------------------------------------------


async def test_get_triggers_returns_full_catalog() -> None:
    """``automations/get_triggers`` returns every catalog trigger."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_triggers()
    catalog_ids = {t.id for t in catalog.all_triggers()}
    assert {t["id"] for t in result} == catalog_ids
    assert "on_boot" in catalog_ids  # device-level
    assert "binary_sensor.on_press" in catalog_ids  # component-level


async def test_get_actions_returns_full_catalog() -> None:
    """``automations/get_actions`` returns every catalog action."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_actions()
    assert {a["id"] for a in result} == {a.id for a in catalog.all_actions()}
    # A few load-bearing built-ins we expect to always be present.
    ids = {a["id"] for a in result}
    for required in ("if", "delay", "lambda", "switch.turn_on", "light.turn_on"):
        assert required in ids, f"{required} missing from action catalog"


async def test_get_conditions_returns_full_catalog() -> None:
    """``automations/get_conditions`` returns every catalog condition."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_conditions()
    ids = {c["id"] for c in result}
    for required in ("and", "or", "not", "lambda", "switch.is_on", "binary_sensor.is_on"):
        assert required in ids, f"{required} missing from condition catalog"


async def test_get_light_effects_returns_full_catalog() -> None:
    """``automations/get_light_effects`` returns every catalog effect."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_light_effects()
    ids = {e["id"] for e in result}
    for required in ("flicker", "pulse"):
        assert required in ids, f"{required} missing from light effects catalog"


# ---------------------------------------------------------------------------
# get_available
# ---------------------------------------------------------------------------


async def test_get_available_scopes_triggers_to_present_domains(tmp_path: Path) -> None:
    """Component-level triggers only surface for configured domains.

    A YAML with ``binary_sensor:`` configured should include
    ``binary_sensor.on_press`` (and other binary_sensor triggers)
    plus every device-level trigger. ``sensor.on_value`` is gated
    on having a ``sensor:`` block and must NOT leak through.
    """
    config = tmp_path / "kitchen.yaml"
    config.write_text(
        "esphome:\n  name: kitchen\n"
        "binary_sensor:\n  - platform: gpio\n    name: b\n    id: btn\n    pin: GPIO0\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="kitchen.yaml")
    trigger_ids = {t["id"] for t in result["triggers"]}
    # Device-level triggers are unconditional.
    assert {"on_boot", "on_loop", "on_shutdown"} <= trigger_ids
    # Binary-sensor triggers surface.
    assert "binary_sensor.on_press" in trigger_ids
    # Sensor-only triggers do not.
    assert "sensor.on_value" not in trigger_ids


async def test_get_available_returns_configured_scripts_with_parameters(
    tmp_path: Path,
) -> None:
    """``scripts:`` declarations surface with their ``parameters:`` map.

    ``script.execute`` renders a dynamic parameter form keyed on the
    selected script's id; without parameters the form would have
    nothing to render. Pin that the controller surfaces both name
    and type per declared parameter.
    """
    config = tmp_path / "alarm.yaml"
    config.write_text(
        "esphome:\n  name: a\n"
        "script:\n"
        "  - id: morning_alarm\n"
        "    parameters:\n"
        "      hour: int\n"
        "      message: string\n"
        "    then:\n"
        "      - logger.log: 'wake up'\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="alarm.yaml")
    assert len(result["scripts"]) == 1
    script = result["scripts"][0]
    assert script["id"] == "morning_alarm"
    params = {p["name"]: p["type"] for p in script["parameters"]}
    assert params == {"hour": "int", "message": "string"}


async def test_get_available_lists_configured_component_instances(tmp_path: Path) -> None:
    """Configured component instances are surfaced for id-picker dropdowns.

    Action params that ``references_component`` (e.g.
    ``switch.turn_on``'s ``id`` field references the ``switch``
    domain) need the list of configured ids in the YAML so the
    frontend can render the picker.
    """
    config = tmp_path / "device.yaml"
    config.write_text(
        "esphome:\n  name: d\n"
        "switch:\n"
        "  - platform: gpio\n"
        "    id: relay_one\n"
        "    name: 'Relay 1'\n"
        "    pin: GPIO5\n"
        "  - platform: gpio\n"
        "    id: relay_two\n"
        "    pin: GPIO6\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="device.yaml")
    devices = {(d["component_id"], d["id"]): d for d in result["devices"]}
    assert ("switch.gpio", "relay_one") in devices
    assert devices[("switch.gpio", "relay_one")]["name"] == "Relay 1"
    assert ("switch.gpio", "relay_two") in devices


async def test_get_available_actions_and_conditions_are_returned_in_full(
    tmp_path: Path,
) -> None:
    """``actions`` / ``conditions`` are not scoped to present domains.

    The frontend's id-pickers handle scoping by filtering on
    ``references_component``; the catalog is returned unfiltered so
    the user can pick e.g. ``light.turn_on`` even on a device that
    has no light yet (they'll add one).
    """
    config = tmp_path / "min.yaml"
    config.write_text("esphome:\n  name: m\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="min.yaml")
    assert len(result["actions"]) == len(catalog.all_actions())
    assert len(result["conditions"]) == len(catalog.all_conditions())


# ---------------------------------------------------------------------------
# parse / upsert / delete
# ---------------------------------------------------------------------------


async def test_parse_returns_empty_list_for_yaml_without_automations(
    tmp_path: Path,
) -> None:
    """A device YAML with no automations parses to an empty list."""
    config = tmp_path / "empty.yaml"
    config.write_text("esphome:\n  name: e\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    result = await controller.parse(configuration="empty.yaml")
    assert result == []


async def test_parse_round_trip_device_on_boot(tmp_path: Path) -> None:
    """Parsing a device with on_boot returns one device_on entry."""
    config = tmp_path / "boot.yaml"
    config.write_text(
        "esphome:\n  name: b\n  on_boot:\n    then:\n      - delay: 1s\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.parse(configuration="boot.yaml")
    assert len(result) == 1
    parsed = result[0]
    assert parsed["location"] == {"kind": "device_on", "trigger": "on_boot"}
    assert parsed["automation"]["trigger_id"] == "on_boot"
    assert parsed["automation"]["actions"][0]["action_id"] == "delay"


async def test_upsert_device_on_boot_returns_yaml_diff(tmp_path: Path) -> None:
    """Upserting on_boot on a device without one returns a splice diff."""
    config = tmp_path / "u.yaml"
    config.write_text("esphome:\n  name: u\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    result = await controller.upsert(
        configuration="u.yaml",
        automation={
            "trigger_id": "on_boot",
            "trigger_params": {},
            "conditions": [],
            "actions": [
                {
                    "action_id": "delay",
                    "params": {"id": "1s"},
                    "children": {},
                    "conditions": [],
                },
            ],
        },
        location={"kind": "device_on", "trigger": "on_boot"},
    )
    diff = result["yaml_diff"]
    assert diff["fromLine"] >= 1
    # The replacement contains the new on_boot handler.
    assert "on_boot" in diff["replacement"]


async def test_upsert_rejects_unknown_location_kind(tmp_path: Path) -> None:
    """An unknown location.kind discriminator surfaces as INVALID_ARGS."""
    config = tmp_path / "u.yaml"
    config.write_text("esphome:\n  name: u\n", encoding="utf-8")
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError):
        await controller.upsert(
            configuration="u.yaml",
            automation={
                "trigger_id": "on_boot",
                "trigger_params": {},
                "conditions": [],
                "actions": [],
            },
            location={"kind": "bogus", "id": "x"},
        )


async def test_delete_device_on_returns_empty_replacement(tmp_path: Path) -> None:
    """Deleting on_boot returns a diff whose replacement is empty."""
    config = tmp_path / "d.yaml"
    config.write_text(
        "esphome:\n  name: d\n  on_boot:\n    then:\n      - delay: 1s\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.delete(
        configuration="d.yaml",
        location={"kind": "device_on", "trigger": "on_boot"},
    )
    diff = result["yaml_diff"]
    assert diff["replacement"] == ""
    assert diff["toLine"] >= diff["fromLine"]


async def test_parse_raises_on_unknown_action_id(tmp_path: Path) -> None:
    """Unknown action ids surface as ``CommandError(INVALID_ARGS)``."""
    config = tmp_path / "x.yaml"
    config.write_text(
        "esphome:\n  name: x\n  on_boot:\n    then:\n      - made_up_action: foo\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError):
        await controller.parse(configuration="x.yaml")
