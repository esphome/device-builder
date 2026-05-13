"""Automations controller package — public surface.

Re-exports :class:`AutomationsController` so existing
``from ..controllers.automations import AutomationsController``
imports keep resolving after the subpackage split. Submodules:

- ``catalog`` — loads and caches ``definitions/automations.json``;
  exposes the three list catalogues and the per-domain index used
  by ``get_available``.
- ``parsing`` — ruamel-round-trip YAML → :class:`ParsedAutomation`
  list. Handles top-level ``script:`` / ``interval:``, inline
  ``on_*:`` under configured components, the device-level
  ``esphome.on_boot`` / ``on_loop`` / ``on_shutdown`` family,
  recursive action / condition trees, and the lambda sentinel.
- ``writing`` — :class:`AutomationTree` → YAML + splice diff. Reuses
  ``helpers.yaml._splice_into_domain_block`` for top-level blocks
  and a new ``upsert_inline_handler`` for inline ``on_*:`` / light
  ``effects:`` siblings.
- ``controller`` — :class:`AutomationsController` itself + the
  eight WS commands the frontend exchanges with.
"""

from __future__ import annotations

from .controller import AutomationsController

__all__ = ["AutomationsController"]
