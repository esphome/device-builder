"""Utilities for generating and modifying ESPHome YAML config files."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from ...models import ComponentCatalogEntry

# Prefer the libyaml-backed C loader when PyYAML was built against
# libyaml. On the M5 MacBook Pro, parsing the full board catalog
# (492 manifests) drops from 1.6s to 210ms — a ~7-8x speedup that
# directly cuts dashboard startup wall-time. Mirrors ESPHome's own
# ``yaml_util.FastestAvailableSafeLoader`` so a future audit
# against upstream lands on the same name. PyYAML wheels ship the
# C extension on every platform we target; the SafeLoader fallback
# is for the rare source install against a libyaml-less build.
#
# We deliberately do NOT replicate the upstream ``parse_yaml``
# C-then-pure-Python retry-on-YAMLError pattern. ESPHome surfaces
# the parse error to the user's terminal and uses the pure-Python
# loader's readable error message; every device-builder load site
# either swallows ``yaml.YAMLError`` (mqtt block, secrets file)
# or catches it inside the outer ``except Exception`` of the
# board-catalog walk where the manifest is our own internal data
# linted by ``script/validate_definitions.py``. A double parse
# would cost us per-error wall-time with no user-visible benefit.
try:
    FastestSafeLoader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    # PyYAML wheels on every platform we ship to bundle libyaml,
    # so the fallback is never exercised in CI; ``# pragma: no
    # cover`` keeps Codecov honest about the patch-coverage number.
    FastestSafeLoader = yaml.SafeLoader

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


# Canonical ESPHome YAML indent: two spaces per level. Mirrors the
# frontend's ``ESPHOME_YAML_INDENT`` (``src/util/esphome-yaml-lang.ts``)
# so any code on either side that synthesises YAML lines uses the
# same width — keeps round-trips through the editor visually
# stable, and means the wizard / clone / friendly-name editor
# emit the same shape the user sees in the editor's auto-indent.
def _locate_top_block(lines: list[str], block_key: str) -> tuple[int, int, str] | None:
    """
    Find the column-0 ``block_key:`` block; return ``(start, end, child_indent)``.

    None when the block isn't present. Raises
    :class:`YamlUpsertNotSupportedError` when the header line
    has an inline value (flow-style ``{…}`` or a tag like
    ``!include``) — the line-based walker can't safely edit
    those.

    Comment rules differ by side of the block. Outside (looking
    for the opener), column-0 ``#`` lines are file / inter-block
    headers and get skipped. Inside, a column-0 line — comment
    or content — terminates the block; column-0 comments visually
    belong to whatever's *next*, and treating them as
    block-internal lets a subsequent insert land between two
    indented children (the wizard's ``# Board:`` /
    ``# Definition:`` annotations were the trigger).
    """
    header_re = re.compile(rf"^{re.escape(block_key)}:\s*(?P<rest>.*)$")
    start: int | None = None
    end = len(lines)
    indent = ESPHOME_YAML_INDENT
    indent_captured = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if not stripped:
            continue
        if start is None:
            if stripped.lstrip().startswith("#"):
                continue
            m = header_re.match(stripped)
            if m is None:
                continue
            if m.group("rest").split("#", 1)[0].strip():
                raise YamlUpsertNotSupportedError(
                    f"{block_key}: uses an inline value or flow-style "
                    "mapping; the line-based upsert can't safely "
                    "edit it. Convert the block to multi-line "
                    f"style ({block_key}:\\n  …) and try again."
                )
            start = i
            continue
        if not stripped[0].isspace():
            end = i
            break
        if stripped.lstrip().startswith("#"):
            continue
        if not indent_captured:
            indent = " " * (len(stripped) - len(stripped.lstrip(" ")))
            indent_captured = True
    if start is None:
        return None
    return start, end, indent


def _find_prepend_anchor(lines: list[str]) -> int:
    """Return the line index past leading YAML directives / ``---`` markers."""
    anchor = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(("%", "---")):
            return anchor
        anchor = i + 1
    return anchor


def upsert_yaml_leaf_under_top_block(
    yaml_text: str,
    block_key: str,
    leaf_key: str,
    new_value: str,
) -> str:
    r"""
    Set or insert ``block_key.leaf_key`` to *new_value* in *yaml_text*.

    Three behaviours, picked by the YAML's existing shape:

    1. **Leaf exists** at ``(block_key, leaf_key)`` — rewrite via
       :func:`rewrite_name_or_substitution` so the substitution-
       redirect / safe-quoting machinery applies.
    2. **Top-level ``block_key:`` exists but no ``leaf_key:``
       child** — insert ``  leaf_key: <value>`` at the end of the
       block body, matching the indent of any existing sibling
       (defaults to two spaces when the block has no children).
    3. **No ``block_key:`` block at all** — prepend a new
       ``block_key:\n  leaf_key: <value>\n`` block. Anchored
       below any leading YAML directives / ``---`` markers so
       the doc still parses. Used for package-driven configs
       where the ``esphome:`` block lives in an ``!include``d
       file; ESPHome's package merge gives our local leaf
       precedence over the package's.

    *new_value* is rendered through :func:`_safe_yaml_scalar` so
    YAML-special characters (``Bedroom #2`` etc.) round-trip
    safely. Caller passes the unquoted user input.
    """
    leaf_path = (block_key, leaf_key)
    if read_yaml_scalar(yaml_text, leaf_path) is not None:
        return rewrite_name_or_substitution(yaml_text, leaf_path, new_value)

    rendered = _safe_yaml_scalar(new_value)
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_top_block(lines, block_key)

    if located is None:
        anchor = _find_prepend_anchor(lines)
        prefix = "".join(lines[:anchor])
        rest = "".join(lines[anchor:])
        sep = "" if not rest or rest.startswith("\n") else "\n"
        new_block = f"{block_key}:\n{ESPHOME_YAML_INDENT}{leaf_key}: {rendered}\n{sep}"
        return f"{prefix}{new_block}{rest}"

    block_start, block_end, indent = located
    # Trim trailing blank lines so the insert lands right after
    # the block's last content line, not after the visual gap.
    insert_at = block_end
    while insert_at > block_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_line = f"{indent}{leaf_key}: {rendered}\n"
    return "".join([*lines[:insert_at], new_line, *lines[insert_at:]])


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


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _append_block(existing: str, block: str) -> str:
    """Append *block* as a new top-level section, normalising spacing."""
    base = existing.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{block}\n"


def upsert_inline_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, int, int] | None:
    """
    Insert or replace ``<handler_key>:`` inline under a configured component.

    Used by the automation writer for inline ``on_*:`` triggers under
    component instances (``binary_sensor[i].on_press``, ``light[i].on_turn_on``,
    ...) and for ``effects:`` entries under a light. Returns
    ``(new_yaml_text, from_line, to_line)`` matching the
    :class:`automations.YamlDiff` convention — ``from_line <= to_line``
    for a replace, ``to_line == from_line - 1`` for a pure insert.
    ``None`` when the component instance can't be located (no
    ``id:`` match under ``<component_domain>:``).

    Adjacent siblings are preserved: this only touches the lines
    spanning ``<handler_key>:`` and its indented children. The
    *rendered_yaml* string is emitted at the same indent as the
    sibling fields.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    instance_start, instance_end, child_indent = span

    # Look for an existing ``<handler_key>:`` line under this
    # instance. The key is at exactly ``child_indent`` columns of
    # leading whitespace.
    handler_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(handler_key)}:\s*(?:#.*)?$")
    handler_start: int | None = None
    handler_end: int | None = None
    for idx in range(instance_start, instance_end):
        if handler_re.match(lines[idx].rstrip("\n\r")):
            handler_start = idx
            # Walk forward to find the first sibling-indented line
            # (or instance end).
            for jdx in range(idx + 1, instance_end):
                content = lines[jdx].rstrip("\n\r")
                if not content:
                    continue
                leading = len(content) - len(content.lstrip(" "))
                if leading <= len(child_indent):
                    handler_end = jdx
                    break
            if handler_end is None:
                handler_end = instance_end
            break

    rendered_lines = _indent_block(rendered_yaml, child_indent)
    rendered_text = "\n".join(rendered_lines) + "\n"

    if handler_start is not None and handler_end is not None:
        # Replace the existing handler block.
        new_lines = [*lines[:handler_start], rendered_text, *lines[handler_end:]]
        new_text = "".join(new_lines)
        return new_text, handler_start + 1, handler_end
    # Insert a new handler at the end of the instance, before any
    # trailing blank lines.
    insert_at = instance_end
    while insert_at > instance_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], rendered_text, *lines[insert_at:]]
    new_text = "".join(new_lines)
    # Pure-insert: ``toLine == fromLine - 1`` flags the empty
    # replaced range. See :class:`automations.YamlDiff`.
    return new_text, insert_at + 1, insert_at


def remove_inline_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    handler_key: str,
) -> tuple[str, int, int] | None:
    """
    Delete an inline handler under a configured component.

    Returns ``(new_yaml_text, from_line, to_line)`` matching the
    same :class:`automations.YamlDiff` shape ``upsert_inline_handler``
    emits, or ``None`` when the handler isn't there.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    instance_start, instance_end, child_indent = span
    handler_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(handler_key)}:\s*(?:#.*)?$")
    for idx in range(instance_start, instance_end):
        if not handler_re.match(lines[idx].rstrip("\n\r")):
            continue
        handler_end = instance_end
        for jdx in range(idx + 1, instance_end):
            content = lines[jdx].rstrip("\n\r")
            if not content:
                continue
            leading = len(content) - len(content.lstrip(" "))
            if leading <= len(child_indent):
                handler_end = jdx
                break
        new_lines = [*lines[:idx], *lines[handler_end:]]
        return "".join(new_lines), idx + 1, handler_end
    return None


def _locate_component_instance(
    lines: list[str],
    domain: str,
    component_id: str,
) -> tuple[int, int, str] | None:
    """
    Find the line range of a specific ``- id: <component_id>`` block.

    Returns ``(start_idx, end_idx, child_indent)`` — dash-line
    index, one-past-last-line index, and the leading whitespace of
    the instance's child fields.
    """
    header_re = re.compile(rf"^{re.escape(domain)}:\s*(?:#.*)?$")
    domain_start: int | None = None
    for idx, line in enumerate(lines):
        if header_re.match(line.rstrip("\n\r")):
            domain_start = idx
            break
    if domain_start is None:
        return None
    domain_end = len(lines)
    for idx in range(domain_start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            domain_end = idx
            break

    # Walk the domain body looking for a list item whose first child
    # line carries ``id: <component_id>``. Only column-2 dashes count
    # as instance starts — deeper dashes are inner action lists.
    item_indent: str | None = None
    item_starts: list[int] = []
    for idx in range(domain_start + 1, domain_end):
        raw = lines[idx].rstrip("\n\r")
        stripped = raw.lstrip(" ")
        if not stripped.startswith("- "):
            continue
        prefix = raw[: len(raw) - len(stripped)]
        if item_indent is None:
            item_indent = prefix
        if prefix != item_indent:
            # Inner action list — deeper indent than the canonical
            # list-of-instances. Skip.
            continue
        item_starts.append(idx)

    for run, start in enumerate(item_starts):
        end = item_starts[run + 1] if run + 1 < len(item_starts) else domain_end
        dash_indent = lines[start][: len(lines[start]) - len(lines[start].lstrip(" "))]
        child_indent = dash_indent + ESPHOME_YAML_INDENT
        if _instance_id_matches(lines, start, end, child_indent, component_id):
            return start, end, child_indent
    return None


def _instance_id_matches(
    lines: list[str],
    start: int,
    end: int,
    child_indent: str,
    component_id: str,
) -> bool:
    """
    Return True iff the instance at *start* carries ``id: component_id``.

    Two shapes the schema permits: ``- id: <comp_id>`` on the dash
    line itself, or ``id:`` as a regular child field at
    ``child_indent`` on a later line.
    """
    first_line = lines[start].rstrip("\n\r")
    inline_match = re.match(r"^\s*-\s*id:\s*(?P<id>\S+)", first_line)
    if inline_match:
        return inline_match.group("id") == component_id
    child_re = re.compile(rf"^{re.escape(child_indent)}id:\s*(?P<id>\S+)")
    for jdx in range(start, end):
        m = child_re.match(lines[jdx].rstrip("\n\r"))
        if m:
            return m.group("id") == component_id
    return False


def _indent_block(block_text: str, indent: str) -> list[str]:
    """Return *block_text* with every non-empty line prefixed by *indent*."""
    out: list[str] = []
    for line in block_text.splitlines():
        if not line:
            out.append("")
            continue
        out.append(indent + line)
    return out


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
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if value in ("true", "false", "null", "yes", "no", "on", "off", "%"):
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
            lines.extend(_emit_field(sub_key, sub_value, indent + ESPHOME_YAML_INDENT))
        return lines
    if isinstance(value, list) and value and isinstance(value[0], dict):
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


# Re-exports at the bottom. Redundant-alias form marks these as
# intentional re-exports (PEP 484) so external callers'
# ``from .helpers.yaml import X`` keeps working unchanged across
# the split arc.
from .api_encryption import generate_api_encryption_key as generate_api_encryption_key  # noqa: E402
from .api_encryption import rewrite_api_encryption_key as rewrite_api_encryption_key  # noqa: E402
from .scalar import ESPHOME_YAML_INDENT as ESPHOME_YAML_INDENT  # noqa: E402
from .scalar import YamlUpsertNotSupportedError as YamlUpsertNotSupportedError  # noqa: E402
from .scalar import _quote as _quote  # noqa: E402
from .scalar import _safe_yaml_scalar as _safe_yaml_scalar  # noqa: E402
from .scalar import _strip_yaml_quotes as _strip_yaml_quotes  # noqa: E402
from .scalar import read_yaml_scalar as read_yaml_scalar  # noqa: E402
from .scalar import rewrite_yaml_scalar as rewrite_yaml_scalar  # noqa: E402
from .substitution import parse_substitution_ref as parse_substitution_ref  # noqa: E402
from .substitution import rewrite_name_or_substitution as rewrite_name_or_substitution  # noqa: E402
