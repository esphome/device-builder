"""Aggregate catalog-load benchmarks.

``test_startup.py`` covers the per-unit cost of one component
dataclass build and one mashumaro ``from_dict`` walk. The
aggregate ``ComponentCatalog.load()`` (orjson decode of the
~20 MB blob, 896x ``_load_component``, featured-registry build)
is the dominant slice of dashboard startup wall-time on HA
Green and isn't covered by the per-unit benches; a regression
in the load-side wiring would only surface here.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog


class _StubContainer:
    """Minimal device_builder-shaped attribute holder for ``ComponentCatalog``."""

    boards: BoardCatalog | None = None
    components: ComponentCatalog | None = None


def test_component_catalog_load_aggregate(benchmark: BenchmarkFixture) -> None:
    """Pin full ``ComponentCatalog.load()``: orjson decode + 896x walk + featured registry."""
    boards = BoardCatalog()
    boards.load()
    container = _StubContainer()
    container.boards = boards

    # Smoke ONCE outside the loop so a refactor that turns ``load``
    # into a no-op surfaces as an assertion failure rather than a
    # CodSpeed "speedup" against nothing.
    warm = ComponentCatalog(container)
    warm.load()
    assert len(warm._components) > 100
    assert warm._by_id

    @benchmark
    def run() -> None:
        catalog = ComponentCatalog(container)
        catalog.load()


def test_board_catalog_load_aggregate(benchmark: BenchmarkFixture) -> None:
    """Pin full ``BoardCatalog.load()`` including the boards.json disk read."""
    warm = BoardCatalog()
    warm.load()
    assert len(warm._boards) > 100

    @benchmark
    def run() -> None:
        catalog = BoardCatalog()
        catalog.load()
