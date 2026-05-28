"""Tests for ``automations/get_bodies`` — the batch detail endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.automations import catalog
from esphome_device_builder.controllers.automations.catalog import get_bodies as _hydrate_bodies

pytestmark = pytest.mark.xdist_group("automations")


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
