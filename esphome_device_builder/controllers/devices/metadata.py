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

    Mixed in directly because every method only touches
    ``self._db`` plus same-mixin siblings; if a future change
    pulls in ``_scanner`` / ``_state_monitor`` / etc. the mixin
    pattern stops fitting and the methods should move to a
    sibling module taking ``controller`` as the first arg.
    """

    # Type stub for mypy: ``_db`` is supplied by the host class.
    if TYPE_CHECKING:
        _db: DeviceBuilder

    def _resolve_device_metadata(self, config_dir: Path, filename: str) -> DeviceFileMetadata:
        """
        Resolve a device's persisted ``board_id`` / ``ip`` / config hash / MAC.

        ``board_id`` priority: the sidecar (set by the user or
        backfilled by an earlier scan); else parse the YAML and
        match by PlatformIO ``board:`` (``find_by_pio_board``);
        else match by platform + variant
        (``find_by_platform_variant``), preferring generic
        catalog entries (``generic-esp32-c3`` over a random
        vendor board sharing the variant). YAML-derived hits
        backfill the sidecar so subsequent scans skip the parse.

        ``expected_config_hash`` reads ``build_info.json`` first
        and only falls back to the sidecar; the sidecar can
        carry a stale pre-codegen hash from older dashboard
        versions, and ``build_info.json`` is the authoritative
        post-codegen value.

        ``mac_address`` is the canonical mDNS-observed value
        persisted to the sidecar so the dashboard renders it
        immediately on restart (devices are mDNS-silent until
        probed).
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
        # Defensive coercion / filter on the scanner's hot path; a
        # single corrupt sidecar entry shouldn't fail the whole scan.
        build_size_bytes = coerce_sidecar_int(md.get("build_size_bytes"))
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
        """Run a blocking ``set_device_metadata`` write on the default executor."""
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir
        await loop.run_in_executor(
            None, lambda: set_device_metadata(config_dir, configuration, **fields)
        )
