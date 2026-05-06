"""User-defined label models."""

from __future__ import annotations

from dataclasses import dataclass

from mashumaro.mixins.orjson import DataClassORJSONMixin


@dataclass
class Label(DataClassORJSONMixin):
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
