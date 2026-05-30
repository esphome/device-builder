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
import logging
import os
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome.util import SerialPort

from esphome_device_builder.controllers import config as config_module
from esphome_device_builder.controllers.config import (
    _APP_DESC_MAGIC,
    _APP_DESC_SIZE,
    _DETECT_UNKNOWN,
    _PROJECT_NAME_OFFSET,
    _PROJECT_NAME_SIZE,
    ConfigController,
    _chip_family_to_descriptor,
    _classify_esptool_failure,
    _is_valid_port_name,
    _load_metadata,
    _parse_chip_family_line,
    _parse_project_name,
    _read_descriptor_file,
    _run_esptool,
    _save_metadata,
    clear_volatile_device_metadata,
    delete_label_cascade,
    get_device_ip,
    get_device_metadata,
    has_remote_build_settings_persisted,
    labels_transaction,
    load_labels,
    load_preferences,
    load_remote_build_settings,
    metadata_transaction,
    remote_build_settings_transaction,
    remove_device_metadata,
    save_labels,
    save_preferences,
    save_remote_build_settings,
    set_device_labels,
    set_device_metadata,
    update_preferences,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    DEFAULT_CLEANUP_TTL_SECONDS,
    MAX_CLEANUP_TTL_SECONDS,
    MIN_CLEANUP_TTL_SECONDS,
    ErrorCode,
    Label,
    RemoteBuildSettings,
)
from esphome_device_builder.models.preferences import (
    DashboardView,
    Theme,
    UserPreferences,
)

from ._storage_fixtures import write_storage_json
from .conftest import MakeSettingsFactory


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="cross-process flock requires fcntl (POSIX-only)",
)
def test_metadata_transaction_serialises_across_flocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-process flock serialises peers RMW-ing disjoint keys."""
    # Bypass _METADATA_LOCK so the two threads race directly at
    # the flock — the same path two real processes would hit.
    monkeypatch.setattr(config_module.metadata, "_METADATA_LOCK", nullcontext())

    thread_a_in_block = threading.Event()
    release_a = threading.Event()
    thread_b_done = threading.Event()

    def _writer_a() -> None:
        with metadata_transaction(tmp_path) as data:
            data["a.yaml"] = {"board_id": "esp32"}
            thread_a_in_block.set()
            # Hold the flock so B has to wait for us.
            release_a.wait(timeout=5.0)

    def _writer_b() -> None:
        thread_a_in_block.wait(timeout=5.0)
        with metadata_transaction(tmp_path) as data:
            data["b.yaml"] = {"board_id": "esp32-c3"}
        thread_b_done.set()

    t_a = threading.Thread(target=_writer_a)
    t_b = threading.Thread(target=_writer_b)
    t_a.start()
    t_b.start()

    # B must NOT have finished while A holds the flock.
    assert not thread_b_done.wait(timeout=0.5), (
        "B's transaction completed without waiting for A's flock — flock not engaged"
    )

    release_a.set()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)
    assert not t_a.is_alive() and not t_b.is_alive()

    final = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert final == {
        "a.yaml": {"board_id": "esp32"},
        "b.yaml": {"board_id": "esp32-c3"},
    }


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fstat S_ISREG check is POSIX-only (fcntl path)",
)
def test_metadata_transaction_rejects_non_regular_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: ``S_ISREG`` fstat refuses oddballs (e.g. block devices)."""
    # Real block/char devices need root + a special FS; flip
    # ``S_ISREG`` in the module so a normal regular-file open
    # takes the rejection branch as if it had hit one.
    monkeypatch.setattr(config_module.metadata.stat, "S_ISREG", lambda _mode: False)

    with pytest.raises(OSError, match="is not a regular file"), metadata_transaction(tmp_path):
        pass


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


def test_set_device_metadata_persists_mac_address(tmp_path: Path) -> None:
    """``mac_address=...`` round-trips through ``get_device_metadata``.

    Persisted so the dashboard surfaces the MAC immediately on
    backend restart — ESPHome devices are mDNS-silent until probed,
    so a runtime-only field renders blank for the discovery sweep's
    bootstrap window.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", mac_address="94:C9:60:1F:8C:F1")

    assert get_device_metadata(tmp_path, "kitchen.yaml") == {"mac_address": "94:C9:60:1F:8C:F1"}


def test_set_device_metadata_clears_mac_on_empty(tmp_path: Path) -> None:
    """``mac_address=""`` actively clears the persisted MAC.

    Mirrors ``expected_config_hash``'s tri-state (``None`` →
    no-change, ``""`` → clear, value → set) — a deleted device's
    archive flow uses the empty-string path to wipe the volatile
    fields.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", mac_address="94:C9:60:1F:8C:F1")
    set_device_metadata(tmp_path, "kitchen.yaml", mac_address="")

    assert "mac_address" not in get_device_metadata(tmp_path, "kitchen.yaml")


def test_set_device_metadata_persists_regen_failure_stamp(tmp_path: Path) -> None:
    """``regen_failed_mtime`` + ``regen_failed_at`` round-trip together.

    Persisted so a backend restart skips replaying a regen on a
    YAML that already failed at this exact mtime — without it,
    every dashboard boot burns a subprocess on broken configs
    (missing ``!secret``, unreachable git package) just to fail
    again. The wall-clock half feeds the controller-side TTL so
    transient external problems eventually get re-checked even
    when the YAML is untouched.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        regen_failed_mtime=1700000000.5,
        regen_failed_at=1700000005.0,
    )

    assert get_device_metadata(tmp_path, "kitchen.yaml") == {
        "regen_failed_mtime": 1700000000.5,
        "regen_failed_at": 1700000005.0,
    }


def test_set_device_metadata_clears_regen_failure_stamp_on_zero(tmp_path: Path) -> None:
    """Both stamp halves cleared explicitly via ``0.0``.

    The success path of ``_schedule_storage_regenerate`` clears
    both fields so a future backend restart picks up the now-good
    YAML. Passing ``0.0`` is the explicit clear path; ``None``
    leaves the field alone.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        regen_failed_mtime=1700000000.5,
        regen_failed_at=1700000005.0,
    )
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        regen_failed_mtime=0.0,
        regen_failed_at=0.0,
    )

    md = get_device_metadata(tmp_path, "kitchen.yaml")
    assert "regen_failed_mtime" not in md
    assert "regen_failed_at" not in md


def test_set_device_metadata_persists_build_size_triple(tmp_path: Path) -> None:
    """The (bytes, dir_mtime, info_mtime) triple round-trips together.

    Both halves of the freshness pair gate the cache, the bytes
    field carries the cached total, and all three are written
    atomically by a single ``set_device_metadata`` call. Persisting
    lets a backend restart skip the heavy recursive walk for every
    device whose pair hasn't moved.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        build_size_bytes=12345678,
        build_size_dir_mtime=1714900000,
        build_size_info_mtime=1714900050,
    )

    md = get_device_metadata(tmp_path, "kitchen.yaml")
    assert md["build_size_bytes"] == 12345678
    assert md["build_size_dir_mtime"] == 1714900000
    assert md["build_size_info_mtime"] == 1714900050


def test_set_device_metadata_clears_build_size_on_zero(tmp_path: Path) -> None:
    """Passing ``0`` for any field actively clears it.

    Used by the archive flow's volatile-field scrub: the build
    tree is wiped, so the cached triple would describe a directory
    that no longer exists.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        build_size_bytes=12345678,
        build_size_dir_mtime=1714900000,
        build_size_info_mtime=1714900050,
    )
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        build_size_bytes=0,
        build_size_dir_mtime=0,
        build_size_info_mtime=0,
    )

    md = get_device_metadata(tmp_path, "kitchen.yaml")
    assert "build_size_bytes" not in md
    assert "build_size_dir_mtime" not in md
    assert "build_size_info_mtime" not in md


def test_remove_device_metadata_clears_only_target(tmp_path: Path) -> None:
    """Removing one device's entry leaves siblings intact."""
    set_device_metadata(tmp_path, "a.yaml", board_id="esp32")
    set_device_metadata(tmp_path, "b.yaml", board_id="esp8266")

    remove_device_metadata(tmp_path, "a.yaml")

    assert get_device_metadata(tmp_path, "a.yaml") == {}
    assert get_device_metadata(tmp_path, "b.yaml") == {"board_id": "esp8266"}


def test_load_preferences_returns_defaults_on_missing(tmp_path: Path) -> None:
    """A fresh install has no preferences — fall back to the default object.

    Equality check (not just ``isinstance``) so a regression
    that builds a non-default preferences object on the
    missing-file path (e.g. one with ``dashboard_view=TABLE``
    instead of CARDS) breaks this test.
    """
    assert load_preferences(tmp_path) == UserPreferences()


def test_load_preferences_returns_defaults_on_bad_data(tmp_path: Path) -> None:
    """Corrupted preferences blob → default object, not partial recovery.

    ``UserPreferences.from_dict`` raises on unknown / malformed
    fields; without the except-fallback the dashboard wouldn't
    load when an older version's preferences file is read by a
    newer mashumaro schema. Equality with ``UserPreferences()``
    pins that the fallback is the same default object, not a
    silently-mutated one a regression could produce.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_preferences": {"unknown_field": 42}}')

    assert load_preferences(tmp_path) == UserPreferences()


def test_load_remote_build_settings_returns_defaults_on_missing(tmp_path: Path) -> None:
    """Fresh config dir → ``RemoteBuildSettings()`` with ``enabled=True``.

    Default-on so fresh installs are discoverable + pairable
    without an extra operator step. The HA-addon path overrides
    this at the bind site via
    :func:`has_remote_build_settings_persisted` rather than at
    load time, so the load function returns the same shape
    regardless of deployment mode.
    """
    assert load_remote_build_settings(tmp_path) == RemoteBuildSettings()
    assert load_remote_build_settings(tmp_path).enabled is True


def test_load_remote_build_settings_fails_safe_on_non_dict_block(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-dict ``_remote_build`` blob → ``enabled=False``, not the permissive default.

    With the default ``RemoteBuildSettings.enabled=True``, a
    corrupted block that fell through to the dataclass defaults
    would silently bind the listener without any operator opt-in.
    Fail safe: a list / scalar / null value lands on
    ``enabled=False`` and emits a warning so the operator can
    spot the corrupted sidecar.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": [1, 2, 3]}')

    with caplog.at_level(logging.WARNING, logger="esphome_device_builder.controllers.config"):
        settings = load_remote_build_settings(tmp_path)
    assert settings.enabled is False
    assert any("Malformed" in r.getMessage() for r in caplog.records)


def test_load_remote_build_settings_fails_safe_on_decode_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dict that fails ``from_dict`` decode → ``enabled=False``.

    Same fail-safe shape as the non-dict path: a schema break
    that raises out of mashumaro's ``from_dict`` lands on
    ``enabled=False`` rather than silently enabling via the
    permissive default. Monkeypatched rather than crafted: most
    real-world malformed values are coerced by mashumaro
    (``["not", "a", "bool"]`` truthy-coerces to ``True``, etc.),
    so the cleanest way to exercise the except-arm is to force
    ``from_dict`` to raise — which is what a hypothetical future
    schema break would do.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": {"enabled": true}}')

    def _boom(*args: object, **kwargs: object) -> RemoteBuildSettings:
        msg = "synthetic schema break"
        raise ValueError(msg)

    monkeypatch.setattr(RemoteBuildSettings, "from_dict", classmethod(_boom))

    with caplog.at_level(logging.ERROR, logger="esphome_device_builder.controllers.config"):
        settings = load_remote_build_settings(tmp_path)
    assert settings.enabled is False
    assert any("Failed to decode" in r.getMessage() for r in caplog.records)


def test_save_remote_build_settings_round_trip(tmp_path: Path) -> None:
    """A non-default settings blob round-trips through save → load."""
    settings = RemoteBuildSettings(enabled=True)
    save_remote_build_settings(tmp_path, settings)
    assert load_remote_build_settings(tmp_path) == settings


def test_has_remote_build_settings_persisted_false_on_fresh_install(tmp_path: Path) -> None:
    """No metadata file → operator has not opted in via the toggle."""
    assert has_remote_build_settings_persisted(tmp_path) is False


def test_has_remote_build_settings_persisted_false_when_other_keys_set(tmp_path: Path) -> None:
    """A metadata file with unrelated keys is still ``False`` (no toggle write)."""
    (tmp_path / ".device-builder.json").write_bytes(b'{"some_other_key": {}}')
    assert has_remote_build_settings_persisted(tmp_path) is False


def test_has_remote_build_settings_persisted_true_after_save(tmp_path: Path) -> None:
    """``save_remote_build_settings`` flips the persistence signal to ``True``.

    Pins the load-bearing contract the HA-addon bind gate
    depends on:
    :meth:`device_builder.DeviceBuilder._maybe_start_remote_build_site`
    skips the bind on HA addon UNTIL this returns ``True``,
    which only happens after ``set_settings`` writes a
    ``_remote_build`` block. Even a write that lands on the
    dataclass defaults still flips the signal -- the existence
    of the block is the "operator opted in" marker, not its
    contents.
    """
    save_remote_build_settings(tmp_path, RemoteBuildSettings(enabled=True))
    assert has_remote_build_settings_persisted(tmp_path) is True


def test_has_remote_build_settings_persisted_true_for_explicit_disable(tmp_path: Path) -> None:
    """An operator who explicitly disabled the toggle still counts as "opted in".

    Once the operator interacts with the toggle (in either
    direction) the persistence signal flips to True. That's the
    right shape for the HA-addon gate: the operator has shown
    they know the feature exists and made a deliberate choice
    -- subsequent boots should respect their choice without
    re-asking. (The bind site then also reads
    ``RemoteBuildSettings.enabled`` and skips the bind because
    that's still False; the gate only suppresses the
    fresh-install default-on path.)
    """
    save_remote_build_settings(tmp_path, RemoteBuildSettings(enabled=False))
    assert has_remote_build_settings_persisted(tmp_path) is True


def test_has_remote_build_settings_persisted_false_on_malformed_block(tmp_path: Path) -> None:
    """A malformed (non-dict) ``_remote_build`` value doesn't count as opt-in.

    ``set_settings`` always writes ``RemoteBuildSettings.to_dict()``
    which is a dict. A non-dict value (list, scalar, null) on
    disk reached the sidecar via a hand-edit or partial-write,
    not an operator interaction with the toggle. The HA-addon
    gate must treat that as "not opted in" so a corrupted
    sidecar doesn't silently bind the listener on the addon
    path.
    """
    (tmp_path / ".device-builder.json").write_bytes(b'{"_remote_build": [1, 2, 3]}')
    assert has_remote_build_settings_persisted(tmp_path) is False

    (tmp_path / ".device-builder.json").write_bytes(b'{"_remote_build": null}')
    assert has_remote_build_settings_persisted(tmp_path) is False

    (tmp_path / ".device-builder.json").write_bytes(b'{"_remote_build": "string"}')
    assert has_remote_build_settings_persisted(tmp_path) is False


@pytest.mark.parametrize(
    "ttl_in",
    [
        True,  # bool (int subclass): decodes as 1, would trigger immediate sweep.
        "86400",  # string: comparison in sweep would raise TypeError.
        None,
        86400.5,  # float
    ],
)
def test_remote_build_settings_post_init_coerces_bad_ttl_to_default(
    ttl_in: object,
) -> None:
    """Non-int / bool ``cleanup_ttl_seconds`` falls back to default at construction.

    The WS validator on ``set_settings`` rejects these, but the
    on-disk decode path (``from_dict`` →
    ``RemoteBuildSettings(...)``) doesn't apply the same gate.
    A hand-edited or corrupt sidecar with
    ``cleanup_ttl_seconds: true`` would deserialise as 1 (bool
    is an int subclass) and trigger near-immediate cache
    deletion. ``__post_init__`` coerces back to
    :data:`DEFAULT_CLEANUP_TTL_SECONDS` so the sweep stays
    safe regardless of what mashumaro produced.

    Doesn't reject the row (no ``ValueError``) — the load path
    stays robust against partially-corrupt sidecars; the
    operator's last-good ``enabled`` value survives even if
    the TTL field is broken.
    """
    settings = RemoteBuildSettings(
        enabled=True,
        cleanup_ttl_seconds=ttl_in,  # type: ignore[arg-type]
    )
    assert settings.cleanup_ttl_seconds == DEFAULT_CLEANUP_TTL_SECONDS
    assert settings.enabled is True  # bad TTL doesn't flip the master switch


@pytest.mark.parametrize(
    ("ttl_in", "expected"),
    [
        (0, MIN_CLEANUP_TTL_SECONDS),
        (60, MIN_CLEANUP_TTL_SECONDS),  # below MIN
        (MAX_CLEANUP_TTL_SECONDS + 1, MAX_CLEANUP_TTL_SECONDS),
        (-3600, MIN_CLEANUP_TTL_SECONDS),
    ],
)
def test_remote_build_settings_post_init_clamps_out_of_range_ttl(
    ttl_in: int, expected: int
) -> None:
    """An out-of-range int is clamped to the nearest MIN / MAX bound.

    Hand-edited sidecars setting silly values (0, negative,
    decades in seconds) don't push the sweep into pathological
    behaviour; they land at the nearest sane bound.
    """
    settings = RemoteBuildSettings(cleanup_ttl_seconds=ttl_in)
    assert settings.cleanup_ttl_seconds == expected


def test_remote_build_settings_post_init_preserves_valid_ttl() -> None:
    """An in-range int passes through unchanged."""
    settings = RemoteBuildSettings(cleanup_ttl_seconds=7200)
    assert settings.cleanup_ttl_seconds == 7200


def test_remote_build_settings_transaction_fails_safe_on_bad_data(
    tmp_path: Path,
) -> None:
    """Corrupted blob → the transaction yields ``enabled=False``, not the permissive default.

    Same fail-safe shape as :func:`load_remote_build_settings`:
    a non-dict ``_remote_build`` value lands on
    ``enabled=False`` rather than the model default ``True``.
    Mutating the yielded settings inside the block replaces the
    corrupt blob with the new canonical state on commit.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": [1, 2, 3]}')

    with remote_build_settings_transaction(tmp_path) as settings:
        # Yielded value is the fail-safe shape; any mutation
        # persists as the new canonical state, replacing the
        # corrupt blob.
        assert settings.enabled is False
        settings.enabled = True

    assert load_remote_build_settings(tmp_path) == RemoteBuildSettings(enabled=True)


def test_remote_build_settings_transaction_discards_on_exception(
    tmp_path: Path,
) -> None:
    """A raise inside the block drops the pending mutation."""
    save_remote_build_settings(tmp_path, RemoteBuildSettings(enabled=False))

    with (
        pytest.raises(RuntimeError, match="boom"),
        remote_build_settings_transaction(tmp_path) as settings,
    ):
        settings.enabled = True
        raise RuntimeError("boom")

    # Original pre-block state survives untouched.
    assert load_remote_build_settings(tmp_path) == RemoteBuildSettings(enabled=False)


def test_save_preferences_round_trip(tmp_path: Path) -> None:
    """A non-default prefs blob round-trips through save → load.

    Pins the actual write path: round-tripping ``UserPreferences()``
    would also pass if save / load both silently lost data, so
    use a non-default value (``dashboard_view=TABLE``) to
    actually exercise the marshalling.
    """
    prefs = UserPreferences(dashboard_view=DashboardView.TABLE)
    save_preferences(tmp_path, prefs)
    assert load_preferences(tmp_path) == prefs


# ---------------------------------------------------------------------------
# ConfigController WS commands — verifies file I/O runs off the event loop
# ---------------------------------------------------------------------------


async def test_get_prefs_returns_loaded_preferences(tmp_path: Path) -> None:
    """``get_prefs`` returns the persisted blob, not a fresh default.

    Persists a non-default preferences object and asserts it
    round-trips back. A regression that bypasses disk I/O and
    just constructs ``UserPreferences()`` would still claim
    ``isinstance`` but would fail this equality.

    The seeding ``save_preferences`` runs in a thread because
    ``metadata_transaction`` -> ``tempfile.mkstemp`` ->
    ``os.path.abspath`` blocks the event loop, and blockbuster
    flags it from inside an async test.
    """
    persisted = UserPreferences(dashboard_view=DashboardView.TABLE)
    await asyncio.to_thread(save_preferences, tmp_path, persisted)
    controller = _make_controller(tmp_path)

    prefs = await controller.get_prefs()
    assert prefs == persisted


async def test_set_prefs_merges_partial_update(tmp_path: Path) -> None:
    """Partial-update merge: only the supplied field changes.

    Persist a known initial state, then call ``set_prefs`` with
    just one field. The unrelated fields must keep their
    persisted values, not snap back to dataclass defaults — a
    regression that re-constructs ``UserPreferences`` from
    kwargs alone (skipping the merge step) would clobber them
    silently.

    Seeding goes via ``asyncio.to_thread`` so the
    ``metadata_transaction`` -> ``tempfile.mkstemp`` write
    doesn't trip blockbuster on Linux CI.
    """
    initial = UserPreferences(dashboard_view=DashboardView.TABLE, theme=Theme.DARK)
    await asyncio.to_thread(save_preferences, tmp_path, initial)
    controller = _make_controller(tmp_path)

    # Update only ``theme``; ``dashboard_view`` should survive.
    result = await controller.set_prefs(theme=Theme.LIGHT)
    assert result.theme == Theme.LIGHT
    assert result.dashboard_view == DashboardView.TABLE
    # Persisted blob matches the merged state.
    persisted = await asyncio.to_thread(load_preferences, tmp_path)
    assert persisted == result


def test_update_preferences_defaults_on_corrupt(tmp_path: Path) -> None:
    """A malformed ``_preferences`` blob merges onto defaults, then persists clean."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_preferences": [1, 2, 3]}')

    updated = update_preferences(tmp_path, {"theme": Theme.DARK})

    assert updated == UserPreferences(theme=Theme.DARK)
    assert load_preferences(tmp_path) == UserPreferences(theme=Theme.DARK)


async def test_set_prefs_concurrent_updates_do_not_lose_writes(tmp_path: Path) -> None:
    """Concurrent partial updates of distinct fields all survive.

    The read-merge-save runs inside one ``metadata_transaction``,
    so two writers can't both read the same baseline and clobber
    each other's field. A non-atomic load-then-save loses one.
    """
    controller = _make_controller(tmp_path)

    await asyncio.gather(
        controller.set_prefs(theme=Theme.DARK),
        controller.set_prefs(dashboard_view=DashboardView.TABLE),
        controller.set_prefs(navigator_visible=False),
    )

    persisted = await asyncio.to_thread(load_preferences, tmp_path)
    assert persisted.theme == Theme.DARK
    assert persisted.dashboard_view == DashboardView.TABLE
    assert persisted.navigator_visible is False


async def test_get_secrets_returns_empty_when_missing(tmp_path: Path) -> None:
    """No secrets.yaml → empty list, not a raise.

    The dashboard's secrets dropdown loads on every config-edit
    open; a missing file shouldn't break the editor.
    """
    controller = _make_controller(tmp_path)
    keys = await controller.get_secrets()
    assert keys == []


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


async def test_get_info_returns_storage_metadata_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: ``StorageJSON.load`` hits → handler returns the metadata dict.

    Pin the field-by-field projection (StorageJSON has more
    fields than we surface; the handler whitelists the
    drawer-relevant subset). A regression that returned
    ``storage.to_dict()`` directly would leak internal fields
    onto the wire and force the frontend to re-derive its UI
    contract from upstream's StorageJSON shape.
    """
    sidecar = write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=Path("/firmware/kitchen.bin"),
        overrides={
            "name": "kitchen",
            "friendly_name": "Kitchen",
            "comment": "By the toaster",
            "address": "kitchen.local",
            "web_port": 80,
            "loaded_integrations": ["api", "wifi", "ota"],
            "target_platform": "esp32",
        },
    )
    # ``ext_storage_path`` keys off ``CORE.config_path`` in production;
    # redirect it onto our seeded sidecar so the handler's read lands
    # there without a real CORE setup.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.resolve_storage_path",
        lambda configuration: sidecar.parent / f"{configuration}.json",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_info(configuration="kitchen.yaml")

    # ``firmware_bin_path`` deserialises into a ``Path`` upstream, so
    # the projection passes that through unchanged. The server's
    # ``send_json`` (orjson) serialises the ``Path`` to its string
    # form on the wire — this in-process assertion checks the
    # pre-serialisation shape, where the handler hasn't coerced.
    # Assert the integration set separately so the test doesn't
    # over-constrain ``loaded_integrations`` to a specific
    # collection type — the upstream ``StorageJSON`` could
    # legitimately switch between ``set`` / ``list`` / ``tuple``
    # without changing the JSON shape on the wire (an unordered
    # collection of strings).
    integrations = result.pop("loaded_integrations")
    assert set(integrations) == {"api", "wifi", "ota"}
    assert result == {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "comment": "By the toaster",
        "address": "kitchen.local",
        "web_port": 80,
        "target_platform": "esp32",
        "current_version": "2026.5.0-dev",
        "deployed_version": Path("/firmware/kitchen.bin"),
    }


async def test_get_info_returns_none_when_storage_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No StorageJSON sidecar on disk → handler returns ``None``.

    The drawer treats ``None`` as "device hasn't been compiled
    yet" and renders a CTA to compile. A regression that raised
    or returned an empty dict would fail open in the wrong
    direction — either crashing the drawer or showing stale-
    looking blank fields instead of the compile prompt.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.resolve_storage_path",
        lambda configuration: tmp_path / "missing-storage.json",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_info(configuration="kitchen.yaml")

    assert result is None


async def test_get_info_rejects_path_traversal(make_settings: MakeSettingsFactory) -> None:
    """Traversal-shaped configuration raises ``CommandError(INVALID_ARGS)``.

    Wires the controller to the real ``DashboardSettings.rel_path``
    so we exercise the production traversal-detection logic, not a
    monkeypatched stub. ``rel_path`` translates the ``ValueError``
    raised by ``Path.relative_to`` into a ``CommandError`` so the
    WS dispatcher surfaces it as ``INVALID_ARGS`` instead of the
    generic ``INTERNAL_ERROR`` an unclassified ``ValueError`` would
    yield. A regression in either side of the boundary breaks the
    test.
    """
    settings = make_settings()

    controller = ConfigController.__new__(ConfigController)
    controller._db = MagicMock()
    controller._db.settings = settings

    with pytest.raises(CommandError) as excinfo:
        await asyncio.wait_for(controller.get_info(configuration="../etc/passwd"), timeout=2.0)
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Invalid configuration filename" in excinfo.value.message


@pytest.mark.parametrize(
    "payload",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "subdir/../../escape.yaml",
        "/absolute/path/escape.yaml",
        "..",
    ],
)
def test_rel_path_translates_traversal_to_command_error(
    make_settings: MakeSettingsFactory, payload: str
) -> None:
    """``rel_path`` raises ``CommandError(INVALID_ARGS)`` on every traversal shape.

    This is the chokepoint behind issue #107 — every WS handler that
    builds a path from a user-supplied ``configuration`` flows
    through ``rel_path``, so this one parametrised test locks the
    dispatcher contract for all of them. The error message is
    truncated + ``!r``-quoted so a pathological payload can't break
    the JSON error response.
    """
    settings = make_settings()

    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Invalid configuration filename" in excinfo.value.message


def test_rel_path_truncates_long_payload(make_settings: MakeSettingsFactory) -> None:
    """A multi-KB ``configuration`` payload is truncated in the error message.

    Keeps the JSON error response bounded so a pathological payload
    can't blow up the WS frame.
    """
    settings = make_settings()

    payload = "../" + "A" * 5000
    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert "..." in excinfo.value.message
    assert len(excinfo.value.message) < 200


def test_rel_path_bounds_control_byte_payload(make_settings: MakeSettingsFactory) -> None:
    r"""Control-heavy payloads stay bounded after ``!r`` expansion.

    A single ``\x00`` repr's to 4 chars; a naive "truncate the raw
    string then ``!r`` it" would let an 80-byte input balloon to
    320+ chars in the final message and re-introduce the
    unbounded-error hazard. ``!r`` runs *before* the bound, so a
    payload of 100 NUL bytes still produces a message ≤ 200 chars.
    """
    settings = make_settings()

    payload = "../" + "\x00" * 100
    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert len(excinfo.value.message) < 200


# ---------------------------------------------------------------------------
# get_serial_ports — config/serial_ports
# ---------------------------------------------------------------------------


async def test_get_serial_ports_returns_path_and_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each upstream ``SerialPort`` round-trips as ``{port, desc}``.

    Pin the field renaming (``path`` → ``port``,
    ``description`` → ``desc``). The executor-route is asserted
    separately in ``test_get_serial_ports_runs_in_executor`` —
    monkeypatching ``get_serial_ports`` to a sync lambda here
    means a regression that dropped ``run_in_executor`` would
    still pass this test, so the contract gets its own dedicated
    pin.
    """
    fake_ports = [
        SerialPort(path="/dev/ttyUSB0", description="USB Serial"),
        SerialPort(path="/dev/ttyACM0", description="Arduino Uno"),
    ]
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.get_serial_ports",
        lambda: fake_ports,
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == [
        {"port": "/dev/ttyUSB0", "desc": "USB Serial"},
        {"port": "/dev/ttyACM0", "desc": "Arduino Uno"},
    ]


async def test_get_serial_ports_runs_in_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_serial_ports`` runs in a worker thread, not the event loop.

    Capture the calling thread inside the monkeypatched lookup
    and assert it isn't the loop's thread. Pyserial's
    ``list_ports`` walks ``/dev`` synchronously on a busy host,
    so dropping ``run_in_executor`` would stall the dashboard
    until the scan finished — the failure mode this test catches
    is silent and platform-dependent (only shows up under load),
    which is exactly what blockbuster's per-frame check can't
    catch from a sync stub. Direct thread-identity assertion is
    what makes the executor route observable.
    """
    loop_thread = threading.get_ident()
    captured_thread: dict[str, int] = {}

    def _record_thread() -> list[SerialPort]:
        captured_thread["tid"] = threading.get_ident()
        return [SerialPort(path="/dev/ttyUSB0", description="USB Serial")]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.get_serial_ports",
        _record_thread,
    )
    controller = _make_controller(tmp_path)

    await controller.get_serial_ports_cmd()

    assert captured_thread.get("tid") is not None, "get_serial_ports was never invoked"
    assert captured_thread["tid"] != loop_thread, (
        "get_serial_ports ran on the event-loop thread — production needs "
        "run_in_executor so pyserial's /dev walk doesn't stall the loop on "
        "a busy host."
    )


async def test_get_serial_ports_substitutes_path_for_na_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``description == "n/a"`` → ``desc`` falls back to the port path.

    pyserial returns the literal string ``"n/a"`` when it can't
    read a USB descriptor (common with adapter chips that don't
    expose product strings). Showing "n/a" in the dashboard's
    flash-target dropdown is unhelpful — a refactor that dropped
    the fallback would make the chooser look broken without
    actually being broken.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.get_serial_ports",
        lambda: [SerialPort(path="/dev/ttyUSB0", description="n/a")],
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == [{"port": "/dev/ttyUSB0", "desc": "/dev/ttyUSB0"}]


# ---------------------------------------------------------------------------
# Chip detection — config/detect_chip
# ---------------------------------------------------------------------------


def _app_descriptor_blob(project_name: str = "starter-kit") -> bytes:
    """Build a synthetic ``esp_app_desc_t`` payload for parser tests.

    Mirrors the layout the parser reads: magic word at offset 0,
    project_name (NUL-terminated, ASCII) at offset 48. Everything
    else is zero — the parser only touches those two fields.
    """
    blob = bytearray(_APP_DESC_SIZE)
    blob[0:4] = _APP_DESC_MAGIC.to_bytes(4, "little")
    encoded = project_name.encode("utf-8")
    assert len(encoded) < _PROJECT_NAME_SIZE
    blob[_PROJECT_NAME_OFFSET : _PROJECT_NAME_OFFSET + len(encoded)] = encoded
    return bytes(blob)


def test_parse_chip_family_line_from_chip_type_line() -> None:
    """Parser picks the family out of esptool v5.2's ``Chip type:`` line."""
    output = (
        "esptool v5.2.0\n"
        "Serial port /dev/cu.usbserial-54FC0197371:\n"
        "Connecting....\n"
        "Detecting chip type... ESP32-C3\n"
        "Connected to ESP32-C3 on /dev/cu.usbserial-54FC0197371:\n"
        "Chip type:          ESP32-C3 (QFN32) (revision v0.3)\n"
        "MAC:                a0:76:4e:19:de:78\n"
    )
    assert _parse_chip_family_line(output) == {
        "chip_family": "ESP32-C3",
        "variant": "esp32c3",
        "platform": "esp32",
    }


def test_parse_chip_family_line_falls_back_to_connected_to() -> None:
    """Parser falls back to ``Connected to X on …`` when ``Chip type:`` is absent."""
    output = (
        "Connecting....\n"
        "Connected to ESP32-S3 on /dev/cu.usbmodem1234:\n"
        "Chip is in Secure Download Mode\n"
    )
    assert _parse_chip_family_line(output) == {
        "chip_family": "ESP32-S3",
        "variant": "esp32s3",
        "platform": "esp32",
    }


def test_parse_chip_family_line_legacy_detecting_line() -> None:
    """Parser still handles esptool's legacy ``Detecting chip type…`` line."""
    output = "Detecting chip type... ESP32-C3"
    assert _parse_chip_family_line(output) == {
        "chip_family": "ESP32-C3",
        "variant": "esp32c3",
        "platform": "esp32",
    }


def test_parse_chip_family_line_returns_none_when_unparseable() -> None:
    assert _parse_chip_family_line("nothing useful here") is None


def test_parse_chip_family_line_returns_none_for_unknown_family() -> None:
    assert _parse_chip_family_line("Chip type: ESP32-Z99 (revision v9.9)") is None


def test_chip_family_to_descriptor_maps_esp8266() -> None:
    assert _chip_family_to_descriptor("ESP8266") == {
        "chip_family": "ESP8266",
        "variant": "",
        "platform": "esp8266",
    }


def test_parse_project_name_returns_project_name_when_magic_matches() -> None:
    blob = _app_descriptor_blob("starter-kit")
    assert _parse_project_name(blob) == "starter-kit"


def test_parse_project_name_returns_none_on_bad_magic() -> None:
    blob = bytearray(_app_descriptor_blob("starter-kit"))
    blob[0:4] = (0xDEADBEEF).to_bytes(4, "little")
    assert _parse_project_name(bytes(blob)) is None


def test_parse_project_name_returns_none_when_empty() -> None:
    # Magic matches but project_name is all zeros — a factory image
    # that didn't set the project name. Treat as "no manifest" so
    # the wizard falls through to chip-family filtering.
    blob = bytearray(_app_descriptor_blob("placeholder"))
    blob[_PROJECT_NAME_OFFSET : _PROJECT_NAME_OFFSET + _PROJECT_NAME_SIZE] = (
        b"\x00" * _PROJECT_NAME_SIZE
    )
    assert _parse_project_name(bytes(blob)) is None


def test_parse_project_name_returns_none_when_blob_truncated() -> None:
    """Short blob (less than offset+size) returns None instead of slicing past it."""
    assert _parse_project_name(b"\x00" * 16) is None


def test_parse_project_name_returns_none_on_unicode_decode_error() -> None:
    """Invalid UTF-8 in project_name returns None rather than raising."""
    blob = bytearray(_app_descriptor_blob("placeholder"))
    # Stuff invalid UTF-8 continuation bytes in front of the NUL.
    blob[_PROJECT_NAME_OFFSET : _PROJECT_NAME_OFFSET + 4] = b"\xff\xfe\xfd\x00"
    assert _parse_project_name(bytes(blob)) is None


def test_classify_esptool_failure_returns_unknown_for_unrecognised_output() -> None:
    """Output that matches no busy / permission / no-response pattern → ``_DETECT_UNKNOWN``."""
    assert _classify_esptool_failure("some unfamiliar esptool error") == _DETECT_UNKNOWN


def test_read_descriptor_file_returns_none_on_oserror(tmp_path: Path) -> None:
    """Missing file → ``None`` (read failure surfaced as "no manifest")."""
    assert _read_descriptor_file(str(tmp_path / "does-not-exist.bin")) is None


async def test_run_esptool_calls_run_subprocess_capture_with_resolved_cmd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_esptool`` resolves the CLI via ``_find_esptool_cmd`` and forwards args.

    Mocks ``run_subprocess_capture`` (the underlying one-shot helper) and
    asserts the full argv shape — sibling-script first slot, then the
    caller's args — plus that the captured ``(rc, stdout, timed_out)``
    tuple is propagated verbatim.
    """
    captured_args: list[tuple[Any, ...]] = []
    captured_kwargs: dict[str, Any] = {}

    class _FakeCaptured:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = b"Chip type:          ESP32-C3\n"
            self.timed_out = False

    async def fake_capture(*args: Any, **kwargs: Any) -> _FakeCaptured:
        captured_args.append(args)
        captured_kwargs.update(kwargs)
        return _FakeCaptured()

    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.chip_detect.run_subprocess_capture", fake_capture
    )

    rc, stdout, timed_out = await _run_esptool(["--port", "/dev/ttyUSB0", "chip-id"], 30.0)

    assert rc == 0
    assert stdout == b"Chip type:          ESP32-C3\n"
    assert timed_out is False
    # Forwarded args land after the resolved CLI prefix, timeout is keyword.
    assert captured_args[0][-3:] == ("--port", "/dev/ttyUSB0", "chip-id")
    assert captured_kwargs == {"timeout": 30.0}


async def test_run_esptool_substitutes_minus_one_when_returncode_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_esptool`` maps a None returncode (timeout-kill) to ``-1``.

    Otherwise a falsy ``None`` could pass for a clean exit and the
    caller would treat the empty stdout as a parse failure.
    """

    class _FakeCaptured:
        def __init__(self) -> None:
            self.returncode = None
            self.stdout = b""
            self.timed_out = True

    async def fake_capture(*args: Any, **kwargs: Any) -> _FakeCaptured:
        return _FakeCaptured()

    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.chip_detect.run_subprocess_capture", fake_capture
    )

    rc, _stdout, timed_out = await _run_esptool(["--port", "/dev/ttyUSB0", "chip-id"], 1.0)
    assert rc == -1
    assert timed_out is True


async def test_detect_chip_omits_board_id_when_read_flash_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest read returning non-zero → ``board_id`` omitted, chip info kept.

    Exercises the ``timed_out or returncode != 0`` early-return inside
    ``_read_app_descriptor_board_id`` — the other manifest tests
    write a blob unconditionally, so they don't reach this branch.
    """

    def fake(args: list[str]) -> tuple[int, bytes]:
        if "chip-id" in args:
            return 0, b"Chip type:          ESP32-C3\n"
        # read-flash fails — non-zero exit, no blob written.
        return 1, b"timeout reading flash"

    _mock_run_esptool(monkeypatch, fake)
    controller = _make_controller(tmp_path)

    result = await controller.detect_chip_cmd(port="/dev/ttyUSB0")

    assert result == {
        "chip_family": "ESP32-C3",
        "variant": "esp32c3",
        "platform": "esp32",
    }
    assert "board_id" not in result


def test_is_valid_port_name_accepts_real_paths() -> None:
    assert _is_valid_port_name("/dev/ttyUSB0")
    assert _is_valid_port_name("/dev/cu.usbserial-10")
    assert _is_valid_port_name("COM3")
    assert _is_valid_port_name("COM127")


def test_is_valid_port_name_rejects_traversal_and_metacharacters() -> None:
    # Defence-in-depth — esptool would itself reject these, but
    # keeping the gate at the WS boundary means a malicious port
    # arg can't ever reach the subprocess argv.
    assert not _is_valid_port_name("/dev/../etc/passwd")
    assert not _is_valid_port_name("/dev/tty;rm")
    assert not _is_valid_port_name("/dev/tty USB0")
    assert not _is_valid_port_name("/etc/passwd")
    assert not _is_valid_port_name("")
    assert not _is_valid_port_name("not-a-port")


def _mock_run_esptool(monkeypatch: pytest.MonkeyPatch, side_effect):
    """Wire a fake ``_run_esptool`` that dispatches by argv shape.

    ``side_effect`` is a callable ``(args: list[str]) → (rc, stdout_bytes)``
    or ``(rc, stdout_bytes, timed_out)``; the wrapper pads a missing
    ``timed_out`` to ``False`` so happy-path tests stay concise. The
    side-effect runs via ``asyncio.to_thread`` because the manifest-
    read tests use ``Path.write_bytes`` to plant a synthetic blob in
    the tempfile esptool would otherwise produce — blockbuster
    flags that as a sync write on the event-loop thread otherwise.
    """

    async def fake(args: list[str], timeout: float) -> tuple[int, bytes, bool]:
        result = await asyncio.to_thread(side_effect, args)
        if len(result) == 2:
            rc, stdout = result
            return rc, stdout, False
        return result

    monkeypatch.setattr("esphome_device_builder.controllers.config.chip_detect._run_esptool", fake)


async def test_detect_chip_returns_chip_and_board_id_on_full_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both esptool calls succeed → result carries chip info AND board_id."""
    blob = _app_descriptor_blob("starter-kit")

    def fake(args: list[str]) -> tuple[int, bytes]:
        if "chip-id" in args:
            return 0, b"Chip type:          ESP32-C3 (QFN32) (revision v0.3)\n"
        # read-flash writes the blob to the tempfile path (last positional)
        Path(args[-1]).write_bytes(blob)
        return 0, b""

    _mock_run_esptool(monkeypatch, fake)
    controller = _make_controller(tmp_path)

    result = await controller.detect_chip_cmd(port="/dev/ttyUSB0")

    assert result == {
        "chip_family": "ESP32-C3",
        "variant": "esp32c3",
        "platform": "esp32",
        "board_id": "starter-kit",
    }


async def test_detect_chip_omits_board_id_when_manifest_read_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest read failure is non-fatal — chip info still returned."""

    def fake(args: list[str]) -> tuple[int, bytes]:
        if "chip-id" in args:
            return 0, b"Chip type:          ESP32-C3\n"
        # Simulate a non-IDF app (magic mismatch) so the read succeeds
        # but the parser bails — exercises the parse-failure branch.
        Path(args[-1]).write_bytes(b"\xff" * _APP_DESC_SIZE)
        return 0, b""

    _mock_run_esptool(monkeypatch, fake)
    controller = _make_controller(tmp_path)

    result = await controller.detect_chip_cmd(port="/dev/ttyUSB0")

    assert result == {
        "chip_family": "ESP32-C3",
        "variant": "esp32c3",
        "platform": "esp32",
    }
    assert "board_id" not in result


async def test_detect_chip_rejects_missing_or_invalid_port(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd()
    assert exc.value.code == ErrorCode.INVALID_ARGS

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/etc/passwd")
    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_detect_chip_surfaces_port_busy_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port-busy errors surface a message that points at closing the other app."""
    posix_blob = (
        b"esptool v5.2.0\n"
        b"A fatal error occurred: Could not open /dev/ttyUSB0, the port is busy or doesn't exist.\n"
        b"([Errno 16] could not open port /dev/ttyUSB0: [Errno 16] Resource busy)\n"
    )
    _mock_run_esptool(monkeypatch, lambda args: (1, posix_blob))
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "another application" in exc.value.message.lower() or (
        "web serial" in exc.value.message.lower()
    )
    assert "/dev/ttyUSB0" in exc.value.message


async def test_detect_chip_surfaces_no_response_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent chip surfaces a message that points at the cable / BOOT button."""
    _mock_run_esptool(
        monkeypatch,
        lambda args: (
            1,
            b"A fatal error occurred: Failed to connect to ESP32: No serial data received.",
        ),
    )
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "cable" in exc.value.message.lower() or "boot" in exc.value.message.lower()


async def test_detect_chip_surfaces_permission_denied_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux EACCES surfaces a ``dialout`` group hint, not the busy-port copy."""
    _mock_run_esptool(
        monkeypatch,
        lambda args: (
            1,
            b"[Errno 13] could not open port /dev/ttyUSB0: "
            b"PermissionError(13, 'Permission denied')",
        ),
    )
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "dialout" in exc.value.message.lower()


async def test_detect_chip_surfaces_timeout_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess timeout surfaces unplug/replug advice, not the unknown-error copy."""
    _mock_run_esptool(monkeypatch, lambda args: (-1, b"", True))
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "didn't finish in time" in exc.value.message.lower()


async def test_detect_chip_surfaces_unknown_chip_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognised chip family points the user at the manual board picker."""
    _mock_run_esptool(
        monkeypatch,
        lambda args: (0, b"Chip type:          ESP32-Z99 (revision v9.9)\n"),
    )
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "manually" in exc.value.message.lower()


async def test_detect_chip_surfaces_no_esptool_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``No module named esptool`` output surfaces an esptool-install hint."""
    _mock_run_esptool(
        monkeypatch,
        lambda args: (1, b"/usr/bin/python3: No module named esptool\n"),
    )
    controller = _make_controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.detect_chip_cmd(port="/dev/ttyUSB0")
    assert exc.value.code == ErrorCode.UNAVAILABLE
    assert "esptool" in exc.value.message.lower()


async def test_get_serial_ports_returns_empty_when_no_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No serial ports → empty list, not None or an exception.

    The dashboard's flash-target dropdown renders "No serial
    ports available" off an empty list; a regression that
    returned ``None`` would break iteration in the frontend.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.controller.get_serial_ports",
        list,
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == []


# ---------------------------------------------------------------------------
# Labels — global catalog + per-device assignments
# ---------------------------------------------------------------------------


def test_load_labels_returns_empty_when_missing(tmp_path: Path) -> None:
    """A fresh install has no ``_labels`` key — load returns ``[]``."""
    assert load_labels(tmp_path) == []


def test_load_labels_skips_corrupt_entries(tmp_path: Path) -> None:
    """Malformed entries don't take the whole catalog down.

    Labels are advisory — a hand-edited sidecar that landed a
    non-dict entry, or an entry missing required fields, would
    otherwise raise ``KeyError`` on every catalog read and lock the
    user out of working with the rest of their labels. The
    implementation skips bad entries silently.
    """
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps(
            {
                "_labels": [
                    {"id": "good1", "name": "Kitchen", "color": "#ff0000"},
                    "garbage-string-not-a-dict",
                    {"name": "missing-id", "color": None},
                    {"id": "good2", "name": "Dev", "color": None},
                ]
            }
        ).encode()
    )

    labels = load_labels(tmp_path)

    assert [lbl.id for lbl in labels] == ["good1", "good2"]


def test_save_labels_round_trip(tmp_path: Path) -> None:
    """``save_labels`` writes a list of dicts the loader reads back identically."""
    catalog = [
        Label(id="abc", name="Kitchen", color="#ff0000"),
        Label(id="xyz", name="Dev", color=None),
    ]

    save_labels(tmp_path, catalog)

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw["_labels"] == [
        {"id": "abc", "name": "Kitchen", "color": "#ff0000"},
        {"id": "xyz", "name": "Dev", "color": None},
    ]
    assert load_labels(tmp_path) == catalog


def test_labels_transaction_yields_mutable_list(tmp_path: Path) -> None:
    """The RMW context yields a list the caller can mutate in-place.

    Mirrors ``metadata_transaction`` — the caller appends / replaces
    entries, the helper writes the canonical encoded form back on
    clean exit. A regression that switched to passing a snapshot
    (instead of the live list) would silently swallow mutations.
    """
    save_labels(tmp_path, [Label(id="a", name="One")])

    with labels_transaction(tmp_path) as catalog:
        assert [lbl.id for lbl in catalog] == ["a"]
        catalog.append(Label(id="b", name="Two", color="#00ff00"))

    assert [lbl.id for lbl in load_labels(tmp_path)] == ["a", "b"]


def test_labels_transaction_discards_changes_on_exception(tmp_path: Path) -> None:
    """A raise inside the block keeps the prior catalog intact."""
    save_labels(tmp_path, [Label(id="a", name="One")])

    with (
        pytest.raises(RuntimeError, match="boom"),
        labels_transaction(tmp_path) as catalog,
    ):
        catalog.append(Label(id="b", name="Two"))
        raise RuntimeError("boom")

    assert [lbl.id for lbl in load_labels(tmp_path)] == ["a"]


def test_set_device_metadata_labels_param_replaces(tmp_path: Path) -> None:
    """Pass a populated list → the device entry's ``labels`` is replaced."""
    set_device_metadata(tmp_path, "kitchen.yaml", labels=["a", "b"])

    raw = _load_metadata(tmp_path)
    assert raw["kitchen.yaml"]["labels"] == ["a", "b"]


def test_set_device_metadata_labels_none_leaves_alone(tmp_path: Path) -> None:
    """``labels=None`` → existing assignments are preserved.

    Tri-state semantics matching the rest of ``set_device_metadata``:
    ``None`` = leave alone, ``[]`` = clear, populated = replace.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", labels=["a"])
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32", labels=None)

    raw = _load_metadata(tmp_path)
    assert raw["kitchen.yaml"]["labels"] == ["a"]
    assert raw["kitchen.yaml"]["board_id"] == "esp32"


def test_set_device_metadata_labels_empty_clears(tmp_path: Path) -> None:
    """``labels=[]`` removes the key entirely (no empty list left behind)."""
    set_device_metadata(tmp_path, "kitchen.yaml", labels=["a", "b"])
    set_device_metadata(tmp_path, "kitchen.yaml", labels=[])

    entry = _load_metadata(tmp_path)["kitchen.yaml"]
    assert "labels" not in entry


def test_set_device_labels_validates_against_catalog(tmp_path: Path) -> None:
    """An ID not in the catalog raises ``ValueError`` and skips the write."""
    save_labels(tmp_path, [Label(id="known", name="Known")])

    with pytest.raises(ValueError, match="Unknown label id"):
        set_device_labels(tmp_path, "kitchen.yaml", ["known", "ghost"])

    # No partial write — the device entry shouldn't carry "known"
    # alone if the call as a whole was supposed to fail.
    raw = _load_metadata(tmp_path)
    assert "kitchen.yaml" not in raw


def test_set_device_labels_dedupes_and_preserves_order(tmp_path: Path) -> None:
    """Duplicate IDs in input are dropped; first-seen order wins."""
    save_labels(tmp_path, [Label(id="a", name="A"), Label(id="b", name="B")])

    set_device_labels(tmp_path, "kitchen.yaml", ["a", "b", "a", "b", "a"])

    raw = _load_metadata(tmp_path)
    assert raw["kitchen.yaml"]["labels"] == ["a", "b"]


def test_set_device_labels_empty_clears(tmp_path: Path) -> None:
    """Passing ``[]`` removes all assignments without leaving the empty key."""
    save_labels(tmp_path, [Label(id="a", name="A")])
    set_device_labels(tmp_path, "kitchen.yaml", ["a"])
    set_device_labels(tmp_path, "kitchen.yaml", [])

    entry = _load_metadata(tmp_path)["kitchen.yaml"]
    assert "labels" not in entry


def test_delete_label_cascade_drops_label_and_returns_affected(tmp_path: Path) -> None:
    """Cascade removes the label from the catalog and every device entry.

    This is the operation the controller wraps; the returned
    set is the worklist of devices the controller force-reloads
    so their live ``Device`` model picks up the trimmed list.
    """
    save_labels(
        tmp_path,
        [Label(id="x", name="X"), Label(id="y", name="Y")],
    )
    set_device_labels(tmp_path, "kitchen.yaml", ["x", "y"])
    set_device_labels(tmp_path, "garage.yaml", ["x"])
    set_device_labels(tmp_path, "office.yaml", ["y"])

    found, affected = delete_label_cascade(tmp_path, "x")

    assert found is True
    assert affected == {"kitchen.yaml", "garage.yaml"}
    raw = _load_metadata(tmp_path)
    # Catalog drops the deleted label.
    assert [entry["id"] for entry in raw["_labels"]] == ["y"]
    # Devices that referenced it have it removed; the others are
    # untouched.
    assert raw["kitchen.yaml"]["labels"] == ["y"]
    assert "labels" not in raw["garage.yaml"]
    assert raw["office.yaml"]["labels"] == ["y"]


def test_delete_label_cascade_when_no_devices_assigned(tmp_path: Path) -> None:
    """A label with no assignments → ``found=True`` and empty affected set."""
    save_labels(tmp_path, [Label(id="ghost", name="Ghost")])

    found, affected = delete_label_cascade(tmp_path, "ghost")

    assert found is True
    assert affected == set()
    assert load_labels(tmp_path) == []


def test_delete_label_cascade_unknown_id_reports_not_found(tmp_path: Path) -> None:
    """Deleting an id that isn't in the catalog returns ``found=False``."""
    save_labels(tmp_path, [Label(id="known", name="Known")])

    found, affected = delete_label_cascade(tmp_path, "ghost")

    assert found is False
    assert affected == set()
    # Existing catalog entry untouched.
    assert [lbl.id for lbl in load_labels(tmp_path)] == ["known"]


def test_delete_label_cascade_removes_corrupt_entry(tmp_path: Path) -> None:
    """A corrupt catalog entry (missing required fields) is still deletable.

    The existence check works against the raw on-disk dict — not the
    decoded ``Label`` instances ``load_labels`` returns — so a
    hand-edited or partially-written entry that ``Label.from_dict``
    would reject can still be cleaned up via ``delete_label``.
    """
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps(
            {
                "_labels": [
                    {"id": "corrupt"},  # missing ``name`` — Label.from_dict raises
                    {"id": "good", "name": "Good", "color": None},
                ]
            }
        ).encode()
    )

    found, affected = delete_label_cascade(tmp_path, "corrupt")

    assert found is True
    assert affected == set()
    # Catalog now carries only the well-formed entry.
    raw = _load_metadata(tmp_path)
    assert [entry["id"] for entry in raw["_labels"]] == ["good"]


def test_set_device_labels_rejects_non_string_items(tmp_path: Path) -> None:
    """Non-string items in ``label_ids`` raise ``TypeError``.

    The controller wraps this into ``CommandError(INVALID_ARGS)``.
    Silent skipping would let a bad payload effectively clear all
    labels, which is surprising and user-hostile.
    """
    save_labels(tmp_path, [Label(id="known", name="Known")])

    with pytest.raises(TypeError, match="label_ids must be strings"):
        set_device_labels(tmp_path, "kitchen.yaml", ["known", 42])  # type: ignore[list-item]

    # No partial write happened.
    assert "kitchen.yaml" not in _load_metadata(tmp_path)


# ---------------------------------------------------------------------------
# Coverage fillers exposed by the package split
# ---------------------------------------------------------------------------


async def test_get_version_returns_server_and_esphome_versions(tmp_path: Path) -> None:
    """``config/version`` reports both the server and the esphome version."""
    controller = _make_controller(tmp_path)
    result = await controller.get_version()
    assert set(result) == {"server_version", "esphome_version"}


def test_status_use_mqtt_reflects_env(
    make_settings: MakeSettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``status_use_mqtt`` mirrors ``ESPHOME_DASHBOARD_USE_MQTT``."""
    settings = make_settings()
    monkeypatch.delenv("ESPHOME_DASHBOARD_USE_MQTT", raising=False)
    assert settings.status_use_mqtt is False
    monkeypatch.setenv("ESPHOME_DASHBOARD_USE_MQTT", "true")
    assert settings.status_use_mqtt is True


def test_metadata_transaction_persists_without_fcntl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-fcntl degraded path (Windows / no fcntl) still round-trips the write."""
    monkeypatch.setattr(config_module.metadata, "_HAS_FCNTL", False)

    with metadata_transaction(tmp_path) as data:
        data["kitchen.yaml"] = {"board_id": "esp32"}

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw == {"kitchen.yaml": {"board_id": "esp32"}}


def test_clear_volatile_device_metadata_drops_emptied_entry(tmp_path: Path) -> None:
    """Clearing the last volatile field drops the now-empty entry; mixed entries survive."""
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps(
            {
                "volatile_only.yaml": {"mac_address": "AA:BB:CC:DD:EE:FF"},
                "mixed.yaml": {"board_id": "esp32", "mac_address": "AA:BB:CC:DD:EE:FF"},
            }
        ).encode()
    )

    clear_volatile_device_metadata(tmp_path, "volatile_only.yaml")
    clear_volatile_device_metadata(tmp_path, "mixed.yaml")

    raw = _load_metadata(tmp_path)
    assert "volatile_only.yaml" not in raw
    assert raw["mixed.yaml"] == {"board_id": "esp32"}


def test_set_device_labels_treats_non_list_catalog_as_empty(tmp_path: Path) -> None:
    """A corrupt non-list ``_labels`` block reads as an empty catalog."""
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps({"_labels": {"corrupt": "mapping"}}).encode()
    )

    # Empty assignment has nothing to validate, so it succeeds; no labels are written.
    set_device_labels(tmp_path, "kitchen.yaml", [])
    assert _load_metadata(tmp_path)["kitchen.yaml"] == {}

    # Any real id can't match the empty catalog → rejected.
    with pytest.raises(ValueError, match="Unknown label id"):
        set_device_labels(tmp_path, "kitchen.yaml", ["a"])


def test_set_device_labels_overwrites_non_dict_entry(tmp_path: Path) -> None:
    """A corrupt non-dict device entry is replaced with a fresh mapping rather than crashing."""
    (tmp_path / ".device-builder.json").write_bytes(
        json.dumps({"_labels": [{"id": "a", "name": "A"}], "kitchen.yaml": "corrupt"}).encode()
    )

    set_device_labels(tmp_path, "kitchen.yaml", ["a"])

    assert _load_metadata(tmp_path)["kitchen.yaml"] == {"labels": ["a"]}
