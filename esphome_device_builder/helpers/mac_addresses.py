"""Derive interface MAC addresses from a device's primary (broadcast) MAC.

The mDNS ``mac`` TXT record on ``_esphomelib._tcp.local.`` always
carries the device's *primary* MAC: on ESP32 / ESP32-S2 / ESP32-S3 /
ESP32-C3 / ESP32-C6 etc. that's the eFuse base MAC (also used as the
Wi-Fi STA MAC); on RP2040 / RP2350 there's only one MAC across
interfaces, and that's it.

ESP32-family devices that *also* enable Ethernet or Bluetooth derive
those interfaces' MACs from the same base via fixed offsets per
Espressif's allocation table[^1] — Ethernet is base + 3 to the last
octet, Bluetooth is base + 2. This module computes those derivations
so the dashboard can show every interface MAC the device owns,
without forcing the firmware to broadcast all of them.

[^1]: https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/misc_system_api.html#mac-address-allocation
"""

from __future__ import annotations

# Platform keys (``Device.target_platform``) that follow Espressif's
# 4-MAC offset scheme. Anything outside this set falls through the
# derivation paths as "no derived MACs" — explicit allowlist beats
# guessing on chips we haven't validated against the eFuse layout.
_ESP32_PLATFORMS: frozenset[str] = frozenset(
    {"esp32", "esp32s2", "esp32s3", "esp32c3", "esp32c6", "esp32h2", "esp32p4"}
)

# Platform keys that share a single MAC across every interface — the
# RP2040 / RP2350 family. When ethernet is enabled the ethernet MAC
# equals the primary; bluetooth on the Pico W routes through a
# separate radio chip with its own allocation scheme so we don't
# derive there.
_SINGLE_MAC_PLATFORMS: frozenset[str] = frozenset({"rp2040", "rp2350"})


def _has_ethernet(loaded_integrations: list[str]) -> bool:
    """Return whether the resolved YAML loads the ``ethernet`` component."""
    return "ethernet" in loaded_integrations


def _has_bluetooth(loaded_integrations: list[str]) -> bool:
    """Return whether any ``esp32_ble*`` / ``bluetooth_*`` integration is loaded.

    ESPHome's bluetooth support spans several integrations
    (``esp32_ble``, ``esp32_ble_tracker``, ``esp32_ble_server``,
    ``esp32_ble_beacon``, ``bluetooth_proxy``…). Match by prefix so a
    new bluetooth-related integration name added upstream Just Works
    without a parallel update here.
    """
    return any(name.startswith(("esp32_ble", "bluetooth_")) for name in loaded_integrations)


def _offset_last_octet(primary: str, offset: int) -> str:
    """Return *primary* with the last octet incremented by *offset* (mod 256).

    *primary* is the canonical ``XX:XX:XX:XX:XX:XX`` form
    :func:`controllers._device_state_monitor._normalize_mac` produces.
    The last two hex chars cover the trailing octet; we wrap modulo
    256 to mirror the ESP-IDF behaviour where the offset addition
    can roll a high-byte value (``0xFF`` + 3 → ``0x02``) without
    touching the upper octets.
    """
    last = int(primary[-2:], 16)
    return f"{primary[:-2]}{(last + offset) % 256:02X}"


def derive_interface_macs(
    primary: str,
    target_platform: str,
    loaded_integrations: list[str],
) -> tuple[str, str]:
    """
    Derive ``(ethernet_mac, bluetooth_mac)`` from the broadcast primary MAC.

    Empty primary or unknown platform → ``("", "")``. Each derived
    MAC is empty when the relevant integration isn't loaded — the
    drawer hides any row whose value is blank or equal to
    ``mac_address``, so a single-MAC platform like RP2040 with
    ethernet just renders one row.

    *primary* must be in the canonical ``XX:XX:XX:XX:XX:XX`` form
    that :func:`controllers._device_state_monitor._normalize_mac`
    produces; all output MACs match that shape so the wire
    surface is uniform.

    The derivation is deterministic and side-effect-free; we recompute
    on every primary-MAC observation rather than persisting the
    derived values, so a YAML edit that toggles bluetooth picks up
    the new derived MAC on the very next reload.
    """
    # 17 = ``XX:XX:XX:XX:XX:XX`` — six octets joined by five
    # colons. Validate hex on the trailing octet too: a corrupt /
    # hand-edited sidecar entry of the right length but bad chars
    # would otherwise raise ``ValueError`` deep inside
    # ``_offset_last_octet`` rather than returning empty here.
    if not primary or len(primary) != 17:
        return "", ""
    try:
        int(primary[-2:], 16)
    except ValueError:
        return "", ""

    has_ethernet = _has_ethernet(loaded_integrations)
    has_bluetooth = _has_bluetooth(loaded_integrations)

    if target_platform in _ESP32_PLATFORMS:
        ethernet = _offset_last_octet(primary, 3) if has_ethernet else ""
        bluetooth = _offset_last_octet(primary, 2) if has_bluetooth else ""
        return ethernet, bluetooth

    if target_platform in _SINGLE_MAC_PLATFORMS:
        # Ethernet on RP2040 / RP2350 reuses the primary MAC; we
        # surface it as a separate field so the frontend can decide
        # whether to render a redundant row (it doesn't — equality
        # with the primary is the hide signal). Bluetooth on the
        # Pico W lives on a CYW43439 radio chip with its own MAC
        # that we can't derive from the RP-side base.
        ethernet = primary if has_ethernet else ""
        return ethernet, ""

    return "", ""
