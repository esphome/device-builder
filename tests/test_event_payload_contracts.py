"""
Belt-and-suspenders for the alias that pins TypedDict ↔ dict-literal builders.

mypy is the primary gate: when a function's return annotation is
a TypedDict (directly or via an alias like ``ReachabilitySnapshot
= DeviceReachabilityData``), mypy emits
``[typeddict-unknown-key]`` / ``[typeddict-item]`` if the
``return {...}`` literal in the body drifts from the declared
fields. The hard mypy gate from #493 blocks any PR that drifts.

The narrow gap mypy *can't* close on its own: the alias annotation
is load-bearing. If a future contributor regresses the return
type — e.g. swaps ``-> ReachabilitySnapshot`` back to
``-> dict[str, object]`` for any reason — mypy stops checking
and the wire shape can silently drift from the TypedDict.
Subscribers that type ``event.data["new_field"]`` would
mypy-pass and ``KeyError`` at runtime.

This file walks the builder function's source via :mod:`ast` and
asserts the dict literal's keys equal the TypedDict's
``__annotations__`` *regardless of the return annotation*. So the
test fails the moment the alias regresses, even though mypy
would still be green.

Today there's one entry: ``ReachabilityTracker.snapshot`` ↔
``DeviceReachabilityData``. New entries land here as new
dict-literal builders are introduced — the (alternative) shape
where subscribers' typed access depends on a function's return
annotation matching a TypedDict declared elsewhere.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

import pytest

from esphome_device_builder.controllers._reachability_tracker import (
    ReachabilityTracker,
)
from esphome_device_builder.models import DeviceReachabilityData


class _OutermostReturnDictFinder(ast.NodeVisitor):
    """
    Collect every ``return {...}`` whose ``Dict`` is at the function's outer scope.

    Skips into nested ``def`` / ``async def`` / ``lambda`` /
    ``class`` bodies so a helper closure inside the function
    (e.g. ``snapshot``'s ``_ago``) can't accidentally shadow the
    outer ``return {...}`` we actually care about. Walks
    everything else (``if`` / ``try`` / ``with`` branches, etc.)
    so a return nested in control flow still counts.

    Visit results land in :attr:`dict_returns` in source order;
    callers typically assert exactly one entry.
    """

    def __init__(self) -> None:
        self.dict_returns: list[ast.Dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Don't recurse — nested function bodies aren't the outer return.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Dict):
            self.dict_returns.append(node.value)
        self.generic_visit(node)


def _outermost_return_dict_keys(func: Callable[..., Any]) -> set[str]:
    """Return the keys of *func*'s outermost ``return {...}`` literal.

    Skips into nested function / class / lambda bodies so a
    helper closure that happens to return a dict can't shadow the
    outer wire-shape return. Asserts exactly one outer-scope
    dict-literal return — a builder with multiple wire-shape
    returns needs a per-branch test, not this one.
    """
    src = inspect.cleandoc("\n" + inspect.getsource(func))
    tree = ast.parse(src)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef | ast.AsyncFunctionDef):
        msg = f"Expected a single function definition in source of {func!r}"
        raise AssertionError(msg)
    func_def = tree.body[0]

    finder = _OutermostReturnDictFinder()
    # Visit the body's children (not the function def itself, which
    # would re-enter ``visit_FunctionDef`` and stop).
    for stmt in func_def.body:
        finder.visit(stmt)

    if len(finder.dict_returns) != 1:
        msg = (
            f"Expected exactly one outer-scope ``return {{...}}`` literal "
            f"in {func!r}, found {len(finder.dict_returns)}."
        )
        raise AssertionError(msg)

    keys: set[str] = set()
    for key_node in finder.dict_returns[0].keys:
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            msg = (
                f"Non-string-literal key in {func!r} dict literal — the "
                "drift check assumes a flat ``{'k': v, ...}`` dict."
            )
            raise AssertionError(msg)  # noqa: TRY004 — contract drift in the test fixture, not a runtime type error
        keys.add(key_node.value)
    return keys


# Each entry: (TypedDict class, builder function whose body
# returns a dict literal aliased to the TypedDict). The contract
# test walks *builder*'s source via :mod:`ast` and asserts the
# literal's keys equal the TypedDict's declared
# ``__annotations__``.
#
# Add an entry here when a new builder lands in the same shape —
# i.e. a function with a TypedDict-aliased return annotation
# whose body builds the wire shape via a single
# ``return {...}`` literal. Fire-site constructions
# (``payload: T = {...}`` / ``T(field=...)``) don't belong here;
# mypy enforces those at the call site.
_DICT_LITERAL_BUILDERS: list[tuple[type, Callable[..., Any]]] = [
    (DeviceReachabilityData, ReachabilityTracker.snapshot),
]


@pytest.mark.parametrize(
    ("typed_dict", "builder"),
    _DICT_LITERAL_BUILDERS,
    ids=[td.__name__ for td, _ in _DICT_LITERAL_BUILDERS],
)
def test_dict_literal_builder_keys_match_typeddict(
    typed_dict: type,
    builder: Callable[..., Any],
) -> None:
    """The builder's literal keys equal the TypedDict's declared fields.

    See the module docstring for the alias-regression scenario
    this guards against. mypy already enforces drift in either
    direction *while the alias is in place*; this test fails
    even if the alias is dropped or loosened to ``dict[str,
    object]``, because the ``ast`` walk doesn't depend on the
    return annotation.
    """
    literal_keys = _outermost_return_dict_keys(builder)
    declared_keys = set(get_type_hints(typed_dict).keys())

    extra = literal_keys - declared_keys
    missing = declared_keys - literal_keys
    assert not extra, (
        f"{builder.__qualname__} dict literal has keys not declared in "
        f"{typed_dict.__name__}: {extra}"
    )
    assert not missing, (
        f"{typed_dict.__name__} declares keys missing from {builder.__qualname__}: {missing}"
    )
