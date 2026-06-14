"""Substitution-aware YAML rewriters: ``$var`` / ``${var}`` indirection."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .scalar import (
    _safe_yaml_scalar,
    _strip_yaml_quotes,
    is_plain_literal_scalar,
    read_yaml_scalar,
    rewrite_yaml_scalar,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


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


def is_retargetable_name(value: str) -> bool:
    """
    Return True when :func:`rewrite_name_or_substitution` can retarget *value*.

    A plain literal (rewrite the leaf) or a pure ``${var}`` reference
    (rewrite the substitution definition) is retargetable. An empty
    value, a YAML tag (``!include``), or an embedded substitution
    (``kitchen_${suffix}``) is not — flattening those would silently drop
    the tag / indirection.
    """
    return is_plain_literal_scalar(value) or parse_substitution_ref(value) is not None


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
