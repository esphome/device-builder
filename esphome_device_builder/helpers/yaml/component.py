"""Component-block generation: merge / generate / id-derive / value-emit."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

from ...models.common import ConfigEntryType
from .scalar import ESPHOME_YAML_INDENT

if TYPE_CHECKING:
    from ...models import ComponentCatalogEntry


# Platform categories that use the list-under-platform YAML pattern
# (`sensor: [- platform: ...]`) rather than a single top-level key.
# Must include every ComponentCategory value whose components carry
# `<domain>.<platform>` ids in the catalog — otherwise add_component
# falls through to writing the qualified id literally as a top-level
# YAML key (`time.homeassistant:`), which ESPHome rejects and our own
# YAML parser can't handle either (the regex only accepts
# `[a-zA-Z_][a-zA-Z0-9_]*:`, no dots).
_ENTITY_CATEGORIES = {
    # Home Assistant entity domains
    "sensor",
    "binary_sensor",
    "switch",
    "light",
    "fan",
    "cover",
    "climate",
    "button",
    "number",
    "select",
    "text",
    "text_sensor",
    "lock",
    "valve",
    "media_player",
    "speaker",
    "microphone",
    "camera",
    "display",
    "touchscreen",
    "output",
    "datetime",
    "event",
    "update",
    "alarm_control_panel",
    # Other platform-pattern domains the sync script tags as their
    # own categories. Each one shows up in YAML as `<domain>: [-
    # platform: ...]` blocks.
    "ota",
    "time",
    "audio_adc",
    "audio_dac",
    "canbus",
    "infrared",
    "media_source",
    "one_wire",
    "packet_transport",
    "stepper",
    "water_heater",
}


def merge_component_yaml(
    existing: str,
    component: ComponentCatalogEntry,
    fields: dict[str, Any],
) -> str:
    """
    Render *component* and merge it into *existing* YAML.

    For platform-style components (``sensor:``, ``output:``, ...) the
    new ``- platform: ...`` list item is appended under the existing
    domain block when one is already present — without this, repeatedly
    adding components of the same domain would produce duplicate
    top-level ``output:`` / ``sensor:`` blocks. Other components fall
    through to a plain append.
    """
    block = generate_component_yaml(component, fields)
    is_platform = component.category in _ENTITY_CATEGORIES
    if is_platform:
        spliced = _splice_into_domain_block(existing, str(component.category), block)
        if spliced is not None:
            return spliced
    return _append_block(existing, block)


def generate_component_yaml(  # noqa: C901
    component: ComponentCatalogEntry,
    fields: dict[str, Any],
) -> str:
    """
    Generate a YAML block for adding a component to a device config.

    Platform-style components (``sensor``, ``switch``, ...) are emitted
    as a list under their category with a ``- platform: <id>`` entry;
    everything else is emitted as a top-level mapping keyed by the
    component id.

    Nested values in ``fields`` (dicts as values) are emitted as
    indented YAML mappings — frontend submits the full structure as a
    single ``fields`` argument, no separate sub-entries dict needed.

    Two kinds of identifier auto-fill happen here:

    - Top-level ``id`` when the caller explicitly passed ``id: ""``
      (a marker that says "give me the default"). Result is
      ``<unqualified>[_<name_slug>]``.
    - Nested entity sub-blocks (entries marked with ``platform_type``,
      e.g. HLW8012's ``current`` / ``energy`` / ``power`` / ``voltage``)
      get a default ``name`` and ``id`` when the caller didn't set
      one — without these the sub-sensor either won't surface in HA
      (no name) or can't be referenced from automations (no id).
    """
    fields = dict(fields)
    _coerce_string_map_values(component, fields)
    category = component.category
    comp_id = component.id

    is_platform = category in _ENTITY_CATEGORIES

    if is_platform:
        # Catalog ids are qualified as ``<domain>.<platform>`` (e.g.
        # ``output.gpio``, ``light.binary``) so distinct platforms can
        # share a stem across categories. ESPHome YAML expects the bare
        # platform stem under ``platform:``, so strip the qualifier.
        unqualified = comp_id.split(".", 1)[1] if "." in comp_id else comp_id
    else:
        unqualified = comp_id

    # Resolve the top-level id once. We only emit it when the caller
    # explicitly opted in by including ``id`` in fields; when they
    # did but left it empty, fill in the auto-generated value here so
    # nested entity sub-blocks can prefix their own ids consistently.
    if "id" in fields and not fields["id"]:
        fields["id"] = _generate_id(unqualified, fields.get("name"))
    parent_id = fields.get("id") or _generate_id(unqualified, fields.get("name"))

    # Auto-fill name + id on nested entity sub-blocks the caller left
    # empty. ESPHome multi-sensor parents (HLW8012, BME280, ...)
    # expose their readings as ``platform_type``-tagged ConfigEntry
    # blocks; an unnamed sub-sensor won't surface in HA, and one
    # without an id can't be referenced from automations.
    for entry in component.config_entries:
        if not entry.platform_type or not entry.config_entries:
            continue
        sub = fields.get(entry.key)
        if not isinstance(sub, dict):
            continue
        if sub.get("name") and sub.get("id"):
            continue
        # Build a fresh dict with name/id at the front so the emitted
        # YAML reads naturally (humans put name/id first).
        autofill: dict[str, Any] = {}
        if not sub.get("name"):
            autofill["name"] = entry.label or entry.key.replace("_", " ").title()
        if not sub.get("id"):
            autofill["id"] = f"{parent_id}_{entry.key}"
        autofill.update(sub)
        fields[entry.key] = autofill

    lines: list[str] = []
    if is_platform:
        lines.append(f"{category}:")
        lines.append(f"{ESPHOME_YAML_INDENT}- platform: {unqualified}")
        indent = ESPHOME_YAML_INDENT * 2
    else:
        lines.append(f"{comp_id}:")
        indent = ESPHOME_YAML_INDENT

    for key, value in fields.items():
        lines.extend(_emit_field(key, value, indent))

    return "\n".join(lines)


def _coerce_string_map_values(
    component: ComponentCatalogEntry,
    fields: dict[str, Any],
) -> None:
    """
    Stringify dict values for MAP fields whose value template is STRING.

    ``sdkconfig_options`` validates as ``Dict[str, str]`` on the
    ESPHome side; a frontend that sends JSON number ``100`` for the
    value would otherwise emit ``CONFIG_FOO: 100`` and trip
    ``cv.string_strict`` (issue #901).
    """
    for entry in component.config_entries:
        if entry.type != ConfigEntryType.MAP:
            continue
        if not entry.config_entries:
            continue
        if entry.config_entries[0].type != ConfigEntryType.STRING:
            continue
        value = fields.get(entry.key)
        if not isinstance(value, dict):
            continue
        fields[entry.key] = {k: _coerce_map_scalar_to_string(v) for k, v in value.items()}


def _coerce_map_scalar_to_string(value: Any) -> str:
    """Convert a MAP-value scalar to its YAML-string form."""
    if isinstance(value, str):
        return value
    # ``str(True)`` is ``"True"`` which YAML 1.1 re-parses as bool;
    # canonicalise to the lowercase form so the downstream quoter
    # recognises and quotes it.
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _append_block(existing: str, block: str) -> str:
    """Append *block* as a new top-level section, normalising spacing."""
    base = existing.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{block}\n"


def _splice_into_domain_block(existing: str, domain: str, block: str) -> str | None:
    """
    Insert the platform-list item from *block* under an existing ``<domain>:``.

    Returns the merged YAML, or ``None`` when the existing file has no
    ``<domain>:`` section (caller should fall back to appending). The
    splice walks line-by-line: it locates the domain header, then finds
    the first subsequent line that starts a new top-level key (column
    zero, alphabetic) — everything in between is the existing block. The
    new list item is inserted before that boundary, preserving any
    trailing blank lines and content that follows.
    """
    block_lines = block.splitlines()
    if len(block_lines) < 2 or block_lines[0].rstrip() != f"{domain}:":
        return None
    inner_lines = block_lines[1:]

    file_lines = existing.splitlines(keepends=True)
    header_re = re.compile(rf"^{re.escape(domain)}:\s*(?:#.*)?$")
    domain_start: int | None = None
    for idx, line in enumerate(file_lines):
        if header_re.match(line.rstrip("\n\r")):
            domain_start = idx
            break
    if domain_start is None:
        return None

    # Walk forward to find the first line that opens a new top-level
    # block, or stop at EOF.
    domain_end = len(file_lines)
    for idx in range(domain_start + 1, len(file_lines)):
        stripped = file_lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            domain_end = idx
            break

    # Trim trailing blank lines belonging to the domain block — we want
    # the new item appended directly after the last content line, then
    # the blank lines preserved before whatever comes next.
    last_content = domain_end
    while last_content > domain_start + 1 and not file_lines[last_content - 1].strip():
        last_content -= 1

    before = "".join(file_lines[:last_content])
    after = "".join(file_lines[last_content:])
    if before and not before.endswith("\n"):
        before += "\n"
    insertion = "\n".join(inner_lines) + "\n"
    return before + insertion + after


def _format_yaml_value(value: Any) -> str:
    """Format a Python value for YAML output."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"' if _string_needs_quoting(value) else value
    return str(value)


_YAML_RESERVED_KEYWORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off"})


def _string_needs_quoting(value: str) -> bool:
    """Return True when *value* needs YAML quoting to round-trip as a string."""
    # YAML 1.1 recognises every case variant of the reserved words
    # (``true``/``True``/``TRUE`` etc.) as bool/null, so ``str(True)``
    # from a JSON bool would otherwise re-parse as ``True``. ``%`` is
    # a YAML directive indicator; ``~`` and empty string are YAML null
    # shorthands; ``!`` opens a tag; ``:`` opens a mapping value;
    # ``#`` opens a comment. Anything that survives those checks then
    # gets the (cheap pre-filtered) ``yaml.safe_load`` round-trip test
    # for numeric-looking strings — the original #901 case.
    if value.lower() in _YAML_RESERVED_KEYWORDS or value in ("%", "~", ""):
        return True
    if value.startswith("!") or ":" in value or "#" in value:
        return True
    return _yaml_reparses_as_non_string(value)


# YAML 1.1 plain scalars can only re-parse as a non-string when the
# first character is a digit, sign, or ``.`` (covers int / float /
# hex / binary / ``.inf`` / ``.nan`` / dates / timestamps). Every
# other plain leading character resolves to a string, so the cheap
# membership test rules out the ``yaml.safe_load`` call for typical
# values like ``"GPIO4"`` or ``"Bedroom Light"`` — without the
# pre-filter the parser ran on every emitted string field and
# regressed ``merge_component_yaml`` by ~600µs per emission
# (CodSpeed flagged this on #908).
_YAML_AMBIGUOUS_FIRST = frozenset("0123456789-+.")


def _yaml_reparses_as_non_string(value: str) -> bool:
    """Return True when ``yaml.safe_load(value)`` is not a string."""
    if not value or value[0] not in _YAML_AMBIGUOUS_FIRST:
        return False
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return False
    return parsed is not None and not isinstance(parsed, str)


def _format_flow_yaml_value(value: Any) -> str:
    """
    Format *value* for emission inside a YAML flow sequence ``[...]``.

    Flow context makes ``,`` and ``[ ] { }`` syntactically significant —
    a plain string like ``"a,b"`` parses as two flow items. Quote
    strings carrying any of those characters so the sequence round
    trips as a single element. Bools / numbers and strings already
    quoted by :func:`_format_yaml_value` pass through unchanged.
    """
    formatted = _format_yaml_value(value)
    if (
        isinstance(value, str)
        and not formatted.startswith('"')
        and any(c in value for c in ",[]{}")
    ):
        return f'"{value}"'
    return formatted


def _emit_field(key: str, value: Any, indent: str) -> list[str]:
    """
    Emit a single ``key: value`` pair as one or more YAML lines.

    Nested mappings (dict values) recurse with deeper indent so a
    ConfigEntry with type=NESTED renders as a YAML mapping under its
    parent. Lists of dicts render as ``- mapping`` entries; lists of
    scalars render as ``[a, b, c]`` flow-style for compactness.
    """
    if isinstance(value, dict):
        lines = [f"{indent}{key}:"]
        for sub_key, sub_value in value.items():
            lines.extend(_emit_field(sub_key, sub_value, indent + ESPHOME_YAML_INDENT))
        return lines
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        lines = [f"{indent}{key}:"]
        for item in value:
            first = True
            for sub_key, sub_value in item.items():
                prefix = (
                    f"{indent}{ESPHOME_YAML_INDENT}- "
                    if first
                    else f"{indent}{ESPHOME_YAML_INDENT * 2}"
                )
                lines.append(f"{prefix}{sub_key}: {_format_yaml_value(sub_value)}")
                first = False
        return lines
    if isinstance(value, list):
        rendered = ", ".join(_format_flow_yaml_value(item) for item in value)
        return [f"{indent}{key}: [{rendered}]"]
    return [f"{indent}{key}: {_format_yaml_value(value)}"]


def _generate_id(component_id: str, name: str | None = None) -> str:
    """
    Auto-generate a component ID from the component type and optional name.

    Returns ``<component_id>_<name_slug>`` when *name* contributes
    usable characters, falling back to bare ``component_id`` when
    *name* is empty / missing or slugifies to nothing (e.g. only
    punctuation). When the slug already leads with ``component_id``
    the redundant prefix is dropped — otherwise a display name that
    starts with the chip stem produces ids like
    ``hlw8012_hlw8012_power_monitor`` instead of
    ``hlw8012_power_monitor``.
    """
    if not name:
        return component_id
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not slug:
        return component_id
    if slug == component_id or slug.startswith(f"{component_id}_"):
        return slug
    return f"{component_id}_{slug}"
