"""``devices/add_component`` WS command body."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    CAPTIVE_PORTAL_PLATFORMS,
    device_ap_label,
    parse_esphome_meta,
    parse_platform_from_yaml,
)
from ...helpers.secrets_state import (
    WIFI_PASSWORD_SECRET_REF,
    WIFI_SSID_SECRET_REF,
    read_secrets_yaml,
    wifi_secrets_defined,
)
from ...helpers.yaml import (
    component_block_present,
    fallback_ap_psk,
    fallback_ap_ssid,
    merge_component_yaml,
)
from ...models import AddComponentResponse, ErrorCode
from ...models.boards import normalize_platform
from .helpers import _apply_featured_presets, _drop_unconfigured_dependent_fields

if TYPE_CHECKING:
    from pathlib import Path

    from ...models import ComponentCatalogEntry, ConfigEntry
    from ..components import ComponentCatalog
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
    is_featured = component_id.startswith("featured.")

    if is_featured:
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

    # The required-field gate catches a user-driven form add that omitted a
    # required field. Featured presets come from the board manifest instead
    # (curated, locked, already validated by ``create`` + compile), and the
    # catalog can't model ESPHome's type-conditional either-or fields, so
    # re-gating a variant-scoped requirement rejects a valid recommended
    # add. Trust the manifest; ESPHome validation at compile is the real gate.
    if not is_featured:
        _require_present_fields(component, fields)

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
    if underlying_component_id == "wifi":
        new_yaml = await _merge_wifi_with_recovery(
            controller._db.components,
            controller._db.settings.config_dir,
            component,
            fields,
            existing,
        )
    else:
        new_yaml = merge_component_yaml(existing, component, fields)
    if yaml is None:
        # Atomic write; wizard-driven add-component should not be able
        # to corrupt the source YAML on a mid-write crash.
        await controller._persist_yaml_mutation(
            configuration, new_yaml, message=f"Add {component.id} to {configuration}"
        )

    return AddComponentResponse(yaml=new_yaml)


async def _merge_wifi_with_recovery(
    catalog: ComponentCatalog,
    config_dir: Path,
    component: ComponentCatalogEntry,
    fields: dict[str, Any],
    existing: str,
) -> str:
    """Merge a ``wifi:`` add, filling the wizard's recovery defaults into a new block."""
    if component_block_present(existing, "wifi"):
        return merge_component_yaml(existing, component, fields)
    add_portal = await _apply_wifi_recovery_defaults(config_dir, fields, existing)
    new_yaml = merge_component_yaml(existing, component, fields)
    if not add_portal:
        return new_yaml
    portal = await catalog.get_component(component_id="captive_portal")
    if portal is None:
        return new_yaml
    return merge_component_yaml(new_yaml, portal, {})


async def _apply_wifi_recovery_defaults(
    config_dir: Path, fields: dict[str, Any], existing: str
) -> bool:
    """
    Fill recovery defaults into a new ``wifi:`` block's *fields*.

    Returns whether ``captive_portal:`` should be merged in as well.
    """
    secrets = await run_in_executor(read_secrets_yaml, config_dir)
    if wifi_secrets_defined(secrets):
        if not fields.get("ssid"):
            fields["ssid"] = WIFI_SSID_SECRET_REF
        if not fields.get("password"):
            fields["password"] = WIFI_PASSWORD_SECRET_REF
    # No credentials typed and none to reference: generating an AP-only
    # device would surprise, so the bare add stays bare.
    if not fields.get("ssid"):
        return False
    # Cheap column-0 scan: a platform arriving through ``packages:``
    # deliberately forfeits the AP/portal leg rather than paying an
    # ``esphome config`` resolve on this UI-latency path.
    platform, _, _ = parse_platform_from_yaml(existing)
    if normalize_platform(platform) not in CAPTIVE_PORTAL_PLATFORMS:
        return False
    if not fields.get("ap"):
        label = device_ap_label(parse_esphome_meta(existing)) or ""
        fields["ap"] = {"ssid": fallback_ap_ssid(label), "password": fallback_ap_psk()}
    return True


def _require_present_fields(component: ComponentCatalogEntry, fields: dict[str, Any]) -> None:
    """Raise ``INVALID_ARGS`` for a required field whose variant gate is active but absent."""
    for entry in component.config_entries:
        if entry.required and _entry_gate_active(entry, fields) and entry.key not in fields:
            msg = f"Missing required field: {entry.label or entry.key}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)


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
