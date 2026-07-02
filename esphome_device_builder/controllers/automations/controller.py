"""
Automations controller — the eight WS commands the frontend speaks.

See ``docs/API.md`` for the per-command contract. ``upsert`` /
``delete`` return a :class:`YamlDiff` the frontend applies in
place; the backend does not persist the YAML — the existing
config-write debounce on the device editor handles that.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAMLError

from ...helpers.api import CommandError, api_command
from ...helpers.async_ import run_in_executor
from ...models.api import ErrorCode
from ...models.automations import (
    ApiActionLocation,
    AutomationLocation,
    AutomationTree,
    AvailableAutomations,
    AvailableComponentInstance,
    AvailableScript,
    AvailableScriptParameter,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
    UpsertResponse,
)
from . import catalog, parsing, writing
from .catalog import AutomationBodyRef

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


class AutomationsController:
    """Owns the automation catalog + parse/upsert/delete WS commands."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder

    # ------------------------------------------------------------------
    # Catalog lookups
    # ------------------------------------------------------------------

    @api_command("automations/get_triggers")
    async def get_triggers(
        self,
        *,
        platform: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """
        Return every trigger in the catalog.

        ``platform`` / ``board_id`` are reserved for future
        platform-gating and ignored today (no trigger carries
        platform constraints).
        """
        del platform
        return [t.to_dict() for t in catalog.all_triggers()]

    @api_command("automations/get_actions")
    async def get_actions(
        self,
        *,
        platform: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """Return every action in the catalog."""
        del platform
        return [a.to_dict() for a in catalog.all_actions()]

    @api_command("automations/get_conditions")
    async def get_conditions(
        self,
        *,
        platform: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """Return every condition in the catalog."""
        del platform
        return [c.to_dict() for c in catalog.all_conditions()]

    @api_command("automations/get_light_effects")
    async def get_light_effects(
        self,
        *,
        platform: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """Return every light effect in the catalog."""
        del platform
        return [e.to_dict() for e in catalog.all_light_effects()]

    @api_command("automations/get_filters")
    async def get_filters(
        self,
        *,
        platform: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """Return every sensor / binary_sensor / text_sensor filter."""
        del platform
        return [f.to_dict() for f in catalog.all_filters()]

    @api_command("automations/get_bodies")
    async def get_bodies(
        self,
        *,
        refs: list[AutomationBodyRef],
        **_kwargs: Any,
    ) -> dict[str, dict]:
        """Hydrate full automation bodies in one batched round trip.

        ``refs`` is a list of ``{"type": str, "id": str}`` entries
        where ``type`` is one of ``triggers`` / ``actions`` /
        ``conditions`` / ``light_effects`` / ``filters``. The
        response is keyed by ``"<type>/<id>"`` and carries the
        full body (config_entries tree included). Unknown or
        missing ids are absent. Mirrors
        ``components/get_component_bodies`` from #424.
        """
        return await catalog.get_bodies(refs)

    # ------------------------------------------------------------------
    # Device-scoped helpers
    # ------------------------------------------------------------------

    @api_command("automations/get_available")
    async def get_available(
        self,
        *,
        configuration: str,
        yaml: str | None = None,
        **_kwargs: Any,
    ) -> dict:
        """
        Return the scoped catalog + script / device id surfaces.

        ``triggers`` / ``actions`` / ``conditions`` are filtered to
        the components present in *configuration*, matched by the
        catalog's canonical ``<domain>.<platform>`` form — an
        action whose ``domain`` is ``switch.template`` only
        surfaces when a switch with ``platform: template`` is
        configured. ``core`` items (control flow, lambda,
        combinators) are always included. ``scripts`` and
        ``devices`` feed the context-aware param dropdowns.

        With ``yaml=`` set, scope that text instead of reading
        *configuration* from disk (same override as ``parse`` /
        ``upsert`` / ``delete``).
        """
        text = yaml if yaml is not None else await self._read_config(configuration)
        scoped = await run_in_executor(_scope_from_yaml, text)
        # Scope builders are catalog-free; stamp the catalog title here.
        components = self._db.components
        if components is not None:
            for device in scoped.devices:
                device.title = components.index_title(device.component_id)
        return AvailableAutomations(
            triggers=catalog.triggers_for_domains(scoped.domains),
            actions=catalog.actions_for_domains(scoped.domains),
            conditions=catalog.conditions_for_domains(scoped.domains),
            scripts=scoped.scripts,
            devices=scoped.devices,
        ).to_dict()

    @api_command("automations/parse")
    async def parse(
        self,
        *,
        configuration: str,
        yaml: str | None = None,
        **_kwargs: Any,
    ) -> list[dict]:
        """Parse the device YAML and return every automation we recognise.

        Accepts the same optional ``yaml`` override as ``upsert`` /
        ``delete``: when the frontend has an in-memory draft that's
        not on disk yet (e.g. the user just used the add wizard, the
        new automation lives in the draft buffer but the global Save
        hasn't run), pass the draft so the parser sees what the user
        sees. Without this the editor's post-add hydrate reads the
        stale on-disk YAML, fails to find the new automation, and
        the form lands empty.
        """
        text = yaml if yaml is not None else await self._read_config(configuration)
        parsed = await run_in_executor(parsing.parse_device_yaml, text)
        return [p.to_dict() for p in parsed]

    @api_command("automations/upsert")
    async def upsert(
        self,
        *,
        configuration: str,
        automation: dict,
        location: dict,
        yaml: str | None = None,
        **_kwargs: Any,
    ) -> dict:
        """Insert or replace one automation at *location*.

        The frontend has an in-memory draft buffer that may already
        contain an earlier auto-applied version of this automation
        (the user is still typing — global save hasn't run yet).
        When that's the case the caller passes the current draft as
        ``yaml`` so the diff is computed against that text instead
        of the on-disk version. Without this the editor's incremental
        auto-apply would double-insert: backend reads disk (no
        automation yet), diff says "insert"; frontend applies diff
        to a draft that already contains an earlier insert. Two
        copies.

        Omit ``yaml`` (or pass ``None``) to fall back to reading
        from disk — convenient for tooling that doesn't track its
        own buffer.
        """
        tree = AutomationTree.from_dict(automation)
        loc = _decode_location(location)
        text = yaml if yaml is not None else await self._read_config(configuration)
        _new_text, diff = await run_in_executor(
            lambda: writing.render_upsert(text, tree=tree, location=loc),
        )
        return UpsertResponse(yaml_diff=diff).to_dict()

    @api_command("automations/delete")
    async def delete(
        self,
        *,
        configuration: str,
        location: dict,
        yaml: str | None = None,
        **_kwargs: Any,
    ) -> dict:
        """Delete the automation at *location*.

        Accepts the same optional ``yaml`` override as ``upsert``
        so the delete is computed against the frontend's current
        draft buffer when one exists.
        """
        loc = _decode_location(location)
        text = yaml if yaml is not None else await self._read_config(configuration)
        _new_text, diff = await run_in_executor(
            lambda: writing.render_delete(text, location=loc),
        )
        return UpsertResponse(yaml_diff=diff).to_dict()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_config(self, configuration: str) -> str:
        """Read a device's YAML off disk in a worker thread."""
        path = self._db.settings.rel_path(configuration)
        return await run_in_executor(path.read_text, "utf-8")


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


class _ScopedYaml:
    """Result of scanning a device YAML for available automation targets."""

    __slots__ = ("devices", "domains", "scripts")

    def __init__(
        self,
        domains: set[str],
        scripts: list[AvailableScript],
        devices: list[AvailableComponentInstance],
    ) -> None:
        self.domains = domains
        self.scripts = scripts
        self.devices = devices


def _scope_from_yaml(text: str) -> _ScopedYaml:
    """Walk *text* and surface the targets ``get_available`` returns.

    ``domains`` is the qualified set used to filter the catalog:
    every top-level YAML key (e.g. ``switch``) plus every
    ``<domain>.<platform>`` pair read off each list item (e.g.
    ``switch.template`` for a switch with ``platform: template``).
    The form matches the canonical ``<domain>.<platform>`` shape
    the component catalog and :class:`AvailableComponentInstance`
    already use, so catalog entries whose ``domain`` field is
    ``switch.template`` only surface when a switch with
    ``platform: template`` is actually configured.
    """
    yaml = parsing.make_yaml()
    try:
        data = yaml.load(text)
    except YAMLError:
        return _ScopedYaml(domains=set(), scripts=[], devices=[])
    if not isinstance(data, dict):
        return _ScopedYaml(domains=set(), scripts=[], devices=[])

    component_domains = _component_trigger_domains()
    scripts: list[AvailableScript] = []
    devices: list[AvailableComponentInstance] = []
    domains: set[str] = set(data.keys())

    if isinstance(data.get("script"), list):
        scripts = _scope_scripts(data["script"])
    for domain in set(data.keys()):
        section = data.get(domain)
        if isinstance(section, list):
            domains.update(_qualified_domains(domain, section))
            if domain in component_domains:
                devices.extend(_scope_component_instances(domain, section))
        elif isinstance(section, dict) and domain in component_domains:
            devices.extend(_scope_singleton_instance(domain, section))
    return _ScopedYaml(domains=domains, scripts=scripts, devices=devices)


def _qualified_domains(domain: str, section: list) -> set[str]:
    """Collect ``<domain>.<platform>`` keys for one section."""
    out: set[str] = set()
    for item in section:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform")
        if isinstance(platform, str) and platform:
            out.add(f"{domain}.{platform}")
    return out


def _component_trigger_domains() -> set[str]:
    """Return every domain that hosts component-level triggers."""
    out: set[str] = set()
    for trigger in catalog.all_triggers():
        if trigger.is_device_level:
            continue
        out.update(trigger.applies_to)
    return out


def _scope_scripts(script_list: list) -> list[AvailableScript]:
    """Pick declared ``script:`` ids + their ``parameters:`` map."""
    out: list[AvailableScript] = []
    for item in script_list:
        if not isinstance(item, dict) or "id" not in item:
            continue
        raw_params = item.get("parameters")
        params: list[AvailableScriptParameter] = []
        if isinstance(raw_params, dict):
            params = [
                AvailableScriptParameter(name=str(pname), type=str(ptype))
                for pname, ptype in raw_params.items()
            ]
        out.append(AvailableScript(id=str(item["id"]), parameters=params))
    return out


def _scope_component_instances(
    domain: str,
    section: list,
) -> list[AvailableComponentInstance]:
    """
    Pick configured component instance ids under one domain.

    A multi-entity platform also surfaces each ided sub-entity (``parent_id``
    set) and flags the container ``is_entity_container``.
    """
    out: list[AvailableComponentInstance] = []
    for idx, item in enumerate(section):
        if not isinstance(item, dict):
            continue
        catalog_id = parsing.catalog_id(domain, item.get("platform"))
        subs = list(parsing.iter_subentities(domain, item))
        comp_id = item.get("id")
        instance_id = str(comp_id) if comp_id else f"{domain}_{idx}"
        # Surface the instance as a target unless it's an id-less container —
        # an id-less leaf (a gpio binary_sensor, an uptime sensor) keys on the
        # same f"{domain}_{idx}" synthetic id the parser and writer use, so it
        # round-trips. Mark a container only when it surfaces ided sub-entities;
        # the frontend hides containers and redirects to those sub-entities, so
        # an id-less container has nothing to redirect to and is left out.
        if comp_id or not subs:
            out.append(_component_instance(catalog_id, instance_id, item, is_container=bool(subs)))
        for sub_domain, sub, sub_id, _sub_key in subs:
            out.append(_component_instance(sub_domain, sub_id, sub, parent_id=instance_id))
    return out


def _scope_singleton_instance(
    domain: str,
    section: dict,
) -> list[AvailableComponentInstance]:
    """Surface a flat singleton component (``sun:`` / ``mqtt:``) as a targetable instance."""
    return [_component_instance(domain, parsing.singleton_component_id(section, domain), section)]


def _component_instance(
    component_id: str,
    id_: str,
    section: dict,
    *,
    is_container: bool = False,
    parent_id: str | None = None,
) -> AvailableComponentInstance:
    """Build one ``AvailableComponentInstance``, carrying ``name:`` only when declared."""
    return AvailableComponentInstance(
        component_id=component_id,
        id=id_,
        name=str(section["name"]) if "name" in section else None,
        is_entity_container=is_container,
        parent_id=parent_id,
    )


# Map each discriminator to its concrete location type; ``_decode_location``
# resolves ``from_dict`` per call. Capturing a bound ``from_dict`` here would,
# under mashumaro ``lazy_compilation``, hold the one-shot stub and recompile the
# unpacker on every call; a class-attribute lookup hits the compiled method
# after first use.
_LOCATION_TYPES: dict[str, type[AutomationLocation]] = {
    "script": ScriptLocation,
    "interval": IntervalLocation,
    "component_on": ComponentOnLocation,
    "component_action": ComponentActionFieldLocation,
    "device_on": DeviceOnLocation,
    "light_effect": LightEffectLocation,
    "api_action": ApiActionLocation,
}


def _decode_location(raw: dict) -> AutomationLocation:
    """Convert a wire-shape ``{kind: ...}`` dict into a typed location."""
    if not isinstance(raw, dict) or "kind" not in raw:
        msg = f"location must carry a 'kind' discriminator; got {raw!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    kind = raw["kind"]
    if not isinstance(kind, str) or (loc_type := _LOCATION_TYPES.get(kind)) is None:
        msg = f"Unknown location kind: {kind!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return loc_type.from_dict(raw)
