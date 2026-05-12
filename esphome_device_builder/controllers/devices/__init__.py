"""
Devices controller package — public surface.

Re-exports ``DevicesController`` so existing
``from .controllers.devices import DevicesController`` imports keep
resolving after the subpackage split. Submodules:

- ``constants`` — module-level regexes and other static config.
- ``helpers`` — pure free functions (``_remove_device_sidecars``,
  ``_apply_featured_presets``, ``_build_address_cache_args``,
  ``friendly_name_slugify`` re-export).
- ``firmware_sync`` — firmware-job → device-state sync helpers
  (post-flash hash refresh, deployed-hash sync, StorageJSON
  version write).
- ``storage_regen`` — background ``--only-generate`` scheduler
  + the disk-stamp guard that keeps it from looping on a
  broken YAML.
- ``reachability`` — per-device reachability streaming + the
  on-subscription mDNS A-record refresh loop.
- ``controller`` — ``DevicesController`` itself + the scan / state
  / MQTT bridge. Hosts thin bound-method delegates that the
  WS dispatch and the per-concern submodules call into.
"""

from __future__ import annotations

from .controller import DevicesController
from .helpers import friendly_name_slugify

__all__ = ["DevicesController", "friendly_name_slugify"]
