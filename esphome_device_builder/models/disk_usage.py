"""Disk usage models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from .common import DashboardModel


@dataclass
class DiskUsage(DashboardModel):
    """
    Disk space on the volume holding the ESPHome data dir, and what ESPHome uses of it.

    The ``*_bytes`` categories are disjoint: a file is counted once, in the
    category whose root is nearest to it (``<data_dir>/build`` counts as builds,
    not cache).
    """

    # Fullest volume holding ``CORE.data_dir`` (``/data`` on the HA add-on) or a build root.
    total_bytes: int
    free_bytes: int
    # The ``ESPHOME_BUILD_PATH`` root (orphans included) and per-device ``build_path``s.
    builds_bytes: int
    # ``.remote_builds`` under the data and config dirs: build-server work for peers.
    remote_builds_bytes: int
    # PlatformIO and ESPHome toolchain caches: what Reset build environment wipes.
    toolchains_bytes: int
    # The rest of ``<data_dir>`` (external components, PlatformIO project cache).
    cache_bytes: int
    # The config dir outside ``<data_dir>``: YAML, secrets, version history.
    config_bytes: int
    # Unix time the category sizes were measured.
    measured_at: float


class DiskUsageEventData(TypedDict):
    """Payload for ``EventType.DISK_USAGE_UPDATED``."""

    disk_usage: DiskUsage
