"""Tests for the unusable-address filters in ``helpers.ip``."""

from __future__ import annotations

from esphome_device_builder.helpers.ip import (
    drop_unusable_addresses,
    is_unusable_address,
    is_usable_ip,
)


def test_is_unusable_address() -> None:
    assert is_unusable_address("0.0.0.0")
    assert is_unusable_address("::")
    assert is_unusable_address("127.0.0.1")
    assert is_unusable_address("127.8.8.8")
    assert is_unusable_address("::1")
    assert not is_unusable_address("10.0.0.1")
    # Link-local stays usable — real devices carry fe80:: addresses.
    assert not is_unusable_address("fe80::1")
    # Unparseable input is not "unusable" — the drop filter only
    # removes recognizable unusable addresses, so this stays False.
    assert not is_unusable_address("not-an-ip")
    assert not is_unusable_address("")


def test_is_usable_ip() -> None:
    assert is_usable_ip("10.0.0.1")
    assert is_usable_ip("fe80::1")
    # Not the negation of is_unusable_address: unparseable input
    # is unusable too (untrusted-payload posture).
    assert not is_usable_ip("0.0.0.0")
    assert not is_usable_ip("::")
    assert not is_usable_ip("127.0.0.1")
    assert not is_usable_ip("::1")
    assert not is_usable_ip("not-an-ip")
    assert not is_usable_ip("")


def test_drop_unusable_addresses_keeps_unparseable() -> None:
    assert drop_unusable_addresses(
        ["0.0.0.0", "10.0.0.1", "fe80::1%en0", "::", "127.0.0.1", "::1"]
    ) == [
        "10.0.0.1",
        "fe80::1%en0",
    ]
    assert drop_unusable_addresses([]) == []
    # The keep-unparseable contract, pinned directly.
    assert drop_unusable_addresses(["junk"]) == ["junk"]
