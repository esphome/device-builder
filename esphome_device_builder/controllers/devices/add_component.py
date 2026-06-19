"""``devices/add_component`` WS command body."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.yaml import merge_component_yaml
from ...models import AddComponentResponse, ErrorCode
from .helpers import _apply_featured_presets, _drop_unconfigured_dependent_fields

if TYPE_CHECKING:
    from ...models import ConfigEntry
    from .controller import DevicesController


async def add_component(
    controller: DevicesController,
    *,
    configuration: str,
    component_id: str,
    fields: dict[str, Any] | None,
    yaml: str | None = None,
) -> AddComponentResponse:
    """
    Add a component block to a device YAML.

    ``fields`` is a flat mapping of config-entry key → value;
    nested entries map to nested dicts. Featured ids
    (``featured.<board>.<local>``) resolve to the underlying
    catalog component, validate user input against the manifest's
    ``locked`` / ``suggestions`` constraints, and merge the
    manifest's preset values into ``fields`` before the regular
    merge.

    When ``yaml`` is given it is the caller's unsaved editor draft:
    the merge runs against it and the result is returned without
    touching disk, so the user's unsaved edits survive (the editor
    saves later). Without it the merge runs against the on-disk YAML
    and is persisted immediately.
    """
    assert controller._db.components is not None  # type narrowing

    fields = dict(fields or {})
    underlying_component_id = component_id

    if component_id.startswith("featured."):
        record = controller._db.components.get_featured_record(component_id)
        if record is None:
            msg = f"Unknown featured component: {component_id}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        underlying_component_id = record.underlying_id
        underlying_body = await controller._db.components.get_body(underlying_component_id)
        if underlying_body is None:
            msg = f"Unknown component body for featured ref: {underlying_component_id}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        fields = _apply_featured_presets(record, fields, underlying_body)
        # The frontend's featured-id suggestion contains the board's
        # dashes (e.g. ``featured_athom-smart-plug-v3_power_monitor_1``),
        # which ESPHome rejects. Reset to empty so generate_component_yaml
        # produces a valid auto-id; user-typed dashless ids pass through.
        user_id = fields.get("id")
        if isinstance(user_id, str) and "-" in user_id:
            fields["id"] = ""

    component = await controller._db.components.get_component(component_id=underlying_component_id)
    if component is None:
        msg = f"Unknown component: {underlying_component_id}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    for entry in component.config_entries:
        if entry.required and _entry_gate_active(entry, fields) and entry.key not in fields:
            msg = f"Missing required field: {entry.label or entry.key}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

    if yaml is None:
        config_path = controller._db.settings.rel_path(configuration)
        existing = await controller._read_yaml_async(config_path)
    else:
        existing = yaml
    # Honour each field's ``depends_on_component`` gate against
    # what's actually in the device YAML; drops MQTT-only options
    # (``availability:``, ``state_topic:``, ...) when the device
    # has no ``mqtt:`` block, mirroring what the frontend already
    # does field-by-field on the input form.
    fields = _drop_unconfigured_dependent_fields(fields, component, existing)
    new_yaml = merge_component_yaml(existing, component, fields)
    if yaml is None:
        # Atomic write; wizard-driven add-component should not be able
        # to corrupt the source YAML on a mid-write crash.
        await controller._persist_yaml_mutation(
            configuration, new_yaml, message=f"Add {component.id} to {configuration}"
        )

    return AddComponentResponse(yaml=new_yaml)


def _entry_gate_active(entry: ConfigEntry, fields: dict[str, Any]) -> bool:
    """Whether *entry*'s ``depends_on`` value gate is satisfied by *fields*."""
    if entry.depends_on is None:
        return True
    dep = fields.get(entry.depends_on)
    if entry.depends_on_value is not None:
        return dep == entry.depends_on_value
    if entry.depends_on_value_not is not None:
        return dep != entry.depends_on_value_not
    if entry.depends_on_value_any is not None:
        return dep in entry.depends_on_value_any
    return True
