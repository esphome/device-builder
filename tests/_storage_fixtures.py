"""
Shared StorageJSON sidecar fixtures for tests that touch the build-output cache.

Several tests stand up a fake ``<config_dir>/.esphome/storage/<configuration>.json``
sidecar to exercise paths that read it (``firmware/download``,
``DevicesController._delete_single`` / ``_archive_single``, the
metadata resolver). They were each writing the same JSON shape
inline; centralising the layout here keeps them in sync when
upstream esphome bumps ``StorageJSON``'s schema.

Currently consumed by ``tests/controllers/firmware/test_download.py``.
The duplicates in ``tests/test_archive_device.py`` and
``tests/test_delete_device.py`` will migrate over once
``device-builder#132`` lands (those files are renamed by that PR
and editing them in parallel would conflict).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Schema-snapshot defaults — what an upstream
# ``StorageJSON.save()`` would land on disk for a typical
# esp32-c3-devkitm-1 ESP-IDF build. Kept verbose so a test that
# needs to override one field doesn't have to learn the full
# shape.
_STORAGE_DEFAULTS: dict[str, Any] = {
    "storage_version": 1,
    "name": None,  # filled from ``configuration`` stem when omitted
    "comment": None,
    "esphome_version": "2026.5.0-dev",
    "src_version": 1,
    "address": "",
    "web_port": None,
    "esp_platform": "esp32",
    "board": "esp32-c3-devkitm-1",
    "build_path": None,  # filled from ``tmp_path/.esphome/build/<stem>`` when omitted
    "firmware_bin_path": None,
    "loaded_integrations": [],
    "loaded_platforms": [],
    "no_mdns": False,
    "framework": "esp-idf",
    "core_platform": "esp32",
}


def write_storage_json(
    tmp_path: Path,
    configuration: str,
    *,
    firmware_bin_path: Path | None = None,
    build_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Path:
    """
    Write a StorageJSON sidecar for *configuration* under *tmp_path*.

    Returns the sidecar path so the test can wipe it for "missing
    sidecar" cases. Mirrors ``ext_storage_path``'s layout
    (``<tmp_path>/.esphome/storage/<configuration>.json``) so a
    monkeypatched redirect of ``ext_storage_path`` to ``tmp_path``
    lands on the right file.

    ``firmware_bin_path`` is the typical override knob — pass
    ``None`` (the default) to model "compile aborted before link",
    pass a real path to model "compile finished and produced this
    binary". ``build_path`` defaults to
    ``<tmp_path>/.esphome/build/<stem>``; override to pin a
    different location. Anything else (``loaded_integrations``,
    ``framework``, ``board``, …) goes through *overrides* — fields
    not listed there fall through to the defaults above.
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    sidecar = storage_dir / f"{configuration}.json"

    stem = Path(configuration).stem
    payload = dict(_STORAGE_DEFAULTS)
    payload["name"] = stem
    payload["build_path"] = str(build_path or (tmp_path / ".esphome" / "build" / stem))
    payload["firmware_bin_path"] = str(firmware_bin_path) if firmware_bin_path else None
    if overrides:
        payload.update(overrides)

    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    return sidecar
