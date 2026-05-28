"""Drift check: ``boards.json`` must match what the YAML manifests produce."""

from __future__ import annotations

from pathlib import Path

import orjson

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_catalog,
)
from esphome_device_builder.models.boards import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    BoardHardware,
    BoardPin,
    BoardTag,
    Connectivity,
    DefaultComponent,
    Esp32Variant,
    FeaturedBundle,
    FeaturedComponent,
    Platform,
)
from esphome_device_builder.models.common import FieldPreset, PinFeature

_BOARDS_JSON = (
    Path(__file__).parent.parent / "esphome_device_builder" / "definitions" / "boards.json"
)


def test_boards_json_matches_manifests() -> None:
    """``boards.json`` must be the faithful product of the YAML manifests."""
    from_yaml = build_board_catalog_from_manifests(strict=True)
    from_json = load_board_catalog()

    # Comparing ``to_dict`` rather than dataclass identity gives a
    # readable key-path diff in the assertion message on failure.
    assert from_yaml.to_dict() == from_json.to_dict(), (
        "boards.json is out of sync with the YAML manifests. "
        "Run `python script/sync_boards.py` to regenerate."
    )


def test_boards_json_omits_default_fields() -> None:
    """Empty ``suggestions`` / ``locked`` default rows are stripped from ``boards.json``."""
    # ``encoding="utf-8"`` is load-bearing on Windows: the file
    # carries em-dashes and other non-ASCII chars, and
    # ``Path.read_text`` defaults to the platform encoding (cp1252
    # on the windows-latest CI runner), which trips on the first
    # 0x90 byte from a u'—'.
    raw = _BOARDS_JSON.read_text(encoding="utf-8")
    # orjson emits compact output (no spaces after ``:``) so the
    # with-space variants would never appear; the no-space checks
    # are the load-bearing ones.
    assert '"suggestions":null' not in raw
    assert '"locked":false' not in raw
    # The ``id`` field is required (no default) so it survives the
    # strip — sanity-check that the file still has board content
    # rather than an accidentally-empty regeneration.
    payload = orjson.loads(raw)
    assert len(payload["boards"]) > 100


def test_omit_default_preserves_meaningful_falsy() -> None:
    """``locked=True`` / falsy non-default ``value`` survive the strip."""
    # ``omit_default`` removes a field only when its runtime value
    # equals the *declared* default. ``FieldPreset.value`` defaults
    # to ``None``, so meaningful ``False`` / ``0`` / ``""`` survive
    # — and ``locked=True`` survives because the declared default
    # is ``False``. The board catalog leans on this asymmetry; pin
    # it so a future "make every preset field optional" sweep
    # doesn't silently break the wire shape.
    assert FieldPreset(value=False).to_dict() == {"value": False}
    assert FieldPreset(value=0).to_dict() == {"value": 0}
    assert FieldPreset(value="").to_dict() == {"value": ""}
    assert FieldPreset(value=5, locked=True).to_dict() == {"value": 5, "locked": True}
    # All-defaults round-trips to an empty dict (the strip's whole point).
    assert FieldPreset().to_dict() == {}


def test_round_trip_all_default_entry_strips_factory_fields() -> None:
    """An all-default ``BoardCatalogEntry`` round-trips losslessly through ``to_dict``."""
    # This is the core safety property: mashumaro's ``omit_default``
    # handling of ``default_factory`` (empty list / dict) has been
    # version-sensitive historically. Pin that the on-wire payload
    # carries *only* the required fields and that ``from_dict``
    # re-defaults every factory-defaulted field on the way back.
    entry = BoardCatalogEntry(
        id="x",
        name="X",
        description="d",
        manufacturer="m",
        esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
    )
    payload = entry.to_dict()
    # Every optional / factory-defaulted field is absent on the wire.
    assert payload == {
        "id": "x",
        "name": "X",
        "description": "d",
        "manufacturer": "m",
        "esphome": {"platform": "esp32", "board": "esp32dev"},
    }
    # ``from_dict`` re-materialises the factory defaults.
    rehydrated = BoardCatalogEntry.from_dict(payload)
    assert rehydrated == entry
    assert rehydrated.hardware == BoardHardware()
    assert rehydrated.images == []
    assert rehydrated.tags == []
    assert rehydrated.pins == []
    assert rehydrated.featured_components == []
    assert rehydrated.featured_bundles == []
    assert rehydrated.default_components == []


def test_round_trip_all_populated_entry_preserves_everything() -> None:
    """An all-populated entry round-trips with every field intact through ``to_dict``."""
    # Mirror image of the all-default test: when every field carries
    # a non-default value, nothing is stripped and the payload
    # round-trips into an equal dataclass instance. This catches the
    # "stripped but not rehydrated" version-skew regression Kōan
    # flagged — if mashumaro ever silently drops a populated field
    # because its serializer mis-identifies the default, we see it
    # here.
    entry = BoardCatalogEntry(
        id="x",
        name="X",
        description="d",
        manufacturer="m",
        esphome=BoardEsphomeConfig(
            platform=Platform.ESP32,
            board="esp32dev",
            variant=Esp32Variant.ESP32S3,
            framework="arduino",
        ),
        hardware=BoardHardware(
            flash_size="4 MB",
            ram_size=320,
            cpu_frequency="240 MHz",
            connectivity=[Connectivity.WIFI, Connectivity.BLUETOOTH],
        ),
        images=["a.png"],
        tags=[BoardTag.DEV_KIT],
        pins=[
            BoardPin(
                gpio=13,
                label="LED",
                features=[PinFeature.ADC],
                available=True,
                occupied_by="Built-in LED",
                notes="n",
            ),
        ],
        docs_url="https://example.com",
        product_url="https://shop.example.com",
        featured=True,
        is_generic=False,  # default — should be stripped but rehydrates
        featured_components=[
            FeaturedComponent(
                id="led",
                component_id="output.gpio",
                name="LED",
                description="onboard",
                fields={"pin": FieldPreset(value=13, locked=True)},
            ),
        ],
        featured_bundles=[
            FeaturedBundle(
                id="status",
                name="Status LED",
                description="combined LED",
                component_ids=["led", "light"],
            ),
        ],
        default_components=[DefaultComponent(id="led", fields={"pin": 13})],
    )
    rehydrated = BoardCatalogEntry.from_dict(entry.to_dict())
    assert rehydrated == entry
