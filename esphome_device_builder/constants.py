"""Constants for the ESPHome Device Builder."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


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

# Shared credentials file in the config dir. It's not a buildable device
# config (no build dir / build_info.json) and is kept out of version
# history, so callers special-case it via ``is_secrets_file``.
SECRETS_FILENAME = "secrets.yaml"


def is_secrets_file(configuration: str | Path) -> bool:
    """Return True when *configuration* names the shared secrets.yaml (by basename)."""
    return Path(configuration).name == SECRETS_FILENAME


# Trusted TCP site for HA Ingress. Bound only when ``--ha-addon`` is set,
# and bypasses the password gate (the supervisor has already authenticated
# the request).
DEFAULT_INGRESS_PORT = 8099

# Gateway of the HA Supervisor's hassio docker bridge. Mirrors the supervisor's
# own hardcoded constant (``DOCKER_IPV4_NETWORK_MASK[1]`` of ``172.30.32.0/23``):
# for a host-network add-on the supervisor computes the ingress target from that
# constant, not the live network, so it always connects here — binding this
# address is matching the supervisor by construction, not assuming a docker
# default. If HA ever changes it, both move in lockstep.
HA_SUPERVISOR_NETWORK_GATEWAY = "172.30.32.1"

# The supervisor's own address on the hassio bridge
# (``DOCKER_IPV4_NETWORK_MASK[2]``). The ingress proxy connects from here, so
# it's the source IP the trusted ingress site sees for the browser-UI path.
HA_SUPERVISOR_IP = "172.30.32.2"

# Default bind targets for the trusted HA Ingress site: loopback plus the
# supervisor gateway only — NEVER all interfaces. On a host-network add-on
# (the ESPHome add-on runs host-network for mDNS) ``0.0.0.0`` would put the
# no-auth ingress site on the LAN. Loopback serves HA core's host-network
# ESPHome integration (it connects to ``127.0.0.1:<ingress_port>``); the
# gateway serves the supervisor's ingress proxy. An explicit ``--ingress-host``
# overrides this default.
HA_INGRESS_DEFAULT_BIND_HOSTS = ("127.0.0.1", HA_SUPERVISOR_NETWORK_GATEWAY)

# Receiver-side TCP listener for the remote-build feature (issue #106).
# Different port from the dashboard's own HTTP listener so a
# misconfigured offloader can't accidentally hit the dashboard auth
# surface, and so paired peers can resolve "the remote-build URL"
# off the mDNS SRV record without ambiguity.
#
# The bind serves a Noise XX WebSocket at ``/remote-build/peer-link``
# over plain TCP — Noise provides confidentiality + mutual auth +
# forward secrecy at the application layer, so there's no SSLContext
# to manage.
DEFAULT_REMOTE_BUILD_PORT = 6055


# Long-form pin keys describing a board GPIO. Any other key in a pin mapping
# names an I/O-expander provider whose value is the hub id. ``id`` is included
# because expander pin schemas share this base, so a channel carrying an ``id``
# must not have it taken for the provider key.
BOARD_PIN_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "number",
        "mode",
        "inverted",
        "allow_other_uses",
        "ignore_strapping_warning",
        "ignore_pin_validation_error",
        "drive_strength",
    }
)

# A board manifest's ``source.type`` written by the devices.esphome.io importer.
# Such boards are complete onboard configs; hand-curated manifests have no
# ``source`` block. Shared so the importer (writer) and the loader (reader)
# can't drift.
DEVICE_IMPORT_SOURCE_TYPE = "esphome-devices"
