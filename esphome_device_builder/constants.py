"""Constants for the ESPHome Device Builder."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("esphome-device-builder")
except PackageNotFoundError:
    # Source checkout without an editable install — keep imports
    # working with a placeholder. Real builds get the version
    # stamped into ``pyproject.toml`` by the release workflow.
    __version__ = "0.0.0"

DEFAULT_PORT = 6052
DEFAULT_HOST = "0.0.0.0"

# Trusted TCP site for HA Ingress. Bound only when ``--ha-addon`` is set,
# on the supervisor's docker bridge network, and bypasses the password
# gate (the supervisor has already authenticated the request).
DEFAULT_INGRESS_PORT = 8099
