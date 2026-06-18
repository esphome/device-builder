"""User-defined label models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from .common import DashboardModel


@dataclass
class Label(DashboardModel):
    """
    A user-defined label that can be assigned to devices.

    Stored in ``.device-builder.json`` under the ``_labels`` key and
    referenced from each device entry by ``id``. Renaming or recoloring
    a label leaves device assignments untouched — only the catalog
    entry changes.
    """

    # Opaque ``uuid.uuid4().hex`` generated server-side. Stable across
    # name and color edits.
    id: str
    # Display name. Trimmed before save; uniqueness is enforced
    # case-insensitively.
    name: str
    # ``#rrggbb`` (lowercase). ``None`` lets the frontend pick a
    # theme-default chip color.
    color: str | None = None


# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is
# type-checked at the call site without changing the wire shape).
# See ``mypy_plan.md`` for the migration scope.
# ---------------------------------------------------------------------------


class LabelEventData(TypedDict):
    """
    Payload for ``EventType.LABEL_CREATED`` / ``LABEL_UPDATED``.

    Both creation and update events carry the full ``Label`` so
    the frontend's catalog renderer has the canonical form
    (server-trimmed name, lowercased ``#rrggbb`` color, opaque
    id) without an additional fetch. Subscribers differentiate by
    the ``EventType`` carried alongside.
    """

    label: Label


class LabelDeletedData(TypedDict):
    """
    Payload for ``EventType.LABEL_DELETED``.

    Carries only the deleted label's id — the catalog row is
    already gone by the time the event fires, and clients track
    labels by id. Per-device label assignment cascades fire
    ``DEVICE_UPDATED`` events for each affected device alongside
    this one.
    """

    label_id: str
