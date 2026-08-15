"""Static guard: no ``helpers.*`` module imports ``controllers`` at module scope."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import esphome_device_builder.helpers as helpers_pkg

_PACKAGE = "esphome_device_builder"
_FORBIDDEN_ROOT = f"{_PACKAGE}.controllers"


def test_helpers_never_import_controllers_at_module_scope() -> None:
    """Function-local imports stay legal; import-time edges into ``controllers`` don't."""
    root = Path(helpers_pkg.__file__).parent
    files = sorted(root.rglob("*.py"))
    assert any(py.name == "dashboard_identity.py" for py in files)
    violations: list[str] = []
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"))
        package_parts = _package_parts(py, root)
        for node in _module_scope_imports(tree):
            for target in _resolved_targets(node, package_parts):
                if target == _FORBIDDEN_ROOT or target.startswith(f"{_FORBIDDEN_ROOT}."):
                    violations.append(f"{py.relative_to(root)}:{node.lineno}: imports {target}")
                    break
    assert not violations, "helpers module-scope imports of controllers:\n" + "\n".join(violations)


def _package_parts(py: Path, root: Path) -> list[str]:
    """Dotted-package parts for *py*, matching its runtime ``__package__``."""
    # Dropping the last part handles both shapes: a module sheds its own
    # name; a package ``__init__`` sheds the ``__init__`` stem.
    parts = [_PACKAGE, *py.relative_to(root.parent).with_suffix("").parts]
    return parts[:-1]


def _module_scope_imports(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """Yield imports that execute at import time; ``if TYPE_CHECKING:`` bodies excluded."""
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        elif _is_type_checking_guard(node):
            stack.extend(node.orelse)
        else:
            stack.extend(
                child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)
            )


def _is_type_checking_guard(node: ast.stmt) -> bool:
    """Report whether *node* is ``if TYPE_CHECKING:`` (bare or ``typing.``-qualified)."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _resolved_targets(node: ast.Import | ast.ImportFrom, package_parts: list[str]) -> Iterator[str]:
    """Absolute dotted names *node* binds, relative levels resolved."""
    if isinstance(node, ast.Import):
        yield from (alias.name for alias in node.names)
        return
    anchor = [] if node.level == 0 else package_parts[: len(package_parts) - (node.level - 1)]
    base = ".".join([*anchor, *(node.module.split(".") if node.module else [])])
    yield base
    # ``from <pkg> import controllers``-style: the submodule rides the alias.
    yield from (f"{base}.{alias.name}" for alias in node.names)
