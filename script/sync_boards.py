#!/usr/bin/env python3
"""
Generate the split board catalog from the per-board manifest YAMLs.

Emits three artefacts under ``esphome_device_builder/definitions/``:

* ``boards.index.json`` — slim ``BoardCatalogIndex`` per board (picker
  fields only: identity, esphome platform/board/variant, tags, images,
  urls, sort flags). This is what ``boards/get_boards`` ships.
* ``board_bodies/<id>.json`` — full body per board (hardware, pins,
  featured_components, featured_bundles, default_components). Lazy-
  loaded via :class:`LazyBodyStore` on ``boards/get_board``. The
  directory name is distinct from the manifests dir at
  ``definitions/boards/<id>/manifest.yaml`` so the body-swap rmtree
  can't trample the hand-curated source.
* ``featured_components.index.json`` — aggregated
  ``{board_id: list[FeaturedComponent]}`` for the components
  controller's startup registry build. Lets ``components.py``
  hook up cross-catalog references without ever touching board
  bodies.

The YAML manifests under ``definitions/boards/<id>/manifest.yaml``
remain the human-editable source of truth; this script is the only
thing that writes the three artefacts.

Usage
-----

    python script/sync_boards.py
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from dataclasses import replace
from operator import attrgetter
from pathlib import Path
from typing import Any

import orjson

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _catalog_split import (  # noqa: E402
    emit_body_with_roundtrip,
    prepare_next_bodies_dir,
    swap_split_catalog_in,
)

from esphome_device_builder.definitions import (  # noqa: E402
    build_board_catalog_from_manifests,
)
from esphome_device_builder.models import (  # noqa: E402
    BoardCatalogEntry,
    BoardCatalogIndex,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardPin,
    Esp32Variant,
    PinFeature,
    Platform,
)

_LOGGER = logging.getLogger("sync_boards")

_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_INDEX_FILE = _DEFINITIONS_DIR / "boards.index.json"
_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_INDEX_FILE = _DEFINITIONS_DIR / "featured_components.index.json"

# Fields stripped from the slim index entry — they belong on the
# per-board body file only.
_INDEX_DROP_FIELDS: frozenset[str] = frozenset(
    {"hardware", "pins", "featured_components", "featured_bundles", "default_components"}
)

# LibreTiny families: ESPHome's ``components/<platform>/boards.py`` carries
# ``*_BOARDS`` (board -> {name, family}) and ``*_BOARD_PINS`` (board ->
# {alias: gpio}). The manifests cover only a curated subset and ship no pin
# maps, so these are the source for both filling manifested boards' pins and
# generating entries for the boards the manifests don't cover.
_LIBRETINY_FAMILIES: dict[str, tuple[str, str]] = {
    "bk72xx": ("BK72XX_BOARDS", "BK72XX_BOARD_PINS"),
    "rtl87xx": ("RTL87XX_BOARDS", "RTL87XX_BOARD_PINS"),
    "ln882x": ("LN882X_BOARDS", "LN882X_BOARD_PINS"),
}

# RP2040/RP2350 also derive pins from ESPHome board data, but as a GPIO matrix:
# ``BOARDS`` carries ``max_pin`` (full GPIO range), ``RP2040_BOARD_PINS`` only the
# conventional default-bus pins.
_RP2040_PLATFORM = "rp2040"

# ESPHome's ``components/esp32/boards.py`` carries ``BOARDS`` (board ->
# {name, variant}) and ``ESP32_BOARD_PINS`` (board -> {alias: gpio}). The
# manifests cover ~50 boards by ``esphome.board``; the rest of the catalog is
# generated from here so every ESPHome esp32 board resolves in the picker.
_ESP32_BOARDS_MODULE = "esphome.components.esp32.boards"
_ESP32_BOARDS_ATTR = "BOARDS"
_ESP32_BOARD_PINS_ATTR = "ESP32_BOARD_PINS"

# Fixed-function pin aliases -> (feature, human signal). First match wins. The
# trailing ``$`` excludes LibreTiny's flexible-mux variants (``WIRE0_SCL_5``
# enumerates every SCL-capable pin, not a fixed bus). ``Dn`` / ``Pn`` / ``PAn``
# positional names carry no capability.
_PIN_ALIAS_RULES: tuple[tuple[re.Pattern[str], PinFeature, str], ...] = (
    (re.compile(r"^(?:SERIAL(\d+)_TX|TX(\d*))$"), PinFeature.UART_TX, "UART{n} TX"),
    (re.compile(r"^(?:SERIAL(\d+)_RX|RX(\d*))$"), PinFeature.UART_RX, "UART{n} RX"),
    (re.compile(r"^(?:WIRE(\d+)_SDA|SDA(\d*))$"), PinFeature.I2C_SDA, "I2C{n} SDA"),
    (re.compile(r"^(?:WIRE(\d+)_SCL|SCL(\d*))$"), PinFeature.I2C_SCL, "I2C{n} SCL"),
    (re.compile(r"^(?:SPI(\d+)_MOSI|MOSI(\d*))$"), PinFeature.SPI_MOSI, "SPI{n} MOSI"),
    (re.compile(r"^(?:SPI(\d+)_MISO|MISO(\d*))$"), PinFeature.SPI_MISO, "SPI{n} MISO"),
    (re.compile(r"^(?:SPI(\d+)_SCK|SCK(\d*))$"), PinFeature.SPI_CLK, "SPI{n} SCK"),
    (re.compile(r"^(?:SPI(\d+)_CS|CS(\d*)|SS(\d*))$"), PinFeature.SPI_CS, "SPI{n} CS"),
    (re.compile(r"^(?:ADC\d*|A\d+)$"), PinFeature.ADC, "ADC"),
    (re.compile(r"^DAC\d*$"), PinFeature.DAC, "DAC"),
    (re.compile(r"^PWM\d*$"), PinFeature.PWM, "PWM"),
)


def _alias_capability(alias: str) -> tuple[PinFeature, str] | None:
    """
    Map a fixed-function pin alias to ``(feature, signal)``, or ``None``.

    ``None`` for positional names (``D4`` / ``P6`` / ``PA07``) and for
    LibreTiny's enumerated flexible-mux aliases (``WIRE0_SCL_5``).
    """
    for pattern, feature, label in _PIN_ALIAS_RULES:
        match = pattern.match(alias)
        if match:
            bus = next((g for g in match.groups() if g), "")
            return feature, label.replace("{n}", bus)
    return None


def _derive_pins_from_aliases(board_pins: dict[str, int]) -> list[BoardPin]:
    """
    Build ``BoardPin``s from an ESPHome ``{alias: gpio}`` map.

    Aliases on a shared GPIO union their features; ``notes`` lists the fixed bus
    roles (``UART1 TX • I2C2 SCL``). A GPIO with only positional aliases still
    emits a bare pin so the dropdown lists it. ``label`` matches ``_load_pin``'s
    ``GPIO{n}`` default so generated and manifest pins read the same.
    """
    features: dict[int, list[PinFeature]] = {}
    signals: dict[int, list[str]] = {}
    for alias, gpio in board_pins.items():
        features.setdefault(gpio, [])
        signals.setdefault(gpio, [])
        cap = _alias_capability(alias)
        if cap is None:
            continue
        feature, signal = cap
        if feature not in features[gpio]:
            features[gpio].append(feature)
        if signal not in signals[gpio]:
            signals[gpio].append(signal)
    return [
        BoardPin(
            gpio=gpio,
            label=f"GPIO{gpio}",
            features=sorted(features[gpio], key=attrgetter("value")),
            notes=" • ".join(signals[gpio]) or None,
        )
        for gpio in sorted(features)
    ]


def _resolve_board_pins(pin_map: dict[str, Any], name: str) -> dict[str, int] | None:
    """Resolve an ESPHome board's ``{alias: gpio}`` map, following string aliases."""
    value = pin_map.get(name)
    seen: set[str] = set()
    while isinstance(value, str) and value not in seen:
        seen.add(value)
        value = pin_map.get(value)
    return value if isinstance(value, dict) else None


def _generated_libretiny_board(
    platform: str, name: str, meta: Any, pins: dict[str, int]
) -> BoardCatalogEntry:
    """Build a minimal catalog entry (name + derived pins) for an unmanifested board."""
    return BoardCatalogEntry(
        id=name,
        name=meta.get("name", name) if isinstance(meta, dict) else name,
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(
            platform=Platform(platform), board=name, variant=None, framework=None
        ),
        pins=_derive_pins_from_aliases(pins),
    )


def _augment_libretiny_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Fill LibreTiny pins from ESPHome and add the boards manifests don't cover.

    ``*_BOARD_PINS`` has the real per-board pinouts the ``pins: []`` manifests
    lack. Per family: (1) fill a manifested board's empty pins, (2) generate a
    canonical entry for every ESPHome board no manifest *id* already defines, so
    ``board: bw15`` resolves with its pinout and lists in the picker. Dedup is on
    board ``id`` (the unique key), not ``esphome.board`` — a chip referenced only
    by vendor-product manifests still gets its own canonical board.
    """
    ids = {b.id for b in boards}
    for platform, (boards_attr, pins_attr) in _LIBRETINY_FAMILIES.items():
        module = importlib.import_module(f"esphome.components.{platform}.boards")
        # No getattr default: an upstream rename of these (private) symbols
        # should fail the sync loudly, not silently emit a pinless catalog.
        board_list: dict[str, Any] = getattr(module, boards_attr)
        pin_map: dict[str, Any] = getattr(module, pins_attr)
        for board in boards:
            if board.esphome.platform.value == platform and not board.pins:
                pins = _resolve_board_pins(pin_map, board.esphome.board)
                if pins:
                    board.pins = _derive_pins_from_aliases(pins)
        for name, meta in board_list.items():
            if name in ids:
                continue
            pins = _resolve_board_pins(pin_map, name)
            if pins:
                boards.append(_generated_libretiny_board(platform, name, meta, pins))
                ids.add(name)


# RP2350B (48-GPIO, max_pin 47) routes its ADC inputs to GPIO40-47; rp2040 and
# rp2350A (30-GPIO) use GPIO26-29.
_RP2350B_MAX_PIN = 47
_RP2040_ADC_GPIOS = range(26, 30)
_RP2350B_ADC_GPIOS = range(40, 48)
_BUILTIN_LED = "Built-in LED"

# RP2040 default-bus aliases -> the note shown on the pin. The bare aliases are
# the default bus (bus 0); ``SDA1``/``SCL1`` the second i2c bus. ``LED`` is
# handled as ``occupied_by``, not a note.
_RP2040_ALIAS_NOTES: dict[str, str] = {
    "TX": "Default UART0 TX",
    "RX": "Default UART0 RX",
    "SDA": "Default I2C0 SDA",
    "SCL": "Default I2C0 SCL",
    "SDA1": "Default I2C1 SDA",
    "SCL1": "Default I2C1 SCL",
    "MOSI": "Default SPI0 MOSI (TX)",
    "MISO": "Default SPI0 MISO (RX)",
    "SCK": "Default SPI0 CLK",
    "SS": "Default SPI0 CS",
}


def _derive_rp2040_pins(board_pins: dict[str, int], max_pin: int) -> list[BoardPin]:
    """
    Build GPIO0..max_pin pins for an RP2040/RP2350 board.

    Every GPIO carries pwm; the analog GPIOs add adc (26-29, or 40-47 on the
    48-GPIO rp2350B); default-bus aliases from ``RP2040_BOARD_PINS`` add their
    feature + note. ``LED`` becomes ``occupied_by``; alias pins past ``max_pin``
    (the CYW43 virtual LED) are dropped.
    """
    features: dict[int, list[PinFeature]] = {g: [PinFeature.PWM] for g in range(max_pin + 1)}
    notes: dict[int, list[str]] = {g: [] for g in range(max_pin + 1)}
    occupied: dict[int, str] = {}

    adc_range = _RP2350B_ADC_GPIOS if max_pin == _RP2350B_MAX_PIN else _RP2040_ADC_GPIOS
    for gpio in adc_range:
        if gpio <= max_pin:
            features[gpio].append(PinFeature.ADC)
            notes[gpio].append(f"ADC{gpio - adc_range.start}")

    for alias, gpio in board_pins.items():
        if gpio > max_pin:
            continue
        if alias == "LED":
            occupied[gpio] = _BUILTIN_LED
            continue
        cap = _alias_capability(alias)
        if cap is None:
            continue
        feature = cap[0]
        if feature not in features[gpio]:
            features[gpio].append(feature)
        note = _RP2040_ALIAS_NOTES.get(alias)
        if note and note not in notes[gpio]:
            notes[gpio].append(note)

    return [
        BoardPin(
            gpio=gpio,
            label=f"GPIO{gpio}",
            features=sorted(features[gpio], key=attrgetter("value")),
            occupied_by=occupied.get(gpio),
            notes=", ".join(notes[gpio]) or None,
        )
        for gpio in range(max_pin + 1)
    ]


def _generated_rp2040_board(
    name: str, meta: dict[str, Any], pins: list[BoardPin]
) -> BoardCatalogEntry:
    """Build a minimal catalog entry (name + matrix pins) for an unmanifested RP2040 board."""
    return BoardCatalogEntry(
        id=name,
        name=meta.get("name", name),
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(
            platform=Platform.RP2040, board=name, variant=None, framework=None
        ),
        pins=pins,
    )


def _generated_esp32_board(
    name: str, meta: dict[str, Any], pins: list[BoardPin], variant: Esp32Variant
) -> BoardCatalogEntry:
    """Build a minimal catalog entry (name + variant + pins) for an unmanifested board."""
    return BoardCatalogEntry(
        id=name,
        name=meta.get("name", name),
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(
            platform=Platform("esp32"), board=name, variant=variant, framework=None
        ),
        pins=pins,
    )


def _augment_rp2040_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Add catalog entries for RP2040/RP2350 boards no manifest id covers.

    No empty-pin fill step — the manifested rp2040 boards already ship full
    pinouts; only generation matters. Dedup on board ``id``. A board missing from
    ``RP2040_BOARD_PINS`` still gets the matrix (GPIO0..max_pin + pwm + adc).
    """
    ids = {b.id for b in boards}
    module = importlib.import_module("esphome.components.rp2040.boards")
    default_max_pin: int = module.DEFAULT_MAX_PIN
    for name, meta in module.BOARDS.items():
        if name in ids:
            continue
        max_pin = meta.get("max_pin", default_max_pin)
        pins = _resolve_board_pins(module.RP2040_BOARD_PINS, name) or {}
        boards.append(_generated_rp2040_board(name, meta, _derive_rp2040_pins(pins, max_pin)))
        ids.add(name)


def _esp32_generic_pins_by_variant(
    boards: list[BoardCatalogEntry],
) -> dict[Esp32Variant, list[BoardPin]]:
    """Index the loaded ``generic-<variant>`` manifests' pins by variant."""
    return {
        b.esphome.variant: b.pins
        for b in boards
        if b.is_generic and b.esphome.platform.value == "esp32" and b.esphome.variant
    }


def _augment_esp32_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Generate ESP32 entries for the boards manifests don't cover.

    ``BOARDS`` lists every esp32 board ESPHome knows; the manifests cover only a
    curated subset. For each uncovered board, derive pins from
    ``ESP32_BOARD_PINS`` when present, else fall back to the chip's
    ``generic-<variant>`` pinout (GPIO capability is variant-defined). Dedup is
    on board ``id`` — the unique key — so a board referenced only by vendor
    manifests still gets its own canonical entry.
    """
    ids = {b.id for b in boards}
    generic_pins = _esp32_generic_pins_by_variant(boards)
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    # No getattr default: an upstream rename should fail the sync loudly.
    board_list: dict[str, Any] = getattr(module, _ESP32_BOARDS_ATTR)
    pin_map: dict[str, Any] = getattr(module, _ESP32_BOARD_PINS_ATTR)
    for name, meta in board_list.items():
        if name in ids:
            continue
        variant = Esp32Variant(meta["variant"].lower())
        pins = _resolve_board_pins(pin_map, name)
        derived = (
            _derive_pins_from_aliases(pins)
            if pins
            else [replace(p, features=list(p.features)) for p in generic_pins.get(variant, [])]
        )
        boards.append(_generated_esp32_board(name, meta, derived, variant))
        ids.add(name)


def build_catalog() -> BoardCatalogResponse:
    """
    Build the catalog as emitted: manifests + ESPHome-derived LibreTiny/RP2040/ESP32 pins.

    Id-sorted so the order matches the split index. Shared by ``main`` and the
    drift test so the committed artefacts stay reproducible.
    """
    catalog = build_board_catalog_from_manifests(strict=True)
    _augment_libretiny_boards(catalog.boards)
    _augment_rp2040_boards(catalog.boards)
    _augment_esp32_boards(catalog.boards)
    catalog.boards.sort(key=attrgetter("id"))
    return catalog


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Abort the sync on the first bad manifest — partial output here
    # would silently ship a board-shaped hole to every install.
    catalog = build_catalog()

    # ``to_dict`` here already applies the omit_default Configs, so
    # body files and index entries both ship the stripped wire shape.
    full_payloads = [board.to_dict() for board in catalog.boards]
    _emit_split_catalog(catalog.boards, full_payloads)
    _emit_featured_components_index(catalog.boards)

    _LOGGER.info(
        "Wrote %s + %d body files under %s + %s",
        _INDEX_FILE,
        len(catalog.boards),
        _BODIES_DIR,
        _FEATURED_INDEX_FILE,
    )
    return 0


def _emit_split_catalog(
    boards: list[BoardCatalogEntry], full_payloads: list[dict[str, Any]]
) -> None:
    """Write ``boards.index.json`` + ``board_bodies/<id>.json`` via atomic swap."""
    next_bodies = _BODIES_DIR.parent / "board_bodies.next"
    prepare_next_bodies_dir(next_bodies)

    for board, payload in zip(boards, full_payloads, strict=True):
        # Body files carry the full BoardCatalogEntry payload — they
        # round-trip through ``BoardCatalogEntry.from_dict`` standalone,
        # mirroring the automations / components split where each
        # body file is self-describing.
        emit_body_with_roundtrip(
            payload,
            board.id,
            next_bodies,
            BoardCatalogEntry,
            log_label="Board",
            sort_keys=True,
        )

    index_payload = {
        "boards": sorted(
            (_strip_body_fields(payload) for payload in full_payloads),
            key=lambda p: p["id"],
        ),
    }
    swap_split_catalog_in(
        next_bodies=next_bodies,
        live_bodies=_BODIES_DIR,
        index_payload=index_payload,
        live_index=_INDEX_FILE,
        index_cls=BoardCatalogIndex,
        index_entries_key="boards",
        sort_keys=True,
    )


def _emit_featured_components_index(boards: list[BoardCatalogEntry]) -> None:
    """Write the aggregated ``{board_id: list[FeaturedComponent]}`` index.

    The components controller hits this once at startup to build its
    cross-catalog featured-component registry without ever touching
    per-board body files. Boards with no featured components are
    omitted so the file stays tight.
    """
    payload: dict[str, list[dict[str, Any]]] = {}
    for board in boards:
        if not board.featured_components:
            continue
        payload[board.id] = [fc.to_dict() for fc in board.featured_components]
    next_path = _FEATURED_INDEX_FILE.with_suffix(".json.next")
    next_path.write_bytes(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
    )
    next_path.replace(_FEATURED_INDEX_FILE)


def _strip_body_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return *payload* with body-only keys removed (slim shape)."""
    return {k: v for k, v in payload.items() if k not in _INDEX_DROP_FIELDS}


if __name__ == "__main__":
    raise SystemExit(main())
