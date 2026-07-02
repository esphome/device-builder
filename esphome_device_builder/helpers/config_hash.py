"""
Read the ``config_hash`` of a freshly-built device.

ESPHome internally hashes a fully-resolved-and-sorted dump of the
config (FNV-1a 32-bit) and exposes it as ``CORE.config_hash``. The
running firmware exposes the same value via ``App.get_config_hash()``,
which esphome/esphome#16145 also publishes on the
``_esphomelib._tcp`` mDNS service as the ``config_hash`` TXT record.

The hash is sensitive to *post-codegen* state — each component's
``to_code`` runs after validation and can mutate the config
(id-pinning, default backfill, normalisation), and ``CORE.config_hash``
is read in ``writer.get_build_info`` after that pass has run. So a
naive "rerun ``read_config`` and read the property" produces a value
that disagrees with the firmware's broadcast — verified empirically
on real Apollo float-monitor configs (pre-codegen ``f3e21d5a`` vs
firmware's ``5a94a12d``).

Rather than reproduce the codegen pipeline ourselves (heavyweight,
fragile across ESPHome upgrades), we read the authoritative value
out of ``<build_path>/build_info.json``. ESPHome writes that file
after every successful build with the canonical hash. The
device-builder sidecar persists the value across cleans, so a wiped
build directory still has a recent value to fall back to until the
next compile rewrites it.

Returns the hash as an 8-char lowercase hex string — matching the
mDNS TXT record shape. ``None`` on miss / parse failure;
``compute_has_pending_changes`` then falls back to the mtime check.
"""

from __future__ import annotations

import logging
from pathlib import Path

from esphome.storage_json import StorageJSON

from .async_ import run_in_executor
from .json import JSONDecodeError, loads
from .storage_path import resolve_storage_path

_LOGGER = logging.getLogger(__name__)

_BUILD_INFO_FILENAME = "build_info.json"


async def compute_yaml_config_hash(yaml_path: Path) -> str | None:
    """
    Return the 8-char lowercase hex ``config_hash`` for *yaml_path*.

    Resolves the device's build directory via the ESPHome
    ``StorageJSON`` sidecar and reads ``build_info.json`` from
    there. Returns ``None`` when the device has never been built,
    the build directory was wiped, or the file is corrupt — callers
    should treat ``None`` as "keep the previously-stored hash"
    (typically already in our metadata sidecar) rather than an
    error to propagate.
    """
    return await run_in_executor(read_build_info_hash, yaml_path)


def read_build_info_hash(yaml_path: Path) -> str | None:
    """Read the canonical hash off disk synchronously.

    Public-by-convention so the device-scanner metadata resolver —
    which runs in a thread executor and needs the hash inline with
    the per-file board_id / ip lookups — can call it directly without
    re-entering the asyncio loop just to dispatch back to the same
    executor. Returns the same value ``compute_yaml_config_hash``
    awaits to.

    Resolves the ``StorageJSON`` sidecar through ``ext_storage_path``
    so the helper honours ``CORE.data_dir``'s deployment-mode logic:
    ``/data/storage/...`` on the Home Assistant addon, the
    ``ESPHOME_DATA_DIR`` env var when set,
    ``<config_dir>/.esphome/storage/...`` otherwise. The earlier
    hardcoded ``<yaml_dir>/.esphome/...`` path matched only the
    default mode and silently returned ``None`` on every addon
    install, so the drawer's Local hash stayed empty and
    ``compute_has_pending_changes`` flipped the orange dot on
    for every device. CORE must be initialised by the time this
    runs — tests that exercise the helper set ``CORE.config_path``
    on a tmp_path sentinel so ``data_dir`` resolves into the same
    fixture tree the storage sidecar was written into.
    """
    storage = StorageJSON.load(resolve_storage_path(yaml_path.name))
    if storage is None or storage.build_path is None:
        return None
    build_info_path = Path(storage.build_path) / _BUILD_INFO_FILENAME
    try:
        raw = build_info_path.read_bytes()
    except FileNotFoundError:
        # Fresh device or post-clean — nothing to read.
        return None
    except OSError:
        _LOGGER.warning("Could not read %s", build_info_path, exc_info=True)
        return None
    try:
        data = loads(raw)
    except JSONDecodeError:
        _LOGGER.warning("build_info.json at %s is corrupt — ignoring", build_info_path)
        return None
    config_hash = data.get("config_hash") if isinstance(data, dict) else None
    if not isinstance(config_hash, int):
        return None
    # ESPHome stores the hash as a 32-bit unsigned int. Format to the
    # 8-char lowercase hex shape the mDNS TXT record carries so the
    # comparison against ``deployed_config_hash`` is a straight
    # string equality.
    return f"{config_hash & 0xFFFFFFFF:08x}"
