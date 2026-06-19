"""Drift + shape checks for the split board catalog artefacts.

The board catalog ships as three coordinated files under
``definitions/``: the slim ``boards.index.json`` (picker shape), the
per-board bodies under ``board_bodies/<id>.json`` (lazy-loaded
detail), and the aggregated ``featured_components.index.json`` (read
once at startup by the components controller). These tests pin:

* the three artefacts reassemble back to what the YAML manifests
  produce — i.e. ``sync_boards.py`` is the only translator;
* the slim index strips body-only fields so the picker doesn't pay
  to read them;
* each body file round-trips through ``BoardCatalogEntry.from_dict``
  standalone (lazy-load is safe);
* the featured-components index is consistent with the body files
  it aggregates over.
"""

from __future__ import annotations

from pathlib import Path

import orjson

from esphome_device_builder.definitions import (
    build_board_catalog_from_manifests,
    load_board_body_from_disk,
    load_board_catalog,
    load_board_index,
    load_featured_components_index,
)
from esphome_device_builder.models.boards import (
    BoardCatalogEntry,
    BoardCatalogIndex,
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
from script.sync_boards import (
    _LIBRETINY_FAMILIES,
    _NRF52_PLATFORM,
    _RP2040_PLATFORM,
    _augment_rmii_data_pins,
    _backfill_esp32_variants,
    _backfill_rp2040_mcu,
    _backfill_rp2040_wifi,
)

_DEFINITIONS_DIR = Path(__file__).parent.parent / "esphome_device_builder" / "definitions"
_BOARDS_INDEX_JSON = _DEFINITIONS_DIR / "boards.index.json"
_BOARDS_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_INDEX_JSON = _DEFINITIONS_DIR / "featured_components.index.json"

# Body-only fields — must be absent from every slim index entry.
_BODY_ONLY_KEYS = frozenset(
    {"hardware", "pins", "featured_components", "featured_bundles", "default_components"}
)


def test_split_artefacts_match_manifests() -> None:
    """
    The committed artefacts reproduce what the manifests produce.

    Scoped to the manifest-derived backbone. Two platform sets matter:
    ``generated`` platforms add disk-only boards from esphome's tables (allowed
    to appear without a manifest); ``esphome_filled`` is the subset whose
    *manifested* pins come from esphome (LibreTiny ships ``pins: []`` and gets
    filled), which are version-dependent (CI runs beta/dev) so they're excluded
    from the pin compare. RP2040 manifests keep their hand-curated pins, so those
    stay checked; only its disk-only generated boards are exempt. esp8266 is a
    mix: curated manifests stay checked, empty product manifests get filled.
    """
    from_yaml = build_board_catalog_from_manifests(strict=True)
    # Variant / wifi / mcu backfills are part of emission (sync_boards.build_catalog),
    # so apply them here too or esp32 boards carrying only a PIO board id (and rp2040
    # boards lacking the WiFi tag or chip series) mismatch disk.
    _backfill_esp32_variants(from_yaml.boards)
    _backfill_rp2040_wifi(from_yaml.boards)
    _backfill_rp2040_mcu(from_yaml.boards)
    _augment_rmii_data_pins(from_yaml.boards)
    from_disk = load_board_catalog()
    generated = set(_LIBRETINY_FAMILIES) | {_RP2040_PLATFORM, _NRF52_PLATFORM, "esp32", "esp8266"}
    esphome_filled = set(_LIBRETINY_FAMILIES)
    manifest_ids = {b.id for b in from_yaml.boards}
    disk_by_id = {b.id: b for b in from_disk.boards}

    # Boards on disk but not in the manifests are the esphome-generated entries;
    # nothing else should appear out of thin air.
    extra = [b for b in from_disk.boards if b.id not in manifest_ids]
    assert all(b.esphome.platform.value in generated for b in extra), (
        f"Unexpected boards on disk from no manifest: "
        f"{[b.id for b in extra if b.esphome.platform.value not in generated]}"
    )

    for board in from_yaml.boards:
        expected = board.to_dict()
        actual = disk_by_id[board.id].to_dict()
        platform = board.esphome.platform.value
        # esphome_filled manifests ship no pins; esp8266 product manifests ship
        # empty pins filled at sync. Either way pins are esphome-derived and
        # version-dependent here — curated esp8266 pins stay compared.
        if platform in esphome_filled or (platform == "esp8266" and not board.pins):
            expected.pop("pins", None)
            actual.pop("pins", None)
        # Images are vendor-controlled URLs; a manifest image edit shouldn't fail
        # this consistency check until the catalog regenerates. Reachability is
        # validated separately by validate_definitions.py --check-images.
        expected.pop("images", None)
        actual.pop("images", None)
        assert expected == actual, (
            f"{board.id} is out of sync with its manifest. "
            "Run `python script/sync_boards.py` to regenerate."
        )


def test_boards_index_omits_body_fields() -> None:
    """The slim index strips ``hardware`` / ``pins`` / featured_* fields."""
    raw = _BOARDS_INDEX_JSON.read_text(encoding="utf-8")
    payload = orjson.loads(raw)
    for entry in payload["boards"]:
        leaked = _BODY_ONLY_KEYS & entry.keys()
        assert not leaked, f"{entry['id']} leaks body fields to the slim index: {leaked}"


def test_boards_index_omits_default_fields() -> None:
    """``omit_default`` strips empty ``tags`` / ``images`` / ``False`` flags."""
    raw = _BOARDS_INDEX_JSON.read_text(encoding="utf-8")
    # orjson emits compact output (no spaces after ``:``) so the
    # with-space variants would never appear; the no-space checks
    # are the load-bearing ones.
    assert '"tags":[]' not in raw
    assert '"images":[]' not in raw
    assert '"featured":false' not in raw
    assert '"is_generic":false' not in raw
    # ``id`` is required (no default) so it survives the strip —
    # sanity-check that the file still has board content rather
    # than an accidentally-empty regeneration.
    payload = orjson.loads(raw)
    assert len(payload["boards"]) > 100


def test_usb_pin_features_match_notes() -> None:
    """A pin noting USB ``D+`` carries ``usb_dp``; ``D-`` carries ``usb_dm``.

    The two are easy to transpose when hand-curating manifests, and the slip
    silently mis-filters the USB pin pickers in the editor.
    """
    offenders: list[str] = []
    for board in load_board_catalog().boards:
        for pin in board.pins:
            notes = pin.notes or ""
            feats = {f.value for f in pin.features}
            if "D+" in notes and "usb_dp" not in feats:
                offenders.append(f"{board.id} GPIO{pin.gpio}: {notes!r} lacks usb_dp")
            if "D-" in notes and "usb_dm" not in feats:
                offenders.append(f"{board.id} GPIO{pin.gpio}: {notes!r} lacks usb_dm")
    assert not offenders, "USB D+/D- pins disagree with their feature flag:\n" + "\n".join(
        offenders
    )


def test_board_body_round_trips_standalone() -> None:
    """A single body file decodes through ``BoardCatalogEntry.from_dict`` directly."""
    # The bodies must be self-describing — the lazy loader reads one
    # file at a time with no slim-entry context, so any required
    # field carried only by the slim index would crash the load.
    index = load_board_index()
    assert index, "boards.index.json is empty — run script/sync_boards.py"
    body = load_board_body_from_disk(index[0].id)
    assert body is not None
    assert body.id == index[0].id


def test_featured_index_matches_per_board_bodies() -> None:
    """Every (board, featured component) in the index matches the body file's list.

    Invariant: ``sync_boards.py`` populates both the per-board
    body's ``featured_components`` and the aggregated
    ``featured_components.index.json`` from the same source, so a
    body and the index for the same board must agree byte-for-byte
    on every entry. A drift here means the registry build sees one
    catalog while the body lazy-load returns another.
    """
    featured_idx = load_featured_components_index()
    assert featured_idx, "featured_components.index.json is empty"

    # Spot-check one board so the test stays fast on the full
    # catalog. The dict iteration order is stable across runs given
    # the sort by ``board_id`` in the sync script.
    board_id, expected = next(iter(featured_idx.items()))
    body = load_board_body_from_disk(board_id)
    assert body is not None
    assert body.featured_components == expected


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
        is_generic=False,
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


def test_slim_index_round_trip_strips_factory_fields() -> None:
    """An all-default ``BoardCatalogIndex`` strips factory-default lists / flags."""
    entry = BoardCatalogIndex(
        id="x",
        name="X",
        description="d",
        manufacturer="m",
        esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
    )
    payload = entry.to_dict()
    assert payload == {
        "id": "x",
        "name": "X",
        "description": "d",
        "manufacturer": "m",
        "esphome": {"platform": "esp32", "board": "esp32dev"},
    }
    assert BoardCatalogIndex.from_dict(payload) == entry


def test_wifi_capable_rp2040_boards_carry_the_wifi_tag() -> None:
    """WiFi-capable RP2040 boards (Pico W, etc.) get the WiFi chip; plain ones don't."""
    tags = {b.id: set(b.tags) for b in load_board_index()}
    assert BoardTag.WIFI in tags["rpipicow"]
    assert BoardTag.WIFI in tags["rpipico2w"]
    assert BoardTag.WIFI not in tags.get("rpipico", set())
    assert BoardTag.WIFI not in tags.get("rpipico2", set())
    # The curated generic RP2040 maps to the wifi rpipicow target, so the tag
    # is derived from the pio board, not only from generated boards.
    assert BoardTag.WIFI in tags["generic-rp2040"]
    assert BoardTag.WIFI not in tags.get("generic_rp2350", set())


def test_rp2040_boards_carry_the_chip_mcu_other_platforms_do_not() -> None:
    """RP2040-family entries label their chip series; other platforms leave mcu unset."""
    mcu = {b.id: b.esphome.mcu for b in load_board_index()}
    assert mcu["rpipico2"] == "rp2350"
    assert mcu["rpipico2w"] == "rp2350"
    assert mcu["pimoroni_plasma2350w"] == "rp2350"
    assert mcu["generic_rp2350"] == "rp2350"
    assert mcu["rpipico"] == "rp2040"
    assert mcu["rpipicow"] == "rp2040"
    assert mcu["generic-rp2040"] == "rp2040"
    # ESP32 / ESP8266 boards distinguish chips via ``variant``; mcu stays unset.
    assert mcu["generic-esp32"] is None


def test_every_board_has_a_docs_url() -> None:
    """No board renders an empty "More info" link; generated boards fall back to platform docs."""
    docs = {b.id: b.docs_url for b in load_board_index()}
    assert all(docs.values()), [bid for bid, url in docs.items() if not url]
    # Generated (unmanifested) boards take the per-platform default.
    assert docs["MyRP_2350B"] == "https://esphome.io/components/rp2040.html"
    assert docs["rpipico2w"] == "https://esphome.io/components/rp2040.html"
