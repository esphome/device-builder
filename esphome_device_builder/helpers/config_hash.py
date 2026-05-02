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

import asyncio
import logging
from pathlib import Path

from esphome.storage_json import StorageJSON

from .json import JSONDecodeError, loads

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
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, read_build_info_hash, yaml_path)


def read_build_info_hash(yaml_path: Path) -> str | None:
    """Read the canonical hash off disk synchronously.

    Public-by-convention so the device-scanner metadata resolver —
    which runs in a thread executor and needs the hash inline with
    the per-file board_id / ip lookups — can call it directly without
    re-entering the asyncio loop just to dispatch back to the same
    executor. Returns the same value ``compute_yaml_config_hash``
    awaits to.

    Resolves ``StorageJSON`` from ``<yaml_dir>/.esphome/storage/<name>.json``
    instead of ``ext_storage_path``. ``ext_storage_path`` is a thin
    wrapper around ``CORE.data_dir`` that crashes when CORE hasn't
    been initialised — fine in production (the dashboard sets
    ``CORE.config_path`` on startup) but a footgun in tests, where
    leaving the helper coupled to global CORE state would force
    every test fixture to spin up a CORE just to read a JSON file.
    """
    config_dir = yaml_path.parent
    storage_path = config_dir / ".esphome" / "storage" / f"{yaml_path.name}.json"
    storage = StorageJSON.load(storage_path)
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
