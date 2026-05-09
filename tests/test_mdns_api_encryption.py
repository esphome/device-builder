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

import pytest

from esphome_device_builder.models import Device, DeviceState, EventType

from .conftest import make_devices_controller_with_bus, make_state_monitor_with_callbacks


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


def test_apply_api_encryption_first_observation_fires_callback() -> None:
    """A first encryption value reaches the controller."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256") is True
    assert callbacks.calls == [
        ("on_api_encryption_change", "kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    ]


def test_apply_api_encryption_empty_string_is_a_real_observation() -> None:
    """Empty string ("TXT absent → plaintext confirmed") fires the callback.

    Distinct from "never observed" — the controller relies on the
    callback firing at least once to know we have ground truth from
    mDNS at all.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_api_encryption("kitchen", "") is True
    assert callbacks.calls == [("on_api_encryption_change", "kitchen", "")]


def test_apply_api_encryption_dedupes_same_value() -> None:
    """Repeated identical observations don't churn the controller."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    assert callbacks.calls == [
        ("on_api_encryption_change", "kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    ]


def test_apply_api_encryption_fires_on_change() -> None:
    """Encrypted → plaintext (or vice versa) re-fires the callback."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_api_encryption("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    monitor.apply_api_encryption("kitchen", "")
    assert callbacks.calls == [
        ("on_api_encryption_change", "kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256"),
        ("on_api_encryption_change", "kitchen", ""),
    ]


def test_apply_api_encryption_unknown_device_is_ignored() -> None:
    """A name that doesn't match any configured device drops the call.

    Discovered-but-not-imported devices fire mDNS too; they shouldn't
    trigger a DEVICE_UPDATED on a configured device that happens to
    share a similar name slot.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_api_encryption("not-a-device", "anything") is False
    assert callbacks.calls == []


def test_apply_api_encryption_dedupes_repeated_empty() -> None:
    """The empty-string state is dedup'd just like a non-empty one."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_api_encryption("kitchen", "")
    monitor.apply_api_encryption("kitchen", "")
    assert callbacks.calls == [("on_api_encryption_change", "kitchen", "")]


# ----------------------------------------------------------------------
# DevicesController._on_api_encryption_change
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_api_encryption_change_updates_device_and_fires_event() -> None:
    """Callback writes the value onto the in-memory device + fires DEVICE_UPDATED."""
    device = _device(api_encryption_active=None)
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert device.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


@pytest.mark.asyncio
async def test_on_api_encryption_change_records_empty_string() -> None:
    """Empty string flips ``None`` → ``""`` and fires the event.

    The transition from "never seen" to "seen plaintext" is itself a
    meaningful state change and the dashboard's lock indicator depends
    on observing it (None → "" makes the four-state classifier flip
    from ``active`` to ``mismatch``/``pending`` when paired with a
    plaintext device).
    """
    device = _device(api_encryption_active=None)
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "")

    assert device.api_encryption_active == ""
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_on_api_encryption_change_skips_when_same() -> None:
    """No-op when the in-memory device already has the announced value AND ``api_encrypted`` agrees.

    Both halves matter: the YAML signal (``api_encrypted=True``)
    and the wire signal (truthy ``api_encryption_active``) have
    to be in agreement before we suppress the bus event. Without
    the second half, a device whose YAML pass missed the
    encryption (issue #437) but whose wire signal already
    reported the cipher would never get its ``api_encrypted``
    flag promoted on subsequent identical broadcasts — the
    handler would short-circuit before the promotion ran. The
    next test pins the promote-on-mismatch path that requires
    falling through this check.
    """
    device = _device(
        api_encrypted=True,
        api_encryption_active="Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    )
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert captured == []


@pytest.mark.asyncio
async def test_on_api_encryption_change_promotes_api_encrypted_when_yaml_missed_it() -> None:
    """Truthy mDNS broadcast flips ``api_encrypted=True`` when YAML missed it.

    Issue #437: a config that wires encryption via ESPHome's
    Jinja-templated packages leaves the dashboard's YAML pass
    with ``api_encrypted=False`` because ``yaml_util.load_yaml``
    doesn't render Jinja. The live mDNS broadcast carries the
    cipher because the firmware really IS running encryption —
    promote ``api_encrypted`` so non-frontend consumers
    (HA integration, table-row menu, "Show API key" gate) see
    the truth.
    """
    device = _device(api_encrypted=False, api_encryption_active=None)
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert device.api_encrypted is True
    assert device.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_on_api_encryption_change_promotion_fires_even_when_active_unchanged() -> None:
    """Repeat truthy broadcast still promotes ``api_encrypted`` if YAML scan reset it.

    The atomic-save scanner pattern (see
    ``DeviceScanner._set_device``) rebuilds devices on YAML
    edits. If a YAML edit retriggers the scan and the YAML pass
    still misses the encryption (the Jinja blind spot is
    persistent across scans), ``api_encrypted`` resets to
    False even though ``api_encryption_active`` is still the
    cipher string. The next mDNS broadcast — which carries the
    same cipher — must still re-promote, which means the
    "skip when same" short-circuit can't apply when the YAML
    side disagrees with the wire side.
    """
    device = _device(
        api_encrypted=False,
        api_encryption_active="Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    )
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")

    assert device.api_encrypted is True
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_on_api_encryption_change_empty_does_not_clear_api_encrypted() -> None:
    """Wire-confirmed-plaintext doesn't *demote* a YAML-claimed encryption.

    ``api_encryption_active = ""`` is the "TXT seen, key absent
    → device confirmed plaintext" tri-state signal. When the
    YAML says encrypted but the wire says plaintext, the right
    state is "mismatch" / "pending" (the user hasn't reflashed
    yet), not "demote ``api_encrypted`` to False." The frontend
    state machine already encodes that distinction; the
    backend must not flatten it by clearing the flag.
    """
    device = _device(
        api_encrypted=True,
        api_encryption_active="Noise_NNpsk0_25519_ChaChaPoly_SHA256",
    )
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_api_encryption_change("kitchen", "")

    # ``api_encryption_active`` updated to the new (empty) value;
    # ``api_encrypted`` stayed truthy (state machine handles the
    # mismatch vs pending distinction from there).
    assert device.api_encryption_active == ""
    assert device.api_encrypted is True
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_on_api_encryption_change_unknown_device_is_noop() -> None:
    """A stray callback for a name we don't track must not raise or fire."""
    controller, captured = make_devices_controller_with_bus([])

    controller._on_api_encryption_change("ghost", "anything")

    assert captured == []
