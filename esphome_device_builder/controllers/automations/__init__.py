"""
Automations controller package — public surface.

Submodules:

- ``catalog`` — loads ``definitions/automations.json`` and exposes
  the four catalog lists.
- ``parsing`` — ruamel YAML → :class:`ParsedAutomation` list.
- ``emitter`` — :class:`AutomationTree` → ruamel structures.
- ``writing`` — splice the emitted YAML into the device YAML,
  returning the :class:`YamlDiff` the frontend applies.
- ``controller`` — :class:`AutomationsController` + the eight WS
  commands.
"""

from __future__ import annotations

from .controller import AutomationsController

__all__ = ["AutomationsController"]
