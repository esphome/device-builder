"""Utilities for generating and modifying ESPHome YAML config files."""

from __future__ import annotations

import base64
import re
import secrets
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ..models import ComponentCatalogEntry

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


# Mapping-key line: optional leading whitespace, an unquoted scalar
# key, ``:``, optional whitespace, optional value, optional trailing
# comment. List items (``- foo: bar``) are excluded — none of the
# rewrite paths we care about land inside a list, and the key stack
# below assumes parent → child mapping nesting only.
_MAPPING_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w-]*):\s*(?P<rest>.*)$")
# Trailing-comment splitter for the value half. ``#`` only opens a
# comment when preceded by whitespace; ``key: ab#cd`` is the literal
# scalar ``ab#cd``. Quoted values aren't stripped (we leave the
# quotes in *raw_value* so the transform sees them).
_VALUE_AND_COMMENT = re.compile(r"^(?P<value>.*?)(?P<comment>\s+#.*)?$")
# Sentinel pushed onto the path stack when we descend into a list
# item. Picked as a string that can't collide with a real YAML key
# (the leading ``-`` prevents a match against the mapping-key regex's
# ``[A-Za-z_]`` anchor).
_LIST_FRAME = "-list-"


def rewrite_yaml_scalar(
    yaml_text: str,
    path: Sequence[str],
    transform: Callable[[str], str | None],
) -> str:
    """
    Rewrite the scalar at the YAML mapping *path* in *yaml_text*.

    *path* is the ancestor → leaf chain of mapping keys
    (e.g. ``("esphome", "name")``, ``("api", "encryption", "key")``).
    The walker tracks the open ancestor stack by indent and only
    rewrites a leaf line whose ancestor chain matches *path[:-1]*
    and whose own key equals *path[-1]*.

    *transform* receives the leaf's *raw value* — the substring
    between the colon's trailing whitespace and any trailing
    ``# comment``, with surrounding whitespace stripped but quotes
    kept. It returns the rendered replacement (caller decides
    whether to wrap in quotes, regenerate from scratch, etc.) or
    ``None`` to leave the line untouched.

    Indentation and trailing comments survive the rewrite. Only the
    first matching leaf is rewritten; pathological YAMLs with the
    same path appearing twice get only the first one touched —
    matches our callers' expectation that a well-formed config
    declares each path once. Returns the input string unchanged when
    no leaf is found or when *transform* returns ``None``.

    Walker only handles unquoted plain mapping keys nested via
    indentation (``foo:`` / ``  bar:`` …) — the shape every path
    our callers care about uses. List items (``- platform: …``)
    and quoted keys (``"foo": …``) are skipped; supporting them
    would change the meaning of "the scalar at *path*" in ways that
    don't match how ESPHome configs are written by hand.
    """
    if not path:
        return yaml_text
    lines = yaml_text.splitlines(keepends=True)
    # ``stack`` holds (indent, key) for *every* ancestor frame
    # we're currently inside, on-path or not. Mapping keys push
    # their own name; list items push the sentinel
    # ``_LIST_FRAME``. Tracking only on-path ancestors would let
    # a deeper key satisfy a shorter path — e.g. for path
    # ``("api", "encryption", "key")``, YAML
    # ``api: { something: { encryption: { key: ... } } }`` would
    # falsely match because ``something`` would be invisible to
    # the ancestor check. We pay one extra ``stack.append`` per
    # off-path key in exchange for a sound path comparison.
    stack: list[tuple[int, str]] = []
    target_parents = tuple(path[:-1])
    leaf_key = path[-1]
    for i, line in enumerate(lines):
        stripped_no_eol = line.rstrip("\n\r")
        # Blank / comment-only lines stay inside whatever block they
        # appear in — popping the stack on whitespace would close
        # blocks that have a blank between the parent and the first
        # child key.
        if not stripped_no_eol.strip() or stripped_no_eol.lstrip().startswith("#"):
            continue
        # List items break the mapping path: anything indented inside
        # a list item is "in a list", not a direct child of the
        # parent mapping. Push an opaque frame at the dash's column
        # so deeper keys' ancestor chain can't satisfy the caller's
        # plain-mapping path.
        lstripped = stripped_no_eol.lstrip(" ")
        if lstripped.startswith("- ") or lstripped == "-":
            indent = len(stripped_no_eol) - len(lstripped)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, _LIST_FRAME))
            continue
        m = _MAPPING_KEY_LINE.match(stripped_no_eol)
        if not m:
            # Non-key line (block scalar continuation, …) — not on
            # any of our supported paths. Don't touch the stack.
            continue
        indent = len(m.group("indent"))
        key = m.group("key")
        rest = m.group("rest")
        # Walking back out: pop every stack entry whose indent is
        # ≥ this line's. Those blocks are closed by virtue of this
        # line living at a sibling-or-shallower position.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        ancestor_chain = tuple(k for _, k in stack)
        if key == leaf_key and ancestor_chain == target_parents:
            value_match = _VALUE_AND_COMMENT.match(rest)
            assert value_match is not None  # _VALUE_AND_COMMENT always matches
            raw_value = value_match.group("value").strip()
            comment = value_match.group("comment") or ""
            replacement = transform(raw_value)
            if replacement is None:
                # Transform opted out — keep walking? No: every
                # caller wants "rewrite the first match"; a deeper
                # duplicate isn't more correct than the first one.
                # Returning unchanged matches the original helpers'
                # behaviour.
                return yaml_text
            ending = "\n" if line.endswith("\n") else ""
            lines[i] = f"{m.group('indent')}{key}: {replacement}{comment}{ending}"
            return "".join(lines)
        # Push every mapping key — soundness over micro-optimisation.
        # An off-path key (e.g. ``something:`` between ``api:`` and
        # ``encryption:`` when the target path is
        # ``("api", "encryption", "key")``) must show up in the
        # ancestor chain so the leaf check at the deeper indent
        # correctly fails to match.
        stack.append((indent, key))
    return yaml_text


def read_yaml_scalar(yaml_text: str, path: Sequence[str]) -> str | None:
    """
    Return the raw scalar at the YAML mapping *path*, or ``None``.

    Same walker as :func:`rewrite_yaml_scalar` — same path
    semantics, same list-item / quoted-key skip rules. The
    returned value is the substring between the colon's trailing
    whitespace and any trailing ``# comment``, with surrounding
    whitespace stripped but quotes intact (the same shape the
    rewrite transform receives). ``None`` distinguishes "key not
    present" from "key present, value is empty string".
    """
    captured: list[str] = []

    def _capture(raw: str) -> str | None:
        captured.append(raw)
        return None  # Don't actually rewrite.

    rewrite_yaml_scalar(yaml_text, path, _capture)
    return captured[0] if captured else None


# Plain (unquoted) YAML scalars accept most printable characters,
# but a small set of leading bytes and embedded sequences make the
# parser interpret the value as something other than a plain
# string. ``_PLAIN_SCALAR_INDICATOR_LEAD`` covers the YAML
# indicator characters that, when leading, change scalar shape;
# ``_PLAIN_SCALAR_FORBIDDEN_SUBSTR`` covers the embedded sequences
# that flip a plain scalar into a key/value or comment. ``_RESERVED_PLAIN``
# is the set of plain scalars YAML interprets as bool / null —
# emitting one of these unquoted would round-trip as a non-string.
_PLAIN_SCALAR_INDICATOR_LEAD = set("!&*?|>%@`#-,[]{}\"'")
_PLAIN_SCALAR_FORBIDDEN_SUBSTR = (": ", " #")
_RESERVED_PLAIN = frozenset(
    {
        "true",
        "false",
        "null",
        "yes",
        "no",
        "on",
        "off",
        "~",
        "",
    }
)


def _safe_yaml_scalar(value: str) -> str:
    r"""
    Render *value* as a YAML scalar — plain when safe, double-quoted otherwise.

    Used by rewriters that accept arbitrary user-supplied strings
    (friendly_name, comments, mqtt topics, etc.) where a value
    like ``"Bedroom #2"`` would otherwise become a comment or
    ``"Lamp: Bedroom"`` would split into a key/value pair on round
    trip. Plain identifiers (``"Kitchen"``, ``"my-device"``) round
    trip without quotes; anything that contains a YAML special
    sequence, starts with an indicator character, ends in ``:`` /
    ``-``, or matches a reserved plain scalar gets double-quoted
    with embedded ``"`` and ``\\`` escaped.
    """
    if not value or value.lower() in _RESERVED_PLAIN:
        return f'"{value}"'
    if value[0] in _PLAIN_SCALAR_INDICATOR_LEAD:
        return _quote(value)
    if value.endswith(":") or value.endswith(" "):
        return _quote(value)
    if any(s in value for s in _PLAIN_SCALAR_FORBIDDEN_SUBSTR):
        return _quote(value)
    # ``\n``, ``\r``, and ``\t`` would either be silently stripped
    # (tab) or split into multiple YAML lines. Quote and escape.
    if any(c in value for c in "\n\r\t"):
        return _quote(value)
    return value


def _quote(value: str) -> str:
    """Render *value* as a double-quoted YAML scalar with minimal escapes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{escaped}"'


def _strip_yaml_quotes(value: str) -> str:
    """
    Strip a single matched pair of surrounding quotes from *value*.

    YAML scalars accept ``"..."`` and ``'...'`` quoting; both shapes
    appear in real configs. Helpers that compare against an unquoted
    target (rename's value gate, the substitution-ref parser) need
    to peel the wrapper before comparing without crashing on
    unquoted values.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1]
    return stripped


# ESPHome substitutions are referenced as ``$name`` or ``${name}`` —
# the ``${name}`` form is the canonical one the wizard emits and
# what users following the upstream docs will write. We only treat
# a value as a substitution reference when the *entire* value is
# the reference (``"$devicename"`` / ``"${devicename}"``); a
# value with extra glue (``"my-${suffix}"``) stays as a literal
# rewrite target — replacing the substitution there would replace
# the suffix's expansion across every other consumer.
_PURE_SUBSTITUTION_REF = re.compile(r"\A(?:\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*))\Z")


def parse_substitution_ref(value: str) -> str | None:
    """
    Return the substitution name when *value* is a pure ``$var``.

    Also accepts ``${var}``. Surrounding whitespace and matched
    quotes are stripped before the test. ``"my-${suffix}"`` returns
    ``None`` because only part of the value is the substitution.
    """
    m = _PURE_SUBSTITUTION_REF.match(_strip_yaml_quotes(value))
    if not m:
        return None
    return m.group(1) or m.group(2)


def rewrite_name_or_substitution(
    yaml_text: str,
    leaf_path: Sequence[str],
    new_value: str,
) -> str:
    """
    Land *new_value* at *leaf_path* or at the substitution it references.

    Two real-world ESPHome patterns drive this:

    1. **Direct literal** — ``esphome.name: kitchen``. The leaf
       line carries the value directly; rewrite it.
    2. **Substitution reference** — ``esphome.name: ${devicename}``
       paired with ``substitutions.devicename: kitchen`` (the
       standard wizard / ``dashboard_import`` shape). The leaf
       carries the indirection name; the actual value lives in
       the substitutions block. Rewriting the leaf with a literal
       would silently orphan the substitution and break any other
       consumer (sensor named ``${devicename}_temp``, etc.).

    When the leaf's current value is a *pure* substitution
    reference (``$var`` / ``${var}`` with no surrounding glue) the
    helper walks to ``substitutions.<var>`` and rewrites that
    leaf instead. Mixed values (``${prefix}-suffix``) and any
    other shape fall through to the leaf rewrite — we have no
    way to split a partial reference without changing what the
    other half resolves to elsewhere.

    Returns the original text unchanged when neither the leaf
    nor the substitution leaf exists.
    """
    rendered = _safe_yaml_scalar(new_value)
    raw = read_yaml_scalar(yaml_text, leaf_path)
    var = parse_substitution_ref(raw) if raw is not None else None
    if var is not None:
        sub_path: tuple[str, ...] = ("substitutions", var)
        # Only redirect when the substitution definition is in
        # *this* file's top-level ``substitutions:`` block. A
        # ``!include``d substitutions file or a package-supplied
        # variable wouldn't be visible here; falling through to the
        # leaf lands the literal in our YAML and leaves the
        # remote definition untouched.
        if read_yaml_scalar(yaml_text, sub_path) is not None:
            return rewrite_yaml_scalar(yaml_text, sub_path, lambda _raw: rendered)
    return rewrite_yaml_scalar(yaml_text, leaf_path, lambda _raw: rendered)


def rewrite_esphome_name(
    yaml_text: str,
    new_name: str,
    *,
    only_if_current: str | None = None,
) -> str:
    """
    Replace ``name:`` under the top-level ``esphome:`` block.

    By default the rewrite is unconditional — every caller that
    knows the new name to land on wants the line replaced
    regardless of what the source had (clone path, future
    create-then-edit flows). Pass *only_if_current* to gate the
    rewrite on the existing leaf value matching that string —
    used by ``_manual_rename``'s file-level fallback as a hedge
    against renaming a config whose YAML ``name:`` no longer
    matches its filename. The gate is opt-in because the
    common case (filename and ``esphome.name`` agree) is the
    same outcome either way; the gate only matters for
    drift between the two, where clone wants "force the
    rename" and rename's fallback wants "leave it alone".

    Indentation and trailing comments are preserved. Returns the
    original text unchanged when nothing matches the path or the
    gate.
    """

    def _swap(raw: str) -> str | None:
        if only_if_current is not None and _strip_yaml_quotes(raw) != only_if_current:
            return None
        return new_name

    return rewrite_yaml_scalar(yaml_text, ("esphome", "name"), _swap)


def generate_api_encryption_key() -> str:
    """Return a fresh 32-byte ESPHome API encryption key, base64-encoded."""
    return base64.b64encode(secrets.token_bytes(32)).decode()


def rewrite_api_encryption_key(yaml_text: str, new_key: str) -> str:
    """
    Replace the literal ``key:`` value under ``api: -> encryption:``.

    Used by the clone path so two devices forked from the same
    source don't share API encryption material — compromise of one
    device must not compromise its siblings. Only rewrites a
    *literal* key value; lines whose value is an indirection
    (``!secret …`` / ``${…}``) are left untouched, because the
    indirection target is shared on disk and stomping on the key
    here would silently desync the clone from whatever
    ``secrets.yaml`` / substitutions block actually drives the
    encryption. Returns the original text unchanged when no
    in-scope ``key:`` is found or when the value is an indirection.

    The replacement is rendered double-quoted so a base64 value
    that happens to start with a YAML special character
    (``!``/``%``/``@``/``-``/``?``/``&``/``*``) parses cleanly.
    """
    rendered = _quote(new_key)

    def _swap(raw: str) -> str | None:
        if raw.startswith("!secret") or raw.startswith("${"):
            return None
        return rendered

    return rewrite_yaml_scalar(yaml_text, ("api", "encryption", "key"), _swap)


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
        lines.append(f"  - platform: {unqualified}")
        indent = "    "
    else:
        lines.append(f"{comp_id}:")
        indent = "  "

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
