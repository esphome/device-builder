"""Utilities for generating and modifying ESPHome YAML config files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import ComponentCatalogEntry

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rewrite_esphome_name(yaml: str, old_name: str, new_name: str) -> str:
    """
    Replace ``name:`` under the top-level ``esphome:`` block.

    Only changes lines inside the ``esphome:`` section whose value
    equals *old_name* (with optional surrounding quotes). Indentation
    and trailing comments are preserved. Returns the original text
    unchanged when nothing matches so callers can detect a no-op.
    """
    lines = yaml.splitlines(keepends=True)
    in_esphome = False
    changed = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        # Enter `esphome:` block
        if re.match(r"^esphome:\s*(#.*)?$", stripped):
            in_esphome = True
            continue
        # A new top-level key (col 0, starts with letter) closes the block
        if stripped and stripped[0].isalpha():
            in_esphome = False
            continue
        if not in_esphome:
            continue
        m = re.match(r"^(\s+)name:\s*(.+?)(\s*#.*)?$", stripped)
        if not m:
            continue
        value = m.group(2).strip().strip('"').strip("'")
        if value != old_name:
            continue
        indent, _, comment = m.groups()
        ending = "\n" if line.endswith("\n") else ""
        lines[i] = f"{indent}name: {new_name}{comment or ''}{ending}"
        changed = True
        break
    return "".join(lines) if changed else yaml


def append_yaml_block(yaml_path: Path, block: str) -> str:
    """Append *block* to the YAML file at *yaml_path* and return the full new content."""
    current = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""
    separator = "\n" if current and not current.endswith("\n\n") else ""
    new_content = current + separator + block
    yaml_path.write_text(new_content, encoding="utf-8")
    return new_content


def build_component_yaml(template: str, fields: dict[str, Any]) -> str:
    """Fill a component template and return the rendered YAML block (legacy)."""
    return _fill_template(template, fields)


def build_automation_yaml(
    yaml_path: Path,
    target_component_name: str,
    trigger: str,
    actions: list[dict[str, Any]],
) -> str:
    """Append an automation block to the named component and return full YAML."""
    current = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else ""

    action_lines = []
    for call in actions:
        action_id = call["action"]
        action_fields = call.get("fields", {})
        action_lines.append(f"        - {action_id}:")
        for k, v in action_fields.items():
            action_lines.append(f"            {k}: {v}")

    trigger_block = f"    {trigger}:\n" + "\n".join(action_lines) + "\n"

    name_pattern = re.compile(
        r"^(\s+name:\s+" + re.escape(target_component_name) + r"\s*)$",
        re.MULTILINE,
    )
    match = None
    for m in name_pattern.finditer(current):
        match = m

    if match:
        insert_pos = match.end()
        new_content = current[:insert_pos] + "\n" + trigger_block + current[insert_pos:]
    else:
        separator = "\n" if current and not current.endswith("\n\n") else ""
        new_content = (
            current + separator + f"# Automation for {target_component_name}\n" + trigger_block
        )

    yaml_path.write_text(new_content, encoding="utf-8")
    return new_content


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


def generate_component_yaml(
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
    """
    lines: list[str] = []
    category = component.category
    comp_id = component.id

    is_platform = category in _ENTITY_CATEGORIES

    if is_platform:
        # Catalog ids are qualified as ``<domain>.<platform>`` (e.g.
        # ``output.gpio``, ``light.binary``) so distinct platforms can
        # share a stem across categories. ESPHome YAML expects the bare
        # platform stem under ``platform:``, so strip the qualifier.
        unqualified = comp_id.split(".", 1)[1] if "." in comp_id else comp_id
        lines.append(f"{category}:")
        lines.append(f"  - platform: {unqualified}")
        indent = "    "
    else:
        unqualified = comp_id
        lines.append(f"{comp_id}:")
        indent = "  "

    for key, value in fields.items():
        emit_value = (
            _generate_id(unqualified, fields.get("name")) if key == "id" and not value else value
        )
        lines.extend(_emit_field(key, emit_value, indent))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


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


def _fill_template(template: str, fields: dict[str, Any]) -> str:
    """Replace ``{key}`` placeholders in a YAML template with field values."""
    result = template
    for key, value in fields.items():
        result = result.replace(f"{{{key}}}", str(value))
    lines = []
    for line in result.splitlines(keepends=True):
        if re.search(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}", line):
            continue
        lines.append(line)
    return "".join(lines)


def _format_yaml_value(value: Any) -> str:
    """Format a Python value for YAML output."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if value in ("true", "false", "null", "yes", "no", "on", "off"):
            return f'"{value}"'
        if value.startswith("!") or ":" in value or "#" in value:
            return f'"{value}"'
        return value
    return str(value)


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
            lines.extend(_emit_field(sub_key, sub_value, indent + "  "))
        return lines
    if isinstance(value, list) and value and isinstance(value[0], dict):
        lines = [f"{indent}{key}:"]
        for item in value:
            first = True
            for sub_key, sub_value in item.items():
                prefix = f"{indent}  - " if first else f"{indent}    "
                lines.append(f"{prefix}{sub_key}: {_format_yaml_value(sub_value)}")
                first = False
        return lines
    return [f"{indent}{key}: {_format_yaml_value(value)}"]


def _generate_id(component_id: str, name: str | None = None) -> str:
    """Auto-generate a component ID from the component type and optional name."""
    if name:
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
        return f"{component_id}_{slug}"
    return component_id
