"""Constants for the ESPHome Device Builder."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _resolve_version() -> str:
    """
    Read the installed package version from wheel metadata.

    Real builds get the version stamped into ``pyproject.toml`` by the
    release workflow, which propagates to the installed distribution
    metadata. Source checkouts without an editable install fall back
    to ``0.0.0`` so imports keep working.
    """
    try:
        return version("esphome-device-builder")
    except PackageNotFoundError:
        return "0.0.0"


__version__ = _resolve_version()

DEFAULT_PORT = 6052
DEFAULT_HOST = "0.0.0.0"

# Trusted TCP site for HA Ingress. Bound only when ``--ha-addon`` is set,
# on the supervisor's docker bridge network, and bypasses the password
# gate (the supervisor has already authenticated the request).
DEFAULT_INGRESS_PORT = 8099
