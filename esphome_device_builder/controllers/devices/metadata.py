"""Device-metadata resolution + sidecar-write mixin for ``DevicesController``."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.build_size import coerce_sidecar_int
from ...helpers.config_hash import read_build_info_hash
from ...helpers.device_yaml import parse_platform_from_yaml
from .._device_scanner import DeviceFileMetadata
from ..config import get_device_metadata, set_device_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from ...device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)


class DeviceMetadataMixin:
    """
    Metadata resolution + persistence methods for ``DevicesController``.

    Mixed in directly on ``DevicesController`` since every method
    only reads ``self._db`` plus same-mixin methods; no reach into
    other controller state. Test instance-patches on
    ``_persist_device_metadata_async`` / ``_derive_board_id_from_yaml``
    work as before because the methods are real attributes on the
    instance via inheritance.
    """

    # Type stub for mypy: the host controller injects ``_db``. Declared
    # under TYPE_CHECKING so the mixin doesn't shadow the host attribute
    # at import time.
    if TYPE_CHECKING:
        _db: DeviceBuilder

    def _resolve_device_metadata(self, config_dir: Path, filename: str) -> DeviceFileMetadata:
        """
        Resolve a device's persisted ``board_id`` / ``ip`` / config hash / MAC.

        ``board_id`` priority:
          1. The metadata sidecar — set explicitly when the user
             picks a board through the UI, or backfilled by a
             previous scan.
          2. Parse the YAML's ``esphome.platform`` / ``board`` /
             ``variant`` and match by PlatformIO board id
             (``find_by_pio_board``).
          3. Same YAML — match by platform + variant
             (``find_by_platform_variant``). Picks up generic
             ``esp32: { variant: esp32c3 }``-style configs that don't
             name a specific PlatformIO ``board:``. Generic catalog
             entries are preferred so the dashboard tags these with
             the matching ``generic-esp32-c3`` rather than a random
             vendor board that shares the variant.

        On any successful YAML-derived match we persist the result to
        metadata so subsequent scans skip the YAML parse.

        ``ip`` is the last-known resolved address from the metadata
        sidecar (``""`` if never seen).

        ``expected_config_hash`` is read from
        ``<build_path>/build_info.json`` — ESPHome's authoritative
        post-codegen value. The metadata sidecar is consulted *only*
        as a fallback for devices whose build directory was wiped
        (clean) but where we'd previously cached a value. Reading
        from ``build_info.json`` first keeps the dashboard from
        getting stuck on a stale sidecar value if a previous run
        wrote a wrong hash (e.g. the pre-codegen subprocess hash
        the dashboard used to compute) — the next scan after this
        change picks up the canonical value automatically.

        ``mac_address`` is the canonical ``XX:XX:XX:XX:XX:XX`` form
        last observed on the device's mDNS ``mac`` TXT, persisted
        to the sidecar so the dashboard renders the value
        immediately on restart (ESPHome devices are mDNS-silent
        until probed). Empty when the device hasn't been seen yet
        — the next mDNS announcement repopulates via
        :meth:`_on_mac_address_change`. The derived
        ``ethernet_mac`` / ``bluetooth_mac`` are recomputed by
        :func:`derive_interface_macs` at ``Device`` construction
        time, not stored in the sidecar.
        """
        md = get_device_metadata(config_dir, filename)
        ip = str(md.get("ip", ""))
        # build_info.json wins; sidecar is the post-clean fallback.
        expected_config_hash = read_build_info_hash(config_dir / filename) or str(
            md.get("expected_config_hash", "")
        )
        board_id = str(md.get("board_id", ""))
        if not board_id:
            board_id = self._derive_board_id_from_yaml(config_dir, filename)
        mac_address = str(md.get("mac_address", ""))
        # ``coerce_sidecar_int`` handles the bad-data fall-throughs
        # (``None`` / object / decimal-string / etc.) — same
        # defensive shape used by the build-size cache reads in
        # ``helpers/build_size.py``. The metadata resolver is on
        # the scanner's per-device hot path; a single corrupt
        # entry shouldn't fail the whole scan.
        build_size_bytes = coerce_sidecar_int(md.get("build_size_bytes"))
        # Defensive filter: a hand-edited sidecar could land non-string
        # entries in the labels list. The scanner is on the hot path,
        # so silently drop bad entries rather than failing the whole
        # device load.
        raw_labels = md.get("labels")
        labels: tuple[str, ...]
        if isinstance(raw_labels, list):
            labels = tuple(item for item in raw_labels if isinstance(item, str))
        else:
            labels = ()
        return DeviceFileMetadata(
            board_id=board_id,
            ip=ip,
            expected_config_hash=expected_config_hash,
            mac_address=mac_address,
            build_size_bytes=build_size_bytes,
            labels=labels,
        )

    def _derive_board_id_from_yaml(self, config_dir: Path, filename: str) -> str:
        """Parse the device YAML and look up a matching catalog board, or ``""``."""
        if self._db.boards is None:
            return ""
        yaml_path = config_dir / filename
        try:
            yaml_content = yaml_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        platform, pio_board, variant = parse_platform_from_yaml(yaml_content)

        matched = None
        if pio_board:
            matched = self._db.boards.find_by_pio_board(pio_board, variant)
        if matched is None and platform:
            matched = self._db.boards.find_by_platform_variant(platform, variant)
        if matched is None:
            return ""

        # Backfill metadata so future scans skip the YAML parse.
        try:
            set_device_metadata(config_dir, filename, board_id=matched.id)
        except Exception:
            _LOGGER.warning("Could not persist derived board_id for %s", filename)
        return matched.id

    async def _persist_device_ip_async(self, configuration: str, ip: str) -> None:
        """Save *ip* to the device-builder metadata sidecar."""
        await self._persist_device_metadata_async(configuration, ip=ip)

    async def _persist_device_metadata_async(self, configuration: str, **fields: Any) -> None:
        """
        Run a blocking ``set_device_metadata`` write on the default executor.

        Centralises the ``run_in_executor`` + ``config_dir`` lookup
        boilerplate that every async-context sidecar write was
        repeating.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(
            None, lambda: set_device_metadata(config_dir, configuration, **fields)
        )
