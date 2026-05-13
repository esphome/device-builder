"""Base class for controllers that hold the shared ``DeviceBuilder`` ref."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder


class DeviceBuilderBase:
    """Owns ``self._db`` so subclasses don't reach for it in their own ``__init__``."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
