"""Component catalog controller package."""

from __future__ import annotations

from ._resolve import INTERNAL_COMPONENT_IDS, _FeaturedRecord, _load_body_from_disk
from .controller import ComponentCatalog, variant_to_key

__all__ = [
    "INTERNAL_COMPONENT_IDS",
    "ComponentCatalog",
    "_FeaturedRecord",
    "_load_body_from_disk",
    "variant_to_key",
]
