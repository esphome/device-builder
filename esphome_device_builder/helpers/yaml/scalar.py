"""Scalar I/O: read / rewrite / quote / safe-scalar helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


# Canonical ESPHome YAML indent: two spaces per level. Mirrors the
# frontend's ``ESPHOME_YAML_INDENT`` (``src/util/esphome-yaml-lang.ts``)
# so any code on either side that synthesises YAML lines uses the
# same width — keeps round-trips through the editor visually
# stable, and means the wizard / clone / friendly-name editor
# emit the same shape the user sees in the editor's auto-indent.
ESPHOME_YAML_INDENT = "  "


class YamlUpsertNotSupportedError(ValueError):
    """The YAML's existing shape can't be safely upserted line-by-line.

    Raised by :func:`upsert_yaml_leaf_under_top_block` when the
    block already exists in a shape the line-based walker can't
    safely modify (flow-style mapping, ``!include`` /
    ``!secret`` tagged value, anything else with a non-empty value
    on the block-header line). The caller is expected to surface
    the message as a typed user-facing error (the WS layer wraps
    in ``CommandError(INVALID_ARGS)``).
    """


# Mapping-key line: optional leading whitespace, an unquoted scalar
# key, ``:``, optional whitespace, optional value, optional trailing
# comment. List items (``- foo: bar``) are excluded — none of the
# rewrite paths we care about land inside a list, and the key stack
# below assumes parent → child mapping nesting only.
_MAPPING_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w-]*):\s*(?P<rest>.*)$")


def _split_value_and_comment(rest: str) -> tuple[str, str]:
    r"""
    Split *rest* into ``(value, comment)`` at a real ``\s+#`` separator.

    A ``#`` only opens a comment when preceded by whitespace
    *and* outside any quoted scalar. Without the quote-state
    check, ``friendly_name: "Bedroom #2"`` would mis-split as
    ``"Bedroom`` (value) + ``" #2"`` (comment).

    Honours both YAML quote-escape conventions so the splitter
    survives a round-trip through our own ``_quote`` (which emits
    ``\"`` inside double-quoted output for friendly names that
    contain ``"``):

    - Double-quoted: ``\"`` escapes a literal quote. Skip the
      escape sequence body so the quote-flip stays accurate.
    - Single-quoted: ``''`` is YAML's escape for a literal single
      quote inside a single-quoted scalar. A doubled closer means
      "stay in the string"; only an unpaired ``'`` ends the scalar.

    *value* keeps the surrounding quotes intact and is stripped
    of trailing whitespace (the comment owns its leading run).
    *comment* includes the leading whitespace + ``#`` so the
    rewriter pastes it back verbatim. Empty *comment* means no
    trailing comment was found.
    """
    quote: str | None = None
    i = 0
    n = len(rest)
    while i < n:
        ch = rest[i]
        if quote is not None:
            if ch == "\\" and quote == '"' and i + 1 < n:
                # Double-quoted escape — skip the escape body so a
                # ``\"`` doesn't read as the closing quote.
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < n and rest[i + 1] == "'":
                    # Single-quoted ``''`` is a literal quote, not
                    # the closer — stay inside the scalar.
                    i += 2
                    continue
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#" and i > 0 and rest[i - 1] in " \t":
            value = rest[:i].rstrip(" \t")
            return value, rest[len(value) :]
        i += 1
    return rest, ""


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
    target_parents = tuple(path[:-1])
    leaf_key = path[-1]
    lines = yaml_text.splitlines(keepends=True)
    # ``stack`` holds (indent, key) for *every* enclosing frame —
    # mapping keys (on-path or off) push their name, list items
    # push the ``_LIST_FRAME`` sentinel. Tracking off-path keys
    # too keeps the path comparison sound: for path
    # ``("api", "encryption", "key")``, YAML ``api: { something:
    # { encryption: { key: ... } } }`` would otherwise falsely
    # match because ``something`` would be invisible to the
    # ancestor check.
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        body = line.rstrip("\n\r")
        head = body.lstrip(" ")
        # Blank / comment-only lines stay inside whatever block
        # they appear in — popping on whitespace would close blocks
        # that have a blank between the parent and the first child.
        if not head or head.startswith("#"):
            continue
        indent = len(body) - len(head)
        # Pop every frame at this indent or shallower before we
        # decide what this line is. The new line lives at a
        # sibling-or-shallower position, so deeper frames are
        # closed regardless of which branch follows.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if head.startswith("- ") or head == "-":
            # List items break the mapping path — anything nested
            # inside is "in a list", not a direct child of the
            # parent mapping. Push the opaque frame so deeper keys
            # can't satisfy a plain-mapping path.
            stack.append((indent, _LIST_FRAME))
            continue
        m = _MAPPING_KEY_LINE.match(body)
        if not m:
            # Block-scalar continuation, plain-scalar list element
            # without a key, … — not on any supported path.
            continue
        key = m.group("key")
        if key == leaf_key and tuple(k for _, k in stack) == target_parents:
            value_part, comment = _split_value_and_comment(m.group("rest"))
            replacement = transform(value_part.strip())
            if replacement is None:
                return yaml_text
            ending = line[len(body) :]  # preserves "\n" / "\r\n" / ""
            lines[i] = f"{m.group('indent')}{key}: {replacement}{comment}{ending}"
            return "".join(lines)
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
    trip without quotes; values get double-quoted (with embedded
    ``"`` and ``\\`` escaped) when any of these holds:

    - empty string or matches a reserved plain scalar
      (``true`` / ``false`` / ``null`` / ``yes`` / ``no`` /
      ``on`` / ``off`` / ``~``);
    - starts with a YAML indicator character (``! & * ? | > %
      @ ` # - , [ ] { } " '``);
    - ends in ``:`` (would parse as a key with empty value) or in
      whitespace (would lose the trailing space on round trip);
    - contains ``: `` (key/value split) or `` #`` (comment marker);
    - contains a control character (``\\n`` / ``\\r`` / ``\\t``).
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


# YAML double-quoted scalar escapes for the five characters that
# would otherwise break round-trip: ``\`` and ``"`` need escaping
# because the closing quote / escape leader; the three control
# characters need escaping because plain-text rendering would split
# the value across lines or eat the tab.
_QUOTE_ESCAPES = str.maketrans(
    {
        "\\": r"\\",
        '"': r"\"",
        "\n": r"\n",
        "\r": r"\r",
        "\t": r"\t",
    }
)


def _quote(value: str) -> str:
    """Render *value* as a double-quoted YAML scalar with minimal escapes."""
    return f'"{value.translate(_QUOTE_ESCAPES)}"'


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
