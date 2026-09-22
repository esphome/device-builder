"""
Automations controller — the eight WS commands the frontend speaks.

See ``docs/API.md`` for the per-command contract. ``upsert`` /
``delete`` return a :class:`YamlDiff` the frontend applies in
place; the backend does not persist the YAML — the existing
config-write debounce on the device editor handles that. ``save``
on either rewrites the file instead, for callers with no editor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAMLError

from ...helpers.api import CommandError, api_command
from ...helpers.async_ import run_in_executor
from ...helpers.device_config import read_device_config
from ...helpers.json import dumps_str
from ...helpers.text import diff_excerpt, same_text
from ...models.api import ErrorCode
from ...models.automations import (
    LOCATION_TYPES,
    AutomationLocation,
    AutomationTree,
    AvailableAutomations,
    AvailableComponentInstance,
    AvailableScript,
    AvailableScriptParameter,
    ParsedAutomation,
    UpsertResponse,
    YamlDiff,
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
        scoped = await self._run_on_config(configuration, yaml, _scope_from_yaml)
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
        parsed = await self._run_on_config(configuration, yaml, parsing.parse_device_yaml)
        return [p.to_dict() for p in parsed]

    @api_command("automations/upsert")
    async def upsert(
        self,
        *,
        configuration: str,
        automation: dict,
        location: dict,
        yaml: str | None = None,
        save: bool = False,
        expected: str | None = None,
        **_kwargs: Any,
    ) -> dict:
        """Insert or replace one automation at *location*.

        ``save`` rewrites the on-disk config, so it is refused beside ``yaml``.
        With ``save`` or ``expected`` (the ``raw_yaml`` a parse returned for the
        automation being replaced) the write is guarded: a replace needs
        ``expected`` to still match and an insert must keep every other
        automation, else ``PRECONDITION_FAILED`` with nothing written.

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
        _check_save_args(save=save, yaml=yaml, expected=expected)
        try:
            tree = AutomationTree.from_dict(automation)
        except (LookupError, ValueError, TypeError) as err:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Invalid automation: {err}") from err
        loc = _decode_location(location)
        render: Callable[[str], tuple[str, YamlDiff]]
        if save or expected is not None:
            render = partial(
                _render_upsert_if_unchanged, tree=tree, location=loc, expected=expected
            )
        else:
            render = partial(writing.render_upsert, tree=tree, location=loc)
        return await self._apply(
            configuration, yaml, render, save=save, message=f"Save an automation to {configuration}"
        )

    @api_command("automations/delete")
    async def delete(
        self,
        *,
        configuration: str,
        location: dict,
        yaml: str | None = None,
        save: bool = False,
        expected: str | None = None,
        **_kwargs: Any,
    ) -> dict:
        """Delete the automation at *location*.

        Accepts the same ``yaml`` draft override and ``save`` as ``upsert``.
        With ``expected``, the ``raw_yaml`` a parse returned for the automation,
        the delete happens only while it still reads that way
        (``PRECONDITION_FAILED``).
        """
        _check_save_args(save=save, yaml=yaml, expected=expected)
        loc = _decode_location(location)
        render: Callable[[str], tuple[str, YamlDiff]]
        if expected is None:
            render = partial(writing.render_delete, location=loc)
        else:
            render = partial(_render_delete_if_unchanged, location=loc, expected=expected)
        return await self._apply(
            configuration,
            yaml,
            render,
            save=save,
            message=f"Delete an automation from {configuration}",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _apply(
        self,
        configuration: str,
        yaml: str | None,
        render: Callable[[str], tuple[str, YamlDiff]],
        *,
        save: bool,
        message: str,
    ) -> dict:
        """Run *render* over the draft or the file, or with *save* rewrite the file in place."""
        if not save:
            _new_text, diff = await self._run_on_config(configuration, yaml, render)
        elif (devices := self._db.devices) is None:
            raise CommandError(ErrorCode.INTERNAL_ERROR, "devices controller unavailable")
        else:
            diff = await devices.rewrite_yaml(configuration, render, message=message)
        return UpsertResponse(yaml_diff=diff).to_dict()

    async def _run_on_config[T](
        self, configuration: str, yaml: str | None, func: Callable[[str], T]
    ) -> T:
        """Run *func* over the *yaml* draft, else the on-disk config, as one executor job."""
        if yaml is not None:
            return await run_in_executor(func, yaml)

        def _read_and_run() -> T:
            path = self._db.settings.rel_path(configuration)
            return func(read_device_config(path, configuration))

        return await run_in_executor(_read_and_run)


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

    scripts: list[AvailableScript] = []
    devices: list[AvailableComponentInstance] = []
    domains: set[str] = set(data.keys())

    if (listed := parsing.listed_block(data, "script")) is not None:
        scripts = _scope_scripts(listed[0])
    # ``devices`` ships to the frontend in this order; keep document-order
    # iteration (a ``set()`` wrap hash-shuffles it per process).
    for domain in data:
        section = data.get(domain)
        if isinstance(section, list):
            domains.update(_qualified_domains(domain, section))
            devices.extend(_scope_component_instances(domain, section))
        elif isinstance(section, dict) and catalog.hosts_component_triggers(
            domain, parsing.catalog_id(domain, section.get("platform"))
        ):
            devices.extend(_scope_singleton_instance(domain, section))
    return _ScopedYaml(domains=domains, scripts=scripts, devices=devices)


def _qualified_domains(domain: str, section: list) -> set[str]:
    """Collect ``<domain>.<platform>`` keys for one section."""
    out: set[str] = set()
    for item in section:
        if not isinstance(item, dict):
            continue
        cat_id = parsing.catalog_id(domain, item.get("platform"))
        if cat_id != domain:
            out.add(cat_id)
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
    Pick configured instance ids under one domain whose domain or platform hosts triggers.

    A multi-entity platform surfaces each configured sub-entity
    (``parent_id`` set) and flags the container ``is_entity_container``.
    """
    out: list[AvailableComponentInstance] = []
    for idx, item in enumerate(section):
        if not isinstance(item, dict):
            continue
        catalog_id = parsing.catalog_id(domain, item.get("platform"))
        if not catalog.hosts_component_triggers(domain, catalog_id):
            continue
        # An id-less instance keys on the parser and writer's declared-or-
        # positional id, so it round-trips. Container-ness comes from the
        # catalog definition, not from which sub-blocks the YAML happens to
        # configure: a multi-entity platform is never a leaf, or entity
        # triggers would splice onto the platform item and produce invalid
        # YAML (#1886).
        instance_id = parsing.instance_id(domain, item, idx, is_list=True)
        is_container = bool(parsing.platform_subentity_keys(catalog_id))
        out.append(_component_instance(catalog_id, instance_id, item, is_container=is_container))
        for sub_domain, sub, sub_id, _sub_key in parsing.iter_subentities(
            domain, item, instance_id, cat_id=catalog_id
        ):
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
    """Build one instance; ``name`` and ``has_explicit_id`` reflect only declared keys."""
    return AvailableComponentInstance(
        component_id=component_id,
        id=id_,
        name=str(section["name"]) if "name" in section else None,
        is_entity_container=is_container,
        parent_id=parent_id,
        has_explicit_id=parsing.declares_id(section),
    )


def _decode_location(raw: dict) -> AutomationLocation:
    """Convert a wire-shape ``{kind: ...}`` dict into a typed location."""
    if not isinstance(raw, dict) or "kind" not in raw:
        msg = f"location must carry a 'kind' discriminator; got {raw!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    kind = raw["kind"]
    if not isinstance(kind, str) or (loc_type := LOCATION_TYPES.get(kind)) is None:
        msg = f"Unknown location kind: {kind!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # Resolve ``from_dict`` per call: a bound method captured at import holds
    # mashumaro's one-shot ``lazy_compilation`` stub and recompiles every call.
    try:
        return loc_type.from_dict(raw)
    except (LookupError, ValueError, TypeError) as err:
        raise CommandError(ErrorCode.INVALID_ARGS, f"Invalid {kind} location: {err}") from err


def _check_save_args(*, save: bool, yaml: str | None, expected: str | None) -> None:
    """Validate the ``save`` / ``expected`` wire args."""
    if not isinstance(save, bool):
        raise CommandError(ErrorCode.INVALID_ARGS, "save must be a boolean")
    if save and yaml is not None:
        raise CommandError(ErrorCode.INVALID_ARGS, "save writes the config on disk; omit yaml")
    if expected is not None and not isinstance(expected, str):
        raise CommandError(ErrorCode.INVALID_ARGS, "expected must be a string")


def _rows(yaml_text: str) -> list[ParsedAutomation]:
    """Parse *yaml_text*'s automations; an unloadable file fails the precondition."""
    try:
        return parsing.parse_device_yaml(yaml_text)
    except CommandError as err:
        if err.code is not ErrorCode.INVALID_ARGS:
            raise
        msg = f"the config no longer loads, nothing was written: {err.message}"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg) from err


def _rendered_rows(new_text: str) -> list[ParsedAutomation]:
    """Parse the writer's output; a result that no longer loads is a logged writer fault."""
    try:
        return parsing.parse_device_yaml(new_text)
    except CommandError as err:
        if err.code is not ErrorCode.INVALID_ARGS:
            raise
        _LOGGER.exception("Automation rewrite produced a config that does not load")
        msg = (
            f"the rewrite produced a config that does not load, nothing was written: {err.message}"
        )
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg) from err


def _require_expected(
    rows: list[ParsedAutomation], location: AutomationLocation, expected: str
) -> None:
    """Raise ``PRECONDITION_FAILED`` unless the row at *location* still reads as *expected*."""
    row = next((p for p in rows if p.location == location), None)
    if row is None:
        msg = "no automation at that location any more; list again and retry"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    if not same_text(row.raw_yaml, expected):
        msg = (
            "the automation at that location differs from the expected text; nothing was "
            f"written, list again and retry\n{diff_excerpt(expected, row.raw_yaml)}"
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def _render_delete_if_unchanged(
    yaml_text: str, *, location: AutomationLocation, expected: str
) -> tuple[str, YamlDiff]:
    """Delete the automation at *location* only while its text still equals *expected*."""
    _require_expected(_rows(yaml_text), location, expected)
    return writing.render_delete(yaml_text, location=location)


def _content(row: ParsedAutomation) -> tuple[str | None, str | None, bool]:
    """Return what a surviving row must keep across a rewrite, serialised so NaN compares equal."""
    tree = dumps_str(row.automation.to_dict()) if row.automation is not None else None
    return tree, row.error, row.unsupported


def _render_upsert_if_unchanged(
    yaml_text: str, *, tree: AutomationTree, location: AutomationLocation, expected: str | None
) -> tuple[str, YamlDiff]:
    """Insert one automation and keep every other, or replace only the one matching *expected*."""
    before = _rows(yaml_text)
    if expected is not None:
        _require_expected(before, location, expected)
    new_text, diff = writing.render_upsert(yaml_text, tree=tree, location=location)
    after = _rendered_rows(new_text)
    landed = next((p for p in after if p.location == location), None)
    if landed is None:  # pragma: no cover — every writer path raises for an index it cannot honour
        msg = (
            "no automation landed at that location, nothing was written; for a list, index "
            "must not exceed the current length"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # Compared by content: an insert may turn a bare action list into then:
    # entries, which renumbers the surviving rows.
    kept = [_content(p) for p in before if expected is None or p.location != location]
    if [_content(p) for p in after if p is not landed] != kept:
        msg = (
            "the automation at that location could not be replaced in place; nothing was written"
            if expected is not None
            else "that location already holds YAML; pass the automation's raw_yaml from a "
            "listing as expected to replace it"
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    return new_text, diff
