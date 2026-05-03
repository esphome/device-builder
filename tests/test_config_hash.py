"""Tests for ``helpers/config_hash``.

The hash comes out of ``<build_path>/build_info.json``, which
ESPHome writes during every successful build (including
``--only-generate``). The dashboard already runs ``--only-generate``
on YAML add / edit, so by the time we'd read the hash the file is
in place. These tests write a representative ``StorageJSON``
sidecar pointing at a tmp build directory and assert each branch
of ``read_build_info_hash`` (the sync entry point) and
``compute_yaml_config_hash`` (the async wrapper).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.helpers.config_hash import (
    compute_yaml_config_hash,
    read_build_info_hash,
)
from tests._storage_fixtures import write_storage_json


def _write_storage_pointer(yaml_path: Path, build_path: Path | None) -> None:
    """Write the ESPHome ``StorageJSON`` sidecar next to *yaml_path*.

    ``build_path=None`` simulates "device has never been compiled" —
    we just don't write the sidecar at all so ``StorageJSON.load``
    returns None.
    """
    if build_path is None:
        return
    write_storage_json(
        yaml_path.parent,
        yaml_path.name,
        firmware_bin_path=build_path / ".pioenvs" / "firmware.bin",
        build_path=build_path,
    )


def _write_build_info(build_path: Path, **fields: Any) -> None:
    """Write a ``build_info.json`` with the given fields.

    Defaults match what ESPHome's writer emits (see
    ``esphome.writer.copy_src_tree``): a 32-bit unsigned int
    ``config_hash``, a unix ``build_time`` etc. Tests override the
    one or two fields they care about.
    """
    build_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "config_hash": 0xDEADBEEF,
        "build_time": 1700000000,
        "build_time_str": "2025-11-14 12:00:00 -0500",
        "esphome_version": "2026.5.0-dev",
    }
    payload.update(fields)
    (build_path / "build_info.json").write_text(json.dumps(payload), encoding="utf-8")


def _setup(tmp_path: Path, *, hash_value: int | None = 0xDEADBEEF) -> Path:
    """Write a YAML, sidecar, and build_info.json with *hash_value* under tmp_path.

    Returns the YAML path so each test can pass it to the helper.
    ``hash_value=None`` skips writing build_info.json (simulates
    post-clean / never-compiled).
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    _write_storage_pointer(yaml_path, build_path)
    if hash_value is not None:
        _write_build_info(build_path, config_hash=hash_value)
    return yaml_path


def test_returns_canonical_hex_from_build_info(tmp_path: Path) -> None:
    """``build_info.json`` round-trips into the 8-char hex shape.

    0x5a94a12d is the value the firmware actually broadcasts via
    mDNS for a representative Apollo float-monitor YAML — the
    original bug report. Locking in this exact shape protects
    against accidentally returning the decimal or hex-with-0x
    forms.
    """
    yaml_path = _setup(tmp_path, hash_value=0x5A94A12D)

    assert read_build_info_hash(yaml_path) == "5a94a12d"


def test_returns_zero_padded_low_value(tmp_path: Path) -> None:
    """Small ints get padded to 8 chars so the comparison is straight strcmp."""
    yaml_path = _setup(tmp_path, hash_value=0x42)

    assert read_build_info_hash(yaml_path) == "00000042"


def test_returns_none_when_storage_missing(tmp_path: Path) -> None:
    """No sidecar → no build path → no hash. Caller falls back to mtime."""
    yaml_path = tmp_path / "ghost.yaml"
    yaml_path.write_text("esphome:\n  name: ghost\n", encoding="utf-8")
    # Intentionally NOT calling _write_storage_pointer.

    assert read_build_info_hash(yaml_path) is None


def test_returns_none_when_build_info_missing(tmp_path: Path) -> None:
    """Sidecar points at a build dir but ``--only-generate`` hasn't run yet."""
    yaml_path = _setup(tmp_path, hash_value=None)

    assert read_build_info_hash(yaml_path) is None


def test_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    """Corrupt build_info.json → None, no exception bubbles up."""
    yaml_path = _setup(tmp_path, hash_value=None)
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    build_path.mkdir(parents=True, exist_ok=True)
    (build_path / "build_info.json").write_text("{this is not json", encoding="utf-8")

    assert read_build_info_hash(yaml_path) is None


def test_returns_none_when_hash_missing_or_wrong_type(tmp_path: Path) -> None:
    """Defensive: build_info.json shape changes shouldn't crash us."""
    yaml_path = _setup(tmp_path, hash_value=None)
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    build_path.mkdir(parents=True, exist_ok=True)

    (build_path / "build_info.json").write_text(
        json.dumps({"build_time": 0}),  # no config_hash key at all
        encoding="utf-8",
    )
    assert read_build_info_hash(yaml_path) is None

    (build_path / "build_info.json").write_text(
        json.dumps({"config_hash": "not-an-int"}),
        encoding="utf-8",
    )
    assert read_build_info_hash(yaml_path) is None


@pytest.mark.asyncio
async def test_async_wrapper_dispatches_to_sync_reader(tmp_path: Path) -> None:
    """``compute_yaml_config_hash`` returns the same value as ``read_build_info_hash``.

    The async path exists so ``_persist_expected_config_hash`` (which
    runs in an event loop) doesn't block the loop on file IO. Lock
    the contract: same yaml + same disk state → same hex string.
    """
    yaml_path = _setup(tmp_path, hash_value=0x12345678)

    assert await compute_yaml_config_hash(yaml_path) == "12345678"


def test_returns_none_when_build_info_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OSError on ``read_bytes`` → ``None`` + warning, no exception bubbles up.

    Distinct from the ``FileNotFoundError`` branch above (silent
    "fresh device" return). This branch is for genuine I/O
    failures the user should notice — permission denied, disk
    error, broken symlink — so the helper logs at WARNING and
    returns None rather than crashing the caller.

    The file *exists* (so the open() succeeds and we don't hit
    the FileNotFoundError branch) but ``read_bytes`` raises a
    permission error mid-read; pin both halves: ``None`` returned,
    warning emitted naming the offending path.
    """
    yaml_path = _setup(tmp_path, hash_value=None)
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    build_path.mkdir(parents=True, exist_ok=True)
    build_info = build_path / "build_info.json"
    build_info.write_text('{"config_hash": 1}', encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def _raise_permission(self: Path) -> bytes:
        if self == build_info:
            raise PermissionError("simulated permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _raise_permission)

    with caplog.at_level(logging.WARNING, logger="esphome_device_builder.helpers.config_hash"):
        result = read_build_info_hash(yaml_path)

    assert result is None
    assert any(
        "Could not read" in rec.message and "build_info.json" in rec.message
        for rec in caplog.records
    ), "expected a WARNING naming the unreadable build_info.json"
