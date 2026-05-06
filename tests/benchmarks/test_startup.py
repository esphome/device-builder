"""Benchmarks for the dashboard startup hot path.

``DeviceBuilder.start()`` blocks on two synchronous catalog loads
before the first WS frame can be served: ``BoardCatalog.load()``
walks ~500 hand-curated ``manifest.yaml`` files under
``definitions/boards/`` and parses each via ``yaml.safe_load``;
``ComponentCatalog.load()`` decodes the ~20 MB pre-generated
``definitions/components.json`` and instantiates ~900
``ComponentCatalogEntry`` objects. Together they account for the
bulk of the wall-time gap a user feels when comparing the new
dashboard's startup against the legacy Tornado one — neither does
network I/O, so the cost is pure CPU and parser overhead.

CodSpeed runs these in CI so a regression in either loader (a
slower YAML round-trip, an O(n²) walk, an extra dataclass
allocation per entry) lands on a PR rather than as a perceptible
"why is the dashboard slower to come up?" report from a user.

Both loaders run synchronously from the production
``DeviceBuilder.start()``, so the benchmarks call them directly
without an event loop — there's no asyncio overhead to subtract,
and blockbuster only fires for blocking calls made from inside a
loop, so the disk reads here don't trip it either.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog


class _CatalogContainer:
    """Minimal ``device_builder``-shaped object the catalogs read from.

    ``ComponentCatalog._build_featured_registry`` walks
    ``self._db.boards.iter_boards()`` to wire featured-component IDs
    back to their owning board, so the container has to expose a
    populated ``boards`` attr. Mirrors the session-scoped fixture in
    the unit-test suite (``tests/conftest.py:_CatalogContainer``);
    duplicated here rather than imported because the benchmark suite
    is intentionally decoupled from ``tests/conftest.py`` so a refactor
    of the unit-test fixtures can't perturb CodSpeed numbers.
    """

    boards: BoardCatalog | None = None
    components: ComponentCatalog | None = None


def test_load_board_catalog(benchmark: BenchmarkFixture) -> None:
    """Pin ``BoardCatalog.load()`` — the dominant startup cost.

    Walks ~500 ``manifest.yaml`` files under
    ``definitions/boards/`` and parses each via
    ``yaml.safe_load``. By far the largest single startup wall-
    time on a fresh process; a regression here is what users feel
    as "the dashboard is slow to come up". Construct the
    ``BoardCatalog`` inside the benchmark so each iteration starts
    from a cold instance — measuring the load, not a no-op repeat
    on a populated catalog.
    """

    @benchmark
    def run() -> None:
        catalog = BoardCatalog()
        catalog.load()


def test_load_component_catalog(benchmark: BenchmarkFixture) -> None:
    """Pin ``ComponentCatalog.load()`` — the second-largest startup cost.

    Decodes ~20 MB of pre-generated JSON via ``orjson`` and
    instantiates ~900 ``ComponentCatalogEntry`` dataclasses, then
    walks the populated board catalog to build the featured-
    component registry. The board-catalog load is a prerequisite
    for the registry walk — but it lives in its own benchmark
    above, so build it once at module load time and reuse the
    instance here. The ``ComponentCatalog`` itself is reconstructed
    per iteration so the benchmark measures a cold load, not the
    cost of dropping an already-populated entry list on the floor.
    """
    container = _CatalogContainer()
    container.boards = BoardCatalog()
    container.boards.load()

    @benchmark
    def run() -> None:
        catalog = ComponentCatalog(container)
        catalog.load()
