"""Tests for ``automations/get_bodies`` — the batch detail endpoint."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from esphome_device_builder.controllers.automations import catalog
from esphome_device_builder.helpers.json import dumps as json_dumps

pytestmark = pytest.mark.xdist_group("automations")


async def _hydrate_bodies(refs: list) -> dict[str, dict]:
    bodies, _ = await catalog.get_bodies(refs)
    return bodies


async def test_get_bodies_returns_full_body_for_known_ref() -> None:
    """A known ``{type, id}`` ref returns the full body dict."""
    result = await _hydrate_bodies([{"type": "triggers", "id": "on_boot"}])
    assert "triggers/on_boot" in result
    body = result["triggers/on_boot"]
    assert body["id"] == "on_boot"
    # Detail-only field: config_entries is the whole reason for the
    # batch endpoint to exist.
    assert "config_entries" in body


async def test_get_bodies_omits_unknown_type() -> None:
    """Refs with an unknown ``type`` field are silently dropped."""
    result = await _hydrate_bodies(
        [{"type": "not_a_type", "id": "something"}],
    )
    assert result == {}


async def test_get_bodies_omits_unknown_id() -> None:
    """Refs whose ``id`` isn't in the index are absent from the response."""
    result = await _hydrate_bodies([{"type": "triggers", "id": "does.not.exist"}])
    assert result == {}


async def test_get_bodies_dedupes_repeated_refs() -> None:
    """Repeated ``(type, id)`` refs collapse to one entry in the response."""
    result = await _hydrate_bodies(
        [
            {"type": "triggers", "id": "on_boot"},
            {"type": "triggers", "id": "on_boot"},
            {"type": "triggers", "id": "on_boot"},
        ]
    )
    assert list(result) == ["triggers/on_boot"]


async def test_get_bodies_handles_mixed_types_in_one_call() -> None:
    """The batch endpoint can span all 5 sub-catalogs in one round trip."""
    result = await _hydrate_bodies(
        [
            {"type": "triggers", "id": "on_boot"},
            {"type": "actions", "id": "delay"},
        ]
    )
    assert "triggers/on_boot" in result
    assert "actions/delay" in result


@pytest.mark.parametrize(
    "ref",
    [
        {"type": "", "id": "x"},
        {"type": "triggers", "id": ""},
        {},
    ],
)
async def test_get_bodies_drops_refs_missing_type_or_id(ref: dict) -> None:
    """Malformed refs are silently dropped."""
    result = await _hydrate_bodies([ref])
    assert result == {}


@pytest.mark.parametrize("ref", [None, "triggers", 42, ["triggers", "on_boot"]])
async def test_get_bodies_drops_non_dict_refs(ref: object) -> None:
    """Non-dict refs from a malformed wire payload are dropped, not raised."""
    result = await _hydrate_bodies([ref])  # type: ignore[list-item]
    assert result == {}


async def test_get_bodies_uses_single_executor_hop_across_types() -> None:
    """A mixed-type batch pays exactly one ``asyncio.to_thread`` hop."""
    catalog._TRIGGER_STORE._cache.clear()
    catalog._ACTION_STORE._cache.clear()
    catalog._CONDITION_STORE._cache.clear()
    with patch.object(catalog.asyncio, "to_thread", wraps=catalog.asyncio.to_thread) as spy:
        await _hydrate_bodies(
            [
                {"type": "triggers", "id": "on_boot"},
                {"type": "actions", "id": "delay"},
                {"type": "conditions", "id": "lambda"},
            ]
        )
    assert spy.call_count == 1


async def test_get_bodies_cache_hits_skip_executor_hop() -> None:
    """When every ref is already cached, no executor hop fires."""
    await _hydrate_bodies([{"type": "triggers", "id": "on_boot"}])
    with patch.object(catalog.asyncio, "to_thread", wraps=catalog.asyncio.to_thread) as spy:
        result = await _hydrate_bodies([{"type": "triggers", "id": "on_boot"}])
    assert "triggers/on_boot" in result
    assert spy.call_count == 0


async def test_get_bodies_skips_loaded_none_bodies() -> None:
    """A loader returning ``None`` (e.g. body file vanished) is dropped."""
    catalog._TRIGGER_STORE._cache.clear()
    with patch.object(catalog._TRIGGER_STORE, "load_one_sync", return_value=None):
        result = await _hydrate_bodies([{"type": "triggers", "id": "on_boot"}])
    assert result == {}


def test_load_body_returns_none_when_resource_missing() -> None:
    """A FileNotFoundError on the body resource collapses to None."""
    loader = catalog._load_body_from_disk("triggers", catalog.AutomationTrigger)
    with patch.object(catalog.resources, "files") as spy:
        spy.return_value.joinpath.return_value.joinpath.return_value.read_bytes.side_effect = (
            FileNotFoundError
        )
        assert loader("on_boot") is None


def test_load_body_returns_none_when_module_missing() -> None:
    """A ModuleNotFoundError on the bodies package collapses to None."""
    loader = catalog._load_body_from_disk("triggers", catalog.AutomationTrigger)
    with patch.object(catalog.resources, "files", side_effect=ModuleNotFoundError):
        assert loader("on_boot") is None


@pytest.mark.parametrize(
    "bad_id", ["../escape", "subdir/escape", "back\\slash", "", "null\x00byte"]
)
def test_load_body_refuses_traversal_shaped_id(bad_id: str) -> None:
    """Traversal-shaped ids are refused before touching the filesystem."""
    loader = catalog._load_body_from_disk("triggers", catalog.AutomationTrigger)
    with patch.object(catalog.resources, "files") as spy:
        assert loader(bad_id) is None
    spy.assert_not_called()


def test_scan_body_sizes_skips_an_unreadable_type_dir() -> None:
    """A body directory that can't be listed contributes no sizes."""
    root = MagicMock()
    root.joinpath.return_value.iterdir.side_effect = FileNotFoundError
    with patch.object(catalog.resources, "files", return_value=root):
        assert catalog._scan_body_sizes() == {}


def test_scan_body_sizes_ignores_non_json_entries() -> None:
    """Only ``.json`` files in a body directory are sized."""
    stray = MagicMock()
    stray.name = "README.md"
    root = MagicMock()
    root.joinpath.return_value.iterdir.return_value = [stray]
    with patch.object(catalog.resources, "files", return_value=root):
        assert catalog._scan_body_sizes() == {}


def test_load_index_returns_empty_skeleton_when_missing() -> None:
    """An absent index file falls back to the empty-lists skeleton."""
    catalog._load_index.cache_clear()
    try:
        with patch.object(catalog.resources, "files", side_effect=FileNotFoundError):
            result = catalog._load_index()
        assert result == {
            "triggers": [],
            "actions": [],
            "conditions": [],
            "light_effects": [],
            "filters": [],
        }
    finally:
        catalog._load_index.cache_clear()
        catalog._load_index()  # repopulate for the next test in this xdist worker


async def test_get_bodies_serves_sensor_in_range_required_group() -> None:
    """The shipped ``sensor.in_range`` body carries the #1905 constraint, not advanced fields."""
    result = await _hydrate_bodies([{"type": "conditions", "id": "sensor.in_range"}])
    body = result["conditions/sensor.in_range"]
    assert body["required_groups"] == [{"kind": "at_least_one", "keys": ["above", "below"]}]
    by_key = {e["key"]: e for e in body["config_entries"]}
    assert "advanced" not in by_key["above"]
    assert "advanced" not in by_key["below"]


_DELAY = {"type": "actions", "id": "delay"}
_ON_BOOT = {"type": "triggers", "id": "on_boot"}
_LAMBDA = {"type": "conditions", "id": "lambda"}


async def test_get_bodies_defers_refs_past_the_budget_unloaded() -> None:
    """Refs past *max_bytes* return in order in ``remaining`` without a disk read."""
    catalog._ACTION_STORE._cache.clear()
    first = (await asyncio.to_thread(catalog._scan_body_sizes))[("triggers", "on_boot")]
    with patch.object(catalog._ACTION_STORE, "load_one_sync") as load:
        bodies, remaining = await catalog.get_bodies([_ON_BOOT, _DELAY, _LAMBDA], first)
    assert list(bodies) == ["triggers/on_boot"]
    assert remaining == [_DELAY, _LAMBDA]
    load.assert_not_called()


async def test_get_bodies_returns_an_oversized_first_body() -> None:
    """A first body over the budget still ships."""
    bodies, remaining = await catalog.get_bodies([_DELAY, _ON_BOOT], 1)
    assert list(bodies) == ["actions/delay"]
    assert remaining == [_ON_BOOT]


async def test_get_bodies_keeps_every_lvgl_page_under_the_ingress_cap() -> None:
    """Paging every lvgl action keeps each reply under the 16 MiB ingress frame cap."""
    refs = [
        {"type": "actions", "id": a.id} for a in catalog.all_actions() if a.id.startswith("lvgl.")
    ]
    seen: set[str] = set()
    while refs:
        bodies, refs = await catalog.get_bodies(refs, catalog.GET_BODIES_MAX_BYTES)
        assert bodies
        assert len(json_dumps({"bodies": bodies, "remaining": refs})) < 16 * 1024 * 1024
        seen.update(bodies)
    assert "actions/lvgl.list.add" in seen
