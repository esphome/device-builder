"""
Pure helpers for the devices controller.

Free functions only — no controller state. A small subset of helpers
is imported outside ``controller.py``, including by tests, and
``friendly_name_slugify`` is re-exported here to keep a single import
path for the rest of the codebase regardless of where esphome
upstream decides to keep it.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

try:
    # ``friendly_name_slugify`` lives in ``esphome.helpers`` from
    # esphome/esphome#16206 onwards so it survives the legacy
    # dashboard's eventual removal. Older esphome releases still
    # expose it from ``esphome.dashboard.util.text``; fall through
    # only for that back-compat case so we don't carry a hard
    # dependency on the dashboard package once it's gone.
    from esphome.helpers import friendly_name_slugify
except ImportError:  # pragma: no cover — covered by the import below
    from esphome.dashboard.util.text import friendly_name_slugify

from esphome.helpers import sort_ip_addresses
from esphome.storage_json import StorageJSON, ext_storage_path

from ...helpers.api import CommandError
from ...helpers.hostname import is_local_hostname, normalize_hostname
from ...models import ConfigEntryType, Device, ErrorCode
from ..config import clear_volatile_device_metadata, remove_device_metadata
from .constants import _CONCEALED_SECRET_RE

if TYPE_CHECKING:
    from .._device_state_monitor import DeviceStateMonitor
    from ..components import _FeaturedRecord

__all__ = [
    "_apply_featured_presets",
    "_archive_clear_device_sidecars",
    "_build_address_cache_args",
    "_normalize_pin_value",
    "_redact_concealed_secrets",
    "_remove_device_sidecars",
    "_validate_archive_configuration",
    "_wipe_device_build_dir",
    "friendly_name_slugify",
]

_LOGGER = logging.getLogger(__name__)


def _wipe_device_build_dir(configuration: str) -> None:
    """Remove the per-device build dir if one exists.

    Reads the canonical ``build_path`` off the StorageJSON sidecar
    (set during compile) and ``shutil.rmtree``s it. No-op when the
    sidecar is gone or the device has never been built. Used by
    archive and delete; both treat compile output as dead weight.
    """
    storage_path = ext_storage_path(configuration)
    storage = StorageJSON.load(storage_path)
    if storage is not None and storage.build_path:
        shutil.rmtree(storage.build_path, ignore_errors=True)


def _remove_device_sidecars(config_dir: Path, configuration: str) -> None:
    """Remove the StorageJSON sidecar and device-metadata entry.

    Best-effort — failures are logged but don't propagate, so a
    partial cleanup (e.g. permission error on one file) doesn't
    block the rest of the delete flow. Used by ``delete`` and
    ``delete_archived``; both want a "leave no trace under this
    filename" semantic at the end of their flow.

    For ``archive`` see ``_archive_clear_device_sidecars`` —
    archive needs to preserve identity fields (``board_id``,
    ``friendly_name``, ``comment``) so an unarchive of the same
    YAML restores the user-visible state unchanged.
    """
    storage_path = ext_storage_path(configuration)
    try:
        storage_path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("Could not remove storage file for %s", configuration)
    try:
        remove_device_metadata(config_dir, configuration)
    except Exception:
        _LOGGER.warning("Could not remove metadata for %s", configuration)


def _archive_clear_device_sidecars(config_dir: Path, configuration: str) -> None:
    """Wipe build artifacts but keep stable identity metadata.

    Variant of ``_remove_device_sidecars`` for the archive flow.
    The StorageJSON sidecar is a build artifact (carries the
    last compile's ``firmware_bin_path`` / ``loaded_integrations``
    / target_platform) and goes stale immediately on archive —
    same wipe behaviour as ``_remove_device_sidecars``. The
    device-metadata sidecar is mixed: identity fields
    (``board_id``, ``friendly_name``, ``comment``) survive an
    archive → unarchive cycle by design, so we only clear the
    volatile fields. ``board_id`` in particular is the catalog
    → YAML match key; losing it on every archive cycle forced a
    re-derive (or a re-pick by the user) on unarchive that wasn't
    necessary.

    Best-effort with the same failure semantics as
    ``_remove_device_sidecars`` — a partial wipe doesn't block
    the YAML move that already happened upstream of this call.
    """
    storage_path = ext_storage_path(configuration)
    try:
        storage_path.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("Could not remove storage file for %s", configuration)
    try:
        clear_volatile_device_metadata(config_dir, configuration)
    except Exception:
        _LOGGER.warning("Could not clear volatile metadata for %s", configuration)


def _validate_archive_configuration(configuration: str) -> None:
    """Reject anything that isn't a pure basename.

    Defense-in-depth at the public-command boundary for archive /
    unarchive / delete_archived. Each helper builds paths from the
    user-supplied filename (``<config_dir>/archive/<configuration>``,
    ``ext_storage_path(configuration)`` -> ``data_dir/storage/<configuration>.json``)
    that don't all flow through ``Settings.rel_path`` — a value
    containing path separators or ``..`` segments could resolve
    outside the intended directory and be unlinked / overwritten.

    Reject anything where ``Path(value).name != value`` (catches
    ``../foo``, ``sub/foo``, backslash-separated paths on Windows),
    the empty string, and the special path components ``.`` / ``..``
    that pass the ``.name`` round-trip but are still traversal
    vectors.
    """
    if not configuration:
        raise CommandError(ErrorCode.INVALID_ARGS, "configuration must not be empty")
    if configuration in (".", ".."):
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"configuration must be a plain filename, not {configuration!r}",
        )
    if (
        "/" in configuration
        or "\\" in configuration
        or "\x00" in configuration
        or PurePosixPath(configuration).name != configuration
        or PureWindowsPath(configuration).name != configuration
    ):
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"configuration must be a plain filename without path separators, "
            f"got {configuration!r}",
        )


def _redact_concealed_secrets(line: str) -> str:
    """Replace ANSI-conceal-wrapped secret runs with ``<removed>``."""
    return _CONCEALED_SECRET_RE.sub("<removed>", line)


def _normalize_pin_value(value: Any) -> Any:
    """
    Reduce a rich pin mapping to its bare GPIO for comparison.

    ESPHome accepts pins as either a bare integer / string label or as
    a ``{number, mode, inverted, ...}`` mapping. Featured-component
    presets express ``suggestions`` and bare-int ``value``s as scalars;
    the frontend submits the mapping form. Returning the inner
    ``number`` (when present) lets the locked / suggestion checks
    treat both shapes equivalently.

    ``bool`` is excluded explicitly since it's an ``int`` subclass.
    """
    if isinstance(value, dict):
        number = value.get("number")
        if isinstance(number, (int, str)) and not isinstance(number, bool):
            return number
    return value


def _apply_featured_presets(
    record: _FeaturedRecord,
    user_fields: dict[str, Any],
) -> dict[str, Any]:
    """
    Merge a featured component's presets onto *user_fields*.

    Returns a new field map ready for the regular merge logic; raises
    ``ValueError`` when *user_fields* violates a preset constraint.

    Per-field semantics:

    - Locked: user must omit the key or supply the locked value verbatim.
    - Suggestions: user-supplied value must be one of the listed values;
      omission falls back to the preset's ``value`` (when set).
    - Plain default: filled in only when the user didn't supply one.

    Pin-typed fields can arrive in two ESPHome shapes — bare GPIO
    (``pin: 12``) or rich mapping (``pin: {number: 12, mode: ..., inverted: ...}``).
    Equality / membership checks compare on the bare GPIO so a manifest's
    ``suggestions: [4, 5]`` accepts whichever shape the frontend submits.
    """
    entries_by_key = {ce.key: ce for ce in record.underlying.config_entries}
    merged: dict[str, Any] = dict(user_fields)
    for key, preset in record.featured.fields.items():
        user_value = merged.get(key)
        user_supplied = key in merged
        is_pin = entries_by_key.get(key) is not None and (
            entries_by_key[key].type == ConfigEntryType.PIN
        )
        compare_user = _normalize_pin_value(user_value) if is_pin else user_value
        compare_preset = _normalize_pin_value(preset.value) if is_pin else preset.value
        if preset.locked:
            # Schema validation rejects ``locked: true`` without a value, but
            # guard the runtime too so a malformed manifest fails fast with a
            # clear error instead of "locked to None".
            if preset.value is None:
                msg = (
                    f"Featured component {record.full_id} field '{key}' has "
                    f"locked=true without a value — board manifest is malformed"
                )
                raise ValueError(msg)
            if user_supplied and compare_user != compare_preset:
                msg = (
                    f"Featured component {record.full_id} field '{key}' is "
                    f"locked to {preset.value!r}; cannot override with "
                    f"{user_value!r}"
                )
                raise ValueError(msg)
            merged[key] = preset.value
            continue
        if preset.suggestions is not None:
            if user_supplied:
                if compare_user not in preset.suggestions:
                    msg = (
                        f"Featured component {record.full_id} field '{key}' "
                        f"must be one of {preset.suggestions}; got "
                        f"{user_value!r}"
                    )
                    raise ValueError(msg)
            elif preset.value is not None:
                merged[key] = preset.value
            continue
        if not user_supplied and preset.value is not None:
            merged[key] = preset.value
    return merged


def _build_address_cache_args(device: Device, monitor: DeviceStateMonitor | None) -> list[str]:
    """Build CLI cache args from the IPs we already have for *device*."""
    address = device.address
    if not address:
        return []

    # mDNS hostnames are case-insensitive and may carry a trailing dot;
    # normalise once so the CLI cache key matches what it'll look up.
    normalized = normalize_hostname(address)
    is_local = is_local_hostname(address)

    # Preferred source per host type:
    #   .local  → zeroconf cache (mDNS-only, freshest while the browser is alive)
    #   non-.local → DNS cache populated by the ping sweep's pre-resolve pass
    # Either falls back to ``device.ip`` (the last-known resolved IP) so
    # an expired cache entry doesn't strip the cache args entirely.
    addresses: list[str] = []
    if monitor is not None:
        cached = (
            monitor.get_cached_addresses(address)
            if is_local
            else monitor.get_cached_dns_addresses(address)
        )
        if cached:
            addresses = list(cached)

    if not addresses and device.ip:
        addresses = [device.ip]

    if not addresses:
        return []

    cache_type = "mdns" if is_local else "dns"
    return [
        f"--{cache_type}-address-cache",
        f"{normalized}={','.join(sort_ip_addresses(addresses))}",
    ]
