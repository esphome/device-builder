"""
Automation catalog + round-trip data models.

Catalog dataclasses (``AutomationTrigger`` / ``AutomationAction`` /
``AutomationCondition`` / ``LightEffect``) load from
``definitions/automations.json``. The round-trip dataclasses
(``AutomationTree`` / ``ActionNode`` / ``ConditionNode`` /
``ParsedAutomation``) carry the structured shape the frontend
exchanges with the backend through ``automations/parse`` /
``automations/upsert``.

The lambda sentinel ``{"_lambda": "<C++ source>"}`` in any
``params`` value round-trips to a YAML block scalar — distinguishes
a templatable literal from a templatable lambda body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from mashumaro.mixins.orjson import DataClassORJSONMixin
from mashumaro.types import Discriminator

from .common import ConfigEntry

# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@dataclass
class AutomationTrigger(DataClassORJSONMixin):
    """
    A trigger that can start an automation.

    ``applies_to`` carries the catalog's canonical
    ``<domain>.<platform>`` ids the trigger is scoped to (e.g.
    ``cover.template`` for a template-cover-only trigger,
    ``binary_sensor`` for any binary sensor). Empty when
    ``is_device_level`` is true or for ``core`` triggers.
    """

    id: str
    name: str
    description: str
    docs_url: str
    applies_to: list[str] = field(default_factory=list)
    is_device_level: bool = False
    config_entries: list[ConfigEntry] = field(default_factory=list)


@dataclass
class AutomationAction(DataClassORJSONMixin):
    """
    An action that can run inside an automation.

    ``domain`` carries the catalog's canonical
    ``<domain>.<platform>`` id (e.g. ``switch.template``,
    ``binary_sensor.nextion``) or the bare ``<domain>`` for
    platform-agnostic actions (``switch.turn_on`` lives under
    ``switch``). ``core`` covers control flow + lambda.
    """

    id: str
    name: str
    description: str
    docs_url: str
    domain: str
    config_entries: list[ConfigEntry] = field(default_factory=list)
    is_control_flow: bool = False
    has_else_branch: bool = False
    accepts_action_list: list[str] = field(default_factory=list)


@dataclass
class AutomationCondition(DataClassORJSONMixin):
    """
    A condition usable inside an ``if`` / ``while`` / ``wait_until``.

    ``domain`` follows the same ``<domain>.<platform>`` shape as
    :class:`AutomationAction`. ``core`` covers boolean combinators
    + ``for`` + ``lambda``.
    """

    id: str
    name: str
    description: str
    docs_url: str
    domain: str
    config_entries: list[ConfigEntry] = field(default_factory=list)
    accepts_condition_list: bool = False


@dataclass
class LightEffect(DataClassORJSONMixin):
    """A light effect (pulse, flicker, addressable_lambda, ...)."""

    id: str
    name: str
    config_entries: list[ConfigEntry] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)


@dataclass
class Filter(DataClassORJSONMixin):
    """
    A sensor / binary_sensor / text_sensor filter (``delta``, ``lambda``, ...).

    ``applies_to`` lists the component domains the filter is valid
    on (``["sensor"]`` / ``["binary_sensor"]`` / ``["text_sensor"]``);
    the REGISTRY_LIST renderer uses it to scope the per-row picker.
    """

    id: str
    name: str
    config_entries: list[ConfigEntry] = field(default_factory=list)
    applies_to: list[str] = field(default_factory=list)


@dataclass
class AutomationCatalog(DataClassORJSONMixin):
    """Top-level shape of ``definitions/automations.json``."""

    esphome_schema_version: str = ""
    triggers: list[AutomationTrigger] = field(default_factory=list)
    actions: list[AutomationAction] = field(default_factory=list)
    conditions: list[AutomationCondition] = field(default_factory=list)
    light_effects: list[LightEffect] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Location (discriminated union)
# ---------------------------------------------------------------------------


@dataclass
class ScriptLocation(DataClassORJSONMixin):
    """A top-level ``script:`` list item, keyed by the script's ``id``."""

    id: str
    kind: Literal["script"] = "script"


@dataclass
class IntervalLocation(DataClassORJSONMixin):
    """A top-level ``interval:`` list item, indexed by list position."""

    index: int
    kind: Literal["interval"] = "interval"


@dataclass
class ComponentOnLocation(DataClassORJSONMixin):
    """An inline ``on_*:`` handler under a configured component instance."""

    component_id: str
    trigger: str
    kind: Literal["component_on"] = "component_on"


@dataclass
class DeviceOnLocation(DataClassORJSONMixin):
    """A device-level ``on_boot`` / ``on_loop`` / ``on_shutdown`` under ``esphome:``."""

    trigger: str
    kind: Literal["device_on"] = "device_on"


@dataclass
class LightEffectLocation(DataClassORJSONMixin):
    """A user-defined effect inside a light's ``effects:`` list."""

    component_id: str
    index: int
    kind: Literal["light_effect"] = "light_effect"


@dataclass
class ApiActionLocation(DataClassORJSONMixin):
    """A user-defined action inside the ``api.actions:`` list."""

    action_name: str
    kind: Literal["api_action"] = "api_action"


AutomationLocation = Annotated[
    ScriptLocation
    | IntervalLocation
    | ComponentOnLocation
    | DeviceOnLocation
    | LightEffectLocation
    | ApiActionLocation,
    Discriminator(field="kind", include_supertypes=True),
]


# ---------------------------------------------------------------------------
# Round-trip tree
# ---------------------------------------------------------------------------


@dataclass
class ConditionNode(DataClassORJSONMixin):
    """
    A single condition node.

    Combinators (``and`` / ``or`` / ``all`` / ``any`` / ``not`` /
    ``xor``) carry their sub-conditions under ``children``; leaf
    conditions carry their arguments under ``params``.
    """

    condition_id: str
    params: dict[str, Any] = field(default_factory=dict)
    children: list[ConditionNode] = field(default_factory=list)


@dataclass
class ActionNode(DataClassORJSONMixin):
    """
    A single action node.

    Control-flow actions carry nested action lists under
    ``children`` (e.g. ``{"then": [...], "else": [...]}`` for
    ``if``). ``conditions`` is the boolean gate, populated only for
    ``if`` / ``wait_until``.
    """

    action_id: str
    params: dict[str, Any] = field(default_factory=dict)
    children: dict[str, list[ActionNode]] = field(default_factory=dict)
    conditions: list[ConditionNode] = field(default_factory=list)


@dataclass
class AutomationTree(DataClassORJSONMixin):
    """
    The structured form of one automation.

    ``trigger_id`` is ``None`` for top-level ``script:`` /
    ``interval:`` blocks — the block kind is implied by the
    location.
    """

    trigger_id: str | None = None
    trigger_params: dict[str, Any] = field(default_factory=dict)
    actions: list[ActionNode] = field(default_factory=list)


@dataclass
class ParsedAutomation(DataClassORJSONMixin):
    """
    One automation extracted from a device YAML.

    ``from_line`` / ``to_line`` are 1-indexed line numbers for the
    navigator. ``raw_yaml`` is the verbatim slice — kept as the
    read-only fallback when the structured form is unrecoverable.
    """

    location: AutomationLocation
    label: str
    automation: AutomationTree
    from_line: int
    to_line: int
    raw_yaml: str


# ---------------------------------------------------------------------------
# get_available response
# ---------------------------------------------------------------------------


@dataclass
class AvailableScriptParameter(DataClassORJSONMixin):
    """A single declared parameter of a ``script:`` block."""

    name: str
    type: str


@dataclass
class AvailableScript(DataClassORJSONMixin):
    """A declared ``script: id`` in the device YAML."""

    id: str
    parameters: list[AvailableScriptParameter] = field(default_factory=list)


@dataclass
class AvailableComponentInstance(DataClassORJSONMixin):
    """A configured component instance the user can target from an action."""

    component_id: str
    id: str
    name: str | None = None


@dataclass
class AvailableAutomations(DataClassORJSONMixin):
    """
    Context-aware catalog scoped to one device's YAML.

    ``triggers`` / ``actions`` / ``conditions`` are filtered to
    the components present in the YAML, matched by the catalog's
    canonical ``<domain>.<platform>`` form: an entry with
    ``domain == "switch.template"`` only surfaces when a switch
    with ``platform: template`` is configured. ``core`` items
    (control flow, lambda, combinators) and device-level
    triggers are always included. ``scripts`` and ``devices``
    feed the action-parameter dropdowns.
    """

    triggers: list[AutomationTrigger] = field(default_factory=list)
    actions: list[AutomationAction] = field(default_factory=list)
    conditions: list[AutomationCondition] = field(default_factory=list)
    scripts: list[AvailableScript] = field(default_factory=list)
    devices: list[AvailableComponentInstance] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Splice diff
# ---------------------------------------------------------------------------


@dataclass
class YamlDiff(DataClassORJSONMixin):
    """
    A splice instruction the frontend applies to the editor pane.

    ``fromLine`` / ``toLine`` are 1-indexed line numbers in the
    *old* YAML text. Two shapes:

    - **Replace** — ``fromLine <= toLine``: lines ``[fromLine,
      toLine]`` (inclusive) are replaced with ``replacement``.
    - **Pure insert** — ``toLine == fromLine - 1``: no lines are
      replaced; ``replacement`` is inserted before ``fromLine``,
      matching CodeMirror's empty-range ``replaceRange``.

    Both shapes are applied through one ``lines.slice(0, fromLine
    - 1) + replacement + lines.slice(toLine)`` pattern on the
    frontend.
    """

    fromLine: int  # noqa: N815 — wire-shape matches frontend
    toLine: int  # noqa: N815
    replacement: str


@dataclass
class UpsertResponse(DataClassORJSONMixin):
    """Wraps the splice diff returned by upsert / delete."""

    yaml_diff: YamlDiff
