"""LRU + locked + batched lazy body store for split catalogs.

Used by the component catalog (per-id ``.json`` under
``definitions/components/``) and the automations catalog
(per-type subdir under ``definitions/automations/<type>/``).

The store owns the runtime caching invariants — bounded LRU,
single ``asyncio.Lock`` + post-acquire cache re-check, one
``asyncio.to_thread`` per batch — and leaves the on-disk layout
to the caller via a ``load_one`` callable. The sync ``get_sync``
accessor (used by parsing / writing on a worker thread) shares
the same cache; cache mutations are simple ``OrderedDict``
operations that the GIL serialises atomically, so concurrent
worker-thread calls at worst incur a duplicate idempotent
disk read on a rare collision.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache

# LRU cap for ``is_unsafe_catalog_id`` (~5x a typical catalog size).
# Bounded rather than ``@cache`` because the predicate sees external
# input (WS handler kwargs); unbounded growth on attacker-controlled
# ids would be a denial-of-service surface.
_UNSAFE_ID_CACHE_MAXSIZE = 4096


@lru_cache(maxsize=_UNSAFE_ID_CACHE_MAXSIZE)
def is_unsafe_catalog_id(catalog_id: str) -> bool:
    """Return True for traversal-shaped catalog ids (flat layout)."""
    return (
        not catalog_id
        or ".." in catalog_id
        or "/" in catalog_id
        or "\\" in catalog_id
        or "\x00" in catalog_id
    )


class LazyBodyStore[BodyT]:
    """Bounded-LRU, lock-coalesced lazy body store for split catalogs.

    ``load_one`` reads one body off disk (or wherever) and returns
    the hydrated model or ``None`` for traversal-shaped /
    missing-on-disk ids. ``is_known`` short-circuits the load for
    ids the caller knows aren't in the index. The store doesn't
    know about Paths or ``from_dict`` — callers wire those in via
    ``load_one`` so the same store shape covers any catalog
    layout (flat ``components/<id>.json`` or per-type-subdir
    ``automations/<type>/<id>.json``).
    """

    def __init__(
        self,
        load_one: Callable[[str], BodyT | None],
        *,
        cache_maxsize: int = 128,
        is_known: Callable[[str], bool] | None = None,
    ) -> None:
        self._load_one = load_one
        self._cache_maxsize = cache_maxsize
        self._is_known = is_known if is_known is not None else _always_true
        self._cache: OrderedDict[str, BodyT] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, catalog_id: str) -> BodyT | None:
        """Return the hydrated body for *catalog_id*, or None if missing."""
        cached = self._cache.get(catalog_id)
        if cached is not None:
            self._cache.move_to_end(catalog_id)
            return cached
        if not self._is_known(catalog_id):
            return None
        bodies = await self.get_many([catalog_id])
        return bodies.get(catalog_id)

    async def get_many(self, catalog_ids: list[str]) -> dict[str, BodyT]:
        """Return bodies for *catalog_ids*; one executor hop per batch.

        Held under the load lock with a post-acquire cache
        re-check. Unknown ids and ids whose body files are missing
        on disk are absent from the result. Callers must use the
        returned dict rather than re-reading the cache because a
        batch larger than ``cache_maxsize`` would evict its own
        entries before returning.
        """
        async with self._lock:
            result: dict[str, BodyT] = {}
            to_load: list[str] = []
            seen: set[str] = set()
            for cid in catalog_ids:
                if cid in seen or not self._is_known(cid):
                    continue
                seen.add(cid)
                cached = self._cache.get(cid)
                if cached is not None:
                    self._cache.move_to_end(cid)
                    result[cid] = cached
                else:
                    to_load.append(cid)
            if to_load:
                bodies = await asyncio.to_thread(self._load_bodies_sync, to_load)
                for cid in to_load:
                    body = bodies.get(cid)
                    if body is None:
                        continue
                    self._cache[cid] = body
                    while len(self._cache) > self._cache_maxsize:
                        self._cache.popitem(last=False)
                    result[cid] = body
            return result

    def _load_bodies_sync(self, ids: list[str]) -> dict[str, BodyT | None]:
        """Read several bodies sequentially in one thread."""
        return {cid: self._load_one(cid) for cid in ids}

    def get_sync(self, catalog_id: str) -> BodyT | None:
        """Sync accessor for worker-thread callers (parsing / writing).

        Blocks on cache miss with a blocking disk read.
        Cache mutations are simple ``OrderedDict`` ops that the GIL
        atomically serialises; concurrent worker-thread callers at
        worst pay a duplicate idempotent disk read on a rare
        collision, no shared-state corruption.
        """
        cached = self.try_get_cached(catalog_id)
        if cached is not None:
            return cached
        if not self.is_known(catalog_id):
            return None
        body = self.load_one_sync(catalog_id)
        if body is None:
            return None
        self.cache_put(catalog_id, body)
        return body

    def is_known(self, catalog_id: str) -> bool:
        """Whether *catalog_id* is in the slim index (cheap predicate)."""
        return self._is_known(catalog_id)

    def try_get_cached(self, catalog_id: str) -> BodyT | None:
        """Return the cached body for *catalog_id* (bumps LRU), or None."""
        cached = self._cache.get(catalog_id)
        if cached is not None:
            self._cache.move_to_end(catalog_id)
        return cached

    def cache_put(self, catalog_id: str, body: BodyT) -> None:
        """Insert *body* into the cache and evict the LRU tail if over cap."""
        self._cache[catalog_id] = body
        while len(self._cache) > self._cache_maxsize:
            self._cache.popitem(last=False)

    def load_one_sync(self, catalog_id: str) -> BodyT | None:
        """Read one body off disk synchronously, bypassing the cache.

        For cross-store batched callers that compose several stores'
        loads into a single ``asyncio.to_thread`` hop; they manage
        ``is_known`` / cache-put themselves around the load.
        """
        return self._load_one(catalog_id)


def _always_true(_: str) -> bool:
    """Default ``is_known`` predicate."""
    return True
