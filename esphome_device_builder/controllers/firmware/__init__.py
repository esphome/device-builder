"""
Firmware controller package — public surface.

Re-exports ``FirmwareController`` so existing
``from .controllers.firmware import FirmwareController`` imports
keep resolving after the subpackage split. Submodules:

- ``constants`` — error patterns, history caps, output cap tunables.
- ``helpers`` — pure free functions (``_find_esphome_cmd`` is the
  only one called from outside the firmware subpackage).
- ``controller`` — ``FirmwareController`` itself + the queue runner.
"""

from __future__ import annotations

from .controller import FirmwareController

__all__ = ["FirmwareController"]
