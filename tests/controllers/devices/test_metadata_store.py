"""Tests for ``DeviceMetadataStore``."""

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
    """First-run migration pulls store-shaped fields out of the shared sidecar."""
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
    """Pre-existing new file wins; orphan fields in the shared sidecar are left alone."""
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
    """A shared-sidecar entry holding only store-shaped fields collapses out."""
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
    """Second ``async_load`` reads the new file; shared sidecar stays byte-identical."""
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
    """Pre-PR sidecar shape survives migration end-to-end through the resolver."""
    await seed_device(tmp_path, "kitchen.yaml")
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
    # ``labels`` isn't exposed via ``set_device_metadata`` so go through the raw helper.
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
    """A non-dict entry in the shared sidecar doesn't trip the strip phase."""
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
    assert shared["good.yaml"] == {"board_id": "esp32"}
    assert shared["bad.yaml"] == "not-a-dict"


@pytest.mark.asyncio
async def test_migration_strip_handles_concurrent_corruption(tmp_path: Path) -> None:
    """A migrated entry that turns non-dict between read and strip skips cleanly."""
    store = _make_store(tmp_path)
    store._state = {"kitchen.yaml": {"ip": "10.0.0.1"}}
    await asyncio.to_thread(_save_metadata, tmp_path, {"kitchen.yaml": "not-a-dict"})

    await asyncio.to_thread(store._migrate_strip_shared_sync, ["kitchen.yaml"])

    shared = await asyncio.to_thread(_load_metadata, tmp_path)
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


@pytest.mark.asyncio
async def test_update_replaces_inner_dict_never_mutates_in_place(tmp_path: Path) -> None:
    """Captured reference sees the pre-update state — no in-place writes."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1")
    captured = store._state["kitchen.yaml"]

    store.update("kitchen.yaml", deployed_version="2026.5.1")

    assert captured == {"ip": "10.0.0.1"}
    assert store._state["kitchen.yaml"] is not captured


# ---------------------------------------------------------------------------
# set_field — bypasses tri-state for plaintext-confirmed and similar
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_field_writes_empty_string_literally(tmp_path: Path) -> None:
    """``set_field`` persists the empty-string sentinel that ``update`` would clear."""
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
async def test_set_field_replaces_inner_dict_never_mutates_in_place(tmp_path: Path) -> None:
    """Captured reference sees the pre-write state — no in-place writes."""
    store = _make_store(tmp_path)
    store.set_field("kitchen.yaml", "api_encryption_active", "cipher")
    captured = store._state["kitchen.yaml"]

    store.set_field("kitchen.yaml", "api_encryption_active", "")

    assert captured == {"api_encryption_active": "cipher"}
    assert store._state["kitchen.yaml"] is not captured


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
    """Re-asserting the same value doesn't reschedule a save."""
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
    await store._store.async_save_now()

    await store.remove("kitchen.yaml")

    assert store.get("kitchen.yaml") == {}
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
    """A reference held mid-iteration sees the pre-clear state, not a half-cleared one."""
    store = _make_store(tmp_path)
    store.update("kitchen.yaml", ip="10.0.0.1", deployed_config_hash="abc12345")
    captured = store._state["kitchen.yaml"]
    assert captured == {"ip": "10.0.0.1", "deployed_config_hash": "abc12345"}

    store.clear_volatile("kitchen.yaml")

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
    """Three mutations in a row collapse into one ``async_save_now`` flush."""
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
