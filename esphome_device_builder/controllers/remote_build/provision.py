"""Receiver-side auto-provisioning capability seam."""

from __future__ import annotations


def receiver_supports_auto_provision() -> bool:
    """Whether this receiver can build a version-mismatched offloader's esphome.

    Advertised on every peer-link session-open so the offloader only
    routes a version-mismatched compile here when the receiver can
    provision the matching esphome. ``False`` until the provisioner lands.
    """
    return False
