"""Discovery / adoption helpers for the devices controller."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from esphome import const
from esphome.storage_json import ignored_devices_storage_path

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.atomic_io import atomic_write_exclusive
from ...helpers.device_yaml import generate_adoption_yaml
from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.lazy_module import async_import_module
from ...models import (
    AdoptableDevice,
    ErrorCode,
    EventType,
    ImportableDeviceAddedData,
    ImportableDeviceRemovedData,
)
from ..editor import IMPORT_VALIDATE_TIMEOUT

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
    # Look up the adoptable by name first; factory firmware
    # broadcasts a MAC-suffixed name (``apollo-plt-1-983300``)
    # so each physical device has a unique key even when
    # multiple identical products share the same
    # ``package_import_url``. Fall back to a URL match for the
    # rename-during-adopt case (the ``import_result`` key no
    # longer matches the chosen YAML name); fall through to
    # Wi-Fi when no row matches at all.
    adoptable = controller.state.import_result.get(name) or next(
        (
            d
            for d in controller.state.import_result.values()
            if d.package_import_url == package_import_url
        ),
        None,
    )
    network = adoptable.network if adoptable and adoptable.network else const.CONF_WIFI
    try:
        if "full_config" in package_import_url.partition("?")[2]:
            # A ``?full_config`` import downloads and rewrites the whole
            # upstream YAML — keep delegating those to esphome's
            # implementation. ``esphome.components.dashboard_import`` pulls
            # in ~14 MB of upstream code; load it through
            # ``async_import_module`` so the first such adoption pays the
            # cost on the dedicated import thread (no event loop block, no
            # concurrent-import race) and sessions that never need it skip
            # the load entirely.
            dashboard_import = await async_import_module("esphome.components.dashboard_import")
            await run_in_executor(
                dashboard_import.import_config,
                path,
                name,
                friendly_name,
                project_name,
                package_import_url,
                network,
                encryption,
            )
        else:
            content = generate_adoption_yaml(
                name,
                friendly_name,
                project_name,
                package_import_url,
                network_provided=network != const.CONF_WIFI,
                api_encryption=bool(encryption),
            )

            def _write_exclusive() -> None:
                # Staged exclusive-create, matching ``import_config``'s
                # FileExistsError contract for a concurrent writer.
                atomic_write_exclusive(path, content.encode("utf-8"))

            await run_in_executor(_write_exclusive)
    except FileExistsError as exc:
        msg = f"Configuration {configuration} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

    # Validate the freshly-written YAML; on a genuine failure the cleanup
    # callback unlinks it so a retry doesn't trip ``FileExistsError``. Adopt
    # tolerates a validator timeout on a short budget: the config's
    # ``github://`` fetch can outlast a full validate.
    def _read() -> str:
        return path.read_text(encoding="utf-8")

    def _cleanup() -> None:
        path.unlink(missing_ok=True)

    try:
        content = await run_in_executor(_read)
    except (OSError, UnicodeDecodeError):
        await run_in_executor(_cleanup)
        raise
    await controller._validate_rewritten_yaml_or_raise(
        configuration,
        content,
        action="import",
        on_error_cleanup=_cleanup,
        tolerate_unavailable=True,
        timeout=IMPORT_VALIDATE_TIMEOUT,
    )

    await controller._commit_history(configuration, f"Import {configuration}")

    # Post-write scan is best-effort; the next periodic scan
    # will catch the new YAML and failing here would mislead the
    # user into a retry that trips ``FileExistsError``.
    try:
        await controller._scanner.scan()
    except Exception:
        _LOGGER.exception("Scan after import failed; will pick up on next poll")

    # Drop any importable rows for this device (matched by URL,
    # since the user may have edited the name during adoption)
    # and remember the broadcast name for the zeroconf-cache
    # lookup below.
    cached_names = [
        n
        for n, d in controller.state.import_result.items()
        if d.package_import_url == package_import_url
    ]
    for cached_name in cached_names:
        controller._on_importable_removed(cached_name)
    mdns_name = cached_names[0] if cached_names else name

    # No state seed — the real sources decide. The device was
    # advertising on mDNS milliseconds ago, so a same-name adopt
    # claims ONLINE via the esphomelib probe's cache hit in this
    # same call; a rename-during-adopt has no PTR under *name* (an
    # mdns claim would have no ``Removed`` to withdraw it, #2389),
    # so the woken sweep pings the cached IP applied below for a
    # real verdict within seconds.
    cached = controller._state_monitor.mdns.get_cached_addresses(f"{mdns_name}.local")
    if cached:
        controller._state_monitor.apply_ip_addresses(name, cached)
    # Both broadcast names are known here: the adoption YAML pins
    # ``name`` with the mac suffix off, so the device broadcasts
    # under it only after its first flash — until then the factory
    # firmware broadcasts ``mdns_name``. Probe that service and
    # apply against ``name``.
    controller._state_monitor.mdns.probe_device(name, service_name=mdns_name)
    controller._state_monitor.probe_device_ping(name)
    return {"configuration": configuration}


async def toggle_ignore(controller: DevicesController, *, name: str, ignore: bool) -> None:
    """Mark a discovered device as ignored / visible in the import list."""
    if ignore:
        controller.state.ignored_devices.add(name)
    else:
        controller.state.ignored_devices.discard(name)
    await run_in_executor(controller._save_ignored_devices)
    # Mirror the new flag onto the cached AdoptableDevice and
    # re-publish ADDED so subscribed frontends update the badge
    # without waiting for a full re-discovery cycle.
    existing = controller.state.import_result.get(name)
    if existing is not None and existing.ignored != ignore:
        updated = replace(existing, ignored=ignore)
        controller.state.import_result[name] = updated
        controller._db.bus.fire(
            EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=updated)
        )


def on_importable_added(controller: DevicesController, device: AdoptableDevice) -> None:
    """Stash a newly-discovered importable device and notify subscribers."""
    controller.state.import_result[device.name] = device
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=device)
    )


def on_importable_removed(controller: DevicesController, name: str) -> None:
    """Forget an importable device that disappeared from mDNS."""
    if controller.state.import_result.pop(name, None) is None:
        return
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_REMOVED, ImportableDeviceRemovedData(name=name)
    )


def get_importable_devices(controller: DevicesController) -> list[AdoptableDevice]:
    """Snapshot of importable devices, filtered against the configured-name set."""
    configured_names = {d.name for d in controller._scanner.devices}
    return [d for d in controller.state.import_result.values() if d.name not in configured_names]


def load_ignored_devices(controller: DevicesController) -> None:
    """Populate ``controller.state.ignored_devices`` from the on-disk JSON file."""
    storage_path = ignored_devices_storage_path()
    try:
        raw = storage_path.read_bytes()
    except FileNotFoundError:
        return
    try:
        data = loads(raw)
    except JSONDecodeError:
        # A corrupt file shouldn't tank controller bootstrap;
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
    # Mutate the set in place rather than replacing it. The
    # ``DeviceStateMonitor`` captures
    # ``state.ignored_devices.__contains__`` at controller
    # ``__init__`` time, before this loader runs in
    # ``start()``; replacing the set here would leave the
    # monitor checking a stale empty set forever.
    ignored = data.get("ignored_devices", [])
    if not isinstance(ignored, list):
        _LOGGER.warning(
            "Ignored-devices file at %s has a non-list ``ignored_devices`` "
            "field; resetting to an empty set",
            storage_path,
        )
        controller.state.ignored_devices.clear()
        return
    controller.state.ignored_devices.clear()
    controller.state.ignored_devices.update(name for name in ignored if isinstance(name, str))


def save_ignored_devices(controller: DevicesController) -> None:
    """Persist ``controller.state.ignored_devices`` to the on-disk JSON file."""
    storage_path = ignored_devices_storage_path()
    storage_path.write_bytes(
        dumps_indent({"ignored_devices": sorted(controller.state.ignored_devices)}),
    )
