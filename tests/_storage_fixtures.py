"""
Shared StorageJSON sidecar fixtures for tests that touch the build-output cache.

Several tests stand up a fake ``<config_dir>/.esphome/storage/<configuration>.json``
sidecar to exercise paths that read it (``firmware/download``,
``DevicesController._delete_single`` / ``_archive_single``, the
metadata resolver, the config-hash helpers, and the device archive
listing). They were each writing the same JSON shape inline;
centralising the layout here keeps them in sync when upstream
esphome bumps ``StorageJSON``'s schema.
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
    "friendly_name": None,  # filled from stem when omitted
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
    "target_platform": "esp32",
}


def write_storage_json(
    tmp_path: Path,
    configuration: str,
    *,
    firmware_bin_path: Path | None = None,
    build_path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    data_dir: Path | None = None,
) -> Path:
    """
    Write a StorageJSON sidecar for *configuration* under *tmp_path*.

    Returns the sidecar path so the test can wipe it for "missing
    sidecar" cases. Default layout mirrors ``ext_storage_path``
    (``<tmp_path>/.esphome/storage/<basename>.json``, keyed on the
    YAML's basename — same shape esphome's ``storage_path()``
    writes) so a monkeypatched redirect of ``ext_storage_path``
    to ``tmp_path`` lands on the right file.

    ``data_dir`` overrides the parent of the ``storage/`` directory
    when set — used by the 7a-5 remote-build fixtures to mirror
    the per-build subtree the receiver-side compile subprocess
    writes into (``<config_dir>/.esphome/.remote_builds/<id>/<device>/``).
    The sidecar then lands at
    ``<data_dir>/storage/<basename>.json``; the keyspace still
    matches esphome's ``CORE.config_filename``-keyed write.

    ``firmware_bin_path`` is the typical override knob — pass
    ``None`` (the default) to model "compile aborted before link",
    pass a real path to model "compile finished and produced this
    binary". ``build_path`` defaults to
    ``<tmp_path>/.esphome/build/<stem>``; override to pin a
    different location. Anything else (``loaded_integrations``,
    ``framework``, ``board``, …) goes through *overrides* — fields
    not listed there fall through to the defaults above.
    """
    storage_dir = (data_dir or tmp_path / ".esphome") / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    # Key on the basename for both code paths — mirrors esphome's
    # ``CORE.config_filename`` (which is ``Path(config_path).name``)
    # so the sidecar lands at ``storage/<basename>.json`` regardless
    # of whether the caller passed a bare ``kitchen.yaml`` or a
    # nested ``.esphome/.remote_builds/<id>/kitchen/kitchen.yaml``.
    # Without this, a non-basename configuration would try to write
    # to ``storage/<segments>/<base>.json`` and fail on the absent
    # intermediate dir.
    sidecar = storage_dir / f"{Path(configuration).name}.json"

    stem = Path(configuration).stem
    payload = dict(_STORAGE_DEFAULTS)
    payload["name"] = stem
    payload["friendly_name"] = stem
    payload["build_path"] = str(build_path or (tmp_path / ".esphome" / "build" / stem))
    payload["firmware_bin_path"] = str(firmware_bin_path) if firmware_bin_path else None
    if overrides:
        payload.update(overrides)

    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    return sidecar
