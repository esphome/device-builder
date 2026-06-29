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
from typing import Any

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
    _augment_rp2040_onboard_ethernet_pins,
    _backfill_esp32_variants,
    _backfill_rp2040_mcu,
    _backfill_rp2040_wifi,
    _consolidate_full_setup_bundles,
    _has_pin_conflict,
    _stamp_featured_locked_pins,
)

_DEFINITIONS_DIR = Path(__file__).parent.parent / "esphome_device_builder" / "definitions"
_BOARDS_INDEX_JSON = _DEFINITIONS_DIR / "boards.index.json"
_BOARDS_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_INDEX_JSON = _DEFINITIONS_DIR / "featured_components.index.json"

# Body-only fields — must be absent from every slim index entry.
_BODY_ONLY_KEYS = frozenset(
    {
        "hardware",
        "pins",
        "featured_components",
        "featured_bundles",
        "default_components",
        "full_config",
    }
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
    _augment_rp2040_onboard_ethernet_pins(from_yaml.boards)
    _augment_rmii_data_pins(from_yaml.boards)
    _stamp_featured_locked_pins(from_yaml.boards)
    _consolidate_full_setup_bundles(from_yaml.boards)
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


def test_no_duplicate_platform_name_boards() -> None:
    """
    No two catalog entries share a ``(platform, display-name)``.

    A curated manifest claiming an ESPHome board key under a different id must not
    leave a same-named generated twin in the picker (the WIZnet/Freenove/Sonoff
    duplicates).
    """
    seen: dict[tuple[str, str], str] = {}
    dups: list[str] = []
    for board in load_board_catalog().boards:
        key = (board.esphome.platform.value, board.name)
        if key in seen:
            dups.append(f"{board.name!r}: {board.id} vs {seen[key]}")
        else:
            seen[key] = board.id
    assert not dups, "Duplicate (platform, name) board entries:\n" + "\n".join(dups)


def test_wiznet_w6300_resolves_to_curated_ethernet_board() -> None:
    """The W6300-EVB-Pico2 lists once, as the curated board carrying onboard ethernet."""
    hits = [b for b in load_board_catalog().boards if b.name == "WIZnet W6300-EVB-Pico2"]
    assert len(hits) == 1
    board = hits[0]
    assert board.esphome.board == "wiznet_6300_evb_pico2"
    assert any(fc.component_id == "ethernet" for fc in board.featured_components)


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


def test_featured_locked_pins_from_schema() -> None:
    """``locked_pins`` carries the schema-derived GPIO for each locked PIN field."""
    body = load_board_body_from_disk("apollo-esk-1")
    assert body is not None
    by_id = {fc.id: fc for fc in body.featured_components}
    # i2c bus: scl/sda are PIN entries, both locked to bare ints.
    assert by_id["i2c_bus"].locked_pins == {"scl": 0, "sda": 1}
    # A locked pin given as the long-form mapping reduces to its bare GPIO.
    assert by_id["boot_button"].locked_pins == {"pin": 9}
    # A featured component with no locked pins ships an empty (omitted) map.
    assert by_id["aht20"].locked_pins == {}


def test_featured_locked_pins_namespace_io_expander_pins() -> None:
    """An expander channel is a ``provider:hub_id:channel`` token, not a board GPIO."""
    body = load_board_body_from_disk("kincony_b16")
    assert body is not None
    by_id = {fc.id: fc for fc in body.featured_components}
    # pcf8574 expander channel — namespaced so it never aliases board GPIO 0.
    assert by_id["b16_input01"].locked_pins == {"pin": "pcf8574:pcf8574_hub_in_1:0"}
    # A real board GPIO on the same board is still recorded as an int.
    assert by_id["binary_sensor_gpio_17"].locked_pins == {"pin": 48}


def test_full_setup_bundle_synthesized_for_independent_components() -> None:
    """A full-config board of independent featured components gets an ``all_recommended`` bundle."""
    body = load_board_body_from_disk("esp32_relay_x4")
    assert body is not None
    assert body.full_config is True
    by_id = {b.id: b for b in body.featured_bundles}
    assert "all_recommended" in by_id
    assert by_id["all_recommended"].component_ids == [
        "switch_gpio_1",
        "switch_gpio_2",
        "switch_gpio_3",
        "switch_gpio_4",
    ]


def test_full_setup_bundle_skipped_for_curated_optional_board() -> None:
    """A hand-curated board (optional components) gets no synthesized ``all_recommended``."""
    body = load_board_body_from_disk("apollo-esk-1")
    assert body is not None
    assert body.full_config is False
    assert "all_recommended" not in {b.id for b in body.featured_bundles}


def test_full_setup_bundle_skipped_when_existing_bundle_covers_all() -> None:
    """No duplicate ``all_recommended`` when the importer's bundle already covers everything."""
    body = load_board_body_from_disk("arlec_grid_connect_smart_led_globe_cwww")
    assert body is not None
    bundle_ids = {b.id for b in body.featured_bundles}
    assert "all_recommended" not in bundle_ids
    # The one derived bundle already lists every featured component.
    (bundle,) = body.featured_bundles
    assert set(bundle.component_ids) == {fc.id for fc in body.featured_components}


def test_synthesize_full_setup_bundle_gating_and_ordering() -> None:
    """Synthesis is gated on full_config, skips <2 featured, orders existing members first."""

    def _board(
        fids: list[str],
        bundles: tuple[tuple[str, list[str]], ...] = (),
        *,
        full_config: bool = True,
    ) -> BoardCatalogEntry:
        return BoardCatalogEntry(
            id="b",
            name="Board",
            description="d",
            manufacturer="m",
            esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
            full_config=full_config,
            featured_components=[FeaturedComponent(id=f, component_id="switch.gpio") for f in fids],
            featured_bundles=[
                FeaturedBundle(id=bid, name=bid, component_ids=members) for bid, members in bundles
            ],
        )

    # Optional-component board (full_config False) is never synthesized into.
    optional = _board(["a", "b"], full_config=False)
    _consolidate_full_setup_bundles([optional])
    assert optional.featured_bundles == []

    # A single featured component needs no bundle.
    single = _board(["only"])
    _consolidate_full_setup_bundles([single])
    assert single.featured_bundles == []

    # A partial dependency bundle is replaced by the single all_recommended:
    # its members come first, standalone after, and the sub-bundle is dropped.
    board = _board(["dep", "consumer", "extra"], bundles=(("c_setup", ["dep", "consumer"]),))
    _consolidate_full_setup_bundles([board])
    (only,) = board.featured_bundles
    assert only.id == "all_recommended"
    assert only.component_ids == ["dep", "consumer", "extra"]
    assert only.name == "Board (full setup)"

    # When a derived bundle already covers every featured component it stays as
    # the single bundle; sibling subset bundles are pruned and no all_recommended
    # is synthesized.
    covered = _board(
        ["dep", "consumer"],
        bundles=(("c_setup", ["dep", "consumer"]), ("d_setup", ["dep"])),
    )
    _consolidate_full_setup_bundles([covered])
    (kept,) = covered.featured_bundles
    assert kept.id == "c_setup"
    assert kept.component_ids == ["dep", "consumer"]


def test_synthesize_full_setup_bundle_skips_pin_conflict() -> None:
    """Two members claiming the same board GPIO get no bundle unless allow_other_uses."""

    def _fc(fid: str, gpio: int, *, allow_other_uses: bool = False) -> FeaturedComponent:
        value: Any = {"number": gpio, "allow_other_uses": True} if allow_other_uses else gpio
        return FeaturedComponent(
            id=fid,
            component_id="switch.gpio",
            fields={"pin": FieldPreset(value=value, locked=True)},
            locked_pins={"pin": gpio},
        )

    def _board(components: list[FeaturedComponent]) -> BoardCatalogEntry:
        return BoardCatalogEntry(
            id="b",
            name="Board",
            description="d",
            manufacturer="m",
            esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
            full_config=True,
            featured_components=components,
        )

    # Two plain locked GPIO 13s would fail ESPHome pin validation — no bundle.
    conflict = _board([_fc("a", 13), _fc("b", 13)])
    _consolidate_full_setup_bundles([conflict])
    assert conflict.featured_bundles == []

    # The same pin shared on purpose (allow_other_uses) is fine — bundle stands.
    shared = _board([_fc("a", 13, allow_other_uses=True), _fc("b", 13, allow_other_uses=True)])
    _consolidate_full_setup_bundles([shared])
    assert {b.id for b in shared.featured_bundles} == {"all_recommended"}

    # ESPHome needs *every* usage to allow it; one plain usage still conflicts.
    mixed = _board([_fc("a", 13, allow_other_uses=True), _fc("b", 13)])
    _consolidate_full_setup_bundles([mixed])
    assert mixed.featured_bundles == []

    # The pin-conflict carve-out wins over the covering-bundle collapse: a
    # bundle that lists every (conflicting) component would not compile, so the
    # partial bundles are kept untouched rather than collapsed onto it.
    conflict_covered = _board([_fc("a", 13), _fc("b", 13)])
    conflict_covered.featured_bundles = [
        FeaturedBundle(id="all_setup", name="x", component_ids=["a", "b"]),
        FeaturedBundle(id="a_setup", name="y", component_ids=["a"]),
    ]
    _consolidate_full_setup_bundles([conflict_covered])
    assert [b.id for b in conflict_covered.featured_bundles] == ["all_setup", "a_setup"]


def test_has_pin_conflict_folds_in_list_valued_pins() -> None:
    """A board GPIO claimed by a list-valued pin (octal SPI ``data_pins``) is detected."""
    # ``locked_pins`` holds one canonical pin per key, so an octal ``data_pins``
    # list never lands there; the detector must read the raw fields to see it.
    spi = FeaturedComponent(
        id="bus",
        component_id="spi",
        fields={"data_pins": FieldPreset(value=[6, 7, 15], locked=True)},
        locked_pins={},
    )
    clash = FeaturedComponent(
        id="relay",
        component_id="switch.gpio",
        fields={"pin": FieldPreset(value=7, locked=True)},
        locked_pins={"pin": 7},
    )
    assert _has_pin_conflict([spi, clash]) is True

    no_clash = FeaturedComponent(
        id="relay",
        component_id="switch.gpio",
        fields={"pin": FieldPreset(value=21, locked=True)},
        locked_pins={"pin": 21},
    )
    assert _has_pin_conflict([spi, no_clash]) is False

    # A list item that opts into allow_other_uses isn't a conflict when the
    # colliding usage also allows it (tracked per item, not hardcoded False).
    spi_shared = FeaturedComponent(
        id="bus",
        component_id="spi",
        fields={
            "data_pins": FieldPreset(value=[{"number": 7, "allow_other_uses": True}], locked=True)
        },
        locked_pins={},
    )
    clash_shared = FeaturedComponent(
        id="relay",
        component_id="switch.gpio",
        fields={"pin": FieldPreset(value={"number": 7, "allow_other_uses": True}, locked=True)},
        locked_pins={"pin": 7},
    )
    assert _has_pin_conflict([spi_shared, clash_shared]) is False


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
