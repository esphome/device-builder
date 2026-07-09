"""Firmware-job bulk submission: compile_bulk + install_bulk."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, NamedTuple

from ...helpers.api import CommandError
from ...models import OTA_PORT, ErrorCode, FirmwareJob
from . import factories
from .helpers import _validate_port

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

_ESPHOME_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(.+))?$")
_PRERELEASE_PART_RE = re.compile(r"\d+|\D+")
_TUPLE_NEW = tuple.__new__
_PRERELEASE_PARTS = tuple[tuple[int, int | str], ...]


class _VersionKey(NamedTuple):
    major: int
    minor: int
    patch: int
    release_rank: int
    prerelease: _PRERELEASE_PARTS


_UNKNOWN_VERSION_KEY = _TUPLE_NEW(_VersionKey, (-1, -1, -1, 0, ()))


def _prerelease_sort_key(prerelease: str) -> _PRERELEASE_PARTS:
    """Split prerelease strings so numeric parts sort numerically."""
    return tuple(
        (0, int(part)) if part.isdecimal() else (1, part)
        for part in _PRERELEASE_PART_RE.findall(prerelease)
    )


def _esphome_version_sort_key(version: str | None) -> _VersionKey:
    """Return a sortable key for ESPHome versions."""
    if not version:
        return _UNKNOWN_VERSION_KEY

    match = _ESPHOME_VERSION_RE.match(version)
    if match is None:
        return _TUPLE_NEW(_VersionKey, (-1, -1, -1, 0, _prerelease_sort_key(version)))

    prerelease = _prerelease_sort_key(match[4] or "")
    release_rank = 0 if prerelease else 1
    return _TUPLE_NEW(
        _VersionKey,
        (int(match[1]), int(match[2]), int(match[3]), release_rank, prerelease),
    )


def _is_older_esphome_version(deployed: str, current: str) -> bool:
    """Return true when *deployed* is strictly older than *current*."""
    if not deployed or not current:
        return False
    return _esphome_version_sort_key(deployed) < _esphome_version_sort_key(current)


def _configuration_order(controller: FirmwareController, configurations: list[str]) -> list[str]:
    """Return bulk firmware configs with stale devices first."""
    devices_controller = controller._db.devices
    if devices_controller is None:
        return configurations

    devices = {device.configuration: device for device in devices_controller.get_devices()}

    def sort_key(item: tuple[int, str]) -> tuple[int, _VersionKey, int]:
        index, config = item
        device = devices.get(config)
        if (
            device is not None
            and device.update_available
            and _is_older_esphome_version(device.deployed_version, device.current_version)
        ):
            return (0, _esphome_version_sort_key(device.deployed_version), index)
        if device is not None and device.has_pending_changes:
            return (1, _UNKNOWN_VERSION_KEY, index)
        return (2, _UNKNOWN_VERSION_KEY, index)

    return [config for _, config in sorted(enumerate(configurations), key=sort_key)]


async def _run_bulk(
    controller: FirmwareController,
    configurations: list[str],
    verb: str,
    enqueue_one: Callable[[str], Awaitable[FirmwareJob]],
) -> list[FirmwareJob]:
    """Enqueue one job per config in stale-first order; the shared bulk error policy."""
    jobs: list[FirmwareJob] = []
    for config in _configuration_order(controller, configurations):
        try:
            job = await enqueue_one(config)
        except CommandError as exc:
            if exc.code is ErrorCode.NO_COMPATIBLE_PEER:
                # Fleet-wide policy refusal — re-raise so the
                # operator gets one toast instead of N silent
                # per-config skips. See issue #985.
                raise
            _LOGGER.info("Skipping %s in %s: %s", config, verb, exc.message)
            continue
        jobs.append(job)
    return jobs


async def compile_bulk(
    controller: FirmwareController,
    *,
    configurations: list[str],
    force_local: bool = False,
) -> list[FirmwareJob]:
    """Queue compile for *configurations*; skip per-device errors and keep going.

    ``force_local=True`` keeps every job LOCAL (otherwise paired-build
    auto-routing may send some REMOTE).
    """
    await controller._validate_configurations_boundary(configurations)

    async def _enqueue_one(config: str) -> FirmwareJob:
        return await factories.enqueue_compile(
            controller, configuration=config, force_local=force_local
        )

    return await _run_bulk(controller, configurations, "compile_bulk", _enqueue_one)


async def install_bulk(
    controller: FirmwareController, *, configurations: list[str], port: str = OTA_PORT
) -> list[FirmwareJob]:
    """Queue install (compile + upload) for *configurations*; defaults to OTA.

    ``port`` is shared across every queued job — pass an explicit IP
    only when every device should install against the same target.
    An OFFLINE device with an OTA target queues a deferred compile-only
    job (flashed on its next wake) instead of a chain.
    Per-device errors skip that device and keep going; a fleet-wide
    ``NO_COMPATIBLE_PEER`` (``EXACT_REQUIRED`` with no eligible
    peer) re-raises so the operator gets one consolidated error
    rather than N silent skips.
    """
    _validate_port(port)
    await controller._validate_configurations_boundary(configurations)

    async def _enqueue_one(config: str) -> FirmwareJob:
        # A chain (COMPILE + dependent UPLOAD) per device, same as single
        # install — so device B's compile pipelines against device A's
        # upload instead of a fused job pinning both to the compile lane.
        return await factories.enqueue_install_or_defer(controller, configuration=config, port=port)

    return await _run_bulk(controller, configurations, "install_bulk", _enqueue_one)
