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

# Receiver-side TCP listener for the remote-build feature (issue #106).
# Different port from the dashboard's own HTTP listener so a
# misconfigured offloader can't accidentally hit the dashboard auth
# surface, and so paired peers can resolve "the remote-build URL"
# off the mDNS SRV record without ambiguity.
#
# Transport changed across phases (the port number didn't): phases
# 3b1-3c shipped HTTPS + bearer auth; phase 4a-r1 part 4 swaps the
# bind to plain TCP serving a Noise XX WebSocket at
# ``/remote-build/peer-link`` (Noise provides confidentiality +
# mutual auth + forward secrecy at the application layer, so no
# SSLContext to manage). The constant survives unchanged because
# pre-release has no installed base of port-config to migrate.
DEFAULT_REMOTE_BUILD_PORT = 6055
