"""
Inventory guard: every ``*_bulk`` WS command has a registered single twin.

Per-verb behavioral parity lives next to each controller's suite
(``tests/controllers/firmware/test_single_bulk_parity.py``,
``tests/controllers/devices/test_single_bulk_parity.py``).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

import esphome_device_builder.controllers as controllers_pkg


def _registered_commands() -> set[str]:
    """Collect every ``@api_command`` name declared under ``controllers/``."""
    commands: set[str] = set()
    for modinfo in pkgutil.walk_packages(controllers_pkg.__path__, controllers_pkg.__name__ + "."):
        module = importlib.import_module(modinfo.name)
        for obj in vars(module).values():
            command = getattr(obj, "_api_command", None)
            if isinstance(command, str):
                commands.add(command)
            if not (inspect.isclass(obj) and obj.__module__ == module.__name__):
                continue
            for name in dir(obj):
                attr = inspect.getattr_static(obj, name, None)
                command = getattr(attr, "_api_command", None)
                if isinstance(command, str):
                    commands.add(command)
    return commands


def test_every_bulk_command_has_a_single_twin() -> None:
    commands = _registered_commands()
    bulk_commands = {command for command in commands if command.endswith("_bulk")}
    # Canary that the walk found the surface at all — an import-path
    # regression must fail loudly, not report an empty-set pass.
    assert {"firmware/install_bulk", "devices/delete_bulk"} <= bulk_commands
    missing = {
        command for command in bulk_commands if command.removesuffix("_bulk") not in commands
    }
    assert not missing, f"bulk commands without a single twin: {sorted(missing)}"
