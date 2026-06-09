"""Scalar I/O: read / rewrite / quote / safe-scalar helpers."""

from __future__ import annotations

import io
import re
from typing import TYPE_CHECKING, Any

import yaml
from yaml.emitter import Emitter
from yaml.resolver import Resolver

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def is_lambda_sentinel(value: Any) -> bool:
    """Return True for the frontend's ``{_lambda, _tag}`` lambda wire-sentinel."""
    return (
        isinstance(value, dict)
        and value.keys() <= {"_lambda", "_tag"}
        and isinstance(value.get("_lambda"), str)
    )


# Canonical ESPHome YAML indent: two spaces per level. Mirrors the
# frontend's ``ESPHOME_YAML_INDENT`` (``src/util/esphome-yaml-lang.ts``)
# so any code on either side that synthesises YAML lines uses the
# same width — keeps round-trips through the editor visually
# stable, and means the wizard / clone / friendly-name editor
# emit the same shape the user sees in the editor's auto-indent.
ESPHOME_YAML_INDENT = "  "


def block_body_is_list(lines: list[str], header_idx: int, end_idx: int) -> bool:
    """Return True when a block body — lines ``(header_idx, end_idx)`` — is a YAML list."""
    for idx in range(header_idx + 1, end_idx):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped.startswith("- ") or stripped == "-"
    return False


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
# key, ``:``, and the rest of the line verbatim — ``rest`` keeps the
# post-colon whitespace so :func:`_split_value_and_comment` sees the
# ``\s+#`` separator on a value-less line (``name: # TODO``). List
# items (``- foo: bar``) are excluded — none of the rewrite paths we
# care about land inside a list, and the key stack below assumes
# parent → child mapping nesting only.
_MAPPING_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<key>[A-Za-z_][\w-]*):(?P<rest>.*)$")


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


# The plain-vs-quoted decision and the escaping are delegated to PyYAML
# so neither can drift from what the parser accepts. analyze_scalar
# answers the syntax half (safe unquoted?); the resolver answers the
# type half (would the plain form reparse as a non-string?). Both are
# stateless per call, so one shared instance of each is reused.
_SCALAR_ANALYZER = Emitter(io.StringIO(), allow_unicode=True)
_SCALAR_RESOLVER = Resolver()
_STR_TAG = "tag:yaml.org,2002:str"
# Huge width keeps a quoted scalar on one line under the hand-built
# ``key: <scalar>`` layout.
_NO_WRAP = 1 << 30
# libyaml's C emitter, mirroring FastestSafeLoader: byte-identical, ~2x.
try:
    _FASTEST_SAFE_DUMPER: type = yaml.CSafeDumper
except AttributeError:  # pragma: no cover
    _FASTEST_SAFE_DUMPER = yaml.SafeDumper

# Fast path: a value built only from these characters, led by a letter
# and not a reserved word, is always a plain string scalar, so it skips
# the PyYAML round-trip. Strict subset of "plain-safe" — see the fuzz
# test in test_yaml_helpers; never causes a value to emit unquoted that
# PyYAML would have quoted.
_PLAIN_FAST_LEAD = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_PLAIN_FAST_CHARS = _PLAIN_FAST_LEAD | frozenset("0123456789_-./ ")
_RESERVED_PLAIN = frozenset({"true", "false", "null", "yes", "no", "on", "off"})


def _safe_yaml_scalar(value: str) -> str:
    """
    Render *value* as a YAML scalar — plain when safe, double-quoted otherwise.

    Used wherever arbitrary user-supplied strings (friendly_name, ssid,
    component field values, …) are emitted into hand-built YAML. Plain
    identifiers round trip unquoted; anything PyYAML can't emit plain is
    double-quoted and escaped by PyYAML.
    """
    if _plain_is_fast_safe(value) or _plain_is_safe(value):
        return value
    return _quote(value)


def _quote(value: str) -> str:
    """Render *value* as a single-line double-quoted YAML scalar."""
    return yaml.dump(
        value,
        Dumper=_FASTEST_SAFE_DUMPER,
        default_style='"',
        allow_unicode=True,
        width=_NO_WRAP,
    ).rstrip("\n")


def _plain_is_fast_safe(value: str) -> bool:
    """Return True for the common identifier class that is trivially plain-safe."""
    # ``value[:1]`` folds the empty check into the leading-letter test:
    # an empty slice is not in the set, so empty strings fall through.
    return (
        value[:1] in _PLAIN_FAST_LEAD
        and value[-1] != " "
        and value.lower() not in _RESERVED_PLAIN
        and _PLAIN_FAST_CHARS.issuperset(value)
    )


def _plain_is_safe(value: str) -> bool:
    """Return True when *value* round-trips as a string emitted plain (unquoted)."""
    if not value:
        return False
    if not _SCALAR_ANALYZER.analyze_scalar(value).allow_flow_plain:
        return False
    return bool(_SCALAR_RESOLVER.resolve(yaml.ScalarNode, value, (True, False)) == _STR_TAG)


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
