"""
Tests for :mod:`esphome_device_builder.discover` (CLI browse helper).

The CLI itself is a thin wrapper around python-zeroconf's
``AsyncServiceBrowser`` — there's no orchestration logic worth
end-to-end-testing against a live mDNS responder. What's worth
pinning:

* The TXT-decode helper handles every wire shape (``bytes`` /
  ``str`` / missing).
* The pin truncator collapses a 64-hex pin to 12 chars + a
  trailing ellipsis but leaves shorter sentinel values alone.
* The state-change callback handles both ``Added`` and
  ``Removed`` events and resolves the cached ServiceInfo.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from zeroconf import IPVersion, ServiceStateChange

from esphome_device_builder.discover import (
    _UNKNOWN,
    _decode,
    _on_service_state_change,
    _truncate_pin,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (b"hello", "hello"),
        ("plain", "plain"),
        (None, _UNKNOWN),
        # ``bytes`` containing a non-UTF-8 sequence falls through
        # the strict decode and lands on the replacement-char path
        # (rather than raising) so a malformed TXT entry doesn't
        # crash the browse loop.
        (b"\xff\xfe", "��"),
    ],
)
def test_decode_handles_every_txt_wire_shape(raw: str | bytes | None, expected: str) -> None:
    """``_decode`` round-trips bytes, leaves strings alone, marks missing."""
    assert _decode(raw) == expected


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        # Full 64-hex pin gets head + ellipsis.
        ("a" * 64, "aaaaaaaaaaaa…"),
        # Anything ≤ 12 chars passes through unchanged so the
        # ``unknown`` sentinel and short test fixtures stay
        # readable.
        ("unknown", "unknown"),
        ("abc", "abc"),
        ("a" * 12, "a" * 12),
        ("a" * 13, "aaaaaaaaaaaa…"),
    ],
)
def test_truncate_pin_trims_to_12_with_ellipsis_above_threshold(pin: str, expected: str) -> None:
    """``_truncate_pin`` collapses long pins, leaves short / sentinel alone."""
    assert _truncate_pin(pin) == expected


def test_on_service_state_change_prints_resolved_info(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``Added`` event prints an ``ONLINE`` row with TXT-derived columns.

    Mocks the cached :class:`AsyncServiceInfo` so the test doesn't
    need a live zeroconf responder. The shape of the printed row
    is what the CLI's stdout contract guarantees; downstream
    tooling (operators piping the output through ``grep`` /
    ``awk``) leans on the column widths staying stable.
    """
    fake_info = MagicMock()
    fake_info.properties = {
        b"server_version": b"0.1.62",
        b"esphome_version": b"2026.5.0-dev",
        b"pin_sha256": b"a" * 64,
        b"remote_build_port": b"6053",
    }
    fake_info.ip_addresses_by_version.return_value = ["192.168.1.10"]
    fake_info.port = 6052

    with patch("esphome_device_builder.discover.AsyncServiceInfo", return_value=fake_info):
        _on_service_state_change(
            MagicMock(),
            "_esphomebuilder._tcp.local.",
            "build-server._esphomebuilder._tcp.local.",
            ServiceStateChange.Added,
        )

    captured = capsys.readouterr().out
    assert "ONLINE" in captured
    assert "build-server" in captured
    assert "192.168.1.10:6052" in captured
    assert "0.1.62" in captured
    assert "2026.5.0-dev" in captured
    assert "6053" in captured
    # Truncated pin lands on the row.
    assert "aaaaaaaaaaaa…" in captured
    fake_info.ip_addresses_by_version.assert_called_once_with(IPVersion.V4Only)


def test_on_service_state_change_prints_offline_on_removal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``Removed`` event prints an ``OFFLINE`` row with the cached fields.

    The cached :class:`AsyncServiceInfo` survives the removal so
    the OFFLINE row carries the same metadata the last ONLINE
    row did. Useful for spotting which exact dashboard just
    dropped off when watching a churning network.
    """
    fake_info = MagicMock()
    fake_info.properties = {b"server_version": b"0.1.62"}
    fake_info.ip_addresses_by_version.return_value = ["192.168.1.10"]
    fake_info.port = 6052

    with patch("esphome_device_builder.discover.AsyncServiceInfo", return_value=fake_info):
        _on_service_state_change(
            MagicMock(),
            "_esphomebuilder._tcp.local.",
            "build-server._esphomebuilder._tcp.local.",
            ServiceStateChange.Removed,
        )

    captured = capsys.readouterr().out
    assert "OFFLINE" in captured
    assert "build-server" in captured
