"""Per-device fields in the metadata sidecar (.device-builder.json)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...helpers.metadata_sidecar import _load_metadata, metadata_transaction


def get_board_id(config_dir: Path, filename: str) -> str:
    """Get the board_id for a device."""
    return str(_load_metadata(config_dir).get(filename, {}).get("board_id", ""))


def set_device_metadata(
    config_dir: Path,
    filename: str,
    *,
    board_id: str | None = None,
    board_id_user_set: bool | None = None,
    friendly_name: str | None = None,
    comment: str | None = None,
    ip: str | None = None,
    expected_config_hash: str | None = None,
    mac_address: str | None = None,
    build_size_bytes: int | None = None,
    build_size_dir_mtime: int | None = None,
    build_size_info_mtime: int | None = None,
    labels: list[str] | None = None,
) -> None:
    """
    Set metadata fields for a device.

    ``board_id_user_set`` marks ``board_id`` as a deliberate user pick
    rather than an auto-derived guess. Only ever written ``True``;
    absence means auto-derived.

    ``ip`` is the last-known resolved IP — persisted so the address
    cache survives backend restarts. Pass an empty string to leave the
    persisted value unchanged (mDNS clears the in-memory IP whenever a
    device drops off the network, but the cache is still useful).

    ``expected_config_hash`` is the 8-char hex FNV-1a-32 hash of the
    YAML as last successfully compiled — pair it with the mDNS
    ``config_hash`` TXT record (esphome/esphome#16145) to tell whether
    the running firmware matches the compiled config. Passing an empty
    string clears it (e.g. after a YAML edit invalidates the prior
    compile).

    ``mac_address`` is the canonical ``XX:XX:XX:XX:XX:XX`` MAC
    from the mDNS ``mac`` TXT record (normalized at ingest).
    Persisted so the dashboard renders the address immediately on
    startup, before the first mDNS probe response. Passing an
    empty string clears it.

    ``build_size_bytes`` caches the total size of the per-device
    ``.esphome/build/<name>/`` tree at the freshness pair
    captured by the last walk. The pair is split because each
    half catches a class of compile-time changes the other
    misses: ``build_size_dir_mtime`` moves on entry-set churn
    (PlatformIO atomic-replaces, sibling add/remove),
    ``build_size_info_mtime`` moves on every real ESPHome
    recompile (``write_file_if_changed`` rewrites
    ``build_info.json``). Either side moving counts as stale,
    so a freshly-restarted dashboard re-walks any device whose
    pair drifted from what was persisted. Pass ``0`` for any
    field to clear (used by the archive flow's volatile-field
    scrub).

    ``labels`` is the list of label IDs assigned to this device
    (opaque ``uuid.uuid4().hex`` references into the global
    ``_labels`` catalog). ``None`` leaves the persisted list
    alone; ``[]`` clears it (drops the key entirely so empty
    entries don't bloat the file); a populated list replaces
    the assignments wholesale.
    """
    with metadata_transaction(config_dir) as data:
        entry = data.setdefault(filename, {})
        if board_id is not None:
            entry["board_id"] = board_id
        if friendly_name is not None:
            entry["friendly_name"] = friendly_name
        if comment is not None:
            entry["comment"] = comment
        if ip:
            entry["ip"] = ip
        if labels is not None:
            if labels:
                entry["labels"] = list(labels)
            else:
                entry.pop("labels", None)
        # Tri-state fields: ``None`` means "leave alone", a truthy
        # value writes, an explicit falsy (``""`` / ``0``) clears.
        # The numeric ``build_size_*`` stamps carry timestamps or
        # sizes whose legitimate values are strictly positive —
        # ``0`` is therefore safe as the explicit-clear sentinel.
        # Loop over the (key, value) pairs so adding a new
        # tri-state field doesn't bump this function's branch
        # count (ruff PLR0912 caps at 12).
        for key, value in (
            ("board_id_user_set", board_id_user_set),
            ("expected_config_hash", expected_config_hash),
            ("mac_address", mac_address),
            ("build_size_bytes", build_size_bytes),
            ("build_size_dir_mtime", build_size_dir_mtime),
            ("build_size_info_mtime", build_size_info_mtime),
        ):
            if value is None:
                continue
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)


def get_device_metadata(config_dir: Path, filename: str) -> dict[str, Any]:
    """Get all metadata for a device."""
    result = _load_metadata(config_dir).get(filename, {})
    return result if isinstance(result, dict) else {}


def get_device_ip(config_dir: Path, filename: str) -> str:
    """Return the last-known resolved IP for a device, or ``""`` if unknown."""
    return str(_load_metadata(config_dir).get(filename, {}).get("ip", ""))


def remove_device_metadata(config_dir: Path, filename: str) -> None:
    """Remove metadata for a device."""
    with metadata_transaction(config_dir) as data:
        data.pop(filename, None)


def rename_device_metadata(config_dir: Path, old_filename: str, new_filename: str) -> None:
    """
    Move *old_filename*'s sidecar entry to *new_filename* in one transaction.

    Identity fields (``board_id`` / ``friendly_name`` / ``comment`` /
    ``labels``) are keyed by filename; a rename would otherwise orphan
    them and the renamed device would load with none. Pre-existing
    *new_filename* fields win on conflict so a concurrent scan-derived
    entry isn't clobbered.
    """
    if old_filename == new_filename:
        return
    with metadata_transaction(config_dir) as data:
        old_entry = data.pop(old_filename, None)
        if not isinstance(old_entry, dict) or not old_entry:
            return
        existing_new = data.get(new_filename)
        data[new_filename] = (
            {**old_entry, **existing_new} if isinstance(existing_new, dict) else old_entry
        )


# Per-device shared-sidecar fields that go stale on archive.
# After the per-device live state moved into the data-dir store,
# only ``mac_address`` remains in the shared sidecar with archive-
# volatile semantics: it's intrinsic to the physical board, but
# unarchive may rebind the YAML to a different board, so the
# cached MAC must clear. Everything else here is identity that
# survives archive.
_VOLATILE_DEVICE_METADATA_FIELDS: frozenset[str] = frozenset({"mac_address"})


def clear_volatile_device_metadata(config_dir: Path, filename: str) -> None:
    """Drop runtime / observed state fields, keep stable identity fields.

    On archive the dashboard removes the YAML's compile output
    and the StorageJSON sidecar (both are build artifacts), but
    the device-metadata entry carries a mix of:

    - Stable identity fields (``board_id``, ``friendly_name``,
      ``comment``) — set by the user or derived from the YAML
      itself, still meaningful on unarchive.
    - Volatile fields (``ip``, ``expected_config_hash``) —
      describe the firmware / network state at archive time and
      go stale immediately.

    The earlier shape removed the entire entry on archive, which
    closed the "future same-name device inherits stale state"
    risk but also lost the identity fields. The catalog → YAML
    match key is ``board_id``; losing it on every archive →
    unarchive cycle forced a re-derive (or a re-pick by the
    user) that wasn't necessary. This helper preserves identity
    + clears volatile so unarchive restores the user-visible
    state unchanged. Same-name new-device leakage of identity
    fields is acceptable: the new device's create flow either
    derives or supplies its own ``board_id``, and friendly_name
    / comment are user labels the new device's editor can
    overwrite if desired.
    """
    with metadata_transaction(config_dir) as data:
        entry = data.get(filename)
        if entry is None:
            return
        if not isinstance(entry, dict):
            # Treat a non-dict value as corrupt — leaving it in place
            # would later break ``set_device_metadata`` (which assumes
            # the existing entry is a dict and item-assigns into it).
            # Drop the bad value so the next write starts from a
            # clean shape.
            data.pop(filename, None)
            return
        for field_name in _VOLATILE_DEVICE_METADATA_FIELDS:
            entry.pop(field_name, None)
        # If the entry is now empty (no identity fields ever
        # set) drop it entirely so we don't leave dead keys
        # behind in the metadata file.
        if not entry:
            data.pop(filename, None)
