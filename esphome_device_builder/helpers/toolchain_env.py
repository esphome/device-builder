"""Drop inherited ESP-IDF env overrides so builds use the managed toolchain."""

from __future__ import annotations

import logging
import os

_LOGGER = logging.getLogger(__name__)

# esphome's native ESP-IDF toolchain uses IDF_PATH verbatim when present and skips its managed
# install entirely, so a machine-level leftover (commonly pointing into ~/.platformio from an
# old PlatformIO-era setup) fails every esp32 build before it starts.
_INHERITED_IDF_VARS = ("IDF_PATH", "IDF_TOOLS_PATH")


def drop_inherited_idf_env() -> None:
    """Remove inherited ``IDF_PATH`` / ``IDF_TOOLS_PATH`` from the process environment."""
    for var in _INHERITED_IDF_VARS:
        if (value := os.environ.pop(var, None)) is not None:
            _LOGGER.warning(
                "Ignoring inherited %s=%s; ESP-IDF is managed by the device builder", var, value
            )
