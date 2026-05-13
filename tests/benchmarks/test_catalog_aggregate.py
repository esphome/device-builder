"""Aggregate ``BoardCatalog.load`` benchmark (incl. ``boards.json`` disk read)."""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.boards import BoardCatalog


def test_board_catalog_load_aggregate(benchmark: BenchmarkFixture) -> None:
    """Pin full ``BoardCatalog.load()`` including the boards.json disk read."""
    warm = BoardCatalog()
    warm.load()
    assert len(warm._boards) > 100

    @benchmark
    def run() -> None:
        catalog = BoardCatalog()
        catalog.load()
