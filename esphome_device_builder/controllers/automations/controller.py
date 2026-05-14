"""
Automations controller — the eight WS commands the frontend speaks.

See ``docs/API.md`` for the per-command contract. ``upsert`` /
``delete`` return a :class:`YamlDiff` the frontend applies in
place; the backend does not persist the YAML — the existing
config-write debounce on the device editor handles that.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAMLError

from ...helpers.api import CommandError, api_command
from ...models.api import ErrorCode
from ...models.automations import (
    AutomationLocation,
    AutomationTree,
    AvailableAutomations,
    AvailableComponentInstance,
    AvailableScript,
    AvailableScriptParameter,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
    UpsertResponse,
)
from . import catalog, parsing, writing

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

    # ------------------------------------------------------------------
    # Device-scoped helpers
    # ------------------------------------------------------------------

    @api_command("automations/get_available")
    async def get_available(
        self,
        *,
        configuration: str,
        **_kwargs: Any,
    ) -> dict:
        """
        Return the scoped catalog + script / device id surfaces.

        ``triggers`` is filtered to component domains present in
        *configuration* plus device-level. ``actions`` /
        ``conditions`` are returned in full (id-pickers filter on
        the frontend). ``scripts`` and ``devices`` feed the
        context-aware param dropdowns.
        """
        text = await self._read_config(configuration)
        loop = asyncio.get_running_loop()
        scoped = await loop.run_in_executor(None, _scope_from_yaml, text)
        triggers = catalog.triggers_for_domains(scoped.domains)
        return AvailableAutomations(
            triggers=triggers,
            actions=catalog.all_actions(),
            conditions=catalog.all_conditions(),
            scripts=scoped.scripts,
            devices=scoped.devices,
        ).to_dict()

    @api_command("automations/parse")
    async def parse(
        self,
        *,
        configuration: str,
        **_kwargs: Any,
    ) -> list[dict]:
        """Parse the device YAML and return every automation we recognise."""
        text = await self._read_config(configuration)
        loop = asyncio.get_running_loop()
        parsed = await loop.run_in_executor(None, parsing.parse_device_yaml, text)
        return [p.to_dict() for p in parsed]

    @api_command("automations/upsert")
    async def upsert(
        self,
        *,
        configuration: str,
        automation: dict,
        location: dict,
        **_kwargs: Any,
    ) -> dict:
        """Insert or replace one automation at *location*."""
        tree = AutomationTree.from_dict(automation)
        loc = _decode_location(location)
        text = await self._read_config(configuration)
        loop = asyncio.get_running_loop()
        _new_text, diff = await loop.run_in_executor(
            None,
            lambda: writing.render_upsert(text, tree=tree, location=loc),
        )
        return UpsertResponse(yaml_diff=diff).to_dict()

    @api_command("automations/delete")
    async def delete(
        self,
        *,
        configuration: str,
        location: dict,
        **_kwargs: Any,
    ) -> dict:
        """Delete the automation at *location*."""
        loc = _decode_location(location)
        text = await self._read_config(configuration)
        loop = asyncio.get_running_loop()
        _new_text, diff = await loop.run_in_executor(
            None,
            lambda: writing.render_delete(text, location=loc),
        )
        return UpsertResponse(yaml_diff=diff).to_dict()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_config(self, configuration: str) -> str:
        """Read a device's YAML off disk in a worker thread."""
        path = self._db.settings.rel_path(configuration)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, path.read_text, "utf-8")


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
    """Walk *text* and surface the targets ``get_available`` returns."""
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
    for domain in component_domains & domains:
        section = data.get(domain)
        if isinstance(section, list):
            devices.extend(_scope_component_instances(domain, section))
    return _ScopedYaml(domains=domains, scripts=scripts, devices=devices)


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
    """Pick configured component instance ids under one domain."""
    out: list[AvailableComponentInstance] = []
    for item in section:
        if not isinstance(item, dict):
            continue
        comp_id = item.get("id")
        if not comp_id:
            continue
        platform = item.get("platform")
        catalog_id = f"{domain}.{platform}" if platform else domain
        out.append(
            AvailableComponentInstance(
                component_id=catalog_id,
                id=str(comp_id),
                name=str(item["name"]) if "name" in item else None,
            ),
        )
    return out


def _decode_location(raw: dict) -> AutomationLocation:
    """Convert a wire-shape ``{kind: ...}`` dict into a typed location."""
    if not isinstance(raw, dict) or "kind" not in raw:
        msg = f"location must carry a 'kind' discriminator; got {raw!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    kind = raw.get("kind")
    # The discriminator narrowing is by-string so mypy can pin the
    # concrete type per branch — a single dict mapping would widen
    # the return to ``Any`` (every value is a different subclass).
    if kind == "script":
        return ScriptLocation.from_dict(raw)
    if kind == "interval":
        return IntervalLocation.from_dict(raw)
    if kind == "component_on":
        return ComponentOnLocation.from_dict(raw)
    if kind == "device_on":
        return DeviceOnLocation.from_dict(raw)
    if kind == "light_effect":
        return LightEffectLocation.from_dict(raw)
    msg = f"Unknown location kind: {kind!r}"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)
