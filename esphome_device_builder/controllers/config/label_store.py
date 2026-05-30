"""Global label catalog + per-device label assignment persistence."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...models import Label
from .metadata import _load_metadata, metadata_transaction

_LOGGER = logging.getLogger(__name__)

_LABELS_KEY = "_labels"


def _decode_labels(raw: Any) -> list[Label]:
    """Parse the on-disk ``_labels`` list, dropping malformed entries."""
    if not isinstance(raw, list):
        return []
    out: list[Label] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(Label.from_dict(entry))
        except (ValueError, TypeError, LookupError) as err:
            # A hand-edited sidecar that landed a malformed entry
            # shouldn't take the whole catalog down — labels are
            # advisory. Debug-log so a developer hunting "why did
            # my label disappear?" can find a paper trail without
            # noisy WARN-level chatter on every load.
            _LOGGER.debug("Skipping malformed label entry %r: %s", entry, err)
    return out


def load_labels(config_dir: Path) -> list[Label]:
    """
    Load the global label catalog.

    Returns an empty list when the ``_labels`` key is missing or
    corrupt. Individual entries that fail to round-trip through
    :class:`Label` are skipped silently so a single bad entry can't
    take the whole catalog down — labels are advisory metadata, not
    load-bearing state.
    """
    return _decode_labels(_load_metadata(config_dir).get(_LABELS_KEY, []))


def save_labels(config_dir: Path, labels: list[Label]) -> None:
    """Replace the global label catalog atomically."""
    with metadata_transaction(config_dir) as data:
        data[_LABELS_KEY] = [label.to_dict() for label in labels]


@contextmanager
def labels_transaction(config_dir: Path) -> Iterator[list[Label]]:
    """
    Atomic read-modify-write context for the global label catalog.

    Yields a mutable list of :class:`Label` instances decoded from the
    ``_labels`` key. Mutate the list in place; on a clean exit the
    catalog is re-encoded and persisted atomically alongside the rest
    of the metadata file. Exceptions raised inside the block discard
    the pending mutation. Use when you need uniqueness / existence
    checks and the write to share a single transaction — the validate
    happens inside the lock so a concurrent writer can't slip in
    between.
    """
    with metadata_transaction(config_dir) as data:
        catalog = _decode_labels(data.get(_LABELS_KEY))
        yield catalog
        data[_LABELS_KEY] = [label.to_dict() for label in catalog]


def set_device_labels(config_dir: Path, configuration: str, label_ids: list[str]) -> None:
    """
    Replace a device's label assignments atomically.

    Validates *label_ids* against the live catalog inside the same
    metadata transaction as the write so a concurrent
    ``labels/delete`` cascade can't leave the device with a
    dangling reference. ``label_ids`` is treated as a set in
    semantics — duplicate IDs in the input are deduplicated while
    preserving first-seen order. Pass ``[]`` to clear all
    assignments. Raises :class:`ValueError` for non-string entries
    in *label_ids* and for ids that aren't in the catalog (caller
    translates to ``CommandError(INVALID_ARGS)`` at the API surface).
    """
    deduped: list[str] = []
    seen: set[str] = set()
    for lid in label_ids:
        if not isinstance(lid, str):
            # Silent skipping would let a payload of all-bad types
            # become an effective ``[]`` (clear-all) write — surprising
            # and user-hostile. Surface a clear error instead so the
            # frontend can fix the payload.
            raise TypeError(f"label_ids must be strings, got {type(lid).__name__}: {lid!r}")
        if lid in seen:
            continue
        deduped.append(lid)
        seen.add(lid)
    with metadata_transaction(config_dir) as data:
        catalog = data.get(_LABELS_KEY, [])
        if isinstance(catalog, list):
            known = {
                entry["id"]
                for entry in catalog
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            }
        else:
            known = set()
        unknown = [lid for lid in deduped if lid not in known]
        if unknown:
            raise ValueError(f"Unknown label id(s): {', '.join(repr(u) for u in unknown)}")
        entry = data.setdefault(configuration, {})
        if not isinstance(entry, dict):
            # A non-dict entry shouldn't survive long — overwrite to
            # restore the expected shape rather than crash here.
            entry = {}
            data[configuration] = entry
        if deduped:
            entry["labels"] = deduped
        else:
            entry.pop("labels", None)


def delete_label_cascade(config_dir: Path, label_id: str) -> tuple[bool, set[str]]:
    """
    Drop *label_id* from the catalog and every device entry.

    Performed inside a single ``metadata_transaction`` so the
    existence check, catalog removal, and per-device cleanup all
    share one lock — a concurrent writer can't reintroduce the
    deleted ID, and the existence check works against the *raw*
    on-disk dict so a corrupt catalog entry (one that
    ``Label.from_dict`` would skip) is still removable.

    Returns a tuple ``(found, affected)`` where *found* is ``True``
    when the catalog actually contained an entry with this id (so
    the caller can raise ``NOT_FOUND`` otherwise) and *affected* is
    the set of configuration filenames whose ``labels`` list
    changed — callers use this to schedule a per-device scanner
    reload so live ``Device`` objects pick up the cleaned state
    without having to wait for the next disk-driven scan.
    """
    affected: set[str] = set()
    found = False
    with metadata_transaction(config_dir) as data:
        existing = data.get(_LABELS_KEY)
        if isinstance(existing, list):
            new_catalog: list[Any] = []
            for entry in existing:
                if isinstance(entry, dict) and entry.get("id") == label_id:
                    found = True
                    continue
                new_catalog.append(entry)
            if found:
                data[_LABELS_KEY] = new_catalog
        if not found:
            return False, set()
        for filename, entry in data.items():
            if filename.startswith("_") or not isinstance(entry, dict):
                continue
            current = entry.get("labels")
            if not isinstance(current, list) or label_id not in current:
                continue
            new = [lid for lid in current if lid != label_id]
            if new:
                entry["labels"] = new
            else:
                entry.pop("labels", None)
            affected.add(filename)
    return True, affected
