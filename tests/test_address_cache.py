"""Tests for the dns/zeroconf address cache hand-off to the esphome CLI.

Originally tracked in https://github.com/esphome/device-builder/issues/6 —
without these args, every OTA invocation in the CLI redoes mDNS / DNS
resolution we already did in the dashboard. The legacy ESPHome dashboard
solves this in ``build_cache_arguments`` (web_server.py); this is the
parity test for the new backend.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers.devices import _build_address_cache_args
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import Device, FirmwareJob, JobType


def _device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
        "address": "kitchen.local",
        "ip": "",
        "loaded_integrations": ["api"],
    }
    base.update(overrides)
    return Device(**base)


def _monitor(addresses: list[str] | None) -> Any:
    monitor = MagicMock()
    monitor.get_cached_addresses.return_value = addresses
    return monitor


# ----------------------------------------------------------------------
# _build_address_cache_args
# ----------------------------------------------------------------------


def test_local_address_uses_zeroconf_cache() -> None:
    """``.local`` host with cache hit → ``--mdns-address-cache``."""
    args = _build_address_cache_args(_device(), _monitor(["192.168.1.50"]))
    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]


def test_local_address_hostname_normalised() -> None:
    """Trailing dot + uppercase normalised to canonical form."""
    args = _build_address_cache_args(_device(address="Kitchen.Local."), _monitor(["192.168.1.50"]))
    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]


def test_local_address_falls_back_to_device_ip() -> None:
    """Cache miss + tracked ip → still emit a cache entry.

    Zeroconf entries can expire between resolution and an OTA build —
    reusing the IP we already saw is better than nothing.
    """
    args = _build_address_cache_args(_device(ip="192.168.1.99"), _monitor(None))
    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.99"]


def test_local_address_no_cache_no_ip_returns_empty() -> None:
    """No source for an IP at all → no cache args (CLI does its own lookup)."""
    args = _build_address_cache_args(_device(), _monitor(None))
    assert args == []


def test_non_local_address_uses_dns_cache() -> None:
    """Non-``.local`` host with tracked IP → ``--dns-address-cache``."""
    args = _build_address_cache_args(
        _device(address="esp.example.com", ip="10.0.0.1"), _monitor(None)
    )
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]


def test_non_local_skips_zeroconf_lookup() -> None:
    """Non-``.local`` addresses don't hit zeroconf — that cache is mDNS-only."""
    monitor = _monitor(["1.1.1.1"])
    args = _build_address_cache_args(_device(address="esp.example.com", ip="10.0.0.1"), monitor)
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]
    monitor.get_cached_addresses.assert_not_called()


def test_no_address_returns_empty() -> None:
    """Device with no address at all → nothing to cache."""
    assert _build_address_cache_args(_device(address=""), _monitor(None)) == []


def test_no_monitor_falls_back_to_device_ip() -> None:
    """``DeviceStateMonitor`` not yet running (e.g. during tests) is tolerated."""
    args = _build_address_cache_args(_device(ip="192.168.1.50"), None)
    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]


def test_multiple_cached_addresses_sorted() -> None:
    """Multiple IPs are passed comma-joined and sorted by ``sort_ip_addresses``."""
    args = _build_address_cache_args(_device(), _monitor(["192.168.1.50", "fe80::1234"]))
    assert len(args) == 2
    assert args[0] == "--mdns-address-cache"
    # Both addresses present, comma-joined.
    hostname, _, ips = args[1].partition("=")
    assert hostname == "kitchen.local"
    assert set(ips.split(",")) == {"192.168.1.50", "fe80::1234"}


# ----------------------------------------------------------------------
# FirmwareController._build_cache_args / _build_command
# ----------------------------------------------------------------------


def _firmware_controller_with(devices_controller: Any) -> FirmwareController:
    db = MagicMock()
    db.devices = devices_controller
    controller = FirmwareController(db)
    controller._esphome_cmd = ["esphome"]
    return controller


def test_cache_args_only_for_ota_upload_install() -> None:
    """Compile / clean / serial-port jobs don't get cache args."""
    devices = MagicMock()
    devices.get_address_cache_args.return_value = ["--mdns-address-cache", "k.local=1.2.3.4"]
    controller = _firmware_controller_with(devices)

    # Compile: no port, irrelevant
    job = FirmwareJob(job_id="1", configuration="kitchen.yaml", job_type=JobType.COMPILE)
    assert controller._build_cache_args(job) == []

    # Clean: no port, irrelevant
    job = FirmwareJob(job_id="2", configuration="kitchen.yaml", job_type=JobType.CLEAN)
    assert controller._build_cache_args(job) == []

    # Upload over serial: port is a /dev path, cache wouldn't help
    job = FirmwareJob(
        job_id="3", configuration="kitchen.yaml", job_type=JobType.UPLOAD, port="/dev/ttyUSB0"
    )
    assert controller._build_cache_args(job) == []

    # Install over OTA: cache args should be returned
    job = FirmwareJob(
        job_id="4", configuration="kitchen.yaml", job_type=JobType.INSTALL, port="OTA"
    )
    assert controller._build_cache_args(job) == ["--mdns-address-cache", "k.local=1.2.3.4"]


def test_cache_args_no_devices_controller() -> None:
    """If devices controller hasn't started yet, fail safely (empty)."""
    controller = _firmware_controller_with(None)
    job = FirmwareJob(
        job_id="1", configuration="kitchen.yaml", job_type=JobType.INSTALL, port="OTA"
    )
    assert controller._build_cache_args(job) == []


def test_command_places_cache_args_before_subcommand() -> None:
    """Esphome CLI parses ``--mdns-address-cache`` on the *top-level* parser.

    If we put it after the subcommand (``run``, ``upload``...) argparse
    rejects it with ``unrecognized arguments``. Verifying the order
    here protects against regressions.
    """
    controller = _firmware_controller_with(None)
    cache_args = ["--mdns-address-cache", "kitchen.local=192.168.1.50"]

    cmd = controller._build_command(JobType.INSTALL, "kitchen.yaml", "OTA", cache_args)

    assert cmd == [
        "esphome",
        "--mdns-address-cache",
        "kitchen.local=192.168.1.50",
        "run",
        "kitchen.yaml",
        "--no-logs",
        "--device",
        "OTA",
    ]


def test_command_without_cache_args_unchanged() -> None:
    """When there are no cache args, the command is identical to before."""
    controller = _firmware_controller_with(None)
    cmd = controller._build_command(JobType.COMPILE, "kitchen.yaml", "")
    assert cmd == ["esphome", "compile", "kitchen.yaml"]
