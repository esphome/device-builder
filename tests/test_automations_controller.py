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

# Co-locate every automations-catalog test on one xdist worker so
# the slim index (cached after first :func:`catalog._load_index`)
# isn't repeatedly re-read across workers.
pytestmark = pytest.mark.xdist_group("automations")


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


async def test_get_actions_excludes_oversized_lvgl_update_forms() -> None:
    """Oversized LVGL actions are dropped from the picker and the known-id set."""
    controller = _make_controller(Path("/unused"))
    ids = {a["id"] for a in await controller.get_actions()}
    # Present in the unfiltered catalog but filtered out of the picker.
    slim_ids = {a.id for a in catalog._slim_actions()}
    for excluded in ("lvgl.label.update", "lvgl.widget.update"):
        assert excluded in slim_ids
        assert excluded not in ids
    assert "lvgl.pause" in ids
    assert "lvgl.page.show" in ids
    # In-memory check (avoids a blocking body read); _ACTION_IDS gates is_known.
    assert "lvgl.label.update" not in catalog._ACTION_IDS
    assert "lvgl.pause" in catalog._ACTION_IDS


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


async def test_get_bodies_endpoint_returns_full_bodies() -> None:
    """``automations/get_bodies`` hydrates refs to keyed full bodies."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_bodies(
        refs=[
            {"type": "triggers", "id": "on_boot"},
            {"type": "actions", "id": "delay"},
        ]
    )
    assert "triggers/on_boot" in result
    assert "actions/delay" in result
    assert "config_entries" in result["actions/delay"]


async def test_get_filters_returns_full_catalog() -> None:
    """``automations/get_filters`` returns every sensor / binary_sensor / text_sensor filter."""
    controller = _make_controller(Path("/unused"))
    result = await controller.get_filters()
    by_id = {f["id"]: f for f in result}
    # Domain-specific filters land with applies_to from their origin.
    assert by_id["delta"]["applies_to"] == ["sensor"], (
        "delta is sensor-only; applies_to should not bleed across domains"
    )
    assert by_id["delayed_on"]["applies_to"] == ["binary_sensor"]
    assert by_id["to_upper"]["applies_to"] == ["text_sensor"]
    # Multi-domain filter ``lambda`` lives in all three registries; the
    # dedup pass should union ``applies_to`` and strip the
    # ``"<Domain> → "`` prefix from the name so the picker reads the
    # bare id regardless of editing context.
    assert sorted(by_id["lambda"]["applies_to"]) == [
        "binary_sensor",
        "sensor",
        "text_sensor",
    ]
    assert "→" not in by_id["lambda"]["name"], "multi-domain filters must drop the Domain → prefix"


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


async def test_get_available_scopes_actions_and_conditions_to_present_domains(
    tmp_path: Path,
) -> None:
    """``actions`` / ``conditions`` only surface for configured domains.

    A minimal YAML (no components) sees only the ``core`` items —
    control flow + ``delay`` / ``lambda`` for actions; combinators +
    ``for`` / ``lambda`` for conditions. Adding ``switch:`` pulls in
    ``switch.turn_on`` / ``switch.is_on``; sibling-domain items like
    ``light.turn_on`` / ``binary_sensor.is_on`` stay filtered out.
    """
    minimal = tmp_path / "min.yaml"
    minimal.write_text("esphome:\n  name: m\n", encoding="utf-8")
    controller = _make_controller(tmp_path)

    bare = await controller.get_available(configuration="min.yaml")
    bare_action_ids = {a["id"] for a in bare["actions"]}
    bare_condition_ids = {c["id"] for c in bare["conditions"]}
    # Core items always present.
    assert {"delay", "lambda", "if", "while", "repeat", "wait_until"} <= bare_action_ids
    assert {"and", "or", "all", "any", "not", "xor", "lambda", "for"} <= bare_condition_ids
    # Component-domain items absent without a matching YAML block.
    assert "switch.turn_on" not in bare_action_ids
    assert "light.turn_on" not in bare_action_ids
    assert "switch.is_on" not in bare_condition_ids
    assert "binary_sensor.is_on" not in bare_condition_ids

    scoped = tmp_path / "scoped.yaml"
    scoped.write_text(
        "esphome:\n  name: s\nswitch:\n  - platform: gpio\n    id: relay\n    pin: GPIO5\n",
        encoding="utf-8",
    )
    result = await controller.get_available(configuration="scoped.yaml")
    action_ids = {a["id"] for a in result["actions"]}
    condition_ids = {c["id"] for c in result["conditions"]}
    assert "switch.turn_on" in action_ids
    assert "switch.is_on" in condition_ids
    # Domains we did not configure stay out.
    assert "light.turn_on" not in action_ids
    assert "binary_sensor.is_on" not in condition_ids


async def test_get_available_scopes_to_configured_platform(tmp_path: Path) -> None:
    """Platform-specific catalog entries only surface for the matching platform.

    A switch with ``platform: gpio`` gets ``switch.turn_on`` but
    NOT ``template.switch.publish`` (no template switch); adding a
    template switch pulls the publish action in. Same shape on the
    trigger side: ``template.switch.turn_on`` (the trigger fired
    on a template switch's state-change automation) appears only
    when ``platform: template`` is configured.
    """
    gpio_only = tmp_path / "gpio.yaml"
    gpio_only.write_text(
        "esphome:\n  name: g\nswitch:\n  - platform: gpio\n    id: relay\n    pin: GPIO5\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    gpio = await controller.get_available(configuration="gpio.yaml")
    gpio_actions = {a["id"] for a in gpio["actions"]}
    assert "switch.turn_on" in gpio_actions
    assert "template.switch.publish" not in gpio_actions

    with_template = tmp_path / "tpl.yaml"
    with_template.write_text(
        "esphome:\n  name: t\nswitch:\n"
        "  - platform: gpio\n    id: relay\n    pin: GPIO5\n"
        "  - platform: template\n    name: tpl\n    id: vsw\n"
        "    turn_on_action:\n      - delay: 1s\n",
        encoding="utf-8",
    )
    tpl = await controller.get_available(configuration="tpl.yaml")
    tpl_actions = {a["id"] for a in tpl["actions"]}
    assert "switch.turn_on" in tpl_actions
    assert "template.switch.publish" in tpl_actions


async def test_get_available_tolerates_non_dict_items_in_component_lists(
    tmp_path: Path,
) -> None:
    """Scalar / non-dict items in a component list don't crash scoping.

    A mid-edit YAML can briefly contain a bare scalar where a
    dict is expected; the scoping pass skips those items rather
    than raising, and the real items in the same list still
    contribute platform qualifiers as usual.
    """
    config = tmp_path / "weird.yaml"
    config.write_text(
        "esphome:\n  name: w\n"
        "switch:\n"
        "  - bogus_scalar\n"
        "  - platform: gpio\n    id: relay\n    pin: GPIO5\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="weird.yaml")
    action_ids = {a["id"] for a in result["actions"]}
    # Real item still drives scoping; bogus scalar is silently skipped.
    assert "switch.turn_on" in action_ids


async def test_get_available_surfaces_namespace_actions_on_base_domain(
    tmp_path: Path,
) -> None:
    """Schema-namespace entries surface against the base domain alone.

    The schema's ``<stem>.<base>`` shape conflates real platforms
    (``template.switch`` ⇒ ``switch.template``) with organisational
    namespaces (``page.display`` — no ``display.page`` component;
    ``date.datetime`` — no ``datetime.date`` component). The sync
    flattens the latter to bare ``<base>`` so they surface for
    any matching base domain. Configuring a display with
    ``platform: ssd1306_i2c`` should expose ``page.display.show``
    (display-page actions, sub-feature of any display) but
    nothing platform-locked to a different platform.
    """
    config = tmp_path / "screen.yaml"
    config.write_text(
        "esphome:\n  name: s\n"
        "i2c:\n  sda: GPIO4\n  scl: GPIO5\n"
        "display:\n  - platform: ssd1306_i2c\n    id: scr\n"
        "    model: SSD1306 128x64\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.get_available(configuration="screen.yaml")
    action_ids = {a["id"] for a in result["actions"]}
    assert "page.display.show" in action_ids
    assert "page.display.show_next" in action_ids
    # Platform-locked display action stays out (we have ssd1306_i2c, not nextion).
    assert "nextion.display.set_brightness" not in action_ids


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
                "actions": [],
            },
            location={"kind": "bogus", "id": "x"},
        )


async def test_parse_uses_yaml_override_when_provided(tmp_path: Path) -> None:
    """Parse honours ``yaml=`` so the editor's post-add hydrate sees the draft.

    The frontend's add-automation wizard inserts the new entry
    into the in-memory draft buffer; the post-add hydrate has to
    parse against THAT buffer, not the unchanged disk file, or
    the form lands empty even though the YAML pane shows the
    user's input.
    """
    config = tmp_path / "p.yaml"
    config.write_text("esphome:\n  name: p\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    draft = (
        "esphome:\n  name: p\ninterval:\n  - interval: 30s\n    then:\n      - logger.log: tick\n"
    )
    result = await controller.parse(configuration="p.yaml", yaml=draft)
    assert len(result) == 1
    parsed = result[0]
    assert parsed["location"] == {"kind": "interval", "index": 0}
    # The interval time the user typed in the wizard round-trips
    # straight back to trigger_params.
    assert parsed["automation"]["trigger_params"]["interval"] == "30s"


async def test_upsert_uses_yaml_override_when_provided(tmp_path: Path) -> None:
    """Passing ``yaml=`` makes the writer splice into the override text.

    The frontend relies on this so its incremental auto-apply doesn't
    double-insert: each auto-apply rewrites the same draft buffer,
    not a stale on-disk version. Without the override the diff would
    be computed against the unchanged file and applying it on top of
    the draft would stack a second copy of the automation.
    """
    config = tmp_path / "u.yaml"
    config.write_text("esphome:\n  name: u\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    # Override yaml already carries an on_boot draft. The new tree
    # replaces that draft's delay with a different one — the diff
    # should target the draft's line range, not the empty disk file.
    draft = "esphome:\n  name: u\n  on_boot:\n    then:\n      - delay: 1s\n"
    result = await controller.upsert(
        configuration="u.yaml",
        automation={
            "trigger_id": "on_boot",
            "trigger_params": {},
            "actions": [
                {
                    "action_id": "delay",
                    "params": {"seconds": "5"},
                    "children": {},
                    "conditions": [],
                },
            ],
        },
        location={"kind": "device_on", "trigger": "on_boot"},
        yaml=draft,
    )
    diff = result["yaml_diff"]
    # The on_boot block in the draft spans lines 3-5; the diff should
    # target that range, not the disk's 2-line file.
    assert diff["fromLine"] >= 3
    assert diff["toLine"] >= diff["fromLine"]
    # Replacement carries the new ``seconds`` field — proves the
    # writer ran against the override, not against the disk text.
    assert "seconds" in diff["replacement"]


async def test_delete_uses_yaml_override_when_provided(tmp_path: Path) -> None:
    """``delete`` honours ``yaml=`` for the same draft-aware reason as upsert.

    Disk file is empty; the override has the automation; the diff
    should target the override's range, not the disk's range.
    """
    config = tmp_path / "d.yaml"
    config.write_text("esphome:\n  name: d\n", encoding="utf-8")
    controller = _make_controller(tmp_path)
    draft = "esphome:\n  name: d\n  on_boot:\n    then:\n      - delay: 1s\n"
    result = await controller.delete(
        configuration="d.yaml",
        location={"kind": "device_on", "trigger": "on_boot"},
        yaml=draft,
    )
    diff = result["yaml_diff"]
    assert diff["fromLine"] >= 3
    assert diff["replacement"] == ""


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


async def test_upsert_api_action_returns_yaml_diff(tmp_path: Path) -> None:
    """Upserting an api-action returns a splice that drops the new entry in."""
    config = tmp_path / "u.yaml"
    config.write_text(
        "esphome:\n  name: u\napi:\n  actions:\n"
        "    - action: existing\n      then:\n        - delay: 1s\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.upsert(
        configuration="u.yaml",
        automation={
            "trigger_id": None,
            "trigger_params": {"variables": {"x": "int"}},
            "actions": [
                {
                    "action_id": "logger.log",
                    "params": {"id": "tick"},
                    "children": {},
                    "conditions": [],
                },
            ],
        },
        location={"kind": "api_action", "action_name": "new_action"},
    )
    diff = result["yaml_diff"]
    assert diff["fromLine"] >= 1
    assert "- action: new_action" in diff["replacement"]
    assert "variables:" in diff["replacement"]


async def test_delete_api_action_returns_empty_replacement(tmp_path: Path) -> None:
    """Deleting an api-action returns an empty replacement over its line range."""
    config = tmp_path / "d.yaml"
    config.write_text(
        "esphome:\n  name: d\napi:\n  actions:\n"
        "    - action: gone\n      then:\n        - delay: 1s\n"
        "    - action: keep\n      then:\n        - delay: 2s\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.delete(
        configuration="d.yaml",
        location={"kind": "api_action", "action_name": "gone"},
    )
    diff = result["yaml_diff"]
    assert diff["replacement"] == ""
    assert diff["toLine"] >= diff["fromLine"]


async def test_parse_surfaces_api_actions(tmp_path: Path) -> None:
    """``automations/parse`` returns one ``api_action`` entry per item."""
    config = tmp_path / "p.yaml"
    config.write_text(
        "esphome:\n  name: p\napi:\n  actions:\n"
        "    - action: first\n      then:\n        - delay: 1s\n"
        "    - action: second\n      variables:\n        name: string\n"
        "      then:\n        - delay: 2s\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)
    result = await controller.parse(configuration="p.yaml")
    api_entries = [p for p in result if p["location"]["kind"] == "api_action"]
    assert [e["location"]["action_name"] for e in api_entries] == ["first", "second"]
    assert api_entries[1]["automation"]["trigger_params"]["variables"] == {"name": "string"}


async def test_parse_isolates_unknown_action_id(tmp_path: Path) -> None:
    """An unknown action id flags its own automation; parse still returns it (#1050)."""
    config = tmp_path / "x.yaml"
    config.write_text(
        "esphome:\n  name: x\n  on_boot:\n    then:\n      - made_up_action: foo\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    result = await controller.parse(configuration="x.yaml")
    assert len(result) == 1
    assert result[0]["error"] is not None
    assert "made_up_action" in result[0]["error"]
    assert result[0]["automation"]["actions"] == []
