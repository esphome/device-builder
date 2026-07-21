"""Tests for the unspecified-address filters in ``helpers.ip``."""

from __future__ import annotations

from esphome_device_builder.helpers.ip import (
    drop_unspecified_addresses,
    is_unspecified_address,
    is_usable_ip,
)


def test_is_unspecified_address() -> None:
    assert is_unspecified_address("0.0.0.0")
    assert is_unspecified_address("::")
    assert not is_unspecified_address("10.0.0.1")
    # Unparseable input is not "unspecified" — the drop filter must
    # keep it (zeroconf scoped forms), so this side stays False.
    assert not is_unspecified_address("not-an-ip")
    assert not is_unspecified_address("")


def test_is_usable_ip() -> None:
    assert is_usable_ip("10.0.0.1")
    assert is_usable_ip("fe80::1")
    # Not the negation of is_unspecified_address: unparseable input
    # is unusable too (untrusted-payload posture).
    assert not is_usable_ip("0.0.0.0")
    assert not is_usable_ip("::")
    assert not is_usable_ip("not-an-ip")
    assert not is_usable_ip("")


def test_drop_unspecified_addresses_keeps_unparseable() -> None:
    assert drop_unspecified_addresses(["0.0.0.0", "10.0.0.1", "fe80::1%en0", "::"]) == [
        "10.0.0.1",
        "fe80::1%en0",
    ]
    assert drop_unspecified_addresses([]) == []
    # The keep-unparseable contract, pinned directly.
    assert drop_unspecified_addresses(["junk"]) == ["junk"]
