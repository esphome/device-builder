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

import argparse
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import IPVersion, ServiceStateChange

from esphome_device_builder.discover import (
    _COLUMN_NAMES,
    _UNKNOWN,
    _build_parser,
    _decode,
    _on_service_state_change,
    _run,
    _truncate_pin,
    main,
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


def test_on_service_state_change_falls_back_to_ipv6_when_no_ipv4(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An IPv6-only dashboard renders with its scoped address.

    Pins the address-resolution fallback: when the browser
    callback fires for a dashboard whose mDNS announcement only
    carries an AAAA record (IPv6-only host, or IPv4 not resolved
    yet), the row still gets a meaningful Address:Port column
    value instead of ``unknown``.
    ``parsed_scoped_addresses(IPVersion.All)`` is the project-
    wide convention for this fallback (cf.
    ``_device_state_monitor`` / ``remote_build.controller``).
    """
    fake_info = MagicMock()
    fake_info.properties = {b"server_version": b"0.1.62"}
    fake_info.ip_addresses_by_version.return_value = []  # no IPv4
    fake_info.parsed_scoped_addresses.return_value = ["fe80::1%eth0", "2001:db8::1"]
    fake_info.port = 6052

    with patch("esphome_device_builder.discover.AsyncServiceInfo", return_value=fake_info):
        _on_service_state_change(
            MagicMock(),
            "_esphomebuilder._tcp.local.",
            "build-server._esphomebuilder._tcp.local.",
            ServiceStateChange.Added,
        )

    captured = capsys.readouterr().out
    assert "fe80::1%eth0:6052" in captured
    fake_info.parsed_scoped_addresses.assert_called_once_with(IPVersion.All)


def test_on_service_state_change_handles_none_properties(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A cache miss with ``properties = None`` doesn't crash the callback.

    ``AsyncServiceInfo.load_from_cache`` can return ``False``
    when the browser callback fires before the resolve
    completes; in that case ``info.properties`` is ``None``.
    The callback must guard so the browse loop survives — a
    crash here would silently kill all subsequent rows.
    """
    fake_info = MagicMock()
    fake_info.properties = None  # cache miss
    fake_info.ip_addresses_by_version.return_value = []
    fake_info.parsed_scoped_addresses.return_value = []
    fake_info.port = 6052

    with patch("esphome_device_builder.discover.AsyncServiceInfo", return_value=fake_info):
        _on_service_state_change(
            MagicMock(),
            "_esphomebuilder._tcp.local.",
            "build-server._esphomebuilder._tcp.local.",
            ServiceStateChange.Added,
        )

    captured = capsys.readouterr().out
    # No traceback escaped; the row landed with every TXT field
    # showing the ``unknown`` sentinel.
    assert "ONLINE" in captured
    assert captured.count("unknown") >= 4  # 4 TXT fields all unknown


@pytest.mark.asyncio
async def test_run_prints_header_then_awaits_then_cleans_up(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``_run`` prints the column header + divider, then parks on the event.

    Drives the orchestration function with mocked
    :class:`AsyncZeroconf` and :class:`AsyncServiceBrowser` so
    the test doesn't open a real mDNS socket. Spawns ``_run`` as
    a task, gives it a turn of the loop to print the header and
    park on ``asyncio.Event().wait()``, then cancels — the
    ``finally`` block runs and we assert both the wire-shape
    setup (browser handlers, service type, cleanup awaits) and
    the printed header.
    """
    fake_aiozc = MagicMock()
    fake_aiozc.zeroconf = MagicMock()
    fake_aiozc.async_close = AsyncMock()
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()

    with (
        patch(
            "esphome_device_builder.discover.AsyncZeroconf",
            return_value=fake_aiozc,
        ),
        patch(
            "esphome_device_builder.discover.AsyncServiceBrowser",
            return_value=fake_browser,
        ) as browser_ctor,
    ):
        runner = asyncio.create_task(_run(argparse.Namespace(verbose=False)))
        # One loop turn lets ``_run`` print the header and reach
        # the ``asyncio.Event().wait()`` await. ``sleep(0)`` is
        # enough because ``AsyncZeroconf()`` / ``AsyncServiceBrowser()``
        # are sync constructors and the only awaitable before the
        # park is the event itself.
        await asyncio.sleep(0)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    # Browser registered against the dashboard service type with
    # our handler. The handler-array shape is what python-zeroconf
    # expects; an empty / wrong-shape handler list would silently
    # drop every browse event.
    browser_ctor.assert_called_once()
    args, kwargs = browser_ctor.call_args
    assert args[1] == "_esphomebuilder._tcp.local."
    assert len(kwargs["handlers"]) == 1

    # Cleanup ran on cancel.
    fake_browser.async_cancel.assert_awaited_once()
    fake_aiozc.async_close.assert_awaited_once()

    captured = capsys.readouterr().out
    # Every column name lands on the printed header.
    for column in _COLUMN_NAMES:
        assert column in captured
    # Divider row is the second line.
    assert "-" * 60 in captured


@pytest.mark.asyncio
async def test_run_verbose_flag_enables_debug_logging() -> None:
    """``-v`` flag flips the root + zeroconf loggers to DEBUG.

    Pin the contract that ``--verbose`` does what the help text
    says: noisy logs for debugging discovery problems. A
    regression that quietly drops the verbose-aware branch
    would surface here.
    """
    fake_aiozc = MagicMock()
    fake_aiozc.zeroconf = MagicMock()
    fake_aiozc.async_close = AsyncMock()
    fake_browser = MagicMock()
    fake_browser.async_cancel = AsyncMock()

    with (
        patch("esphome_device_builder.discover.AsyncZeroconf", return_value=fake_aiozc),
        patch("esphome_device_builder.discover.AsyncServiceBrowser", return_value=fake_browser),
    ):
        runner = asyncio.create_task(_run(argparse.Namespace(verbose=True)))
        await asyncio.sleep(0)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    assert logging.getLogger("zeroconf").level == logging.DEBUG


def test_main_suppresses_keyboard_interrupt() -> None:
    """Ctrl-C exits cleanly rather than dumping a traceback.

    ``contextlib.suppress(KeyboardInterrupt)`` is the user-facing
    contract that Ctrl-C is the documented way to stop the
    browse. Without the suppression, the shell would see a
    traceback on every clean exit.
    """
    with (
        patch(
            "esphome_device_builder.discover.sys.argv",
            ["esphome-device-builder-discover"],
        ),
        patch(
            "esphome_device_builder.discover.asyncio.run",
            side_effect=KeyboardInterrupt,
        ),
    ):
        # Doesn't raise — the ``contextlib.suppress`` swallows it.
        main()


def test_main_runs_to_completion_when_inner_returns() -> None:
    """When ``asyncio.run`` returns cleanly, ``main`` returns cleanly.

    The orchestration's happy path: ``_run`` finishes (e.g. the
    parked event was set externally — which production never
    does, but the contract is that ``main`` doesn't add error
    paths beyond the Ctrl-C suppression).
    """
    with (
        patch(
            "esphome_device_builder.discover.sys.argv",
            ["esphome-device-builder-discover"],
        ),
        patch("esphome_device_builder.discover.asyncio.run", return_value=None) as mock_run,
    ):
        main()
    mock_run.assert_called_once()


def test_build_parser_accepts_verbose_flag() -> None:
    """The CLI parser carries the documented ``-v`` / ``--verbose`` flag.

    Pin the argparse surface — a future copy / refactor that
    drops the flag without updating callers would silently
    stop honouring DEBUG-log requests.
    """
    parser = _build_parser()
    args = parser.parse_args(["-v"])
    assert args.verbose is True
    long_args = parser.parse_args(["--verbose"])
    assert long_args.verbose is True
    default_args = parser.parse_args([])
    assert default_args.verbose is False
