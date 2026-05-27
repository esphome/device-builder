"""Derive which esphome light platforms each abstract ``light.<SCHEMA>`` accepts.

Extracted from ``sync_components.py`` so the resolver / derivation
helpers are unit-testable in isolation against a fixture
``components/`` tree.
"""

from __future__ import annotations

import json
import logging
import re
from functools import cache
from pathlib import Path

_LOG = logging.getLogger(__name__)

LIGHT_SCHEMA_NAMES = (
    "ADDRESSABLE_LIGHT_SCHEMA",
    "RGB_LIGHT_SCHEMA",
    "BRIGHTNESS_ONLY_LIGHT_SCHEMA",
    "BINARY_LIGHT_SCHEMA",
)


_LIGHT_SCHEMA_RE = re.compile(r"\blight\.(" + "|".join(LIGHT_SCHEMA_NAMES) + r")\b")

# Chases helper schemas defined in a sibling component (e.g.
# ``fastled_base.BASE_SCHEMA`` which in turn extends a light schema).
# We follow ``<module>.<NAME_WITH_SCHEMA>`` refs into the named module's
# source files; the recursion is bounded by ``components_dir`` membership.
_INDIRECT_SCHEMA_REF_RE = re.compile(r"\b(\w+)\.\w*SCHEMA\w*\b")


def resolve_schema_ref(
    path: Path,
    components_dir: Path,
    visited: set[Path],
) -> str | None:
    """Return the abstract light schema *path* (transitively) references."""
    if path in visited or not path.is_file():
        return None
    visited.add(path)
    try:
        text = path.read_text()
    except OSError as err:
        # A flaky read silently dropping a platform from the resolved
        # set would surface as an applies_to regression on the next
        # sync with no log trail. Keep the swallow (one bad file isn't
        # fatal) but log so the cause is diagnosable.
        _LOG.warning("Skipping %s: failed to read (%s)", path, err)
        return None
    m = _LIGHT_SCHEMA_RE.search(text)
    if m is not None:
        return m.group(1)
    for module in _INDIRECT_SCHEMA_REF_RE.findall(text):
        if module == "light":
            continue
        module_dir = components_dir / module
        if not module_dir.is_dir():
            continue
        for candidate in (
            module_dir / "__init__.py",
            module_dir / "light.py",
            module_dir / "light" / "__init__.py",
        ):
            ref = resolve_schema_ref(candidate, components_dir, visited)
            if ref is not None:
                return ref
    return None


def derive_light_platforms_from_dir(
    components_dir: Path,
) -> dict[str, frozenset[str]]:
    """Map each abstract light schema to platform ids using *components_dir*.

    Pure form of :func:`derive_light_platforms_by_schema` that takes
    the components directory as an argument so unit tests can hand it
    a fixture tree.
    """
    out: dict[str, set[str]] = {name: set() for name in LIGHT_SCHEMA_NAMES}
    # ``components/<platform>/light.py`` is the single-platform shape;
    # ``components/<platform>/light/__init__.py`` is the multi-platform
    # shape (binary, hbridge, lvgl, m5stack_8angle, status_led, tuya).
    candidates: list[tuple[Path, str]] = [
        (path, f"light.{path.parent.name}") for path in components_dir.glob("*/light.py")
    ]
    candidates.extend(
        (path, f"light.{path.parent.parent.name}")
        for path in components_dir.glob("*/light/__init__.py")
    )
    for path, platform_id in candidates:
        schema = resolve_schema_ref(path, components_dir, set())
        if schema is not None:
            out[schema].add(platform_id)
    return {k: frozenset(v) for k, v in out.items()}


@cache
def derive_light_platforms_by_schema() -> dict[str, frozenset[str]]:
    """
    Map each abstract light schema to the platform ids that use it.

    Empty when esphome isn't importable; an empty applies_to is
    rendered as "no restriction" on the frontend.
    """
    try:
        import esphome
    except ImportError:
        _LOG.warning("esphome not importable; light effects applies_to will be unrestricted")
        return {}
    components_dir = Path(esphome.__file__).resolve().parent / "components"
    if not components_dir.is_dir():
        _LOG.warning(
            "esphome components directory missing at %s; "
            "light effects applies_to will be unrestricted",
            components_dir,
        )
        return {}
    return derive_light_platforms_from_dir(components_dir)


def resolve_light_effects_applies_to(
    effect_name: str,
    schema_dir: Path,
) -> list[str]:
    """Return the canonical light-platform ids that accept *effect_name*."""
    light_json = Path(schema_dir / "light.json")
    try:
        with light_json.open() as f:
            light_schema = json.load(f)
    except FileNotFoundError:
        # No light schema → no platforms to scope against. The catalog
        # entry still ships with applies_to=[] which is rendered as "no
        # restriction" on the frontend.
        _LOG.warning("Light schema missing at %s; applies_to=[]", light_json)
        return []
    # JSONDecodeError deliberately propagates: it indicates a real
    # upstream schema bug or a partially-written sync and should fail
    # the run loudly rather than ship a broken catalog.
    schemas = (light_schema.get("light") or {}).get("schemas") or {}
    platforms_by_schema = derive_light_platforms_by_schema()
    applies: set[str] = set()
    for schema_name, platforms in platforms_by_schema.items():
        schema_body = schemas.get(schema_name) or {}
        cv = (schema_body.get("schema") or {}).get("config_vars") or {}
        effects_filter = (cv.get("effects") or {}).get("filter") or []
        if effect_name in effects_filter:
            applies.update(platforms)
    return sorted(applies)
