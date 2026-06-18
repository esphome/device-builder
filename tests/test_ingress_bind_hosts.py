"""Coverage for ``DashboardSettings.ingress_bind_hosts``.

The trusted HA Ingress site bypasses auth, so its bind targets are
security-relevant: the default must never include a LAN interface (a
host-network add-on would otherwise serve the no-auth dashboard on the LAN).
"""

from __future__ import annotations

from esphome_device_builder.controllers.config import DashboardSettings


def test_default_binds_loopback_and_supervisor_gateway_only() -> None:
    """No ``--ingress-host`` → loopback + supervisor gateway, never 0.0.0.0."""
    settings = DashboardSettings()
    settings.ingress_host = ""

    assert settings.ingress_bind_hosts == ["127.0.0.1", "172.30.32.1"]


def test_explicit_ingress_host_overrides_the_default() -> None:
    """An explicit IP literal is used verbatim (operator override)."""
    settings = DashboardSettings()
    settings.ingress_host = "10.0.0.5"

    assert settings.ingress_bind_hosts == ["10.0.0.5"]
