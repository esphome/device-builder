"""Unit tests for ``_fix_borrowed_page_titles`` in ``script/sync_components.py``.

Pokes the post-pass directly with synthetic catalog fragments so the rule holds
independent of the checked-in catalog (which the integration test covers).
"""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _fix_borrowed_page_titles,
)


def _entry(component_id: str, name: str, page: str) -> dict:
    """Minimal catalog entry: id, name, and a docs_url ending in *page*."""
    return {
        "id": component_id,
        "name": name,
        "docs_url": f"https://esphome.io/components/{page}",
    }


def test_borrowed_title_is_rederived_from_stem() -> None:
    """An entry linking to another component's page drops the borrowed title."""
    entries = [
        _entry("esphome", "ESPHome Core Configuration", "esphome"),
        _entry("preferences", "ESPHome Core Configuration", "esphome"),
    ]
    _fix_borrowed_page_titles(entries)
    assert entries[0]["name"] == "ESPHome Core Configuration"  # page owner, untouched
    assert entries[1]["name"] == "Preferences"


def test_same_family_variant_keeps_shared_title() -> None:
    """A variant whose stem extends the page slug keeps the shared title."""
    entries = [
        _entry("pn532", "PN532 NFC/RFID", "pn532"),
        _entry("pn532_spi", "PN532 NFC/RFID", "pn532"),
    ]
    _fix_borrowed_page_titles(entries)
    assert entries[1]["name"] == "PN532 NFC/RFID"


def test_platform_entry_on_its_own_page_untouched() -> None:
    """A ``<domain>.<stem>`` entry whose page is its own stem is left alone."""
    entries = [_entry("sensor.dht", "DHT Temperature+Humidity Sensor", "dht")]
    _fix_borrowed_page_titles(entries)
    assert entries[0]["name"] == "DHT Temperature+Humidity Sensor"


def test_unrelated_page_with_no_owner_entry_untouched() -> None:
    """No rewrite when the linked page isn't owned by another catalog entry."""
    entries = [_entry("as3935_spi", "AMS AS3935 Franklin Lightning Sensor", "as3935")]
    _fix_borrowed_page_titles(entries)
    assert entries[0]["name"] == "AMS AS3935 Franklin Lightning Sensor"
