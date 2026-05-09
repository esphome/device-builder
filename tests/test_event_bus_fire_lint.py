"""
Lint: every ``bus.fire(EventType.X, payload)`` site uses a typed payload.

The TypedDict-per-event convention (CLAUDE.md "Event payloads use
TypedDict, not dataclass") is enforced at the construction site
by mypy:

- ``DataT(field=value)`` — TypedDict-call syntax; mypy validates
  the kwargs against the declared fields.
- ``payload: SomeData = {...}; bus.fire(...)`` — annotated local
  binding; mypy validates the literal against the annotation.

The hole mypy *can't* close: ``bus.fire`` is generic on
``DataT`` with no upper bound, so a bare dict literal at the
fire site (``bus.fire(EventType.X, {"k": "v"})``) types as
``dict[str, str]`` and silently bypasses the convention. A new
``EventType`` member could land without any TypedDict and mypy
would never object.

This file walks every Python source file under
``esphome_device_builder/`` with :mod:`ast` and asserts no
``bus.fire(EventType.X, {...})`` site exists. Migrating a fire
site away from a typed payload would have to land here too,
making the regression visible at review time and in CI.

Listener sites are intentionally not linted — there's a single
``bus.add_listener(EventType.X, _handler)`` call site outside
the bus itself today (``DevicesController._on_firmware_job_completed``)
and its handler is typed ``Event[JobLifecycleData]``. A
multi-listener-site lint can land alongside the next
``add_listener`` call that needs guarding.
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).parent.parent / "esphome_device_builder"


def _is_event_type_attr(node: ast.expr) -> bool:
    """Return True when *node* is an ``EventType.<NAME>`` attribute access."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "EventType"
    )


def _resolve_fire_args(node: ast.Call) -> tuple[ast.expr, ast.expr] | None:
    """Resolve ``(event_type, data)`` from a ``.fire(...)`` call.

    Handles both positional (``fire(EventType.X, payload)``) and
    keyword (``fire(event_type=EventType.X, data=payload)``)
    forms, plus mixed (``fire(EventType.X, data=payload)``).
    Returns ``None`` when either argument is missing — the lint
    skips those calls entirely; mypy already rejects an ``.fire``
    call with the wrong arity.
    """
    event_type: ast.expr | None = None
    data: ast.expr | None = None

    if len(node.args) >= 1:
        event_type = node.args[0]
    if len(node.args) >= 2:
        data = node.args[1]

    for kw in node.keywords:
        if kw.arg == "event_type":
            event_type = kw.value
        elif kw.arg == "data":
            data = kw.value

    if event_type is None or data is None:
        return None
    return event_type, data


def _find_raw_dict_fire_sites(src: str, path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, EventType.X)`` for every raw-dict-literal fire site.

    Matches ``<expr>.fire(EventType.X, {<dict literal>}, ...)``
    anywhere in *src* — covers ``bus.fire``, ``self._db.bus.fire``,
    and any other attribute chain ending in ``.fire``. Resolves
    both positional and keyword forms (``fire(EventType.X, {})``
    and ``fire(event_type=EventType.X, data={})`` are both
    flagged). The event_type argument has to be a direct
    ``EventType.X`` reference; aliased / dynamic event values
    (``event = EventType.JOB_COMPLETED if success else
    EventType.JOB_FAILED``) bypass this lint by design — those
    sites already pass typed local payloads, and the call-graph
    analysis to chase a ``Name`` back to its EventType assignment
    is more brittle than it's worth.
    """
    tree = ast.parse(src, filename=str(path))
    bad: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "fire":
            continue
        resolved = _resolve_fire_args(node)
        if resolved is None:
            continue
        event_type, data = resolved
        if not _is_event_type_attr(event_type):
            continue
        if isinstance(data, ast.Dict):
            assert isinstance(event_type, ast.Attribute)  # type narrowing for mypy
            bad.append((node.lineno, f"EventType.{event_type.attr}"))
    return bad


def test_no_raw_dict_payloads_at_bus_fire_sites() -> None:
    """Every ``bus.fire(EventType.X, payload)`` site uses a typed payload.

    See module docstring for why mypy can't close this gap on
    its own. Failing this test means a fire site landed with a
    bare ``{...}`` dict literal — convert it to TypedDict-call
    syntax (``SomeData(field=value)``) or an annotated local
    (``payload: SomeData = {...}; bus.fire(..., payload)``).
    """
    offenders: list[str] = []
    for path in _PACKAGE_ROOT.rglob("*.py"):
        # Force UTF-8 — Windows CI's default ``cp1252`` locale chokes
        # on the em-dashes / smart quotes used throughout the
        # codebase's docstrings and comments.
        src = path.read_text(encoding="utf-8")
        for lineno, event_name in _find_raw_dict_fire_sites(src, path):
            rel = path.relative_to(_PACKAGE_ROOT.parent)
            offenders.append(f"{rel}:{lineno}  bus.fire({event_name}, {{...}})")

    assert not offenders, (
        "Bare dict literal at bus.fire site bypasses the per-event TypedDict "
        "convention. Convert each to ``SomeData(...)`` or a typed local:\n  "
        + "\n  ".join(offenders)
    )
