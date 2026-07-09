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

# Pin every test in the file onto the same xdist worker as the rest of
# the catalog-heavy suite so they share one ``ComponentCatalog.load``
# instead of each worker paying ~2s on Linux CI.
pytestmark = pytest.mark.xdist_group("catalog")


@pytest.fixture
def catalog(session_component_catalog: ComponentCatalog) -> ComponentCatalog:
    """Reuse the session-scoped catalog — none of the tests below mutate it."""
    return session_component_catalog


async def test_spi_is_multi_conf(catalog: ComponentCatalog) -> None:
    """The shipped ``spi`` entry is multi-instance.

    ESPHome's ``spi:`` accepts a list of buses (``cv.ensure_list``) but doesn't set
    ``MULTI_CONF``, so a naive sync stamps it single-instance and the merge drops a
    board's second SPI bus (CYD display + separate touch bus). Guard the override.
    """
    body = await catalog.get_body("spi")
    assert body is not None
    assert body.multi_conf is True


async def test_mipi_spi_dc_pin_is_optional(catalog: ComponentCatalog) -> None:
    """The shipped ``display.mipi_spi`` `dc_pin` is not required.

    DC exists only on single/octal panels, never on quad AMOLED — requiredness is a
    runtime validator, and esphome declares the field ``cv.Optional``. esphome 2026.6.4
    dumps it required, so the sync forward-ports 2026.7.0's optional behaviour; without
    this the frontend seeds a bogus `dc_pin` on quad displays. Guard the override.
    """
    body = await catalog.get_body("display.mipi_spi")
    assert body is not None
    dc = next((e for e in body.config_entries if e.key == "dc_pin"), None)
    assert dc is not None
    assert dc.required is not True


async def test_top_level_components_resolved(catalog: ComponentCatalog) -> None:
    """Top-level catalog ids land on esphome.io/components/<id>."""
    docs = await catalog.get_integration_docs()
    for name in ("api", "wifi", "ethernet", "mdns", "logger", "web_server"):
        assert name in docs, f"missing top-level docs for {name}"
        assert docs[name]["url"].startswith("https://esphome.io/components/")


async def test_category_landing_pages_resolved(catalog: ComponentCatalog) -> None:
    """Category names like ``sensor`` / ``ota`` / ``light`` resolve too.

    The URL is synthesized from any subcomponent's docs URL parent path.
    """
    docs = await catalog.get_integration_docs()
    for category in ("sensor", "binary_sensor", "ota", "light", "switch"):
        assert category in docs, f"missing category landing for {category}"
        assert docs[category]["url"].rstrip("/").endswith(f"/components/{category}")


async def test_stem_match_for_category_scoped_components(
    catalog: ComponentCatalog,
) -> None:
    """A bare ``ltr390`` resolves to the sensor.ltr390 docs page."""
    docs = await catalog.get_integration_docs()
    assert "ltr390" in docs
    # Pin the exact path so a regression that silently picks a
    # different category for the stem fails this assertion instead of
    # trivially passing on a substring.
    assert docs["ltr390"]["url"].rstrip("/").endswith("/components/sensor/ltr390")


async def test_top_level_wins_over_stem(catalog: ComponentCatalog) -> None:
    """When a top-level id and a stem collide, top-level claims the key.

    ``api`` exists as a top-level component page; the ``api`` key in the
    map must point at the top-level docs URL, not at any nested page
    that happens to share the stem.
    """
    docs = await catalog.get_integration_docs()
    assert "api" in docs, "api top-level component must always resolve"
    assert docs["api"]["url"].rstrip("/").endswith("/components/api")


async def test_ambiguous_stems_omitted(catalog: ComponentCatalog) -> None:
    """Stems that resolve to multiple distinct docs URLs are dropped.

    ``gpio`` is the canonical case — ``binary_sensor.gpio``,
    ``switch.gpio``, ``output.gpio`` etc. each have their own page. We
    can't pick one without misleading the user, so the bare ``gpio``
    name must NOT be in the map (frontend then renders it as plain
    text). The category landing for any of those parent categories
    still works — this only guards the stem-alias slot.
    """
    docs = await catalog.get_integration_docs()
    # If a future catalog change consolidates gpio docs we may need to
    # revisit this; today they're distinct URLs across categories.
    if "gpio" in docs:
        # Only acceptable when every collision converges on the same URL.
        # Surface the URL for the failure message so it's easy to
        # diagnose without re-running locally.
        msg = (
            f"gpio resolved to {docs['gpio']!r} — expected omission because "
            "binary_sensor/switch/output gpio variants have distinct docs URLs"
        )
        raise AssertionError(msg)


async def test_qualified_log_tag_aliases(catalog: ComponentCatalog) -> None:
    """Both orders of every qualified id resolve (upstream tag order varies)."""
    docs = await catalog.get_integration_docs()
    assert docs["esphome.ota"]["url"].rstrip("/").endswith("/components/ota/esphome")
    assert docs["gpio.binary_sensor"]["url"].rstrip("/").endswith("/components/binary_sensor/gpio")
    assert docs["switch.gpio"]["url"].rstrip("/").endswith("/components/switch/gpio")


async def test_qualified_alias_does_not_shadow_bare_names(
    catalog: ComponentCatalog,
) -> None:
    """The dotted aliases leave every bare-name resolution untouched."""
    docs = await catalog.get_integration_docs()
    assert docs["esphome"]["url"].rstrip("/").endswith("/components/esphome")
    assert docs["ota"]["url"].rstrip("/").endswith("/components/ota")


async def test_helper_alias_for_undocumented_internals(
    catalog: ComponentCatalog,
) -> None:
    """Undocumented internals inherit their longest documented multi-word prefix."""
    docs = await catalog.get_integration_docs()
    assert docs["esp32_ble_client"]["url"].rstrip("/").endswith("/components/esp32_ble")
    assert docs["web_server_base"]["url"].rstrip("/").endswith("/components/web_server")
    assert "esp32_rmt" not in docs


async def test_entries_carry_display_names(catalog: ComponentCatalog) -> None:
    """Entries carry the catalog display name; landings fall back to the key."""
    docs = await catalog.get_integration_docs()
    assert docs["ethernet"]["name"] == "Ethernet Component"
    assert docs["sensor"]["name"] == "sensor"
    assert docs["esp32_ble_client"]["name"] == docs["esp32_ble"]["name"]


async def test_entries_carry_trimmed_descriptions(catalog: ComponentCatalog) -> None:
    """Entries carry the first sentence of the catalog description, markdown flattened."""
    docs = await catalog.get_integration_docs()
    assert (
        docs["ethernet"]["description"]
        == "This ESPHome component enables wired Ethernet connections for ESP32 and RP2040 boards."
    )
    # Category landings have no catalog entry, so no description.
    assert docs["sensor"]["description"] == ""
    # Trimming is a hard cap — no entry ships a full markdown paragraph.
    assert all(len(entry["description"]) <= 242 for entry in docs.values())


async def test_unknown_integration_omitted(catalog: ComponentCatalog) -> None:
    """Names without a catalog hit are simply absent from the map."""
    docs = await catalog.get_integration_docs()
    # ``runtime_stats``-style helpers don't have a docs page; verify
    # the contract by picking one that definitely won't exist.
    assert "definitely_not_a_component_xyzzy" not in docs


async def test_umbrella_entries_for_legacy_bare_keys(
    catalog: ComponentCatalog,
) -> None:
    """``ota`` and ``time`` resolve to umbrella entries, not just docs URLs.

    Both blocks accept a legacy bare-mapping form (no ``- platform:`` list)
    that predates platform-based OTA / time. Sync-time umbrella injection
    gives ``get_component`` an exact-id hit for the bare key with a
    description that names the implicit default platform — without it,
    users on the legacy form get ``None`` from the catalog lookup.
    """
    for domain, default_platform in (("ota", "esphome"), ("time", "homeassistant")):
        umbrella = await catalog.get_component(component_id=domain)
        assert umbrella is not None, f"{domain} umbrella missing from catalog"
        # The description must name the implicit default platform so the
        # frontend can surface "esphome is the default OTA provider" to
        # users still on the bare form.
        assert f"`{default_platform}`" in umbrella.description, (
            f"{domain} umbrella description should name `{default_platform}` as default"
        )
        # The umbrella shouldn't replace the platform entry — both must
        # exist independently so explicit-platform configs still resolve.
        platform_entry = await catalog.get_component(component_id=f"{domain}.{default_platform}")
        assert platform_entry is not None, f"{domain}.{default_platform} platform entry must remain"
