"""Labels controller — global catalog of user-defined device labels."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import TYPE_CHECKING, Any

from ..helpers.api import CommandError, api_command
from ..helpers.async_ import run_in_executor
from ..models import ErrorCode, EventType, Label, LabelDeletedData, LabelEventData
from .config import (
    delete_label_cascade,
    labels_transaction,
    load_labels,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Sentinel that distinguishes "leave color unchanged" (parameter
# absent from the WS payload) from "clear the color" (caller
# explicitly passed ``null``). ``None`` alone can't carry both
# meanings — see ``update_label`` for the resulting code shape.
_UNSET: Any = object()

# Keep the regex case-insensitive at the match level but normalize
# to lowercase before save so the on-disk shape is canonical.
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_NAME_MAX_LEN = 50


def _validate_name(name: Any) -> str:
    """
    Trim and validate a label name. Returns the canonical form.

    Names are trimmed before storage so a stray space doesn't slip past
    the uniqueness check. Empty / whitespace-only and over-long names
    are user errors.
    """
    if not isinstance(name, str):
        raise CommandError(ErrorCode.INVALID_ARGS, "Label name must be a string")
    trimmed = name.strip()
    if not trimmed:
        raise CommandError(ErrorCode.INVALID_ARGS, "Label name must not be empty")
    if len(trimmed) > _NAME_MAX_LEN:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"Label name must be {_NAME_MAX_LEN} characters or fewer",
        )
    return trimmed


def _validate_color(color: Any) -> str | None:
    """Normalize an optional color to lowercase ``#rrggbb`` or ``None``."""
    if color is None:
        return None
    if not isinstance(color, str) or not _COLOR_RE.fullmatch(color):
        raise CommandError(ErrorCode.INVALID_ARGS, "Label color must be #rrggbb hex or null")
    return color.lower()


def _check_name_unique(catalog: list[Label], name: str, *, exclude_id: str | None = None) -> None:
    """Raise ``CommandError(INVALID_ARGS)`` if *name* collides with an existing label.

    Comparison is case-insensitive: ``"Kitchen"`` and ``"kitchen"`` are
    treated as the same name. The caller's preferred case is preserved
    on save — this only blocks the conflict.
    """
    target = name.lower()
    for label in catalog:
        if exclude_id is not None and label.id == exclude_id:
            continue
        if label.name.lower() == target:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Label name {name!r} already exists")


class LabelsController:
    """
    Manages the global label catalog.

    Labels are user-defined chips (name + optional color) that can be
    assigned to devices via ``devices/set_labels``. The catalog itself
    lives at the ``_labels`` key in ``.device-builder.json`` alongside
    per-device entries and ``_preferences``; assignments are stored
    on each device entry's ``labels`` field as a list of label IDs.

    All CRUD operations validate against the catalog *inside* the
    metadata transaction so a concurrent writer (e.g. another
    ``labels/create`` racing on uniqueness, or a ``labels/delete``
    cascading while ``devices/set_labels`` validates IDs) can't sneak
    past the lock.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder

    @api_command("labels/list")
    async def list_labels(self, **kwargs: Any) -> list[Label]:
        """Return every label in the global catalog."""
        return await run_in_executor(load_labels, self._db.settings.config_dir)

    @api_command("labels/create")
    async def create_label(
        self,
        *,
        name: str,
        color: str | None = None,
        **kwargs: Any,
    ) -> Label:
        """
        Create a new label.

        ``name`` is required; ``color`` is optional ``#rrggbb`` hex
        (case-insensitive on input, lowercased on save). Label names
        are unique case-insensitively. The server generates the
        opaque ``id``; clients should treat it as a stable handle.
        """
        clean_name = _validate_name(name)
        clean_color = _validate_color(color)
        new_id = uuid.uuid4().hex

        config_dir = self._db.settings.config_dir

        def _persist() -> Label:
            with labels_transaction(config_dir) as catalog:
                _check_name_unique(catalog, clean_name)
                created = Label(id=new_id, name=clean_name, color=clean_color)
                catalog.append(created)
                return created

        label = await asyncio.to_thread(_persist)
        self._db.bus.fire(EventType.LABEL_CREATED, LabelEventData(label=label))
        return label

    @api_command("labels/update")
    async def update_label(
        self,
        *,
        label_id: str,
        name: str | None = None,
        color: Any = _UNSET,
        **kwargs: Any,
    ) -> Label:
        """
        Update a label's name and / or color.

        Pass ``name`` to rename. Pass ``color`` (including
        ``null``) to change or clear it; omit ``color`` from the
        request to leave it unchanged. At least one of ``name`` or
        ``color`` must be provided.
        """
        if name is None and color is _UNSET:
            raise CommandError(ErrorCode.INVALID_ARGS, "Pass at least one of name or color")
        clean_name = _validate_name(name) if name is not None else None
        clean_color: Any = _validate_color(color) if color is not _UNSET else _UNSET

        config_dir = self._db.settings.config_dir

        def _persist() -> Label:
            with labels_transaction(config_dir) as catalog:
                target_idx = next((i for i, lbl in enumerate(catalog) if lbl.id == label_id), -1)
                if target_idx < 0:
                    raise CommandError(ErrorCode.NOT_FOUND, f"Label {label_id!r} not found")
                current = catalog[target_idx]
                next_name = clean_name if clean_name is not None else current.name
                next_color = clean_color if clean_color is not _UNSET else current.color
                if next_name.lower() != current.name.lower():
                    _check_name_unique(catalog, next_name, exclude_id=label_id)
                updated = Label(id=label_id, name=next_name, color=next_color)
                catalog[target_idx] = updated
                return updated

        label = await asyncio.to_thread(_persist)
        self._db.bus.fire(EventType.LABEL_UPDATED, LabelEventData(label=label))
        return label

    @api_command("labels/delete")
    async def delete_label(self, *, label_id: str, **kwargs: Any) -> dict[str, bool]:
        """
        Delete a label.

        The deletion cascades: every device entry whose ``labels``
        list contains this id has the id removed in the same
        transaction, and a ``DEVICE_UPDATED`` event fires for each
        affected device after the live ``Device`` reloads from the
        sidecar. Returns ``{"deleted": True}`` on success; raises
        ``NOT_FOUND`` if the id wasn't in the catalog.

        The existence check runs inside ``delete_label_cascade``'s
        own transaction against the raw on-disk dict, so a corrupt
        catalog entry (one that wouldn't survive ``Label.from_dict``)
        is still removable.
        """
        config_dir = self._db.settings.config_dir

        found, affected = await asyncio.to_thread(delete_label_cascade, config_dir, label_id)
        if not found:
            raise CommandError(ErrorCode.NOT_FOUND, f"Label {label_id!r} not found")

        # Reload every affected device so the live ``Device`` model picks up
        # its trimmed labels list. Each reload fires its own
        # ``DEVICE_UPDATED`` via the scanner's existing scan-change
        # pipeline, so we don't refire here.
        devices = self._db.devices
        if devices is not None:
            for filename in affected:
                try:
                    await devices.reload_configuration(filename)
                except Exception:  # noqa: BLE001 — cascade is best-effort; one bad device must not block the others
                    _LOGGER.warning("Failed to reload device %s after label cascade", filename)

        self._db.bus.fire(EventType.LABEL_DELETED, LabelDeletedData(label_id=label_id))
        return {"deleted": True}
