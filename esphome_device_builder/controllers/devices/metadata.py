"""Device-metadata resolution + sidecar-write base class for ``DevicesController``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.build_size import coerce_sidecar_int
from ...helpers.config_hash import read_build_info_hash
from ...helpers.device_yaml import parse_platform_from_yaml
from .._device_builder_base import DeviceBuilderBase
from .._device_scanner import DeviceFileMetadata
from ._metadata_store import STORE_FIELDS

if TYPE_CHECKING:
    from ._metadata_store import DeviceMetadataStore
    from ._shared_sidecar import SharedSidecarClient

_LOGGER = logging.getLogger(__name__)


def _partition_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split *fields* into (store_fields, shared_fields); drop ``None`` values."""
    store: dict[str, Any] = {}
    shared: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        (store if key in STORE_FIELDS else shared)[key] = value
    return store, shared


class DeviceMetadataBase(DeviceBuilderBase):
    """Metadata resolution + persistence."""

    # Subclass (``DevicesController``) populates these in ``__init__``.
    _metadata_store: DeviceMetadataStore
    _shared_sidecar: SharedSidecarClient

    def _resolve_device_metadata(self, config_dir: Path, filename: str) -> DeviceFileMetadata:
        """Resolve identity (shared sidecar) + live state (store) for *filename*.

        ``expected_config_hash`` prefers ``build_info.json`` over
        the persisted value; older dashboard versions wrote a
        stale pre-codegen hash to the sidecar.
        """
        store_md = self._metadata_store.get(filename)
        shared_md = self._shared_sidecar.get_sync(filename)
        ip = str(store_md.get("ip", ""))
        expected_config_hash = read_build_info_hash(config_dir / filename) or str(
            store_md.get("expected_config_hash", "")
        )
        board_id = str(shared_md.get("board_id", ""))
        if not board_id:
            board_id = self._derive_board_id_from_yaml(config_dir, filename)
        mac_address = str(shared_md.get("mac_address", ""))
        # Defensive coercion: a corrupt sidecar entry shouldn't
        # fail the whole scan.
        build_size_bytes = coerce_sidecar_int(store_md.get("build_size_bytes"))
        raw_labels = shared_md.get("labels")
        labels: tuple[str, ...]
        if isinstance(raw_labels, list):
            labels = tuple(item for item in raw_labels if isinstance(item, str))
        else:
            labels = ()
        deployed_config_hash = str(store_md.get("deployed_config_hash", ""))
        deployed_version = str(store_md.get("deployed_version", ""))
        raw_api_encryption = store_md.get("api_encryption_active")
        api_encryption_active = raw_api_encryption if isinstance(raw_api_encryption, str) else None
        return DeviceFileMetadata(
            board_id=board_id,
            ip=ip,
            expected_config_hash=expected_config_hash,
            mac_address=mac_address,
            build_size_bytes=build_size_bytes,
            labels=labels,
            deployed_config_hash=deployed_config_hash,
            deployed_version=deployed_version,
            api_encryption_active=api_encryption_active,
        )

    def _derive_board_id_from_yaml(self, config_dir: Path, filename: str) -> str:
        """Parse the YAML, match a catalog board, backfill on cache miss.

        Backfills via ``set_device_metadata`` only when the
        shared sidecar lacks ``board_id`` so subsequent scans
        skip the YAML parse.
        """
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
            matched = self._db.boards.find_by_pio_board(pio_board, variant, platform)
        if matched is None and platform:
            matched = self._db.boards.find_by_platform_variant(platform, variant)
        if matched is None:
            return ""
        try:
            self._shared_sidecar.update_sync(filename, board_id=matched.id)
        except OSError:
            _LOGGER.warning("Could not persist derived board_id for %s", filename)
        return matched.id

    async def _persist_device_metadata_async(self, configuration: str, **fields: Any) -> None:
        """Route *fields* between the data_dir store and the shared sidecar."""
        store_fields, shared_fields = _partition_fields(fields)
        if store_fields:
            self._metadata_store.update(configuration, **store_fields)
        if shared_fields:
            await self._shared_sidecar.update(configuration, **shared_fields)

    async def _delete_device_metadata(self, configuration: str) -> None:
        """Drop the store entry + shared-sidecar entry; flush immediately."""
        await self._metadata_store.remove(configuration)
        await self._shared_sidecar.remove(configuration)

    async def _migrate_device_metadata(
        self, old_configuration: str, new_configuration: str
    ) -> None:
        """Move store + shared-sidecar entries from *old* to *new* filename.

        ``esphome rename`` swaps the YAML filename out; the
        filename-keyed sidecar identity fields (labels / comment /
        board_id) would otherwise be lost on the renamed device.
        """
        await self._metadata_store.rename(old_configuration, new_configuration)
        await self._shared_sidecar.rename(old_configuration, new_configuration)

    async def _clear_volatile_device_metadata(self, configuration: str) -> None:
        """Clear archive-volatile fields in both stores (keeps identity).

        Unlike :meth:`_delete_device_metadata`, the store side
        rides the default debounce — the YAML's in ``archive/``
        already and no live device matches it, so stale fields
        on disk are invisible until unarchive (where the next
        mDNS sweep corrects them).
        """
        self._metadata_store.clear_volatile(configuration)
        await self._shared_sidecar.clear_volatile(configuration)
