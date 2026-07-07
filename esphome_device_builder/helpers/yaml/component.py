"""Component-block generation: merge / generate / id-derive / value-emit."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ...models.common import ConfigEntryType
from .scalar import (
    ESPHOME_YAML_INDENT,
    _quote,
    _safe_yaml_scalar,
    _split_value_and_comment,
    _strip_yaml_quotes,
    block_body_is_list,
    is_lambda_sentinel,
)
from .scan import block_end_index, find_block_header, leading_ws

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
    "motion",
    "one_wire",
    "packet_transport",
    "radio_frequency",
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

    Platform-style entity domains (``sensor:``, ``output:``, ...) and
    ``multi_conf`` non-platform components (``rtttl:``, ``i2c:``,
    ``uart:``, ...) splice the new entry under an existing top-level
    block — without this, repeated adds emit duplicate top-level keys,
    which ESPHome rejects. A singleton (``ethernet:``, ``wifi:``, ...)
    already present is a no-op — re-adding it (a board bundle re-listing
    the network provider ``create`` already injected) can't splice, so
    an append would duplicate the key. Absent singletons append.

    Idempotent on the entry ``id``: a block whose id is already defined
    is a no-op, so a bundle re-listing a component the user already added
    (its onboard buzzer, an i2c bus) can't emit a second definition of
    the same id (``ID redefined``). ESPHome ids are config-global, so the
    check spans domains.
    """
    block = generate_component_yaml(component, fields)
    if _redefines_existing_id(existing, block, fields.get("id")):
        return existing
    is_platform = component.category in _ENTITY_CATEGORIES
    if is_platform:
        spliced = _splice_into_domain_block(existing, str(component.category), block)
        if spliced is not None:
            return spliced
    elif component.multi_conf:
        spliced = _splice_into_multi_conf_block(existing, component.id, block)
        if spliced is not None:
            return spliced
    elif _find_top_level_block_bounds(existing.splitlines(keepends=True), component.id):
        return existing
    return _append_block(existing, block)


def generate_component_yaml(  # noqa: C901, PLR0912
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
    # without an id can't be referenced from automations. Only fill a
    # field the sub-schema declares: config sub-blocks like the speaker
    # media_player ``announcement_pipeline`` are ``platform_type``-tagged
    # too but take no ``name``.
    for entry in component.config_entries:
        if not entry.platform_type or not entry.config_entries:
            continue
        sub = fields.get(entry.key)
        if not isinstance(sub, dict):
            continue
        sub_keys = {c.key for c in entry.config_entries}
        if sub.get("name") and sub.get("id"):
            continue
        # Build a fresh dict with name/id at the front so the emitted
        # YAML reads naturally (humans put name/id first).
        autofill: dict[str, Any] = {}
        if "name" in sub_keys and not sub.get("name"):
            autofill["name"] = entry.label or entry.key.replace("_", " ").title()
        if "id" in sub_keys and not sub.get("id"):
            autofill["id"] = f"{parent_id}_{entry.key}"
        autofill.update(sub)
        fields[entry.key] = autofill

    if is_platform:
        lines = [f"{category}:", f"{ESPHOME_YAML_INDENT}- platform: {unqualified}"]
        indent = ESPHOME_YAML_INDENT * 2
        for key, value in fields.items():
            lines.extend(_emit_field(key, value, indent))
        return "\n".join(lines)

    body: list[str] = []
    for key, value in fields.items():
        body.extend(_emit_field(key, value, ESPHOME_YAML_INDENT))

    # ``multi_conf`` components are YAML lists (``globals:`` a list of
    # typed variables, ``i2c:`` a list of buses), so emit the first
    # entry in ``- `` list form. A bare mapping only survives via
    # ``cv.ensure_list`` and misleads the user about the block's shape;
    # the next add normalises it to list form anyway.
    if component.multi_conf:
        return "\n".join([f"{comp_id}:", *_mapping_body_to_list_item(body)])

    return "\n".join([f"{comp_id}:", *body])


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


def _redefines_existing_id(existing: str, block: str, id_hint: Any) -> bool:
    """
    Whether the entry in *block* redefines an id already defined in *existing*.

    *id_hint* is the caller's ``fields["id"]`` when set, sparing the block
    scan. The id appears verbatim on its ``id:`` line, so a cheap substring
    reject skips parsing *existing* on the common add path; only a name that
    actually occurs pays for the precise ``id:``-only scan.
    """
    new_id = id_hint if isinstance(id_hint, str) and id_hint else _first_defined_id(block)
    if new_id is None or new_id not in existing:
        return False
    return new_id in _defined_ids(existing)


def _first_defined_id(block: str) -> str | None:
    """Return the id a freshly generated *block* defines at its entry level, or None."""
    for line in block.splitlines():
        if (ident := _line_defined_id(line)) is not None:
            return ident
    return None


def _defined_ids(text: str) -> set[str]:
    """Every id defined via an ``id:`` key in *text* (references like ``i2c_id:`` excluded)."""
    return {ident for line in text.splitlines() if (ident := _line_defined_id(line)) is not None}


def _line_defined_id(line: str) -> str | None:
    """
    Return the id *line* defines if it is a bare ``id:`` definition, else None.

    A reference key (``output:``, ``i2c_id:``) never starts with the bare
    ``id:`` token, so it never reads as a definition. A plain identifier
    value skips the scalar splitter; only a quoted or comment-trailing
    value pays for it.
    """
    stripped = line.strip()
    if stripped.startswith("- "):
        stripped = stripped[2:].lstrip()
    if not stripped.startswith("id:"):
        return None
    rest = stripped[len("id:") :].strip()
    if "#" in rest or rest[:1] in ("'", '"'):
        rest = _strip_yaml_quotes(_split_value_and_comment(rest)[0].strip())
    return rest or None


def _append_block(existing: str, block: str) -> str:
    """Append *block* as a new top-level section, normalising spacing."""
    base = existing.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{block}\n"


def _find_top_level_block_bounds(file_lines: list[str], key: str) -> tuple[int, int] | None:
    """
    Locate the ``<key>:`` block in *file_lines*; return ``(header, end)``.

    *end* is the index of the first line that belongs to the next
    top-level block (or ``len(file_lines)`` at EOF), rewound past any
    trailing blank lines so an inserted item lands directly after the
    last content line. Returns ``None`` when no matching header exists.
    """
    block_start = find_block_header(file_lines, key)
    if block_start is None:
        return None

    block_end = block_end_index(file_lines, block_start)
    while block_end > block_start + 1 and not file_lines[block_end - 1].strip():
        block_end -= 1
    return block_start, block_end


def _list_item_indent(file_lines: list[str], header_idx: int, end_idx: int) -> str:
    """
    Dash indent of the first list item in a block body.

    Falls back to the canonical indent when the body holds no item.
    """
    for idx in range(header_idx + 1, end_idx):
        stripped = file_lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") or stripped == "-":
            return leading_ws(file_lines[idx].rstrip("\n\r"))
    return ESPHOME_YAML_INDENT


def _splice_into_domain_block(existing: str, domain: str, block: str) -> str | None:
    """
    Insert the platform-list item from *block* under an existing ``<domain>:``.

    The item is re-indented to the existing list's dash indent so a
    zero- or 4-space-indented block stays parseable. Returns ``None``
    when the existing file has no ``<domain>:`` section so the caller
    can fall back to appending.
    """
    block_lines = block.splitlines()
    if len(block_lines) < 2 or block_lines[0].rstrip() != f"{domain}:":
        return None
    file_lines = existing.splitlines(keepends=True)
    bounds = _find_top_level_block_bounds(file_lines, domain)
    if bounds is None:
        return None
    block_start, last_content = bounds

    dash_indent = _list_item_indent(file_lines, block_start, last_content)
    src_indent = leading_ws(block_lines[1])
    items: list[str] = []
    for line in block_lines[1:]:
        if not line.strip():
            items.append(line)
            continue
        rest = line[len(src_indent) :] if line.startswith(src_indent) else line.lstrip()
        items.append(dash_indent + rest)

    before = "".join(file_lines[:last_content])
    after = "".join(file_lines[last_content:])
    if before and not before.endswith("\n"):
        before += "\n"
    insertion = "\n".join(items) + "\n"
    return before + insertion + after


def _splice_into_multi_conf_block(existing: str, comp_id: str, block: str) -> str | None:
    """
    Normalise an existing ``<comp_id>:`` body to list-form, then splice *block* in.

    *block* already arrives list-form from
    :func:`generate_component_yaml`; this only has to rewrite a legacy
    mapping-form body already on disk before appending the new item.
    Returns ``None`` when no such block exists so the caller can fall
    back to a plain append.
    """
    normalized = _normalize_multi_conf_block(existing, comp_id)
    if normalized is None:
        return None
    return _splice_into_domain_block(normalized, comp_id, block)


def _normalize_multi_conf_block(existing: str, comp_id: str) -> str | None:
    """
    Ensure the existing ``<comp_id>:`` body is in YAML list-form.

    Returns ``None`` when no such block exists. A body whose first
    non-comment line starts ``- ...`` (or is a bare ``-``) is already
    list-form and passes through; a mapping body is rewritten as a
    single ``- mapping`` item.
    """
    file_lines = existing.splitlines(keepends=True)
    bounds = _find_top_level_block_bounds(file_lines, comp_id)
    if bounds is None:
        return None
    block_start, last_content = bounds

    if block_body_is_list(file_lines, block_start, last_content):
        return existing

    body_lines = [line.rstrip("\n\r") for line in file_lines[block_start + 1 : last_content]]
    rewritten = "\n".join(_mapping_body_to_list_item(body_lines)) + "\n"
    return "".join(file_lines[: block_start + 1]) + rewritten + "".join(file_lines[last_content:])


def _mapping_body_to_list_item(body_lines: list[str]) -> list[str]:
    """
    Convert a mapping body at any indent to a canonically indented list item.

    The ``- `` marker is anchored on the first non-comment key line;
    a leading ``# ...`` stays above the marker so a comment-decorated
    mapping doesn't demote into a ``- # comment`` null head item.
    """
    body_indent = ""
    for line in body_lines:
        if line.strip() and not line.lstrip().startswith("#"):
            body_indent = leading_ws(line)
            break
    result: list[str] = []
    marked = False
    for line in body_lines:
        if not line.strip():
            result.append(line)
            continue
        rest = line[len(body_indent) :] if line.startswith(body_indent) else line.lstrip()
        if not marked and not line.lstrip().startswith("#"):
            result.append(f"{ESPHOME_YAML_INDENT}- {rest}")
            marked = True
        else:
            result.append(ESPHOME_YAML_INDENT * 2 + rest)
    return result


def _format_yaml_value(value: Any) -> str:
    """Format a Python value for YAML output."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _safe_yaml_scalar(value)
    return str(value)


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
        return _quote(value)
    return formatted


def _emit_lambda_lines(header_prefix: str, body_indent: str, value: dict[str, Any]) -> list[str]:
    """
    Emit a ``{_lambda, _tag}`` sentinel as a ``!lambda |-`` block scalar.

    ``_tag: "!lambda"`` re-emits the tag so the body compiles as a
    lambda; an untagged sentinel emits a bare ``|-``.
    """
    header = "!lambda |-" if value.get("_tag") == "!lambda" else "|-"
    lines = [f"{header_prefix}{header}"]
    lines.extend(f"{body_indent}{line}" if line else "" for line in value["_lambda"].split("\n"))
    return lines


def _emit_field(key: str, value: Any, indent: str) -> list[str]:
    """
    Emit a single ``key: value`` pair as one or more YAML lines.

    Nested mappings (dict values) recurse with deeper indent so a
    ConfigEntry with type=NESTED renders as a YAML mapping under its
    parent. Lists of dicts render as ``- mapping`` entries; lists of
    scalars render as ``[a, b, c]`` flow-style for compactness.
    Lambda sentinels (``{_lambda, _tag}``) emit a ``!lambda |-`` block.
    """
    safe_key = _safe_yaml_scalar(key)
    if is_lambda_sentinel(value):
        return _emit_lambda_lines(f"{indent}{safe_key}: ", indent + ESPHOME_YAML_INDENT, value)
    if isinstance(value, dict):
        lines = [f"{indent}{safe_key}:"]
        for sub_key, sub_value in value.items():
            lines.extend(_emit_field(sub_key, sub_value, indent + ESPHOME_YAML_INDENT))
        return lines
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        lines = [f"{indent}{safe_key}:"]
        # Block-scalar lambda bodies under a list item indent three
        # steps past the field key (matches the frontend serializer).
        body_indent = indent + ESPHOME_YAML_INDENT * 3
        for item in value:
            first = True
            for sub_key, sub_value in item.items():
                prefix = (
                    f"{indent}{ESPHOME_YAML_INDENT}- "
                    if first
                    else f"{indent}{ESPHOME_YAML_INDENT * 2}"
                )
                safe_sub = _safe_yaml_scalar(sub_key)
                if is_lambda_sentinel(sub_value):
                    lines.extend(
                        _emit_lambda_lines(f"{prefix}{safe_sub}: ", body_indent, sub_value)
                    )
                else:
                    lines.append(f"{prefix}{safe_sub}: {_format_yaml_value(sub_value)}")
                first = False
        return lines
    if isinstance(value, list):
        rendered = ", ".join(_format_flow_yaml_value(item) for item in value)
        return [f"{indent}{safe_key}: [{rendered}]"]
    return [f"{indent}{safe_key}: {_format_yaml_value(value)}"]


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
