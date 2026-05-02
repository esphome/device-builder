"""Tests for mDNS-driven API encryption observation.

The ``_esphomelib._tcp`` service announcement carries an
``api_encryption`` TXT record (e.g.
``Noise_NNpsk0_25519_ChaChaPoly_SHA256``) when the device's API is
running encryption, and omits it when the device is running plaintext.
The dashboard reads this through the monitor → controller pipeline so
the four-state lock indicator can tell active / pending-flash /
mismatch / plaintext apart.

Three states matter for the apply path:
- "never seen" — the callback never fires; the controller leaves
  ``api_encryption_active`` at ``None`` and the UI trusts the YAML.
- "" — mDNS seen, TXT absent. Device is broadcasting plaintext.
- non-empty — mDNS seen, TXT present. Encryption confirmed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, DeviceState


def _device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
        "address": "kitchen.local",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


def _monitor(devices: list[Device]) -> tuple[DeviceStateMonitor, MagicMock]:
    on_enc = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_api_encryption_change=on_enc,
    )
    return monitor, on_enc


def test_apply_api_encryption_first_observation_fires_callback() -> None:
    """A first encryption value reaches the controller."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256") is True
    cb.assert_called_once_with("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")


def test_apply_api_encryption_empty_string_is_a_real_observation() -> None:
    """Empty string ("TXT absent → plaintext confirmed") fires the callback.

    Distinct from "never observed" — the controller relies on the
    callback firing at least once to know we have ground truth from
    mDNS at all.
    """
    monitor, cb = _monitor([_device()])
    assert monitor.apply_api_encryption("kitchen", "") is True
    cb.assert_called_once_with("kitchen", "")


def test_apply_api_encryption_dedupes_same_value() -> None:
    """Repeated identical observations don't churn the controller."""
    monitor, cb = _monitor([_device()])
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    cb.assert_called_once()


def test_apply_api_encryption_fires_on_change() -> None:
    """Encrypted → plaintext (or vice versa) re-fires the callback."""
    monitor, cb = _monitor([_device()])
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    monitor.apply_api_encryption("kitchen", "")
    assert cb.call_count == 2
    assert cb.call_args_list[1].args == ("kitchen", "")


def test_apply_api_encryption_unknown_device_is_ignored() -> None:
    """A name that doesn't match any configured device drops the call.

    Discovered-but-not-imported devices fire mDNS too; they shouldn't
    trigger a DEVICE_UPDATED on a configured device that happens to
    share a similar name slot.
    """
    monitor, cb = _monitor([_device()])
    assert monitor.apply_api_encryption("not-a-device", "anything") is False
    cb.assert_not_called()


def test_apply_api_encryption_dedupes_repeated_empty() -> None:
    """The empty-string state is dedup'd just like a non-empty one."""
    monitor, cb = _monitor([_device()])
    monitor.apply_api_encryption("kitchen", "")
    monitor.apply_api_encryption("kitchen", "")
    cb.assert_called_once()
