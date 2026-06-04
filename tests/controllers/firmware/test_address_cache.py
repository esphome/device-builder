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

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices.helpers import _build_address_cache_args
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import Device, FirmwareJob, JobType
from tests.conftest import make_device
from tests.controllers.devices.conftest import RecordingStateMonitor


def _device(**overrides: Any) -> Device:
    overrides.setdefault("loaded_integrations", ["api"])
    return make_device(**overrides)


_TEST_HOSTS = ("kitchen.local", "esp.example.com")


def _seed(values: list[str] | None) -> dict[str, list[str]] | None:
    """Map every test hostname to *values*, or return ``None`` if no values given."""
    return dict.fromkeys(_TEST_HOSTS, values) if values is not None else None


def _monitor(
    addresses: list[str] | None = None,
    dns_addresses: list[str] | None = None,
) -> RecordingStateMonitor:
    """Build a typed-fake state monitor with the cache lookups pre-seeded.

    Both maps are keyed by hostname with normalize_hostname semantics
    so production-equivalent inputs like ``Kitchen.Local.`` hit the
    same entry as ``kitchen.local``. The two test addresses
    (``kitchen.local`` and ``esp.example.com``) cover every test
    in this file.
    """
    return RecordingStateMonitor(
        cached_addresses=_seed(addresses),
        cached_dns_addresses=_seed(dns_addresses),
    )


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
    """Non-``.local`` host with DNS-cache hit → ``--dns-address-cache``."""
    args = _build_address_cache_args(
        _device(address="esp.example.com"),
        _monitor(dns_addresses=["10.0.0.1"]),
    )
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]


def test_non_local_dns_cache_preferred_over_device_ip() -> None:
    """A fresh DNS-cache hit wins over the stale ``device.ip`` fallback."""
    args = _build_address_cache_args(
        _device(address="esp.example.com", ip="10.0.0.99"),
        _monitor(dns_addresses=["10.0.0.1"]),
    )
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]


def test_non_local_falls_back_to_device_ip() -> None:
    """DNS cache miss + tracked IP → still emit a cache entry."""
    args = _build_address_cache_args(_device(address="esp.example.com", ip="10.0.0.1"), _monitor())
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]


def test_non_local_skips_zeroconf_lookup() -> None:
    """Non-``.local`` addresses don't hit zeroconf — that cache is mDNS-only."""
    monitor = _monitor(addresses=["1.1.1.1"], dns_addresses=["10.0.0.1"])
    args = _build_address_cache_args(_device(address="esp.example.com"), monitor)
    assert args == ["--dns-address-cache", "esp.example.com=10.0.0.1"]
    assert not any(call[0] == "get_cached_addresses" for call in monitor.calls)


def test_local_skips_dns_cache_lookup() -> None:
    """``.local`` addresses don't hit the DNS cache — zeroconf is the source of truth."""
    monitor = _monitor(addresses=["192.168.1.50"], dns_addresses=["10.0.0.1"])
    args = _build_address_cache_args(_device(), monitor)
    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]
    assert not any(call[0] == "get_cached_dns_addresses" for call in monitor.calls)


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
# DevicesController.get_address_cache_args integration gate
# ----------------------------------------------------------------------


def _devices_controller_with(*devices: Device) -> Any:
    """Build a thin DevicesController shell with a stubbed scanner + monitor.

    ``get_address_cache_args`` only reads the scanner's device list,
    the state monitor's cached-addresses lookup, and the device's
    ``loaded_integrations`` field — keep the rest of the controller
    out of the test surface.
    """
    controller = DevicesController.__new__(DevicesController)
    scanner = MagicMock()
    scanner.devices = list(devices)
    controller._scanner = scanner
    controller._state_monitor = _monitor(["192.168.1.50"])
    return controller


def test_get_address_cache_args_returns_cache_for_native_api_device() -> None:
    """Native API OTA path uses ``CORE.address_cache`` — feed it the args."""
    controller = _devices_controller_with(_device(loaded_integrations=["api", "wifi"]))

    args = controller.get_address_cache_args("kitchen.yaml")

    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]


def test_get_address_cache_args_returns_cache_for_web_server_only_device() -> None:
    """web_server OTA path also uses ``CORE.address_cache``.

    esphome/esphome#16207 added an HTTP OTA upload through the
    ``web_server`` component that resolves IPs via the same
    ``CORE.address_cache`` plumbing as the native API path. A device
    whose YAML enables ``web_server`` but not ``api`` (e.g. user lost
    the API password and falls back to HTTP OTA) should still get
    the cache args so the upload doesn't pay an unnecessary DNS
    lookup.
    """
    controller = _devices_controller_with(_device(loaded_integrations=["web_server", "wifi"]))

    args = controller.get_address_cache_args("kitchen.yaml")

    assert args == ["--mdns-address-cache", "kitchen.local=192.168.1.50"]


def test_get_address_cache_args_skipped_for_neither_api_nor_web_server() -> None:
    """MQTT-only / sensor-bridge configs don't need the cache.

    Devices with neither ``api`` nor ``web_server`` loaded flash via
    paths that don't take a host/port — the cache args would be
    noise the CLI ignores.
    """
    controller = _devices_controller_with(_device(loaded_integrations=["mqtt", "wifi"]))

    args = controller.get_address_cache_args("kitchen.yaml")

    assert args == []


def test_get_address_cache_args_unknown_configuration_returns_empty() -> None:
    """Unknown filename → empty list, no exception.

    The firmware controller calls this for every queued job; a stale
    rename or a deleted YAML shouldn't crash the queue.
    """
    controller = _devices_controller_with()  # no devices

    args = controller.get_address_cache_args("ghost.yaml")

    assert args == []


def test_get_ota_address_cache_args_returns_cache_for_ota_port() -> None:
    """Strict ``port == "OTA"`` → delegate to ``get_address_cache_args``."""
    controller = _devices_controller_with(_device())

    assert controller.get_ota_address_cache_args("kitchen.yaml", "OTA") == [
        "--mdns-address-cache",
        "kitchen.local=192.168.1.50",
    ]


def test_get_ota_address_cache_args_empty_for_serial_port() -> None:
    """``--device /dev/tty*`` doesn't consult the address cache."""
    controller = _devices_controller_with(_device())

    assert controller.get_ota_address_cache_args("kitchen.yaml", "/dev/ttyUSB0") == []


def test_get_ota_address_cache_args_empty_for_missing_port() -> None:
    """Empty string is *not* treated as OTA — callers must normalise first."""
    controller = _devices_controller_with(_device())

    assert controller.get_ota_address_cache_args("kitchen.yaml", "") == []


def test_get_ota_address_cache_args_none_is_always_ota() -> None:
    """``port=None`` skips the OTA gate — for always-OTA flows like ``rename``."""
    controller = _devices_controller_with(_device())

    assert controller.get_ota_address_cache_args("kitchen.yaml", None) == [
        "--mdns-address-cache",
        "kitchen.local=192.168.1.50",
    ]


# ----------------------------------------------------------------------
# FirmwareController._build_cache_args / _build_command
# ----------------------------------------------------------------------


def _firmware_controller_with(devices_controller: Any) -> FirmwareController:
    db = MagicMock()
    db.devices = devices_controller
    controller = FirmwareController(db)
    controller.state.esphome_cmd = ["esphome"]
    return controller


def test_cache_args_only_for_ota_upload_install() -> None:
    """Compile / clean / serial-port jobs don't get cache args."""
    cache = ["--mdns-address-cache", "k.local=1.2.3.4"]
    devices = MagicMock()
    devices.get_address_cache_args.return_value = cache
    devices.get_ota_address_cache_args.side_effect = lambda _configuration, port: (
        cache if port == "OTA" else []
    )
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
    assert controller._build_cache_args(job) == cache


def test_cache_args_no_devices_controller() -> None:
    """If devices controller hasn't started yet, fail safely (empty)."""
    controller = _firmware_controller_with(None)
    job = FirmwareJob(
        job_id="1", configuration="kitchen.yaml", job_type=JobType.INSTALL, port="OTA"
    )
    assert controller._build_cache_args(job) == []


def test_cache_args_rename_passes_none_port_to_skip_gate() -> None:
    """``rename`` calls the helper with ``port=None`` (always-OTA marker).

    The user-supplied ``job.port`` is the post-rename re-install
    target — irrelevant to the inner ``esphome run`` against the
    old address, which is always OTA.
    """
    cache = ["--mdns-address-cache", "k.local=1.2.3.4"]
    devices = MagicMock()
    devices.get_ota_address_cache_args.side_effect = lambda _configuration, port: (
        cache if port in (None, "OTA") else []
    )
    controller = _firmware_controller_with(devices)

    job = FirmwareJob(
        job_id="rename-1",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        port="/dev/ttyUSB0",
    )
    assert controller._build_cache_args(job) == cache
    devices.get_ota_address_cache_args.assert_called_once_with("kitchen.yaml", None)


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
        "--dashboard",
        "--mdns-address-cache",
        "kitchen.local=192.168.1.50",
        "run",
        "kitchen.yaml",
        "--no-logs",
        "--device",
        "OTA",
    ]


def test_command_includes_dashboard_flag_with_no_cache_args() -> None:
    """Even without cache args every job command carries ``--dashboard``.

    The flag flips ESPHome's ``CORE.dashboard`` log-formatter mode so
    ANSI colour codes survive the colorama strip when stdout is piped
    to us — without it the dashboard log view renders monochrome.
    """
    controller = _firmware_controller_with(None)
    cmd = controller._build_command(JobType.COMPILE, "kitchen.yaml", "")
    assert cmd == ["esphome", "--dashboard", "compile", "kitchen.yaml"]


def test_clean_job_command_invokes_esphome_clean_against_yaml() -> None:
    """A CLEAN job runs ``esphome clean <yaml>``; no port, no extra flags.

    Pins the receiver-side seam the ``firmware/clean`` fan-out
    depends on: paired with the per-dashboard ``ESPHOME_DATA_DIR``
    override (see ``test_subprocess_env.py``), invoking
    ``esphome clean <yaml>`` resolves ``CORE.data_dir`` to the
    receiver's per-offloader subtree and wipes
    ``<that>/build/<device_name>/`` — the directory the operator
    actually wanted gone.

    A regression that mapped ``JobType.CLEAN`` to ``"clean-all"``
    or added ``--no-logs`` or any other accidental flag would
    surface here rather than as a confusing "the clean ran but
    nothing got removed" report from the field.
    """
    controller = _firmware_controller_with(None)
    cmd = controller._build_command(JobType.CLEAN, "kitchen.yaml", "")
    assert cmd == ["esphome", "--dashboard", "clean", "kitchen.yaml"]


def test_clean_job_command_passes_remote_build_yaml_path_through() -> None:
    """The CLEAN command threads the receiver-side YAML path through verbatim.

    The receiver's submit_job dispatch lands a ``FirmwareJob``
    whose ``configuration`` is the relative POSIX path under
    ``.esphome/.remote_builds/<dir_id>/<device>/``. The
    build-command builder must thread that path verbatim — the
    paired ``ESPHOME_DATA_DIR`` override resolves esphome's
    ``CORE.data_dir`` to the per-offloader subtree, and
    ``esphome clean <yaml>`` keys its delete target off the
    YAML's resolved ``CORE.config_filename``. A regression that
    rewrites the path (e.g. strips the leading subtree segments
    on the assumption the YAML lives next to the dashboard's
    config) would clean the wrong build directory or fail with
    "config not found."
    """
    controller = _firmware_controller_with(None)
    rel_yaml = ".esphome/.remote_builds/a1b2c3d4/kitchen/kitchen.yaml"
    cmd = controller._build_command(JobType.CLEAN, rel_yaml, "")
    assert cmd == ["esphome", "--dashboard", "clean", rel_yaml]
