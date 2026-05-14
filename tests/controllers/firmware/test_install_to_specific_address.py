"""Tests for the "Install to Specific Address" feature.

The ``port`` parameter on ``firmware/upload``, ``firmware/install``,
and ``firmware/install_bulk`` accepts:

- ``"OTA"`` (the default) — let the CLI resolve the configured
  device's address from the YAML.
- A serial path — wired flash.
- An IP address or hostname — explicit OTA target. Useful when
  re-flashing a device whose address has drifted, or flashing a
  known-good IP when mDNS is broken.

The address-cache shortcut is correctly bypassed when the user
names the target explicitly — the cache is keyed on the
configured hostname, so feeding it for an unrelated IP would
mislead the CLI's resolver.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware.helpers import (
    PortType,
    _validate_port,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, FirmwareJob, JobType
from tests.controllers.firmware.conftest import BareFirmwareControllerFactory

# ---------------------------------------------------------------------------
# _validate_port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port",
    [
        "",  # default (upload, no explicit target)
        "OTA",  # the CLI's special "configured host" token
        "/dev/ttyUSB0",  # Linux serial
        "/dev/cu.usbserial-1410",  # macOS serial
        "COM3",  # Windows serial
        "/dev/ttyACM0",  # ESP32-S3 native USB CDC
        "192.168.1.42",  # explicit IPv4 target
        "10.0.0.1",  # private range IPv4
        "fe80::1",  # IPv6 (link-local)
        "kitchen.local",  # mDNS hostname
        "kitchen",  # bare hostname
        "apollo-plt-1-983300",  # hyphenated mDNS name
        "device.example.com",  # routable DNS hostname
        "kitchen.local.",  # FQDN trailing-dot from zeroconf
        "device.example.com.",  # FQDN trailing-dot from system resolver
    ],
)
def test_validate_port_accepts_known_shapes(port: str) -> None:
    """Anything an ESPHome user might reasonably type is accepted.

    The validator's job is to catch typos and bad input *before*
    the user spends 30 seconds compiling, not to reject things the
    CLI itself would handle. Any of the documented input shapes
    should pass without raising.
    """
    _validate_port(port)


@pytest.mark.parametrize(
    "port",
    [
        "192.168.1",  # truncated IPv4
        "256.256.256.256",  # out-of-range octets
        "192.168.1.1.1",  # too many octets
        "192.168.1 1",  # space in the middle
        "device name",  # space (hostname disallows)
        "kitchen$",  # punctuation
        "-leading-dash.local",  # hostname label can't start with dash
        "trailing-dash-.local",  # nor end with one
    ],
)
def test_validate_port_rejects_typos(port: str) -> None:
    """A typo'd target raises ``INVALID_ARGS`` instead of queueing a doomed job.

    Without the early check the user would compile + queue, and
    only see the failure at the flash phase ~30 s later when the
    CLI rejects the value. Surface a clean WS error up front so
    the dialog can re-prompt.
    """
    with pytest.raises(CommandError) as exc:
        _validate_port(port)
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert port in exc.value.message  # offending value is named
    # The error wording is shared across firmware/upload, install,
    # and install_bulk — must use neutral "device target" rather
    # than naming a single command, since the message is surfaced
    # verbatim over WS to whichever command the user actually ran.
    assert "device target" in exc.value.message
    assert "install target" not in exc.value.message


def test_validate_port_consults_get_port_type_for_serial_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_validate_port`` accepts via ``get_port_type(port) is PortType.SERIAL``."""
    calls: list[str] = []

    def fake(port: str) -> PortType:
        calls.append(port)
        return PortType.SERIAL

    monkeypatch.setattr("esphome_device_builder.controllers.firmware.helpers.get_port_type", fake)

    # An input the hostname/IP regex would reject — proves the SERIAL
    # short-circuit was taken before the fallthrough branches ran.
    _validate_port("not a valid hostname OR an IP")

    assert calls == ["not a valid hostname OR an IP"]


# ---------------------------------------------------------------------------
# _build_command — IP-target shape
# ---------------------------------------------------------------------------


def test_install_to_ip_emits_device_arg_with_no_cache_args(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """Explicit IP installs route ``--device <ip>`` with no address-cache args.

    The cache shortcut is keyed on the device's *configured*
    hostname (from YAML's ``esphome.address``); it's irrelevant
    when the user has named a different target. Passing the cache
    args anyway would make the CLI prefer the configured host's
    cached IP over what the user typed — the opposite of what the
    user is asking for. ``_build_cache_args`` returns ``[]`` for
    non-OTA ports, and this test pins that the resulting command
    line stays clean.
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)
    job = FirmwareJob(
        job_id="install-1",
        configuration="kitchen.yaml",
        job_type=JobType.INSTALL,
        port="192.168.1.42",
    )

    cache_args = controller._build_cache_args(job)
    cmd = controller._build_command(JobType.INSTALL, "kitchen.yaml", "192.168.1.42", cache_args)

    # No ``--mdns-address-cache`` — the user picked their own target.
    assert "--mdns-address-cache" not in cmd
    assert "--dns-address-cache" not in cmd
    # The IP reaches the CLI as ``--device <ip>``.
    assert cmd == [
        "esphome",
        "--dashboard",
        "run",
        "kitchen.yaml",
        "--no-logs",
        "--device",
        "192.168.1.42",
    ]


def test_upload_to_ip_emits_device_arg_with_no_cache_args(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """``firmware/upload`` with an IP target gets the same shape as install.

    Both endpoints route through ``_build_command``; the
    ``UPLOAD`` job type just runs ``upload`` instead of ``run``
    and skips the ``--no-logs`` suffix the install path adds.
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)
    job = FirmwareJob(
        job_id="upload-1",
        configuration="kitchen.yaml",
        job_type=JobType.UPLOAD,
        port="192.168.1.42",
    )

    cache_args = controller._build_cache_args(job)
    cmd = controller._build_command(JobType.UPLOAD, "kitchen.yaml", "192.168.1.42", cache_args)

    assert "--mdns-address-cache" not in cmd
    assert cmd == [
        "esphome",
        "--dashboard",
        "upload",
        "kitchen.yaml",
        "--device",
        "192.168.1.42",
    ]


def test_install_to_hostname_routes_through_device_arg(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A bare or ``.local`` hostname reaches the CLI verbatim.

    The CLI does its own resolution; we shouldn't second-guess it
    by mDNS-resolving here. Passing the literal hostname keeps the
    user's intent intact even when the device's mDNS broadcast is
    flaky or the cached A-record is stale.
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)
    cmd = controller._build_command(JobType.INSTALL, "kitchen.yaml", "kitchen.local", [])
    assert cmd[-2:] == ["--device", "kitchen.local"]


def test_install_ota_default_keeps_cache_args(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """The OTA default still gets the address-cache shortcut.

    Regression guard for the cache path — locking IP-target
    behaviour shouldn't accidentally make the OTA fast path slow
    too. Builds a controller whose devices controller surfaces
    cache args and verifies they pass through to the command.
    """
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)
    cache = ["--mdns-address-cache", "kitchen.local=192.168.1.50"]
    controller._db.devices = MagicMock()
    controller._db.devices.get_address_cache_args.return_value = cache
    controller._db.devices.get_ota_address_cache_args.side_effect = lambda _configuration, port: (
        cache if port == "OTA" else []
    )
    job = FirmwareJob(
        job_id="install-2",
        configuration="kitchen.yaml",
        job_type=JobType.INSTALL,
        port="OTA",
    )

    cache_args = controller._build_cache_args(job)
    cmd = controller._build_command(JobType.INSTALL, "kitchen.yaml", "OTA", cache_args)

    assert cache_args == cache
    assert "--mdns-address-cache" in cmd
