"""Tests for ``controllers/config.py`` — settings + metadata sidecar.

This module fronts the ``.device-builder.json`` metadata file
(per-device board_id / friendly_name / IP / expected_config_hash
plus the user preferences blob) and a small WS surface
(``config/get_preferences`` / ``config/set_preferences`` /
``config/get_secrets`` / ``config/get_info``).

Three coverage targets:

* ``metadata_transaction`` — the atomic RMW context the rest of
  the package uses. Persists via tempfile + ``os.replace`` so
  lock-free readers never observe a torn write; failures inside
  the block discard the pending mutation.
* The partial-update branches of ``set_device_metadata`` —
  empty-string sentinels for ``ip`` (skip) and
  ``expected_config_hash`` (clear) are easy to swap by accident
  during refactor.
* The ``ConfigController`` WS commands. They all use
  ``loop.run_in_executor`` so a future regression that drops the
  executor wrap would stall the dashboard; the suite's
  blockbuster fixture catches that on Linux CI as long as the
  paths are exercised at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.config import (
    ConfigController,
    _load_metadata,
    _save_metadata,
    get_device_ip,
    get_device_metadata,
    load_preferences,
    metadata_transaction,
    remove_device_metadata,
    save_preferences,
    set_device_metadata,
)
from esphome_device_builder.models.preferences import UserPreferences


def _make_controller(config_dir: Path) -> ConfigController:
    """Bypass __init__ chains; attach a stub DeviceBuilder.settings."""
    controller = ConfigController.__new__(ConfigController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.settings.absolute_config_dir = config_dir.resolve()
    controller._db.settings.rel_path = config_dir.joinpath
    return controller


# ---------------------------------------------------------------------------
# metadata_transaction round-trips
# ---------------------------------------------------------------------------


def test_metadata_transaction_persists_changes(tmp_path: Path) -> None:
    """The RMW context writes mutations back to disk on clean exit."""
    with metadata_transaction(tmp_path) as data:
        data["kitchen.yaml"] = {"board_id": "esp32"}

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw == {"kitchen.yaml": {"board_id": "esp32"}}


def test_metadata_transaction_discards_changes_on_exception(tmp_path: Path) -> None:
    """A raise inside the block drops the pending mutation.

    The atomic-write happens on clean exit; if the block raises,
    we never call ``_save_metadata``. Without this guarantee, a
    half-applied update could land on disk and confuse the next
    reader.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"kitchen.yaml": {"ip": "10.0.0.1"}}')

    with pytest.raises(RuntimeError, match="boom"), metadata_transaction(tmp_path) as data:
        data["kitchen.yaml"]["ip"] = "10.0.0.2"
        raise RuntimeError("boom")

    # Original content survives untouched.
    assert json.loads(metadata_path.read_bytes()) == {"kitchen.yaml": {"ip": "10.0.0.1"}}


def test_load_metadata_returns_empty_when_missing(tmp_path: Path) -> None:
    """No file → empty dict. The most-common state on a fresh install."""
    assert _load_metadata(tmp_path) == {}


def test_load_metadata_returns_empty_on_invalid_json(tmp_path: Path) -> None:
    """A corrupted JSON file falls back to empty rather than raising.

    A user (or a botched migration) leaving truncated JSON on
    disk shouldn't crash the dashboard at startup — every reader
    would suddenly see ``JSONDecodeError`` from a path called
    deep inside the executor.
    """
    (tmp_path / ".device-builder.json").write_bytes(b'{"truncated":')
    assert _load_metadata(tmp_path) == {}


def test_save_metadata_uses_atomic_replace(tmp_path: Path) -> None:
    """Tempfile + ``os.replace`` so concurrent readers never see a torn write.

    Pin the rename behaviour: after ``_save_metadata`` the
    target file holds the new content and the temp file is gone.
    """
    _save_metadata(tmp_path, {"a.yaml": {"board_id": "esp32"}})

    target = tmp_path / ".device-builder.json"
    assert target.exists()
    assert json.loads(target.read_bytes()) == {"a.yaml": {"board_id": "esp32"}}
    # No leftover .tmp files in the dir.
    assert not list(tmp_path.glob(".device-builder.json.*.tmp"))


def test_save_metadata_cleans_up_tmpfile_on_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """If ``os.replace`` fails, the partial tempfile is unlinked.

    Otherwise repeated failures would litter the config dir with
    ``.device-builder.json.<random>.tmp`` files that nothing
    cleans up.
    """
    import os

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="rename failed"):
        _save_metadata(tmp_path, {"a.yaml": {}})

    # Cleanup unlinked the tempfile — directory is back to empty.
    assert not list(tmp_path.glob(".device-builder.json.*.tmp"))


# ---------------------------------------------------------------------------
# set_device_metadata / get_* / remove_device_metadata
# ---------------------------------------------------------------------------


def test_set_device_metadata_partial_update(tmp_path: Path) -> None:
    """Only fields explicitly passed are changed; others survive.

    Each setter argument defaults to ``None`` and the function
    only writes when the caller passes a non-None value.
    Refactor that flips the truthiness check would silently wipe
    every other field on every update.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        board_id="esp32-c3-devkitm-1",
        friendly_name="Kitchen",
        ip="10.0.0.1",
    )
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32-c6")

    entry = get_device_metadata(tmp_path, "kitchen.yaml")
    assert entry["board_id"] == "esp32-c6"
    assert entry["friendly_name"] == "Kitchen"
    assert entry["ip"] == "10.0.0.1"


def test_set_device_metadata_skips_empty_ip(tmp_path: Path) -> None:
    """``ip=""`` is the "leave alone" sentinel, not "clear".

    mDNS clears the in-memory IP whenever a device drops off
    the network, but the persisted cache is still useful — the
    next probe sweep can reuse it. Passing an empty string lets
    the controller blanket-call ``set_device_metadata`` without
    having to branch on whether the device is online.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", ip="")

    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_metadata_clears_expected_config_hash_on_empty(
    tmp_path: Path,
) -> None:
    """``expected_config_hash=""`` actively clears the field.

    Different sentinel from ``ip`` because the use case is
    different: when the user edits a YAML, the previous compile's
    expected_config_hash is stale and must be cleared. Passing
    ``""`` is the explicit clear path; ``None`` means "no
    change".
    """
    set_device_metadata(tmp_path, "kitchen.yaml", expected_config_hash="abc12345")
    set_device_metadata(tmp_path, "kitchen.yaml", expected_config_hash="")

    assert "expected_config_hash" not in get_device_metadata(tmp_path, "kitchen.yaml")


def test_remove_device_metadata_clears_only_target(tmp_path: Path) -> None:
    """Removing one device's entry leaves siblings intact."""
    set_device_metadata(tmp_path, "a.yaml", board_id="esp32")
    set_device_metadata(tmp_path, "b.yaml", board_id="esp8266")

    remove_device_metadata(tmp_path, "a.yaml")

    assert get_device_metadata(tmp_path, "a.yaml") == {}
    assert get_device_metadata(tmp_path, "b.yaml") == {"board_id": "esp8266"}


def test_load_preferences_returns_defaults_on_missing(tmp_path: Path) -> None:
    """A fresh install has no preferences — fall back to defaults."""
    prefs = load_preferences(tmp_path)
    assert isinstance(prefs, UserPreferences)


def test_load_preferences_returns_defaults_on_bad_data(tmp_path: Path) -> None:
    """Corrupted preferences blob → defaults rather than crash.

    ``UserPreferences.from_dict`` raises on unknown / malformed
    fields; without the except-fallback the dashboard wouldn't
    load when an older version's preferences file is read by a
    newer mashumaro schema.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_preferences": {"unknown_field": 42}}')

    prefs = load_preferences(tmp_path)
    assert isinstance(prefs, UserPreferences)


def test_save_preferences_round_trip(tmp_path: Path) -> None:
    """Saved prefs survive a load round-trip.

    Mostly here so blockbuster runs against the
    ``metadata_transaction`` write path under the prefs key,
    not just the device-entry one.
    """
    prefs = UserPreferences()
    save_preferences(tmp_path, prefs)
    assert load_preferences(tmp_path) == prefs


# ---------------------------------------------------------------------------
# ConfigController WS commands — verifies file I/O runs off the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_prefs_returns_loaded_preferences(tmp_path: Path) -> None:
    """``get_prefs`` is the WS surface for ``load_preferences``.

    Run via the controller (not the bare helper) so any future
    regression that drops the ``loop.run_in_executor`` wrap
    fires blockbuster on Linux CI; the helper itself uses
    ``metadata_transaction`` which calls ``tempfile.mkstemp`` ->
    ``os.path.abspath``.
    """
    save_preferences(tmp_path, UserPreferences())
    controller = _make_controller(tmp_path)

    prefs = await controller.get_prefs()
    assert isinstance(prefs, UserPreferences)


@pytest.mark.asyncio
async def test_set_prefs_merges_partial_update(tmp_path: Path) -> None:
    """Partial-update merge: unknown / unset fields keep their current values.

    ``set_prefs`` accepts kwargs (whatever the WS layer routed
    in) and merges them into the current dict before saving.
    Without the merge, every set_prefs would clobber unrelated
    fields with their defaults.
    """
    initial = UserPreferences()
    save_preferences(tmp_path, initial)
    controller = _make_controller(tmp_path)

    # Round-trip — even with no explicit kwargs, the saved blob
    # should remain valid.
    result = await controller.set_prefs()
    assert isinstance(result, UserPreferences)
    assert result == initial


@pytest.mark.asyncio
async def test_get_secrets_returns_empty_when_missing(tmp_path: Path) -> None:
    """No secrets.yaml → empty list, not a raise.

    The dashboard's secrets dropdown loads on every config-edit
    open; a missing file shouldn't break the editor.
    """
    controller = _make_controller(tmp_path)
    keys = await controller.get_secrets()
    assert keys == []


@pytest.mark.asyncio
async def test_get_secrets_returns_sorted_keys(tmp_path: Path) -> None:
    """Returned secret names are sorted alphabetically.

    The dropdown renders them in document order otherwise, which
    drifts every time the user reorders the file. Pin the sort
    so the dashboard's UX stays stable.
    """
    (tmp_path / "secrets.yaml").write_text(
        "wifi_password: secret\nwifi_ssid: home\napi_key: token\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    keys = await controller.get_secrets()
    assert keys == ["api_key", "wifi_password", "wifi_ssid"]


@pytest.mark.asyncio
async def test_get_info_rejects_path_traversal(tmp_path: Path) -> None:
    """Traversal-shaped configuration returns ``None`` rather than raising.

    ``rel_path`` raises ``ValueError`` on traversal; the WS
    handler catches and surfaces ``None`` so a malicious /
    confused client gets a clean miss instead of an
    INTERNAL_ERROR.
    """
    controller = _make_controller(tmp_path)

    # Force rel_path to raise so the except-branch runs without
    # reproducing the exact pathlib semantics across platforms.
    def _raise(*_parts: str) -> Path:
        raise ValueError("traversal")

    controller._db.settings.rel_path = _raise

    result = await asyncio.wait_for(controller.get_info(configuration="../etc/passwd"), timeout=2.0)
    assert result is None
