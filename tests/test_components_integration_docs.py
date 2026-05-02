"""Smoke test for ``components/get_integration_docs``.

Loads the real shipped catalog so the keys we expect users to see
linked actually round-trip — this is the same data that drives the
frontend's loaded-integration tags. A regression in the lookup logic
(stem stripping, top-level priority) would silently turn a user's
``api`` chip into plain text, so spot-check the common cases here.
"""

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.components import ComponentCatalog


@pytest.fixture
def catalog() -> ComponentCatalog:
    cat = ComponentCatalog()
    cat.load()
    return cat


async def test_top_level_components_resolved(catalog: ComponentCatalog) -> None:
    """Top-level catalog ids land on esphome.io/components/<id>."""
    docs = await catalog.get_integration_docs()
    for name in ("api", "wifi", "ethernet", "mdns", "logger", "web_server"):
        assert name in docs, f"missing top-level docs for {name}"
        assert docs[name].startswith("https://esphome.io/components/")


async def test_category_landing_pages_resolved(catalog: ComponentCatalog) -> None:
    """Category names like ``sensor`` / ``ota`` / ``light`` resolve too.

    The URL is synthesized from any subcomponent's docs URL parent path.
    """
    docs = await catalog.get_integration_docs()
    for category in ("sensor", "binary_sensor", "ota", "light", "switch"):
        assert category in docs, f"missing category landing for {category}"
        assert docs[category].rstrip("/").endswith(f"/components/{category}")


async def test_stem_match_for_category_scoped_components(
    catalog: ComponentCatalog,
) -> None:
    """A bare ``ltr390`` resolves to the sensor.ltr390 docs page."""
    docs = await catalog.get_integration_docs()
    assert "ltr390" in docs
    assert "sensor/ltr390" in docs["ltr390"] or "ltr390" in docs["ltr390"]


async def test_top_level_wins_over_stem(catalog: ComponentCatalog) -> None:
    """When a top-level id and a stem collide, top-level claims the key.

    ``api``, ``light``, ``switch`` exist as both top-level component pages
    and as category-scoped components (e.g. ``light.binary``). The
    top-level docs URL is the one users mean when they write ``api`` in
    YAML, so it must win.
    """
    docs = await catalog.get_integration_docs()
    # api top-level page lives at /components/api (no nested category).
    if "api" in docs:
        assert docs["api"].rstrip("/").endswith("/components/api")


async def test_unknown_integration_omitted(catalog: ComponentCatalog) -> None:
    """Names without a catalog hit are simply absent from the map."""
    docs = await catalog.get_integration_docs()
    # ``runtime_stats``-style helpers don't have a docs page; verify
    # the contract by picking one that definitely won't exist.
    assert "definitely_not_a_component_xyzzy" not in docs
