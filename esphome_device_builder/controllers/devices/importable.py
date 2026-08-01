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
from .mutations_yaml import packages_block_span

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
    # The adopt dialog always imports under the factory broadcast name
    # (frontend applies an edited name via the post-adopt rename flow),
    # so the name-keyed lookup is exact; an absent row falls through to
    # Wi-Fi.
    adoptable = controller.state.import_result.get(name)
    network = adoptable.network if adoptable and adoptable.network else const.CONF_WIFI
    full_config_import = "full_config" in package_import_url.partition("?")[2]
    try:
        if full_config_import:
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
    warning = await controller._validate_rewritten_yaml_or_raise(
        configuration,
        content,
        action="import",
        on_error_cleanup=_cleanup,
        tolerate_unavailable=True,
        timeout=IMPORT_VALIDATE_TIMEOUT,
        # The delegated full-config path writes verbatim upstream YAML;
        # only our generated adoption shape gets the keep-with-warning
        # classification.
        packages_span=None if full_config_import else packages_block_span(content),
        failure_tail=". The import was rolled back; nothing was written.",
    )

    await controller._commit_history(configuration, f"Import {configuration}")

    # Post-write scan is best-effort; the next periodic scan
    # will catch the new YAML and failing here would mislead the
    # user into a retry that trips ``FileExistsError``.
    try:
        await controller._scanner.scan()
    except Exception:
        _LOGGER.exception("Scan after import failed; will pick up on next poll")

    _drop_importable_row_and_probe(controller, name)
    result = {"configuration": configuration}
    if warning:
        result["warning"] = warning
    return result


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


def _drop_importable_row_and_probe(controller: DevicesController, name: str) -> None:
    """Retire the adopted device's importable row and kick its first probe."""
    # Drop only the adopted name's row — a URL-wide sweep would retire
    # every sibling unit of the same product. The removal is a no-op
    # when the post-write scan already pruned it.
    controller._on_importable_removed(name)

    # No state seed — the real sources decide. Discovery is
    # mDNS-based, so the adopt claims ONLINE via the esphomelib
    # probe's cache hit in this same call.
    cached = controller._state_monitor.mdns.get_cached_addresses(f"{name}.local")
    if cached:
        controller._state_monitor.apply_ip_addresses(name, cached)
    controller._state_monitor.mdns.probe_device(name)
