"""Receiver-side auto-provisioning capability seam."""

from __future__ import annotations


def receiver_supports_auto_provision() -> bool:
    """Whether this receiver can build a version-mismatched offloader's esphome.

    Advertised on every peer-link session-open so the offloader only
    routes a version-mismatched compile here when the receiver can
    provision the matching esphome. On by default now the
    :class:`~.env_provisioner.EnvProvisioner` engine exists.
    """
    return True


def receiver_supports_reset_build_env() -> bool:
    """Whether this receiver accepts remote ``reset_build_env`` requests.

    Advertised on every peer-link session-open so the offloader's UI
    only offers the action against a new-enough receiver.
    """
    return True
