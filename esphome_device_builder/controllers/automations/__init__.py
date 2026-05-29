"""
Automations controller package — public surface.

Submodules:

- ``catalog`` — loads the slim ``definitions/automations.index.json``
  and exposes the five catalog lists (triggers / actions /
  conditions / light_effects / filters); full bodies hydrate
  lazily through per-type :class:`LazyBodyStore` caches.
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
