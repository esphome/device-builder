"""
Inventory guards: every ``*_bulk`` WS command keeps a single twin and its keyword surface.

Per-verb behavioral parity lives next to each controller's suite
(``tests/controllers/firmware/test_single_bulk_parity.py``,
``tests/controllers/devices/test_single_bulk_parity.py``).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable

import esphome_device_builder.controllers as controllers_pkg
from esphome_device_builder.helpers.api import collect_api_commands

# Keywords the single twin exposes that its bulk deliberately does not.
# firmware/install_bulk: fleet-wide installs stay scheduler-routed OTA
# app flashes; per-device overrides go through firmware/install.
# devices/set_labels_bulk: label_ids rides inside each ``updates`` row.
_INTENTIONAL_BULK_GAPS: dict[str, set[str]] = {
    "firmware/install_bulk": {"force_local", "bootloader"},
    "devices/set_labels_bulk": {"label_ids"},
}

# The per-item carrier differs by arity; strip it before comparing.
_SINGLE_CARRIERS = {"configuration"}
_BULK_CARRIERS = {"configurations", "updates"}


def _registered_handlers() -> dict[str, Callable]:
    """Collect every ``@api_command`` handler declared under ``controllers/``."""
    handlers: dict[str, Callable] = {}
    for modinfo in pkgutil.walk_packages(controllers_pkg.__path__, controllers_pkg.__name__ + "."):
        module = importlib.import_module(modinfo.name)
        for obj in vars(module).values():
            if inspect.isclass(obj) and obj.__module__ == module.__name__:
                handlers.update(collect_api_commands(obj))
    return handlers


def _keyword_parameters(func: Callable) -> set[str]:
    return {
        name
        for name, parameter in inspect.signature(func).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }


def test_every_bulk_command_has_a_single_twin() -> None:
    commands = set(_registered_handlers())
    bulk_commands = {command for command in commands if command.endswith("_bulk")}
    # Canary that the walk found the surface at all — an import-path
    # regression must fail loudly, not report an empty-set pass.
    assert {"firmware/install_bulk", "devices/delete_bulk"} <= bulk_commands
    missing = {
        command for command in bulk_commands if command.removesuffix("_bulk") not in commands
    }
    assert not missing, f"bulk commands without a single twin: {sorted(missing)}"


def test_every_bulk_command_carries_its_single_twin_keywords() -> None:
    """A keyword added to a single handler must reach its bulk twin or the gap allowlist."""
    handlers = _registered_handlers()
    bulk_names = sorted(name for name in handlers if name.endswith("_bulk"))
    assert bulk_names
    for bulk_name in bulk_names:
        single_keywords = (
            _keyword_parameters(handlers[bulk_name.removesuffix("_bulk")]) - _SINGLE_CARRIERS
        )
        bulk_keywords = _keyword_parameters(handlers[bulk_name]) - _BULK_CARRIERS
        missing = single_keywords - bulk_keywords - _INTENTIONAL_BULK_GAPS.get(bulk_name, set())
        assert not missing, (
            f"{bulk_name} is missing keywords its single twin has: {sorted(missing)}"
        )
