"""
Devices controller package — public surface.

Re-exports ``DevicesController`` so existing
``from .controllers.devices import DevicesController`` imports keep
resolving after the subpackage split. Submodules:

- ``constants`` — module-level regexes and other static config.
- ``helpers`` — pure free functions (``_remove_device_sidecars``,
  ``_apply_featured_presets``, ``_build_address_cache_args``,
  ``friendly_name_slugify`` re-export).
- ``controller`` — ``DevicesController`` itself + the scan / state
  / MQTT bridge.
"""

from __future__ import annotations

from .controller import DevicesController
from .helpers import friendly_name_slugify

__all__ = ["DevicesController", "friendly_name_slugify"]
