"""Pure helpers for the devices controller (no controller state)."""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any

try:
    # ``friendly_name_slugify`` lives in ``esphome.helpers`` from
    # esphome/esphome#16206 onwards. The fallback is for older
    # esphome releases that still expose it from the dashboard
    # package; once those drop the hard dependency goes away.
    from esphome.helpers import friendly_name_slugify
except ImportError:  # pragma: no cover; covered by the import below
    from esphome.dashboard.util.text import friendly_name_slugify

try:
    from esphome.core.config import FRIENDLY_NAME_MAX_LEN
except ImportError:  # pragma: no cover; older esphome without the constant
    FRIENDLY_NAME_MAX_LEN = 120

from esphome.helpers import sort_ip_addresses

from ...helpers.api import CommandError
from ...helpers.hostname import is_local_hostname, normalize_hostname
from ...helpers.yaml import read_yaml_scalar, rewrite_name_or_substitution
from ...models import ConfigEntryType, Device, ErrorCode
from .constants import _CONCEALED_SECRET_RE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ...models import ComponentCatalogEntry, ConfigEntry
    from .._device_state_monitor import DeviceStateMonitor
    from ..components import _FeaturedRecord

# Top-level YAML key matcher; used instead of yaml.safe_load
# because ESPHome configs commonly carry custom tags
# (``!secret``, ``!include``) the standard loader can't handle.
_TOP_LEVEL_KEY_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*:", re.MULTILINE)

__all__ = [
    "_apply_featured_presets",
    "_build_address_cache_args",
    "_drop_unconfigured_dependent_fields",
    "_looks_binary",
    "_normalize_pin_value",
    "_redact_concealed_secrets",
    "_rewrite_required_yaml_leaf",
    "_validate_archive_configuration",
    "clean_friendly_name",
    "friendly_name_slugify",
    "slugify_hostname",
]


_LOGGER = logging.getLogger(__name__)


# C0 controls, DEL, and C1 controls. ESPHome's friendly_name validator
# doesn't reject these, and ``_safe_yaml_scalar`` only escapes a few
# (``\n`` / ``\t`` / ``\r``) while emitting the rest (NUL, bell, …)
# literally — a literal NUL alone makes the YAML unparsable.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Any C0/C1 control or U+FFFD means non-text content; a text config
# never carries these, but a .tar.gz read as text does (gzip magic 0x1f
# at byte 0, then NULs and invalid-UTF-8 bytes readAsText turns into
# U+FFFD). The YAML-safe whitespace (tab, LF, CR) is stripped by
# ``_looks_binary`` before the match so the class can use the same
# contiguous control range as ``_CONTROL_CHARS_RE``.
_BINARY_CONTENT_RE = re.compile("[\x00-\x1f\x7f-\x9f\ufffd]")
_YAML_SAFE_WHITESPACE = str.maketrans("", "", "\t\n\r")

# ESPHome's ``validate_hostname`` caps the device name at 31 chars (24
# with name_add_mac_suffix, which the create wizard never sets). Keep
# in sync with ESPHOME_DEVICE_NAME_MAX_LEN in esphome/core/entity_base.h.
_HOSTNAME_MAX_LEN = 31


def slugify_hostname(value: str) -> str:
    """
    Slugify *value* into a valid ``esphome.name`` (the mDNS hostname).

    Wraps ESPHome's ``friendly_name_slugify`` (lowercase-dashed ASCII)
    and clamps to the hostname length cap ``validate_hostname``
    enforces — ``friendly_name_slugify`` itself doesn't, so a long
    display name would otherwise generate a config ESPHome rejects.
    A dash left dangling at the cut is dropped so the slug stays valid.
    """
    slug: str = friendly_name_slugify(value)
    return slug[:_HOSTNAME_MAX_LEN].rstrip("-")


def clean_friendly_name(value: str) -> str:
    """
    Clean a raw display name into a valid ``esphome.friendly_name``.

    ESPHome validates friendly_name only with ``string_no_slash`` plus a
    ``FRIENDLY_NAME_MAX_LEN``-byte cap — it accepts control characters,
    so a pasted newline / tab / NUL would otherwise land in the YAML
    scalar. This swaps the reserved ``/`` for ``⁄`` (U+2044, ESPHome's
    own substitution), turns control characters (EOL / tab / …) into
    spaces and collapses whitespace runs, then clamps to the byte cap on
    a character boundary. YAML-metacharacter quoting (``:``, ``#``) is
    the separate job of ``_safe_yaml_scalar`` at emit time.
    """
    cleaned = _CONTROL_CHARS_RE.sub(" ", value.replace("/", "⁄"))
    cleaned = " ".join(cleaned.split())
    encoded = cleaned.encode("utf-8")
    if len(encoded) > FRIENDLY_NAME_MAX_LEN:
        cleaned = encoded[:FRIENDLY_NAME_MAX_LEN].decode("utf-8", "ignore").rstrip()
    return cleaned


def _looks_binary(content: str) -> bool:
    """Return True when *content* holds control/replacement chars no text YAML carries."""
    return _BINARY_CONTENT_RE.search(content.translate(_YAML_SAFE_WHITESPACE)) is not None


def _rewrite_required_yaml_leaf(
    content: str,
    leaf_path: Sequence[str],
    new_value: str,
) -> str:
    """
    Rewrite the leaf scalar at *leaf_path* in *content*; raise if missing.

    Wrapper around :func:`rewrite_name_or_substitution` that
    rejects missing leaves rather than silently no-op'ing. The
    clone path needs the rejection: a missing in-file
    ``esphome.name`` would otherwise let the clone keep the
    source's hostname and collide on mDNS. The error message
    points at both fixes (add directly, or edit the included
    source) since the leaf can be missing for genuine-absence
    or packages-defined reasons.
    """
    if read_yaml_scalar(content, leaf_path) is None:
        leaf_dotted = ".".join(leaf_path)
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"No {leaf_dotted} line found in this YAML; add one "
            "directly, or edit the package / !include where it's "
            "defined.",
        )
    return rewrite_name_or_substitution(content, leaf_path, new_value)


def _validate_archive_configuration(configuration: str) -> None:
    """
    Reject anything that isn't a pure basename.

    Defense-in-depth at the public archive / unarchive /
    delete_archived boundary; the helpers build paths like
    ``<config_dir>/archive/<configuration>`` directly. A value
    containing path separators or ``..`` segments would resolve
    outside the archive dir and let the caller unlink an
    arbitrary file. ``resolve_storage_path`` collapses to the
    basename so even traversal-into-storage would only land at
    ``<basename>.json``, but a ``../etc/passwd``-style value
    would still let an attacker target an attacker-named
    sidecar inside ``data_dir/storage``. Rejecting non-basenames
    closes both gaps at the WS boundary.
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

    ESPHome accepts pins as either a bare int / string label or
    a ``{number, mode, inverted, ...}`` mapping; returning the
    inner ``number`` lets locked / suggestion checks treat both
    shapes equivalently. ``bool`` is excluded since it's an
    ``int`` subclass.
    """
    if isinstance(value, dict):
        number = value.get("number")
        if isinstance(number, (int, str)) and not isinstance(number, bool):
            return number
    return value


def _apply_featured_presets(
    record: _FeaturedRecord,
    user_fields: dict[str, Any],
    underlying: ComponentCatalogEntry,
) -> dict[str, Any]:
    """
    Merge a featured component's presets onto *user_fields*.

    Returns a new field map; raises ``ValueError`` when
    *user_fields* violates a preset constraint. *underlying* is
    the hydrated body the record's ``underlying_id`` resolves to —
    callers fetch it via :meth:`ComponentCatalog.get_body` before
    invoking this helper.

    Per-field semantics:

    - **Locked**: user must omit the key or supply the locked
      value verbatim.
    - **Suggestions**: user value must be one of the listed
      values; omission falls back to the preset's ``value``.
    - **Plain default**: filled in only when the user didn't
      supply one.

    After the merge, fields that just echo their catalog
    ``default_value`` are stripped (a deliberate override
    survives). Pin-typed fields normalise to their bare GPIO
    for comparison so the manifest's ``suggestions: [4, 5]``
    accepts either the bare or the mapping shape the frontend
    might submit.
    """
    entries_by_key = {ce.key: ce for ce in underlying.config_entries}
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
            # Schema validation rejects ``locked: true`` without a
            # value; runtime guard here so a malformed manifest
            # fails fast instead of "locked to None".
            if preset.value is None:
                msg = (
                    f"Featured component {record.full_id} field '{key}' has "
                    f"locked=true without a value; board manifest is malformed"
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
    keep_unconditional.update(ce.key for ce in underlying.config_entries if ce.required)
    return _strip_default_echoes(merged, entries_by_key, keep_unconditional)


def _strip_default_echoes(
    fields: dict[str, Any],
    entries_by_key: dict[str, ConfigEntry],
    keep_unconditional: set[str],
) -> dict[str, Any]:
    """
    Drop fields that just echo their catalog default back.

    Unknown keys (no catalog entry) and entries with no
    ``default_value`` ride through; we have no baseline to call
    them an echo of, and silently dropping a typo would mask
    input rather than failing visibly.
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
    """
    Return True when *value* is just an unmodified echo of *default*.

    Plain ``==`` first (bool/bool, str/str, int/int), then a
    stringified compare for the cross-type case where the
    catalog stores numeric / time-period defaults as strings
    (``'2.8'``, ``'0 us'``) while the frontend submits parsed
    scalars. Containers are never treated as echoes.
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

    Fields that gate on a separate top-level component (``mqtt:``,
    ``web_server:``, ``zigbee:``, ...) are dropped when that block
    isn't configured. The component currently being added counts
    as configured, so adding ``mqtt`` itself with
    ``discovery: true`` keeps the discovery field.

    Recurses into nested dict fields so sub-fields with their
    own ``depends_on_component`` get the same gate. Top-level
    blocks contributed by ``packages:`` aren't detected since
    the scan is regex-based on the file text rather than a full
    package merge.
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

    # mDNS hostnames are case-insensitive and may carry a
    # trailing dot; normalise once so the CLI cache key matches
    # what it'll look up.
    normalized = normalize_hostname(address)
    is_local = is_local_hostname(address)

    # Preferred source per host type:
    #   .local      -> zeroconf cache (mDNS-only, freshest while
    #                  the browser is alive)
    #   non-.local  -> DNS cache populated by the ping sweep's
    #                  pre-resolve pass
    # Either falls back to ``device.ip`` (last-known resolved IP)
    # so an expired cache entry doesn't strip cache args entirely.
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
