"""Tests for :class:`LazyBodyStore` and :func:`is_unsafe_catalog_id`.

The component catalog (`controllers/components.py`) exercises the
real shape end-to-end against bundled JSON; this file pins the
generic contract (cache + lock + batch + traversal predicate)
against synthetic body classes so a regression in the helper
itself surfaces here rather than as a downstream cascade.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from esphome_device_builder.helpers.lazy_catalog import (
    LazyBodyStore,
    is_unsafe_catalog_id,
)


@dataclass
class _Body:
    """Minimal stand-in for a real catalog body dataclass."""

    id: str


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../escape",
        "subdir/escape",
        "back\\slash",
        "null\x00byte",
    ],
)
def test_is_unsafe_catalog_id_rejects_traversal_shapes(bad: str) -> None:
    assert is_unsafe_catalog_id(bad) is True


@pytest.mark.parametrize("good", ["wifi", "sensor.dht", "sensor.bme280_i2c"])
def test_is_unsafe_catalog_id_passes_flat_catalog_ids(good: str) -> None:
    assert is_unsafe_catalog_id(good) is False


async def test_get_caches_first_hit() -> None:
    calls: list[str] = []

    def loader(cid: str) -> _Body | None:
        calls.append(cid)
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader)

    first = await store.get("wifi")
    second = await store.get("wifi")

    assert first is second
    assert calls == ["wifi"]


async def test_get_many_dedupes_repeated_ids() -> None:
    calls: list[str] = []

    def loader(cid: str) -> _Body | None:
        calls.append(cid)
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader)

    result = await store.get_many(["a", "a", "b", "a"])

    assert set(result) == {"a", "b"}
    assert calls == ["a", "b"]


async def test_get_many_returns_full_batch_larger_than_cache() -> None:
    """A batch larger than ``cache_maxsize`` still returns every entry.

    Mirrors the components catalog correctness contract: the cache
    is a hot-read optimization, not a result store. An early entry
    evicted during the batch must still appear in the returned
    dict.
    """

    def loader(cid: str) -> _Body | None:
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader, cache_maxsize=4)

    ids = [f"comp_{i}" for i in range(20)]
    result = await store.get_many(ids)

    assert len(result) == 20
    assert len(store._cache) == 4
    assert result["comp_0"].id == "comp_0"
    assert result["comp_19"].id == "comp_19"


async def test_is_known_short_circuits_unknown_id() -> None:
    """``is_known`` returning False skips the disk read entirely."""
    calls: list[str] = []

    def loader(cid: str) -> _Body | None:
        calls.append(cid)
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(
        load_one=loader,
        is_known=lambda cid: cid == "wifi",
    )

    found = await store.get("wifi")
    missing = await store.get("does-not-exist")

    assert found is not None
    assert missing is None
    assert calls == ["wifi"]


async def test_concurrent_same_id_calls_share_one_load() -> None:
    """The single asyncio.Lock + post-acquire cache re-check coalesces same-id loads."""
    calls: list[str] = []

    def loader(cid: str) -> _Body | None:
        calls.append(cid)
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader)

    a, b = await asyncio.gather(store.get("wifi"), store.get("wifi"))

    assert a is b
    assert calls == ["wifi"]


async def test_load_one_is_dispatched_through_executor() -> None:
    """``get_many`` runs ``load_one`` inside an ``asyncio.to_thread`` dispatch.

    Regression guard for the previous components-side bug where
    each id was its own to_thread call; the store should pay one
    executor hop per batch, mirroring the HA ``translation.py``
    shape.
    """

    def loader(cid: str) -> _Body | None:
        return _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader)

    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def _counting_to_thread(func, /, *args, **kwargs):
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    with patch.object(asyncio, "to_thread", _counting_to_thread):
        result = await store.get_many([f"comp_{i}" for i in range(10)])

    assert len(result) == 10
    assert to_thread_calls == 1


async def test_get_many_skips_missing_on_disk_ids() -> None:
    """A loader returning ``None`` for an id leaves it out of the result."""

    def loader(cid: str) -> _Body | None:
        return None if cid == "ghost" else _Body(id=cid)

    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=loader)
    result = await store.get_many(["wifi", "ghost"])
    assert "ghost" not in result
    assert result["wifi"].id == "wifi"


def test_get_sync_returns_none_when_loader_returns_none() -> None:
    """A known id whose body file vanished resolves to ``None``."""
    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=lambda _: None)
    assert store.get_sync("wifi") is None


def test_cache_put_evicts_oldest_when_over_cap() -> None:
    """``cache_put`` past ``cache_maxsize`` drops the LRU tail."""
    store: LazyBodyStore[_Body] = LazyBodyStore(load_one=lambda cid: _Body(id=cid), cache_maxsize=2)
    store.cache_put("a", _Body(id="a"))
    store.cache_put("b", _Body(id="b"))
    store.cache_put("c", _Body(id="c"))
    assert list(store._cache) == ["b", "c"]
