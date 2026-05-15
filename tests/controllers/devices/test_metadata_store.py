"""Tests for ``DeviceMetadataStore``.

Pin the contract the controller relies on: RAM-canonical reads,
tri-state field semantics, idempotent updates, one-shot migration
out of the shared sidecar, and the volatile-field clear used by
the archive flow. The store's debounced disk write is pinned by
the underlying ``helpers.storage.Store`` tests; this file tests
the per-device adapter's behaviour on top.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.config import (
    _load_metadata,
    _save_metadata,
)
from esphome_device_builder.controllers.devices._metadata_store import (
    _DEFAULT_SAVE_DELAY,
    STORE_FIELDS,
    DeviceMetadataStore,
)


def _make_store(tmp_path: Path) -> DeviceMetadataStore:
    """Build a store anchored at *tmp_path* with a noop shutdown register."""
    return DeviceMetadataStore(
        config_dir=tmp_path,
        data_dir=tmp_path,
        shutdown_register=lambda _cb: None,
    )


# ---------------------------------------------------------------------------
# async_load: empty / migration / new-file paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_load_with_no_files_leaves_state_empty(tmp_path: Path) -> None:
    """No shared sidecar + no new file → empty RAM, no disk writes."""
    store = _make_store(tmp_path)

    await store.async_load()

    assert store.snapshot_all() == {}
    assert not (tmp_path / ".device-builder-devices.json").exists()
    assert not (tmp_path / ".device-builder.json").exists()


@pytest.mark.asyncio
async def test_async_load_migrates_live_fields_from_shared_sidecar(tmp_path: Path) -> None:
    """First run after upgrade pulls store-shaped fields out of the shared sidecar.

    Identity fields (``board_id``, ``labels``, ``friendly_name``)
    stay in the shared sidecar; only live observation + cache
    fields migrate. The new file lands on disk after migration.
    """
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "_labels": [{"id": "abc", "name": "Bedroom"}],
            "kitchen.yaml": {
                "board_id": "esp32-c3-devkitm-1",
                "labels": ["abc"],
                "friendly_name": "Kitchen",
                "ip": "192.168.1.42",
                "expected_config_hash": "deadbeef",
                "mac_address": "94:C9:60:1F:8C:F1",
                "build_size_bytes": 12345,
            },
        },
    )
    store = _make_store(tmp_path)

    await store.async_load()

    snap = store.snapshot_all()
    assert snap == {
        "kitchen.yaml": {
            "ip": "192.168.1.42",
            "expected_config_hash": "deadbeef",
            "build_size_bytes": 12345,
        }
    }
    # Shared sidecar: identity + labels + mac_address survive; only
    # store-shaped fields strip out.
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["_labels"] == [{"id": "abc", "name": "Bedroom"}]
    assert shared["kitchen.yaml"] == {
        "board_id": "esp32-c3-devkitm-1",
        "labels": ["abc"],
        "friendly_name": "Kitchen",
        "mac_address": "94:C9:60:1F:8C:F1",
    }
    assert (tmp_path / ".device-builder-devices.json").exists()


@pytest.mark.asyncio
async def test_async_load_skips_migration_when_new_file_exists(tmp_path: Path) -> None:
    """Pre-existing new file is the source of truth; pins crash-recovery shape.

    Also covers the "crashed between flush and strip" recovery
    path: the shared sidecar still carries orphan store-shaped
    fields and the new file holds the migrated state. Next boot
    must read the new file and leave the orphan data alone.
    """
    new_path = tmp_path / ".device-builder-devices.json"
    new_path.write_bytes(b'{"kitchen.yaml": {"ip": "10.0.0.1"}}')
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {"kitchen.yaml": {"ip": "192.168.1.42", "expected_config_hash": "stale"}},
    )

    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot_all() == {"kitchen.yaml": {"ip": "10.0.0.1"}}
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["kitchen.yaml"] == {
        "ip": "192.168.1.42",
        "expected_config_hash": "stale",
    }


@pytest.mark.asyncio
async def test_async_load_drops_shared_entry_with_only_store_fields(tmp_path: Path) -> None:
    """An entry holding ONLY store-shaped fields collapses out of the shared sidecar.

    Without the drop the shared sidecar would carry an empty
    dict for every device that's never had an identity field
    written (legacy fleets pre-PR where the user never named
    a board through the wizard).
    """
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "kitchen.yaml": {
                "ip": "192.168.1.42",
                "expected_config_hash": "deadbeef",
            },
        },
    )
    store = _make_store(tmp_path)

    await store.async_load()

    assert store.snapshot_all() == {
        "kitchen.yaml": {"ip": "192.168.1.42", "expected_config_hash": "deadbeef"}
    }
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert "kitchen.yaml" not in shared


@pytest.mark.asyncio
async def test_async_load_migration_is_idempotent_across_loads(tmp_path: Path) -> None:
    """A second ``async_load`` reads the new file; the shared sidecar isn't re-stripped.

    Pins crash-free repeat boots: the new file is canonical
    after the first migration, and subsequent loads must
    never re-enter the migration path.
    """
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "kitchen.yaml": {
                "board_id": "esp32-c3-devkitm-1",
                "ip": "192.168.1.42",
                "expected_config_hash": "deadbeef",
            },
        },
    )

    first = _make_store(tmp_path)
    await first.async_load()
    # Snapshot the shared-sidecar state after migration; a second
    # load must leave it byte-identical.
    shared_after_first = await asyncio.to_thread(_load_metadata, tmp_path)

    second = _make_store(tmp_path)
    await second.async_load()

    assert second.snapshot_all() == first.snapshot_all()
    assert await asyncio.to_thread(_load_metadata, tmp_path) == shared_after_first


@pytest.mark.asyncio
async def test_async_load_preserves_top_level_catalogs(tmp_path: Path) -> None:
    """Migration leaves ``_labels`` / ``_preferences`` / ``_remote_build`` alone."""
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "_labels": [{"id": "abc", "name": "Bedroom"}],
            "_preferences": {"theme": "dark"},
            "_remote_build": {"enabled": True},
            "kitchen.yaml": {
                "board_id": "esp32",
                "ip": "192.168.1.42",
            },
        },
    )
    store = _make_store(tmp_path)

    await store.async_load()

    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["_labels"] == [{"id": "abc", "name": "Bedroom"}]
    assert shared["_preferences"] == {"theme": "dark"}
    assert shared["_remote_build"] == {"enabled": True}


@pytest.mark.asyncio
async def test_e2e_pre_pr_shape_migrates_through_resolver(
    tmp_path: Path,
    make_controller: Any,
    seed_device: Any,
) -> None:
    """Pre-PR sidecar shape survives migration end-to-end.

    Drives the full ``async_load`` → ``_resolve_device_metadata``
    → ``DeviceFileMetadata`` path: seed a realistic pre-PR
    ``.device-builder.json`` with mixed live + identity fields,
    run migration, then assert the resolver sees every field via
    its two-source split (identity from shared, live from store).
    """
    await seed_device(tmp_path, "kitchen.yaml")
    # Pre-PR shape: every field that used to live in
    # ``.device-builder.json``, both identity and live state.
    pre_pr = {
        "board_id": "esp32-c3-devkitm-1",
        "friendly_name": "Kitchen Sensor",
        "comment": "By the toaster",
        "mac_address": "94:C9:60:1F:8C:F1",
        "ip": "192.168.1.42",
        "expected_config_hash": "deadbeef",
        "deployed_config_hash": "12345678",
        "deployed_version": "2026.5.1",
        "api_encryption_active": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        "build_size_bytes": 12345,
        "build_size_dir_mtime": 1714900000,
        "build_size_info_mtime": 1714900050,
    }
    # Use the raw ``_save_metadata`` to land everything (including
    # ``labels``, which ``set_device_metadata`` doesn't expose) in
    # one shot, matching what an existing user's pre-PR file holds.
    existing = await asyncio.to_thread(_load_metadata, tmp_path)
    existing["kitchen.yaml"] = {**existing.get("kitchen.yaml", {}), **pre_pr, "labels": ["abc"]}
    existing["_labels"] = [{"id": "abc", "name": "Bedroom"}]
    await asyncio.to_thread(_save_metadata, tmp_path, existing)

    controller = make_controller(tmp_path)
    await controller._metadata_store.async_load()

    # ``_resolve_device_metadata`` runs in the scanner's executor
    # thread in production (it reads ``build_info.json`` and the
    # shared sidecar from disk); the test mirrors that.
    metadata = await asyncio.to_thread(
        controller._resolve_device_metadata, tmp_path, "kitchen.yaml"
    )

    # Identity fields (shared sidecar source).
    assert metadata.board_id == "esp32-c3-devkitm-1"
    assert metadata.mac_address == "94:C9:60:1F:8C:F1"
    assert metadata.labels == ("abc",)
    # Live state (store source).
    assert metadata.ip == "192.168.1.42"
    assert metadata.expected_config_hash == "deadbeef"
    assert metadata.deployed_config_hash == "12345678"
    assert metadata.deployed_version == "2026.5.1"
    assert metadata.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
    assert metadata.build_size_bytes == 12345

    # On-disk state: shared keeps identity + labels + top-level
    # catalogs; store holds the live fields; ``mac_address`` stays
    # in shared since it's intrinsic to the physical board.
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["_labels"] == [{"id": "abc", "name": "Bedroom"}]
    assert shared["kitchen.yaml"] == {
        "board_id": "esp32-c3-devkitm-1",
        "friendly_name": "Kitchen Sensor",
        "comment": "By the toaster",
        "labels": ["abc"],
        "mac_address": "94:C9:60:1F:8C:F1",
    }
    store_entry = controller._metadata_store.get("kitchen.yaml")
    assert store_entry == {
        "ip": "192.168.1.42",
        "expected_config_hash": "deadbeef",
        "deployed_config_hash": "12345678",
        "deployed_version": "2026.5.1",
        "api_encryption_active": "Noise_NNpsk0_25519_ChaChaPoly_SHA256",
        "build_size_bytes": 12345,
        "build_size_dir_mtime": 1714900000,
        "build_size_info_mtime": 1714900050,
    }


@pytest.mark.asyncio
async def test_async_load_recovers_from_corrupt_store_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt JSON in the store file → empty state + warning, not a crash."""
    (tmp_path / ".device-builder-devices.json").write_bytes(b"{not valid json")

    store = _make_store(tmp_path)
    with caplog.at_level("WARNING"):
        await store.async_load()

    assert store.snapshot_all() == {}
    assert any("corrupt JSON" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_async_load_recovers_from_non_dict_store_json(tmp_path: Path) -> None:
    """A non-dict top-level value in the store file decodes as empty state."""
    (tmp_path / ".device-builder-devices.json").write_bytes(b"[1, 2, 3]")

    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot_all() == {}


@pytest.mark.asyncio
async def test_migration_strip_skips_non_dict_entries(tmp_path: Path) -> None:
    """A non-dict entry in the shared sidecar doesn't trip the strip phase.

    Defensive — a hand-edited / corrupt sidecar shouldn't crash
    migration. The read phase already filters by ``isinstance``;
    pin the same shape on the strip side too.
    """
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "good.yaml": {"ip": "10.0.0.1", "board_id": "esp32"},
            "bad.yaml": "not-a-dict",
        },
    )

    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot_all() == {"good.yaml": {"ip": "10.0.0.1"}}
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    # ``good.yaml``'s store field stripped; identity stays.
    assert shared["good.yaml"] == {"board_id": "esp32"}
    # ``bad.yaml`` non-dict left untouched (not in migrated keys, so
    # the strip phase doesn't pop it).
    assert shared["bad.yaml"] == "not-a-dict"


@pytest.mark.asyncio
async def test_migration_strip_handles_concurrent_corruption(tmp_path: Path) -> None:
    """If a migrated entry turns non-dict between read and strip, skip cleanly.

    Pin the defensive ``isinstance(entry, dict)`` guard inside
    the strip transaction: a concurrent writer could in theory
    replace a per-device entry with a non-dict value between
    the read transaction snapshotting it and the strip transaction
    re-reading it. The guard ensures the strip pass doesn't
    crash on the racy entry.
    """
    store = _make_store(tmp_path)
    # Pretend the read phase already happened and pulled
    # ``kitchen.yaml``'s store fields into RAM.
    store._state = {"kitchen.yaml": {"ip": "10.0.0.1"}}
    # Race: the shared sidecar's entry for ``kitchen.yaml`` is now
    # a non-dict (concurrent corruption / external edit).
    await asyncio.to_thread(_save_metadata, tmp_path, {"kitchen.yaml": "not-a-dict"})

    # Direct strip call mimics the second migration transaction.
    await asyncio.to_thread(store._migrate_strip_shared_sync, ["kitchen.yaml"])

    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    # Bad entry left as-is; strip phase didn't crash or pop it.
    assert shared == {"kitchen.yaml": "not-a-dict"}


@pytest.mark.asyncio
async def test_round_trip_after_migration(tmp_path: Path) -> None:
    """Migration → mutate → flush → reload: full state round-trips through disk."""
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {"kitchen.yaml": {"board_id": "esp32", "ip": "192.168.1.42"}},
    )

    first = _make_store(tmp_path)
    await first.async_load()
    first.update("kitchen.yaml", deployed_version="2026.5.1")
    await first._store.async_save_now()

    second = _make_store(tmp_path)
    await second.async_load()
    assert second.get("kitchen.yaml") == {"ip": "192.168.1.42", "deployed_version": "2026.5.1"}


@pytest.mark.asyncio
async def test_async_load_drops_corrupt_non_dict_entries(tmp_path: Path) -> None:
    """A non-dict shared-sidecar entry is ignored during migration, not crashed on."""
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "good.yaml": {"ip": "10.0.0.1"},
            "bad.yaml": "not-a-dict",
            "_labels": [],
        },
    )

    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot_all() == {"good.yaml": {"ip": "10.0.0.1"}}


# ---------------------------------------------------------------------------
# get / update / tri-state semantics
# ---------------------------------------------------------------------------


def test_get_returns_empty_dict_for_unknown_filename(tmp_path: Path) -> None:
    """Missing filename → ``{}`` (not ``None``) so callers can ``.get(...)`` on it."""
    store = _make_store(tmp_path)
    assert store.get("never-seen.yaml") == {}


def test_get_returns_defensive_copy(tmp_path: Path) -> None:
    """Callers can't mutate the store's RAM via the returned dict."""
    store = _make_store(tmp_path)
    store._state["kitchen.yaml"] = {"ip": "10.0.0.1"}

    snapshot = store.get("kitchen.yaml")
    snapshot["ip"] = "MUTATED"

    assert store._state["kitchen.yaml"]["ip"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_update_merges_truthy_fields_into_entry(tmp_path: Path) -> None:
    """Truthy values write; subsequent updates merge into the entry."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1", deployed_version="2026.5.1")
    store.update("kitchen.yaml", expected_config_hash="deadbeef")

    assert store.get("kitchen.yaml") == {
        "ip": "10.0.0.1",
        "deployed_version": "2026.5.1",
        "expected_config_hash": "deadbeef",
    }


@pytest.mark.asyncio
async def test_update_treats_none_as_leave_alone(tmp_path: Path) -> None:
    """``None`` keeps the existing value."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1")
    store.update("kitchen.yaml", ip=None, deployed_version="2026.5.1")

    assert store.get("kitchen.yaml") == {"ip": "10.0.0.1", "deployed_version": "2026.5.1"}


@pytest.mark.asyncio
async def test_update_treats_falsy_as_clear(tmp_path: Path) -> None:
    """A falsy value pops the field; an empty entry drops the filename."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1", deployed_version="2026.5.1")

    store.update("kitchen.yaml", ip="")
    assert store.get("kitchen.yaml") == {"deployed_version": "2026.5.1"}

    store.update("kitchen.yaml", deployed_version="")
    assert store.get("kitchen.yaml") == {}
    assert "kitchen.yaml" not in store.snapshot_all()


# ---------------------------------------------------------------------------
# set_field — bypasses tri-state for plaintext-confirmed and similar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_field_writes_empty_string_literally(tmp_path: Path) -> None:
    """``set_field`` persists the empty-string sentinel that ``update`` would clear.

    Plaintext-confirmed ``api_encryption_active=""`` is the
    canonical case: ``update(api_encryption_active="")`` would
    clear the key under tri-state semantics, but the empty
    string IS the truth that needs persisting (distinct from
    ``None`` for "not yet observed").
    """
    store = _make_store(tmp_path)
    store.set_field("kitchen.yaml", "api_encryption_active", "")
    assert store.get("kitchen.yaml") == {"api_encryption_active": ""}


@pytest.mark.asyncio
async def test_set_field_overwrites_existing_value(tmp_path: Path) -> None:
    """A subsequent ``set_field`` replaces the prior value verbatim."""
    store = _make_store(tmp_path)
    store.set_field("kitchen.yaml", "api_encryption_active", "Noise_NNpsk0_25519_ChaChaPoly_SHA256")
    store.set_field("kitchen.yaml", "api_encryption_active", "")
    assert store.get("kitchen.yaml") == {"api_encryption_active": ""}


@pytest.mark.asyncio
async def test_set_field_no_op_when_value_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-asserting the same value doesn't wake the debounce timer."""
    schedules: list[float] = []
    store = _make_store(tmp_path)
    original = store._store.async_delay_save

    def _track(data_func: Any, delay: float = 0.0) -> None:
        schedules.append(delay)
        original(data_func, delay=delay)

    monkeypatch.setattr(store._store, "async_delay_save", _track)

    store.set_field("kitchen.yaml", "api_encryption_active", "")
    assert schedules == [_DEFAULT_SAVE_DELAY]
    store.set_field("kitchen.yaml", "api_encryption_active", "")
    assert schedules == [_DEFAULT_SAVE_DELAY]


@pytest.mark.asyncio
async def test_update_idempotent_no_op_when_value_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-asserting the same value doesn't reschedule a save.

    The idempotency check matters on the scan hot path: a
    no-op call would otherwise wake the debounce timer on
    every re-assert.
    """
    schedules: list[float] = []
    store = _make_store(tmp_path)
    original = store._store.async_delay_save

    def _track(data_func: Any, delay: float = 0.0) -> None:
        schedules.append(delay)
        original(data_func, delay=delay)

    monkeypatch.setattr(store._store, "async_delay_save", _track)

    store.update("kitchen.yaml", ip="10.0.0.1")
    assert schedules == [_DEFAULT_SAVE_DELAY]

    store.update("kitchen.yaml", ip="10.0.0.1")
    assert schedules == [_DEFAULT_SAVE_DELAY]  # second update did NOT reschedule


# ---------------------------------------------------------------------------
# remove / clear_volatile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_drops_entry_and_flushes(tmp_path: Path) -> None:
    """``remove`` pops + flushes immediately so a quick restart can't resurrect."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1")
    # Drain the initial save.
    await store._store.async_save_now()

    await store.remove("kitchen.yaml")

    assert store.get("kitchen.yaml") == {}
    # On disk: the entry is gone.
    new_file = tmp_path / ".device-builder-devices.json"
    on_disk = new_file.read_bytes()
    assert b"kitchen.yaml" not in on_disk


@pytest.mark.asyncio
async def test_remove_unknown_filename_is_noop(tmp_path: Path) -> None:
    """``remove`` for a filename never in the store doesn't touch disk."""
    store = _make_store(tmp_path)
    await store.remove("never-seen.yaml")  # no exception
    assert not (tmp_path / ".device-builder-devices.json").exists()


@pytest.mark.asyncio
async def test_clear_volatile_pops_every_store_field(tmp_path: Path) -> None:
    """``clear_volatile`` drops every store-owned field for the filename."""
    store = _make_store(tmp_path)
    store.update(
        "kitchen.yaml",
        ip="10.0.0.1",
        expected_config_hash="deadbeef",
        deployed_config_hash="abc12345",
        deployed_version="2026.5.1",
        api_encryption_active="cipher",
        build_size_bytes=4096,
        build_size_dir_mtime=1714900000,
        build_size_info_mtime=1714900050,
        regen_failed_mtime=1700000000.0,
        regen_failed_at=1700000005.0,
    )

    store.clear_volatile("kitchen.yaml")

    assert store.get("kitchen.yaml") == {}


@pytest.mark.asyncio
async def test_clear_volatile_replaces_entry_does_not_mutate_in_place(tmp_path: Path) -> None:
    """A reference held by another thread sees the pre-clear state.

    In-place mutation would let executor-thread ``get()`` calls
    observe a half-cleared entry mid-iteration.
    """
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1", deployed_config_hash="abc12345")
    captured = store._state["kitchen.yaml"]
    assert captured == {"ip": "10.0.0.1", "deployed_config_hash": "abc12345"}

    store.clear_volatile("kitchen.yaml")

    # The captured reference still holds the original entry —
    # ``clear_volatile`` replaced rather than popping in place.
    assert captured == {"ip": "10.0.0.1", "deployed_config_hash": "abc12345"}


@pytest.mark.asyncio
async def test_clear_volatile_unknown_filename_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clear_volatile`` on a missing entry doesn't schedule a save."""
    schedules: list[float] = []
    store = _make_store(tmp_path)
    original = store._store.async_delay_save

    def _track(data_func: Any, delay: float = 0.0) -> None:
        schedules.append(delay)
        original(data_func, delay=delay)

    monkeypatch.setattr(store._store, "async_delay_save", _track)

    store.clear_volatile("never-seen.yaml")
    assert schedules == []


# ---------------------------------------------------------------------------
# STORE_FIELDS shape (regression guard)
# ---------------------------------------------------------------------------


def test_store_fields_pinned() -> None:
    """Pin ``STORE_FIELDS`` so a silent addition forces a routing decision."""
    assert (
        frozenset(
            {
                "ip",
                "deployed_config_hash",
                "deployed_version",
                "api_encryption_active",
                "expected_config_hash",
                "build_size_bytes",
                "build_size_dir_mtime",
                "build_size_info_mtime",
                "regen_failed_mtime",
                "regen_failed_at",
            }
        )
        == STORE_FIELDS
    )


# ---------------------------------------------------------------------------
# debounced save coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_updates_coalesce_into_one_disk_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Three mutations in a row collapse into one ``async_save_now`` flush.

    The whole point of the store: turn N hot-path mutations
    into one debounced disk write. ``async_save_now`` cancels
    the pending delay handle and flushes the captured
    ``_data_func`` once — so monkeypatching ``_encode_and_write``
    and counting calls is the cleanest assertion of the
    coalescing guarantee.
    """
    store = _make_store(tmp_path)

    writes = 0

    def _count_writes(_value: dict[str, dict[str, Any]]) -> None:
        nonlocal writes
        writes += 1

    monkeypatch.setattr(store._store, "_encode_and_write", _count_writes)

    store.update("kitchen.yaml", ip="10.0.0.1")
    store.update("kitchen.yaml", deployed_version="2026.5.1")
    store.update("kitchen.yaml", expected_config_hash="deadbeef")

    assert writes == 0

    await store._store.async_save_now()
    assert writes == 1
    assert store.get("kitchen.yaml") == {
        "ip": "10.0.0.1",
        "deployed_version": "2026.5.1",
        "expected_config_hash": "deadbeef",
    }


# ---------------------------------------------------------------------------
# round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_then_load_round_trip(tmp_path: Path) -> None:
    """A second store instance reads back what the first one persisted."""
    first = _make_store(tmp_path)
    first.update("kitchen.yaml", ip="10.0.0.1", deployed_version="2026.5.1")
    await first._store.async_save_now()

    second = _make_store(tmp_path)
    await second.async_load()

    assert second.get("kitchen.yaml") == {
        "ip": "10.0.0.1",
        "deployed_version": "2026.5.1",
    }
