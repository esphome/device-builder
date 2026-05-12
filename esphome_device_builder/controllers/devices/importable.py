"""
Discovery / adoption helpers for the devices controller.

Backs the ``devices/import`` and ``devices/ignore`` WS commands
plus the importable-device cache wired off the state monitor's
``IMPORTABLE_DEVICE_*`` events. The controller keeps thin
bound-method delegates (``import_device``, ``toggle_ignore``,
``_on_importable_added`` / ``_on_importable_removed``,
``get_importable_devices``, ``_load_ignored_devices`` /
``_save_ignored_devices``) so WS dispatch, the state monitor's
captured callbacks, and tests that reach in by attribute name
all keep resolving.

The ignored-set + import_result dict live on the controller
(``ignored_devices``, ``import_result``) — they're shared with
the listing path that filters discovered-but-configured devices,
so they stay controller-owned and the helpers here read / write
them via the ``controller`` arg.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from esphome import const
from esphome.components.dashboard_import import import_config
from esphome.storage_json import ignored_devices_storage_path

from ...helpers.api import CommandError
from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...models import (
    AdoptableDevice,
    DeviceState,
    ErrorCode,
    EventType,
    ImportableDeviceAddedData,
    ImportableDeviceRemovedData,
)

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def import_device(
    controller: DevicesController,
    *,
    name: str,
    project_name: str,
    package_import_url: str,
    friendly_name: str | None,
    encryption: str | None,
) -> dict:
    """Import / adopt a discovered device."""
    configuration = f"{name}.yaml"
    path = controller._db.settings.rel_path(configuration)
    # Honour the network type the discovery TXT advertised — an
    # ESP32-PoE / Olimex / etc. broadcasts ``network=ethernet``
    # and the imported template needs to start from
    # ``ethernet:`` rather than the Wi-Fi default.
    #
    # Prefer the direct ``name`` → ``import_result`` lookup since
    # factory firmware broadcasts with a MAC suffix
    # (``apollo-plt-1-983300``), which keeps each entry unique
    # per physical device even when multiple identical products
    # share the same ``package_import_url``. The frontend
    # pre-fills the adoption dialog with the discovery row's
    # broadcast name, so this matches in the common path.
    # Fall back to a ``package_import_url`` match only when the
    # user edited the name during adoption — at that point the
    # ``import_result`` key no longer matches. The fallback is
    # technically ambiguous between identical-product devices,
    # but those share the same ``network`` value so picking
    # whichever lands first is correct in practice.
    # Final fallback to Wi-Fi when no row matches at all (older
    # factory firmware that didn't advertise the field, or a
    # discovery row that was already purged).
    adoptable = controller.import_result.get(name) or next(
        (
            d
            for d in controller.import_result.values()
            if d.package_import_url == package_import_url
        ),
        None,
    )
    network = adoptable.network if adoptable and adoptable.network else const.CONF_WIFI
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            import_config,
            path,
            name,
            friendly_name,
            project_name,
            package_import_url,
            network,
            encryption,
        )
    except FileExistsError as exc:
        # ``import_config`` refuses to overwrite an existing YAML.
        # Surface this as a user-facing error so the dialog can
        # show "Configuration <file> already exists" instead of
        # the WS layer's generic "Command failed".
        msg = f"Configuration {configuration} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

    # Validate the freshly-written YAML before announcing it.
    # ``import_config`` produces a wizard-style YAML by
    # construction, but a regression upstream — or a project
    # whose ``packages:`` reference doesn't resolve cleanly
    # against the current esphome / zeroconf state — would
    # otherwise leave an unflashable YAML on disk that every
    # downstream operation refuses. Hand the helper an
    # ``on_error_cleanup`` so any non-success path (validation
    # rejection, validator subprocess wedged, ...) unlinks
    # the half-imported file before re-raising — without it
    # a retry would trip ``FileExistsError`` on the leftover
    # YAML. The window between ``import_config`` and the
    # cleanup is short and the scanner only runs on poll (no
    # inotify watcher), so no half-imported device leaks
    # into ``devices/list``.
    def _read() -> str:
        return path.read_text(encoding="utf-8")

    def _cleanup() -> None:
        path.unlink(missing_ok=True)

    try:
        content = await loop.run_in_executor(None, _read)
    except (OSError, UnicodeDecodeError):
        # Transient FS error or non-UTF-8 bytes in what we
        # just wrote via ``import_config``. Roll back either
        # way so a retry doesn't see a leftover file.
        await loop.run_in_executor(None, _cleanup)
        raise
    await controller._validate_rewritten_yaml_or_raise(
        configuration, content, action="import", on_error_cleanup=_cleanup
    )

    # Picking up the new YAML is best-effort — if the scanner
    # hiccups (e.g. a transient stat error on a network mount),
    # the next periodic scan will catch it. We've already written
    # the YAML, so failing the whole command here would lie to
    # the user and trip a follow-up FileExistsError if they retry.
    try:
        await controller._scanner.scan()
    except Exception:
        _LOGGER.exception("Scan after import failed; will pick up on next poll")

    # Drop the discovery banner entry: the device is now configured,
    # so it shouldn't continue to show up under "Discovered". The
    # importable cache key is the device's mDNS-advertised name,
    # which usually matches the user-chosen YAML name but may
    # differ (e.g. they edited the MAC suffix off). Match by
    # ``package_import_url`` so we always find the right entry,
    # and remember the cached name so we can use it for the
    # zeroconf-cache lookup below — the device is broadcasting
    # under that name, not the YAML name.
    cached_names = [
        n for n, d in controller.import_result.items() if d.package_import_url == package_import_url
    ]
    for cached_name in cached_names:
        controller._on_importable_removed(cached_name)
    mdns_name = cached_names[0] if cached_names else name

    # Skip-the-wait state seed. We just adopted a device that was
    # advertising on mDNS milliseconds ago, so the next ping sweep
    # would only confirm what zeroconf already knew. Pull the
    # cached IP out of zeroconf — keyed by the mDNS-advertised
    # name, not the user's chosen YAML name — and apply both
    # ONLINE and the address right away so the new card lands
    # online instead of blinking through OFFLINE for ~10s.
    controller._state_monitor.apply(name, DeviceState.ONLINE, "mdns", claim=True)
    cached = controller._state_monitor.get_cached_addresses(f"{mdns_name}.local")
    if cached:
        controller._state_monitor.apply_ip_addresses(name, cached)
    # Eagerly probe the esphomelib service so the new card lands
    # with version / config_hash / api_encryption populated, not
    # just IP. The device on the network is still broadcasting
    # under its factory-firmware ``mdns_name`` (the user may have
    # picked a different YAML name during adoption), so look up
    # the service under that name but apply the result against
    # the configured device's chosen name. Cache hit returns
    # synchronously; otherwise the probe runs as a fire-and-
    # forget task whose results land via the same
    # browser-callback path. The ``_on_scan_change`` handler
    # also probes when the scan picked up the new YAML, but it
    # uses the YAML name only — for adoption that name has no
    # mDNS broadcast yet, so this explicit call covers the
    # rename-during-adopt case.
    controller._state_monitor.probe_device(name, service_name=mdns_name)
    return {"configuration": configuration}


async def toggle_ignore(controller: DevicesController, *, name: str, ignore: bool) -> None:
    """Mark a discovered device as ignored / visible in the import list."""
    if ignore:
        controller.ignored_devices.add(name)
    else:
        controller.ignored_devices.discard(name)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, controller._save_ignored_devices)
    # Mirror the new flag onto the cached AdoptableDevice and
    # re-publish ADDED so subscribed frontends update the badge
    # without waiting for a full re-discovery cycle.
    existing = controller.import_result.get(name)
    if existing is not None and existing.ignored != ignore:
        updated = replace(existing, ignored=ignore)
        controller.import_result[name] = updated
        controller._db.bus.fire(
            EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=updated)
        )


def on_importable_added(controller: DevicesController, device: AdoptableDevice) -> None:
    """Stash a newly-discovered importable device and notify subscribers."""
    # Keyed by device name so ``devices/list`` can dedupe against
    # configured devices and ``devices/ignore`` can flip the flag
    # by name without juggling the full mdns service-instance.
    controller.import_result[device.name] = device
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=device)
    )


def on_importable_removed(controller: DevicesController, name: str) -> None:
    """Forget an importable device that disappeared from mDNS."""
    if controller.import_result.pop(name, None) is None:
        return
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_REMOVED, ImportableDeviceRemovedData(name=name)
    )


def get_importable_devices(controller: DevicesController) -> list[AdoptableDevice]:
    """
    Snapshot of the current importable list (used for ``initial_state``).

    Filters against the configured-name set on every call so an
    adoption that landed without an mDNS Removed (the device kept
    announcing on its old name) doesn't leak through into the
    seed a fresh page load gets.
    """
    configured_names = {d.name for d in controller._scanner.devices}
    return [d for d in controller.import_result.values() if d.name not in configured_names]


def load_ignored_devices(controller: DevicesController) -> None:
    storage_path = ignored_devices_storage_path()
    try:
        raw = storage_path.read_bytes()
    except FileNotFoundError:
        return
    try:
        data = loads(raw)
    except JSONDecodeError:
        # A corrupt file shouldn't tank controller bootstrap —
        # start with an empty ignored set and let the next
        # toggle_ignore call rewrite it cleanly.
        _LOGGER.warning(
            "Ignored-devices file at %s is corrupt; starting with an empty set",
            storage_path,
        )
        return
    if not isinstance(data, dict):
        _LOGGER.warning(
            "Ignored-devices file at %s isn't a JSON object; starting with an empty set",
            storage_path,
        )
        return
    ignored = data.get("ignored_devices", [])
    if not isinstance(ignored, list):
        _LOGGER.warning(
            "Ignored-devices file at %s has a non-list ``ignored_devices`` "
            "field; resetting to an empty set",
            storage_path,
        )
        controller.ignored_devices = set()
        return
    controller.ignored_devices = {name for name in ignored if isinstance(name, str)}


def save_ignored_devices(controller: DevicesController) -> None:
    storage_path = ignored_devices_storage_path()
    storage_path.write_bytes(
        dumps_indent({"ignored_devices": sorted(controller.ignored_devices)}),
    )


# Re-export for tests that historically patched ``import_config`` on
# the controller module via ``monkeypatch.setattr(devices_module,
# "import_config", ...)``. Tests that point at ``importable`` see the
# same symbol.
__all__ = [
    "get_importable_devices",
    "import_config",
    "import_device",
    "load_ignored_devices",
    "on_importable_added",
    "on_importable_removed",
    "save_ignored_devices",
    "toggle_ignore",
]
