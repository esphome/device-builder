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
import re
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
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.hostname import is_local_hostname, normalize_hostname
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import read_yaml_scalar, rewrite_name_or_substitution
from ...models import ConfigEntryType, Device, ErrorCode
from ..config import clear_volatile_device_metadata, remove_device_metadata
from .constants import _CONCEALED_SECRET_RE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...models import ComponentCatalogEntry, ConfigEntry
    from .._device_state_monitor import DeviceStateMonitor
    from ..components import _FeaturedRecord

# Top-level YAML key matcher: line starts at column zero with an
# identifier, followed by ``:``. ``re.MULTILINE`` so ``^`` matches the
# start of every line, not just the document. Used instead of
# ``yaml.safe_load`` for top-level-block detection because ESPHome
# configs commonly carry custom tags (``!secret``, ``!include``) the
# standard loader can't handle.
_TOP_LEVEL_KEY_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:", re.MULTILINE)

__all__ = [
    "_apply_featured_presets",
    "_archive_clear_device_sidecars",
    "_build_address_cache_args",
    "_drop_unconfigured_dependent_fields",
    "_normalize_pin_value",
    "_redact_concealed_secrets",
    "_remove_device_sidecars",
    "_rewrite_required_yaml_leaf",
    "_validate_archive_configuration",
    "_wipe_device_build_dir",
    "friendly_name_slugify",
]


_LOGGER = logging.getLogger(__name__)


def _rewrite_required_yaml_leaf(
    content: str,
    leaf_path: Sequence[str],
    new_value: str,
) -> str:
    """
    Rewrite the leaf scalar at *leaf_path* in *content* — raise if missing.

    Thin wrapper around :func:`rewrite_name_or_substitution` that
    folds in the precondition the clone path needs: the leaf has
    to actually exist in *this* file. Silently no-op'ing on a
    missing leaf would let the clone produce a duplicate device
    under the source's hostname (no in-file ``esphome.name`` to
    rewrite ⇒ rewrite is a no-op ⇒ clone keeps the source's
    hostname).

    Friendly-name edit (``devices/edit_friendly_name``) doesn't
    use this helper — it goes through
    :func:`upsert_yaml_leaf_under_top_block` instead, which
    *inserts* a missing leaf rather than rejecting. That's the
    right choice for a display-label edit (ESPHome's package
    merge gives the local leaf precedence over the included
    one); for a hostname rewrite a duplicate would collide on
    mDNS, so reject is correct there.

    The leaf can be missing for two reasons that look the same to
    us — either the field is genuinely absent from this YAML or
    it lives in a ``packages:`` / ``!include``d file. The error
    message names the missing path and points at both fixes
    ("add it here, or edit the included source") so the dialog
    can surface something concrete the user can act on without
    us having to disambiguate.
    """
    if read_yaml_scalar(content, leaf_path) is None:
        leaf_dotted = ".".join(leaf_path)
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"No {leaf_dotted} line found in this YAML — add one "
            "directly, or edit the package / !include where it's "
            "defined.",
        )
    return rewrite_name_or_substitution(content, leaf_path, new_value)


def _wipe_device_build_dir(configuration: str) -> None:
    """Remove the per-device build dir if one exists.

    Reads the canonical ``build_path`` off the StorageJSON sidecar
    (set during compile) and ``shutil.rmtree``s it. No-op when the
    sidecar is gone or the device has never been built. Used by
    archive and delete; both treat compile output as dead weight.
    """
    storage_path = resolve_storage_path(configuration)
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
    storage_path = resolve_storage_path(configuration)
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
    storage_path = resolve_storage_path(configuration)
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
    user-supplied filename (``<config_dir>/archive/<configuration>``
    — used directly without ``Path.name`` collapse — and
    ``resolve_storage_path(configuration)`` which keys on
    ``Path(configuration).name`` so it lands at
    ``data_dir/storage/<basename>.json``). The archive path joins
    the raw value; a configuration containing path separators or
    ``..`` segments would resolve outside ``<config_dir>/archive``
    and let the caller unlink / overwrite an arbitrary file. The
    storage path is basename-collapsed by ``resolve_storage_path``
    so the traversal can't escape ``data_dir/storage`` directly,
    but a value like ``../etc/passwd`` would still collapse to
    ``passwd.json`` and let a malicious caller target an
    attacker-named sidecar inside ``data_dir/storage`` (e.g.
    after writing a file there via another vector). Rejecting
    non-basename inputs closes both gaps at the WS boundary.

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

    The manifest is authoritative for featured components, so optional
    default-fills the frontend echoes back from the form (a light's
    ``gamma_correct: 2.8``, an output's ``frequency: 1kHz``, and so on)
    are stripped before the YAML render. The filter is intentionally
    narrow: a non-manifest, non-required key is dropped only when its
    submitted value equals the catalog ``default_value`` for that
    entry — a deliberate override (``gamma_correct: 1.5``) survives.
    Catalog defaults for numeric and time-period fields are stored as
    strings (``'2.8'``, ``'0 us'``) while the frontend submits parsed
    scalars; the comparison stringifies both sides so the round-trip
    matches without false positives on real overrides.

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
    keep_unconditional = set(record.featured.fields)
    keep_unconditional.update(ce.key for ce in record.underlying.config_entries if ce.required)
    return _strip_default_echoes(merged, entries_by_key, keep_unconditional)


def _strip_default_echoes(
    fields: dict[str, Any],
    entries_by_key: dict[str, ConfigEntry],
    keep_unconditional: set[str],
) -> dict[str, Any]:
    """
    Drop fields that just echo their catalog default back at us.

    Unknown keys (no matching catalog entry) and entries with no
    ``default_value`` ride through — we have no baseline to call them
    an echo of, and silently dropping a typo would mask input rather
    than failing visibly.
    """
    filtered: dict[str, Any] = {}
    for key, value in fields.items():
        if key in keep_unconditional:
            filtered[key] = value
            continue
        entry = entries_by_key.get(key)
        if entry is None or entry.default_value is None:
            filtered[key] = value
            continue
        if not _is_catalog_default_echo(value, entry.default_value):
            filtered[key] = value
    return filtered


def _is_catalog_default_echo(value: Any, default: Any) -> bool:
    """Return True when *value* is just an unmodified echo of *default*.

    Plain ``==`` first (handles bool/bool, str/str, int/int matches),
    then a stringified compare for the cross-type case where the
    catalog stores numeric and time-period defaults as strings
    (``'2.8'``, ``'0 us'``) and the frontend submits the parsed
    scalar. Container values (dicts, lists) are never treated as an
    echo — those are nested sub-blocks the user opts into explicitly,
    and the catalog default is always ``None`` for them anyway.
    """
    if value == default:
        return True
    if isinstance(value, (dict, list)) or value is None:
        return False
    return str(value).strip() == str(default).strip()


def _drop_unconfigured_dependent_fields(
    fields: dict[str, Any],
    component: ComponentCatalogEntry,
    existing_yaml: str,
) -> dict[str, Any]:
    """
    Strip fields whose ``depends_on_component`` block isn't in *existing_yaml*.

    Returns a new field map with fields that gate on a separate
    top-level component (``mqtt:``, ``web_server:``, ``zigbee:``, ...)
    removed when that block isn't configured on the device. Featured
    components target the dashboard's native-API setup, which doesn't
    carry an ``mqtt:`` block — emitting ``availability:`` /
    ``state_topic:`` / ``qos:`` defaults the frontend pre-fills would
    produce a YAML config ESPHome rejects (or silently ignores).

    The component currently being added counts as configured — adding
    ``mqtt`` itself with ``discovery: true`` keeps the discovery field
    even though ``mqtt:`` isn't in the existing YAML yet.

    Recurses into nested dict fields so sub-fields carrying their own
    ``depends_on_component`` (multi-phase sensors with per-phase MQTT
    options, ...) get the same gate.

    Top-level blocks contributed by ``packages:`` aren't detected — the
    scan is regex-based on the file text rather than a full ESPHome
    package merge, so a device gating MQTT fields on a package-provided
    ``mqtt:`` block won't see those fields kept. Standard ESPHome tags
    (``!secret``, ``!include``) ride through unaffected.
    """
    configured_blocks = set(_TOP_LEVEL_KEY_RE.findall(existing_yaml))
    # ``component.id`` is qualified for platform-style entries
    # (``switch.gpio``); the YAML lands under the bare domain stem.
    configured_blocks.add(component.id.split(".", 1)[0])

    entries_by_key = {ce.key: ce for ce in component.config_entries}
    return _filter_dependent_recursive(fields, entries_by_key, configured_blocks)


def _filter_dependent_recursive(
    fields: dict[str, Any],
    entries_by_key: dict[str, ConfigEntry],
    configured_blocks: set[str],
) -> dict[str, Any]:
    """Recursively apply the depends_on_component gate to a fields mapping."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        ce = entries_by_key.get(key)
        if _gates_on_unconfigured_block(ce, configured_blocks):
            continue
        if isinstance(value, dict) and ce is not None and ce.config_entries:
            sub_entries = {sub.key: sub for sub in ce.config_entries}
            out[key] = _filter_dependent_recursive(value, sub_entries, configured_blocks)
        else:
            out[key] = value
    return out


def _gates_on_unconfigured_block(
    entry: ConfigEntry | None,
    configured_blocks: set[str],
) -> bool:
    """Return True when *entry* depends on a top-level block not in *configured_blocks*."""
    if entry is None:
        return False
    gate = entry.depends_on_component
    return bool(gate) and gate not in configured_blocks


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
