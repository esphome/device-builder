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

Single-board mode (``BOARD_ID``) must run against the same ESPHome the
rest of the committed catalog was generated against (the
``esphome_version`` a full sync stamps into ``boards.index.json``),
since it rebuilds the shared index from every board; it refuses on a
mismatch. A full sync regenerates everything from the installed ESPHome
and re-stamps that version, so it does not check.

Usage
-----

    python script/sync_boards.py              # regenerate every board
    python script/sync_boards.py BOARD_ID     # regenerate only one board
"""

from __future__ import annotations

import argparse
import importlib
import logging
import re
import shutil
import sys
from collections.abc import Iterator
from dataclasses import replace
from functools import cache
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
from _esphome_version import assert_installed_esphome  # noqa: E402

from esphome_device_builder.constants import BOARD_PIN_KEYS  # noqa: E402
from esphome_device_builder.definitions import (  # noqa: E402
    build_board_catalog_from_manifests,
)
from esphome_device_builder.helpers.pin_gpio import parse_board_gpio  # noqa: E402
from esphome_device_builder.models import (  # noqa: E402
    BoardCatalogEntry,
    BoardCatalogIndex,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardPin,
    BoardTag,
    Esp32Variant,
    FeaturedBundle,
    FeaturedComponent,
    PinFeature,
    Platform,
)

_LOGGER = logging.getLogger("sync_boards")

_DEFINITIONS_DIR = _REPO_ROOT / "esphome_device_builder" / "definitions"
_INDEX_FILE = _DEFINITIONS_DIR / "boards.index.json"
_BODIES_DIR = _DEFINITIONS_DIR / "board_bodies"
_FEATURED_INDEX_FILE = _DEFINITIONS_DIR / "featured_components.index.json"
_COMPONENTS_DIR = _DEFINITIONS_DIR / "components"

# Fields stripped from the slim index entry — they belong on the
# per-board body file only.
_INDEX_DROP_FIELDS: frozenset[str] = frozenset(
    {
        "hardware",
        "pins",
        "featured_components",
        "featured_bundles",
        "default_components",
        "full_config",
    }
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

# ESPHome LibreTiny board meta carries ``family`` (the chip). Fold it into the
# picker's per-chip series token (``mcu``): BK7231N/T/Q share one ``bk7231``
# filter, the rest map 1:1. An unmapped future family falls back to its own
# lowercased token in _backfill_libretiny_mcu, so a new chip still gets a
# distinct section (only a frontend chip line is then needed).
_LIBRETINY_MCU: dict[str, str] = {
    "BK7231N": "bk7231",
    "BK7231T": "bk7231",
    "BK7231Q": "bk7231",
    "BK7238": "bk7238",
    "BK7251": "bk7251",
    "RTL8710B": "rtl8710b",
    "RTL8720C": "rtl8720c",
    "LN882H": "ln882h",
}

# Per-platform documentation page for generated boards (those no manifest
# covers). Curated manifests already point at these same ESPHome component pages,
# so a generated board's "More info" link lands on the right docs instead of an
# empty href.
_PLATFORM_DOCS_URL: dict[Platform, str] = {
    Platform.ESP32: "https://esphome.io/components/esp32.html",
    Platform.ESP8266: "https://esphome.io/components/esp8266.html",
    Platform.RP2040: "https://esphome.io/components/rp2040.html",
    Platform.BK72XX: "https://esphome.io/components/libretiny.html",
    Platform.RTL87XX: "https://esphome.io/components/libretiny.html",
    Platform.LN882X: "https://esphome.io/components/libretiny.html",
    Platform.NRF52: "https://esphome.io/components/nrf52.html",
}


# ESPHome's ``components/esp32/boards.py`` carries ``BOARDS`` (board ->
# {name, variant}) and ``ESP32_BOARD_PINS`` (board -> {alias: gpio}). The
# manifests cover ~50 boards by ``esphome.board``; the rest of the catalog is
# generated from here so every ESPHome esp32 board resolves in the picker.
_ESP32_BOARDS_MODULE = "esphome.components.esp32.boards"
_ESP32_BOARDS_ATTR = "BOARDS"
_ESP32_BOARD_PINS_ATTR = "ESP32_BOARD_PINS"

# nRF52 ships no per-board pin aliases: ``boards.BOARDS_ZEPHYR`` is just the board
# list (bootloader config, no name/pins) and ``const.AIN_TO_GPIO`` is a
# chip-level ADC map shared by every board. Buses (I2C/SPI/UART) are
# software-routable to any pin, so ADC is the only fixed-function signal to
# derive; we emit every valid GPIO as a matrix so the editor renders a dropdown.
_NRF52_PLATFORM = "nrf52"
_NRF52_MAX_GPIO = 48  # gpio.py::validate_gpio_pin: 0 <= value <= 32 + 16

# BOARDS_ZEPHYR has no display name; these are the user-facing picker labels.
_NRF52_BOARD_NAMES: dict[str, str] = {
    "xiao_ble": "Seeed XIAO nRF52840",
    "adafruit_itsybitsy_nrf52840": "Adafruit ItsyBitsy nRF52840",
}

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


# A ``GPIO<n>`` alias is just the ``label`` under another name — drop it from
# ``aliases`` so the field only carries the named forms the user might type.
_GPIO_LABEL_RE = re.compile(r"^GPIO\d+$", re.IGNORECASE)


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
    ``GPIO{n}`` default so generated and manifest pins read the same. ``aliases``
    carries the named forms (``RX``, ``D1``) so the editor can select a pin a
    config refers to by name rather than ``GPIO{n}``.
    """
    features: dict[int, list[PinFeature]] = {}
    signals: dict[int, list[str]] = {}
    aliases: dict[int, list[str]] = {}
    for alias, gpio in board_pins.items():
        features.setdefault(gpio, [])
        signals.setdefault(gpio, [])
        names = aliases.setdefault(gpio, [])
        if not _GPIO_LABEL_RE.match(alias) and alias not in names:
            names.append(alias)
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
            aliases=sorted(aliases[gpio]),
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


def _meta_name(meta: Any, name: str) -> str:
    """Display name from an ESPHome board's ``meta`` dict, falling back to the id."""
    return meta.get("name", name) if isinstance(meta, dict) else name


def _generated_board(
    platform: Platform,
    name: str,
    display_name: str,
    pins: list[BoardPin],
    variant: Esp32Variant | None = None,
) -> BoardCatalogEntry:
    """Build a minimal catalog entry (identity + derived pins) for an unmanifested board."""
    return BoardCatalogEntry(
        id=name,
        name=display_name,
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=platform, board=name, variant=variant, framework=None),
        pins=pins,
        docs_url=_PLATFORM_DOCS_URL.get(platform, ""),
    )


def _generation_dedup_keys(
    boards: list[BoardCatalogEntry],
) -> tuple[set[str], set[tuple[Platform, str]]]:
    """Seed the id and (platform, display-name) sets a generator skips covered boards by."""
    ids = {b.id for b in boards}
    names = {(b.esphome.platform, b.name) for b in boards}
    return ids, names


def _name_already_listed(
    platform: Platform, name: str, display: str, names: set[tuple[Platform, str]]
) -> bool:
    """
    Return True when ``(platform, display)`` is already in the catalog; log the skip.

    A curated/generated twin today; the log surfaces a future upstream board
    shadowed by a name collision (generated ``id == board key``, so it would
    otherwise vanish without a catalog entry).
    """
    if (platform, display) not in names:
        return False
    _LOGGER.debug(
        "Not generating %s board %r: display name %r already in the catalog",
        platform.value,
        name,
        display,
    )
    return True


def _augment_libretiny_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Fill LibreTiny pins from ESPHome and add the boards manifests don't cover.

    ``*_BOARD_PINS`` has the real per-board pinouts the ``pins: []`` manifests
    lack. Per family: (1) fill a manifested board's empty pins, (2) generate a
    canonical entry for every ESPHome board no manifest *id* already defines, so
    ``board: bw15`` resolves with its pinout and lists in the picker. Dedup is on
    board ``id`` (the unique key), not ``esphome.board`` — a chip referenced only
    by vendor-product manifests still gets its own canonical board — and on
    display name so a curated board claiming an ESPHome key isn't twinned.
    """
    ids, names = _generation_dedup_keys(boards)
    for platform, (boards_attr, pins_attr) in _LIBRETINY_FAMILIES.items():
        module = importlib.import_module(f"esphome.components.{platform}.boards")
        # No getattr default: an upstream rename of these (private) symbols
        # should fail the sync loudly, not silently emit a pinless catalog.
        board_list: dict[str, Any] = getattr(module, boards_attr)
        pin_map: dict[str, Any] = getattr(module, pins_attr)
        pf = Platform(platform)
        for board in boards:
            if board.esphome.platform.value == platform and not board.pins:
                pins = _resolve_board_pins(pin_map, board.esphome.board)
                if pins:
                    board.pins = _derive_pins_from_aliases(pins)
        for name, meta in board_list.items():
            display = _meta_name(meta, name)
            if name in ids or _name_already_listed(pf, name, display, names):
                continue
            pins = _resolve_board_pins(pin_map, name)
            if pins:
                boards.append(_generated_board(pf, name, display, _derive_pins_from_aliases(pins)))
                ids.add(name)
                names.add((pf, display))


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


def _augment_rp2040_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Add catalog entries for RP2040/RP2350 boards no manifest id covers.

    No empty-pin fill step — the manifested rp2040 boards already ship full
    pinouts; only generation matters. Dedup on board ``id`` and display name, so a
    curated board claiming an ESPHome key under a different id doesn't also emit a
    same-named twin. A board missing from ``RP2040_BOARD_PINS`` still gets the matrix.
    """
    ids, names = _generation_dedup_keys(boards)
    module = importlib.import_module("esphome.components.rp2040.boards")
    default_max_pin: int = module.DEFAULT_MAX_PIN
    for name, meta in module.BOARDS.items():
        display = _meta_name(meta, name)
        if name in ids or _name_already_listed(Platform.RP2040, name, display, names):
            continue
        max_pin = meta.get("max_pin", default_max_pin)
        pins = _resolve_board_pins(module.RP2040_BOARD_PINS, name) or {}
        boards.append(
            _generated_board(Platform.RP2040, name, display, _derive_rp2040_pins(pins, max_pin))
        )
        ids.add(name)
        names.add((Platform.RP2040, display))


def _backfill_rp2040_wifi(boards: list[BoardCatalogEntry]) -> None:
    """
    Tag each WiFi-capable rp2040 board (Pico W, etc.) so the picker shows a chip.

    Derived from the pio board's ESPHome ``wifi`` flag, so it covers curated boards
    too (e.g. ``generic-rp2040`` maps to the wifi ``rpipicow`` target). Wi-Fi is
    universal on esp32/esp8266/libretiny, so only rp2040 gets the chip. A backfill
    (not part of generation) so the manifest-only drift test applies it the same way.
    """
    module = importlib.import_module("esphome.components.rp2040.boards")
    for board in boards:
        if board.esphome.platform is Platform.RP2040 and BoardTag.WIFI not in board.tags:
            meta = module.BOARDS.get(board.esphome.board)
            if isinstance(meta, dict) and meta.get("wifi"):
                board.tags.append(BoardTag.WIFI)


def _backfill_rp2040_mcu(boards: list[BoardCatalogEntry]) -> None:
    """
    Set each rp2040 board's chip series ("rp2040" / "rp2350") from ESPHome.

    ESPHome lumps both chips under the rp2040 platform; ``mcu`` is the only
    structured discriminator, letting the picker split the filter and badge the
    real chip. Covers curated and generated boards alike; a backfill (not part of
    generation) so the manifest-only drift test applies it the same way.
    """
    module = importlib.import_module("esphome.components.rp2040.boards")
    for board in boards:
        if board.esphome.platform is Platform.RP2040:
            meta = module.BOARDS.get(board.esphome.board)
            board.esphome.mcu = meta.get("mcu", "rp2040") if isinstance(meta, dict) else "rp2040"


def _backfill_libretiny_mcu(boards: list[BoardCatalogEntry]) -> None:
    """
    Set each LibreTiny board's chip series (``mcu``) from ESPHome's family.

    bk72xx/rtl87xx/ln882x each lump several chips under one platform; ``mcu`` is
    the picker's per-chip discriminator (BK7231N/T/Q fold to ``bk7231``). Covers
    curated and generated boards; a backfill so the manifest-only drift test
    applies it the same way. A board ESPHome doesn't list falls back to the
    platform's sole token (ln882x -> ``ln882h``) or stays unset.
    """
    for platform, (boards_attr, _pins_attr) in _LIBRETINY_FAMILIES.items():
        module = importlib.import_module(f"esphome.components.{platform}.boards")
        board_list: dict[str, Any] = getattr(module, boards_attr)
        family_by_board = {board: meta.get("family") for board, meta in board_list.items()}
        tokens = {_LIBRETINY_MCU[f] for f in family_by_board.values() if f in _LIBRETINY_MCU}
        sole_token = next(iter(tokens)) if len(tokens) == 1 else None
        for board in boards:
            if board.esphome.platform.value != platform:
                continue
            family = family_by_board.get(board.esphome.board)
            if family in _LIBRETINY_MCU:
                board.esphome.mcu = _LIBRETINY_MCU[family]
            elif family:
                board.esphome.mcu = re.sub(r"[^a-z0-9]", "", family.lower())
            elif sole_token:
                board.esphome.mcu = sole_token


# SPI ethernet pin field -> the occupied_by label shown on the overlaid pin.
_RP2040_ETHERNET_PIN_ROLES: dict[str, str] = {
    "clk_pin": "Ethernet CLK",
    "mosi_pin": "Ethernet MOSI",
    "miso_pin": "Ethernet MISO",
    "cs_pin": "Ethernet CS",
    "interrupt_pin": "Ethernet INT",
    "reset_pin": "Ethernet RESET",
}

# Canonical Pico/Pico2 pinout each onboard-ethernet board overlays onto, by chip
# series. Reusing these curated bodies (not ESPHome's matrix) fixes RP2350A, whose
# ESPHome ``max_pin`` is 47 though the Pico2 form factor exposes 30 GPIOs.
_RP2040_BASE_PINOUT_BOARD: dict[str, str] = {"rp2040": "rpipico", "rp2350": "rpipico2"}


def _gpio_number(value: object) -> int | None:
    """GPIO number from a pin-field value (``"GPIO17"`` / ``"P23"`` / ``17``), or ``None``."""
    return parse_board_gpio(value)


def _augment_rp2040_onboard_ethernet_pins(boards: list[BoardCatalogEntry]) -> None:
    """
    Overlay a pinless rp2040 ethernet board's SPI pins onto the Pico pinout.

    Uses rpipico / rpipico2 by chip series and locks the ethernet pins; skips
    boards that already ship pins.
    """
    base_by_mcu = {
        board.esphome.mcu: board.pins
        for board in boards
        if board.esphome.board in _RP2040_BASE_PINOUT_BOARD.values()
    }
    for board in boards:
        if board.esphome.platform is not Platform.RP2040 or board.pins:
            continue
        ethernet = next(
            (fc for fc in board.featured_components if fc.component_id == "ethernet"), None
        )
        base = base_by_mcu.get(board.esphome.mcu or "rp2040")
        if ethernet is None or not base:
            continue
        roles: dict[int, str] = {}
        for field, role in _RP2040_ETHERNET_PIN_ROLES.items():
            preset = ethernet.fields.get(field)
            gpio = _gpio_number(preset.value) if preset is not None else None
            if gpio is not None:
                roles[gpio] = role
        board.pins = [
            replace(pin, available=False, occupied_by=roles[pin.gpio], features=[], notes=None)
            if pin.gpio in roles
            else replace(pin, features=list(pin.features))
            for pin in base
        ]


def _esp32_generic_pins_by_variant(
    boards: list[BoardCatalogEntry],
) -> dict[tuple[Esp32Variant, bool], list[BoardPin]]:
    """
    Index the loaded ``generic-<variant>`` manifests' pins by (variant, ES-ness).

    The P4 has one generic per silicon revision (GPIO54 is a GPIO on pre-rev3,
    a power rail on rev3); keying on variant alone let whichever generic loads
    last hand its pinout to every generated board of the variant.
    """
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    board_list: dict[str, Any] = getattr(module, _ESP32_BOARDS_ATTR)
    return {
        (b.esphome.variant, bool(board_list.get(b.esphome.board, {}).get("engineering_sample"))): (
            b.pins
        )
        for b in boards
        if b.is_generic and b.esphome.platform.value == "esp32" and b.esphome.variant
    }


def _esp32_board_pins(generic: list[BoardPin], board_pins: dict[str, int] | None) -> list[BoardPin]:
    """
    Variant pinout enriched with a board's ESP32_BOARD_PINS aliases.

    Returns the generic-<variant> pinout with the board's LED flagged occupied
    and fixed-bus aliases adding a feature plus note. Pins the variant marks
    unavailable (flash) keep their generic definition; aliases on them are
    ignored. Bare alias derivation is the fallback for a variant with no
    generic manifest.
    """
    board_pins = board_pins or {}
    if not generic:
        return _derive_pins_from_aliases(board_pins)
    overlay = {p.gpio: p for p in _derive_pins_from_aliases(board_pins)}
    led = {gpio for alias, gpio in board_pins.items() if alias == "LED"}
    out: list[BoardPin] = []
    for base in generic:
        extra = overlay.get(base.gpio)
        if base.available is False:
            # Flash pins stay authoritative; an alias must not list a bus on one.
            out.append(replace(base, features=list(base.features)))
            continue
        # Preserve the curated generic order; board aliases append after it.
        features = list(base.features)
        notes = [base.notes] if base.notes else []
        if extra is not None:
            features += [f for f in extra.features if f not in features]
            if extra.notes and extra.notes not in notes:
                notes.append(extra.notes)
        out.append(
            replace(
                base,
                features=features,
                notes=" • ".join(notes) or None,
                occupied_by=base.occupied_by or (_BUILTIN_LED if base.gpio in led else None),
            )
        )
    return out


def _backfill_esp32_variants(boards: list[BoardCatalogEntry]) -> None:
    """Fill ``esphome.variant`` from the PIO board id for esp32 boards missing it.

    Imported manifests sometimes carry only ``board:`` (no ``variant:``). Without
    a variant the generated ``esp32:`` block has neither key (the schema needs at
    least one) and the picker tags the board as bare ESP32 instead of its
    sub-variant. ``BOARDS`` is the authoritative board -> variant map.
    """
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    board_list: dict[str, Any] = getattr(module, _ESP32_BOARDS_ATTR)
    for board in boards:
        cfg = board.esphome
        if cfg.platform.value != "esp32" or cfg.variant is not None:
            continue
        meta = board_list.get(cfg.board)
        if meta is not None:
            cfg.variant = Esp32Variant(meta["variant"].lower())


def _augment_esp32_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Generate ESP32 entries for the boards manifests don't cover.

    ``BOARDS`` lists every esp32 board ESPHome knows; the manifests cover only a
    curated subset. Each uncovered board takes its chip's ``generic-<variant>``
    pinout, enriched with any named ``ESP32_BOARD_PINS`` aliases. Dedup is on
    board ``id`` (the unique key), so a board referenced only by vendor
    manifests still gets its own canonical entry, and on display name so an
    ESPHome key-aliased board (``freenove-esp32-s3-n8r8`` /
    ``freenove_esp32_s3_wroom``) isn't listed twice.
    """
    ids, names = _generation_dedup_keys(boards)
    generic_pins = _esp32_generic_pins_by_variant(boards)
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    # No getattr default: an upstream rename should fail the sync loudly.
    board_list: dict[str, Any] = getattr(module, _ESP32_BOARDS_ATTR)
    pin_map: dict[str, Any] = getattr(module, _ESP32_BOARD_PINS_ATTR)
    for name, meta in board_list.items():
        display = _meta_name(meta, name)
        if name in ids or _name_already_listed(Platform("esp32"), name, display, names):
            continue
        variant = Esp32Variant(meta["variant"].lower())
        pins = _resolve_board_pins(pin_map, name)
        es = bool(meta.get("engineering_sample"))
        # Fall back to the other revision's generic for variants with one manifest.
        base = generic_pins.get((variant, es)) or generic_pins.get((variant, not es), [])
        derived = _esp32_board_pins(base, pins)
        boards.append(_generated_board(Platform("esp32"), name, display, derived, variant))
        ids.add(name)
        names.add((Platform("esp32"), display))


def _backfill_esp32_engineering_sample(boards: list[BoardCatalogEntry]) -> None:
    """
    Stamp ``esphome.engineering_sample`` from ESPHome's ``BOARDS`` table.

    Pre-rev3 ESP32-P4 pio boards are flagged upstream; generated configs must
    emit ``engineering_sample: true`` or esphome builds rev3-only firmware that
    faults at boot on that silicon. Fails the sync on an esp32 board id ESPHome
    doesn't know (a typo shipped ``esp32-p4-function-ev-board`` unnoticed).
    """
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    board_list: dict[str, Any] = getattr(module, _ESP32_BOARDS_ATTR)
    unknown: list[str] = []
    for board in boards:
        cfg = board.esphome
        if cfg.platform.value != "esp32":
            continue
        meta = board_list.get(cfg.board)
        if meta is None:
            unknown.append(f"{board.id} ({cfg.board})")
            continue
        cfg.engineering_sample = bool(meta.get("engineering_sample"))
    if unknown:
        raise SystemExit(
            "esp32 manifests whose esphome.board is unknown to the installed "
            f"ESPHome: {', '.join(sorted(unknown))}"
        )


def _esp8266_generic_pins(boards: list[BoardCatalogEntry]) -> list[BoardPin]:
    """Return the curated ``generic-esp8266`` GPIO0-17 pinout every board overlays onto."""
    return next(
        (b.pins for b in boards if b.is_generic and b.esphome.platform.value == "esp8266"),
        [],
    )


def _overlay_esp8266_aliases(target: list[BoardPin], board_pins: dict[str, int]) -> list[BoardPin]:
    """
    Overlay an ESP8266 board's named aliases onto a full pinout by gpio.

    Keeps every pin in *target* so plain GPIOs (``GPIO0``/``GPIO2``) stay
    selectable, and annotates the gpios that carry an alias with the name
    (``RX``/``D1``) and its bus feature. The generic pin's curated note
    already names the role, so it's left as-is — the alias name is the new
    fact. Falls back to bare alias derivation when there is no pinout to enrich.
    """
    if not target:
        return _derive_pins_from_aliases(board_pins)
    overlay = {p.gpio: p for p in _derive_pins_from_aliases(board_pins)}
    out: list[BoardPin] = []
    for base in target:
        extra = overlay.get(base.gpio)
        if extra is None:
            out.append(replace(base, features=list(base.features)))
            continue
        out.append(
            replace(
                base,
                features=sorted(set(base.features) | set(extra.features), key=attrgetter("value")),
                aliases=list(extra.aliases),
            )
        )
    return out


def _augment_esp8266_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Fill ESP8266 pins from ESPHome and add the boards manifests don't cover.

    A pinless esp8266 entry (``esp01_1m``, the empty product manifests) takes the
    generic GPIO0-17 pinout with the board's named aliases overlaid by gpio, so a
    plain ``GPIO0`` stays selectable and a value written as ``RX`` resolves to its
    GPIO. Curated manifests keep their authored pins untouched. Per-board
    ``ESP8266_BOARD_PINS`` carry positional ``Dn``/``LED`` names; the fixed-
    function bus pins (``RX``/``TX``/``SDA`` …) live in shared
    ``ESP8266_BASE_PINS``, so overlay ``{**base, **board}`` — the merge ESPHome's
    pin resolver uses. Dedup on board ``id`` (curated esp8266 manifests use the
    ESPHome board name verbatim, so a base board referenced only by vendor
    products still generates its own canonical entry, e.g. ``esp01_1m``;
    otherwise ``find_by_pio_board`` falls back to an arbitrary product — #395),
    and on display name so a curated product claiming an ESPHome key under a
    different id (``sonoff-basic`` for ``sonoff_basic``) isn't listed twice.
    """
    module = importlib.import_module("esphome.components.esp8266.boards")
    # Direct access (not getattr-with-default): an upstream rename of these
    # private symbols should fail the sync loudly, not emit a pinless catalog.
    board_list: dict[str, Any] = module.BOARDS
    pin_map: dict[str, Any] = module.ESP8266_BOARD_PINS
    base: dict[str, int] = module.ESP8266_BASE_PINS
    generic = _esp8266_generic_pins(boards)
    ids, names = _generation_dedup_keys(boards)
    for board in boards:
        if board.esphome.platform.value != "esp8266" or board.pins:
            continue
        amap = {**base, **(_resolve_board_pins(pin_map, board.esphome.board) or {})}
        board.pins = _overlay_esp8266_aliases(generic, amap)
    for name, meta in board_list.items():
        display = _meta_name(meta, name)
        if name in ids or _name_already_listed(Platform("esp8266"), name, display, names):
            continue
        amap = {**base, **(_resolve_board_pins(pin_map, name) or {})}
        boards.append(
            _generated_board(
                Platform("esp8266"), name, display, _overlay_esp8266_aliases(generic, amap)
            )
        )
        ids.add(name)
        names.add((Platform("esp8266"), display))


def _derive_nrf52_pins(adc_gpios: set[int]) -> list[BoardPin]:
    """
    Build P0.0..P1.16 pins for an nRF52 board.

    Labels use the chip's ``P{port}.{pin}`` notation (``port*32 + pin``) — the
    form ESPHome's validator accepts; ``GPIOn`` is rejected. Only the chip-level
    ADC pins carry a feature; the rest list bare so the dropdown shows them.
    """
    return [
        BoardPin(
            gpio=gpio,
            label=f"P{gpio // 32}.{gpio % 32}",
            features=[PinFeature.ADC] if gpio in adc_gpios else [],
            notes="ADC" if gpio in adc_gpios else None,
        )
        for gpio in range(_NRF52_MAX_GPIO + 1)
    ]


def _augment_nrf52_boards(boards: list[BoardCatalogEntry]) -> None:
    """
    Add catalog entries for nRF52 boards no manifest id covers.

    nRF52 ships no per-board pin aliases, so every board gets the same ADC-tagged
    full-GPIO matrix from the chip-level ``AIN_TO_GPIO``. A ``BOARDS_ZEPHYR`` name
    whose catalog id is already owned by another platform (rp2040's
    ``adafruit_itsybitsy``, the PlatformIO string both platforms share) can't be
    served by an id-keyed catalog, so it's skipped with a warning rather than
    shadowed onto the other platform's pinout. Also deduped on display name so a
    curated nRF52 board claiming a Zephyr id under a different id isn't twinned.
    """
    platform_by_id = {b.id: b.esphome.platform.value for b in boards}
    _, names = _generation_dedup_keys(boards)
    boards_module = importlib.import_module("esphome.components.nrf52.boards")
    const_module = importlib.import_module("esphome.components.nrf52.const")
    adc_gpios = set(const_module.AIN_TO_GPIO.values())
    for name in boards_module.BOARDS_ZEPHYR:
        owner = platform_by_id.get(name)
        if owner is not None:
            if owner != _NRF52_PLATFORM:
                _LOGGER.warning(
                    "nRF52 board %r shares a catalog id with an existing %s board; "
                    "not generating it (an id-keyed catalog can't serve both — needs "
                    "platform-aware board resolution)",
                    name,
                    owner,
                )
            continue
        display = _NRF52_BOARD_NAMES.get(name, name)
        if _name_already_listed(Platform.NRF52, name, display, names):
            continue
        boards.append(
            _generated_board(Platform.NRF52, name, display, _derive_nrf52_pins(adc_gpios))
        )
        platform_by_id[name] = _NRF52_PLATFORM
        names.add((Platform.NRF52, display))


def _has_rmii_ethernet(board: BoardCatalogEntry) -> bool:
    """Return True when the board's onboard ethernet is RMII (signalled by ``mdc_pin``)."""
    return any(
        fc.component_id == "ethernet" and "mdc_pin" in fc.fields for fc in board.featured_components
    )


def _augment_rmii_data_pins(boards: list[BoardCatalogEntry]) -> None:
    """
    Mark the hardware-fixed RMII Ethernet data pins occupied on RMII boards.

    The EMAC consumes TXD0/TXD1/TX_EN/RXD0/RXD1/CRS_DV on fixed GPIOs (per ESP32
    variant) that never appear in the ``ethernet:`` config, so the pin picker
    would otherwise offer them as free. Sourced from ESPHome so a pinout revision
    flows through; the configurable pins (MDC/MDIO/CLK/power) are already marked
    by the board's featured ethernet component.
    """
    module = importlib.import_module("esphome.components.ethernet")
    # No getattr default: an upstream rename should fail the sync loudly.
    by_variant: dict[Esp32Variant, dict[int, str]] = {
        Esp32Variant.ESP32: module.ESP32_RMII_FIXED_PINS,
        Esp32Variant.ESP32P4: module.ESP32P4_RMII_DEFAULT_PINS,
    }
    for board in boards:
        if board.esphome.platform is not Platform.ESP32 or not _has_rmii_ethernet(board):
            continue
        fixed = by_variant.get(board.esphome.variant or Esp32Variant.ESP32)
        if not fixed:
            continue
        roles = {gpio: f"Ethernet {emac.removeprefix('EMAC_')}" for gpio, emac in fixed.items()}
        # Merge keyed by gpio: replace a free data pin in place (leaving an
        # already-occupied one, e.g. a CLK on a CLK_OUT board, untouched), and
        # append any fixed pin the manifest didn't declare at all.
        merged = {pin.gpio: pin for pin in board.pins}
        for gpio, role in roles.items():
            pin = merged.get(gpio)
            if pin is None:
                merged[gpio] = BoardPin(
                    gpio=gpio, label=f"GPIO{gpio}", available=False, occupied_by=role
                )
            elif not pin.occupied_by:
                merged[gpio] = replace(
                    pin, available=False, occupied_by=role, features=[], notes=None
                )
        board.pins = list(merged.values())


@cache
def _component_body(component_id: str) -> dict[str, Any]:
    """Load and parse ``<component_id>.json``; an empty dict when missing/unreadable."""
    path = _COMPONENTS_DIR / f"{component_id}.json"
    try:
        return orjson.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}


@cache
def _component_pin_keys(component_id: str) -> frozenset[str]:
    """Top-level config_entry keys of ``type: pin`` for *component_id* (empty if unknown)."""
    entries = _component_body(component_id).get("config_entries", [])
    return frozenset(e["key"] for e in entries if e.get("type") == "pin")


@cache
def _component_reference_keys(component_id: str) -> frozenset[str]:
    """Config-entry keys (any depth) that cross-reference another component by id."""
    keys: set[str] = set()

    def _walk(entries: list[dict[str, Any]] | None) -> None:
        for entry in entries or []:
            if entry.get("references_component") and entry.get("key"):
                keys.add(entry["key"])
            _walk(entry.get("config_entries"))

    _walk(_component_body(component_id).get("config_entries"))
    return frozenset(keys)


@cache
def _component_pin_paths(component_id: str) -> tuple[tuple[str, ...], ...]:
    """Config-entry paths of every ``type: pin`` field for *component_id*, nested pins included."""
    paths: list[tuple[str, ...]] = []

    def _walk(entries: list[dict[str, Any]] | None, prefix: tuple[str, ...]) -> None:
        for entry in entries or []:
            key = entry.get("key")
            if key is None:
                continue
            here = (*prefix, key)
            if entry.get("type") == "pin":
                # A pin field's own children are long-form flags (mode / inverted),
                # never nested pins — stop here.
                paths.append(here)
            else:
                _walk(entry.get("config_entries"), here)

    _walk(_component_body(component_id).get("config_entries"), ())
    return tuple(paths)


def _canonical_gpio(value: Any) -> int | None:
    """Reduce a manifest pin value (bare int, ``GPIOn`` string, ``{number: n}``) to a board GPIO int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        # Skip expander pins: a hub-referencing key means a channel, not a GPIO.
        if value.keys() - BOARD_PIN_KEYS:
            return None
        return _canonical_gpio(value.get("number"))
    return parse_board_gpio(value)


def _canonical_pin(value: Any) -> int | str | None:
    """
    Reduce a manifest pin value to its occupied-pin identity.

    A board GPIO is an int; a pin on an I/O expander
    (``{number: 0, pcf8574: hub}``) is the namespaced token
    ``"<provider>:<hub_id>:<channel>"`` so an expander channel never aliases a
    board GPIO of the same number. ``None`` when no concrete pin is present.
    """
    if isinstance(value, dict):
        expander = value.keys() - BOARD_PIN_KEYS
        if expander:
            provider = sorted(expander)[0]
            hub = value.get(provider)
            channel = value.get("number")
            if isinstance(hub, str) and isinstance(channel, int) and not isinstance(channel, bool):
                return f"{provider}:{hub}:{channel}"
            return None
    return _canonical_gpio(value)


def _locked_pin_value(fc: FeaturedComponent, pin_path: tuple[str, ...]) -> Any:
    """
    Locked preset value of *fc* at *pin_path*, descending nested fields.

    ``None`` when the top-level field is absent or unlocked, or the path
    doesn't resolve — e.g. ethernet ``("clk", "pin")`` reads ``GPIO0`` out of
    ``clk: {mode, pin: GPIO0}``.
    """
    preset = fc.fields.get(pin_path[0])
    if preset is None or not preset.locked:
        return None
    value: Any = preset.value
    for sub in pin_path[1:]:
        if not isinstance(value, dict):
            return None
        value = value.get(sub)
    return value


def _stamp_featured_locked_pins(boards: list[BoardCatalogEntry]) -> None:
    """Fill each featured component's ``locked_pins`` from the underlying PIN schema."""
    for board in boards:
        for fc in board.featured_components:
            for pin_path in _component_pin_paths(fc.component_id):
                pin = _canonical_pin(_locked_pin_value(fc, pin_path))
                if pin is not None:
                    fc.locked_pins[".".join(pin_path)] = pin


def _stamp_featured_requires(boards: list[BoardCatalogEntry]) -> None:
    """
    Fill each featured component's ``requires`` from cross-references to siblings.

    A featured field whose value equals a *sibling* featured component's emitted
    id (its ``id`` preset) is a prerequisite — an ``rtttl`` ``output:`` pointing
    at a sibling ``output.ledc``, a sensor ``i2c_id:`` at its bus. That sibling
    must be added first or the config references an undefined id. Inferred ids
    union with hand-authored ``requires`` and are flattened transitively, since
    the frontend resolves only a component's direct list.
    """
    for board in boards:
        direct = _direct_featured_requires(board.featured_components)
        for fc in board.featured_components:
            fc.requires = _flatten_requires(fc.id, direct)


def _direct_featured_requires(
    components: list[FeaturedComponent],
) -> dict[str, list[str]]:
    """Direct prereqs per featured id: hand-authored first, then inferred sibling references."""
    by_emitted_id: dict[str, str] = {}
    for fc in components:
        preset = fc.fields.get("id")
        if preset is not None and isinstance(preset.value, str):
            by_emitted_id.setdefault(preset.value, fc.id)
    direct: dict[str, list[str]] = {}
    for fc in components:
        deps = list(fc.requires)
        # Only a genuine cross-reference field (``output``, ``i2c_id``, a light's
        # colour channels) points at a sibling; a free-text field whose value
        # happens to match an id (a ``name``) must not infer a dependency.
        reference_keys = _component_reference_keys(fc.component_id)
        for key, preset in fc.fields.items():
            if key not in reference_keys or not isinstance(preset.value, str):
                continue
            target = by_emitted_id.get(preset.value)
            if target is not None and target != fc.id and target not in deps:
                deps.append(target)
        direct[fc.id] = deps
    return direct


def _flatten_requires(local_id: str, direct: dict[str, list[str]]) -> list[str]:
    """Transitive prereq closure of *local_id*, deps ordered before dependents (cycle-safe)."""
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(node: str, stack: frozenset[str]) -> None:
        for dep in direct.get(node, ()):
            if dep in stack:  # cycle guard
                continue
            visit(dep, stack | {node})
            if dep not in seen:
                seen.add(dep)
                ordered.append(dep)

    visit(local_id, frozenset({local_id}))
    return ordered


_ALL_RECOMMENDED_BUNDLE_ID = "all_recommended"


def _consolidate_full_setup_bundles(boards: list[BoardCatalogEntry]) -> None:
    """
    Leave each ``full_config`` board with a single complete "(full setup)" bundle.

    An imported device is one upstream config, so it gets one bundle that sets up
    the whole thing; the importer's per-consumer sub-bundles (which set up only
    part of the device, often badly named) are dropped. When a derived bundle
    already covers every featured component it stays as that one bundle; otherwise
    a board-named ``all_recommended`` covering them all replaces the lot. Left
    alone for: hand-curated boards (optional add-ons, not one device), a single
    featured component, or a board GPIO claimed by two members (the combined
    config would not compile, so the partial bundles are the only valid options).
    """
    for board in boards:
        if not board.full_config:
            continue
        featured_ids = [fc.id for fc in board.featured_components]
        if len(featured_ids) < 2:
            continue
        featured_set = set(featured_ids)
        # Pin conflict first: a board whose components share a locked GPIO can't
        # be set up all at once, so a single combined bundle (even an existing
        # covering one) would not compile — leave the partial bundles in place.
        if _has_pin_conflict(board.featured_components):
            _LOGGER.info(
                "Skipping all_recommended for %s: featured components share a board GPIO",
                board.id,
            )
            continue
        covering = next(
            (b for b in board.featured_bundles if featured_set <= set(b.component_ids)),
            None,
        )
        if covering is not None:
            board.featured_bundles = [covering]
            continue
        # Existing-bundle members first (dependency-ordered by the importer),
        # then the remaining featured ids in manifest order; dict.fromkeys
        # dedups while preserving that first-seen order.
        existing = [m for b in board.featured_bundles for m in b.component_ids if m in featured_set]
        ordered = list(dict.fromkeys(existing + featured_ids))
        board.featured_bundles = [
            FeaturedBundle(
                id=_ALL_RECOMMENDED_BUNDLE_ID,
                name=f"{board.name} (full setup)",
                component_ids=ordered,
            )
        ]


def _has_pin_conflict(components: list[FeaturedComponent]) -> bool:
    """
    Whether the featured components reuse a board GPIO without permission.

    ESPHome accepts a pin used more than once only when *every* usage sets
    ``allow_other_uses``; a single plain usage of a shared pin fails validation.
    Mirror that: a board GPIO used by more than one locked pin is a conflict
    unless all of those usages allow it. Namespaced expander channels are string
    tokens, not board GPIOs, so they're ignored. List-valued pins (octal SPI
    ``data_pins``) are folded in from the raw fields — ``locked_pins`` only holds
    one canonical pin per key, so it drops them.
    """
    usages: dict[int, list[bool]] = {}
    for fc in components:
        for key, gpio in fc.locked_pins.items():
            if isinstance(gpio, int):
                value = _locked_pin_value(fc, tuple(key.split(".")))
                usages.setdefault(gpio, []).append(_pin_allows_reuse(value))
        for gpio, allows in _list_pin_gpios(fc):
            usages.setdefault(gpio, []).append(allows)
    return any(len(uses) > 1 and not all(uses) for uses in usages.values())


def _pin_allows_reuse(value: Any) -> bool:
    """Whether a pin value opts into ``allow_other_uses`` (long-form pin mappings only)."""
    return isinstance(value, dict) and bool(value.get("allow_other_uses"))


def _list_pin_gpios(fc: FeaturedComponent) -> Iterator[tuple[int, bool]]:
    """Yield each ``(board GPIO, allow_other_uses)`` a component locks via a list pin field."""
    for key in _component_pin_keys(fc.component_id):
        preset = fc.fields.get(key)
        if preset is None or not preset.locked or not isinstance(preset.value, list):
            continue
        for item in preset.value:
            gpio = _canonical_gpio(item)
            if gpio is not None:
                yield gpio, _pin_allows_reuse(item)


def build_catalog() -> BoardCatalogResponse:
    """
    Build the catalog as emitted: manifests + ESPHome-derived per-platform pins.

    Id-sorted so the order matches the split index. Shared by ``main`` and the
    drift test so the committed artefacts stay reproducible.
    """
    catalog = build_board_catalog_from_manifests(strict=True)
    _backfill_esp32_variants(catalog.boards)
    _augment_libretiny_boards(catalog.boards)
    _augment_rp2040_boards(catalog.boards)
    _backfill_rp2040_wifi(catalog.boards)
    _backfill_rp2040_mcu(catalog.boards)
    _backfill_libretiny_mcu(catalog.boards)
    _augment_rp2040_onboard_ethernet_pins(catalog.boards)
    _augment_esp32_boards(catalog.boards)
    _backfill_esp32_engineering_sample(catalog.boards)
    _augment_esp8266_boards(catalog.boards)
    _augment_nrf52_boards(catalog.boards)
    _augment_rmii_data_pins(catalog.boards)
    _stamp_featured_locked_pins(catalog.boards)
    _stamp_featured_requires(catalog.boards)
    _consolidate_full_setup_bundles(catalog.boards)
    catalog.boards.sort(key=attrgetter("id"))
    return catalog


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Regenerate the board catalog JSON from the YAML manifests."
    )
    parser.add_argument(
        "board",
        nargs="?",
        help="Board id (the folder name under esphome_device_builder/definitions/boards/) "
        "to regenerate on its own. Omit to regenerate the whole catalog.",
    )
    args = parser.parse_args()

    # Only single-board mode needs the match: it rebuilds the shared index from
    # every board, so a mismatched esphome drifts the others' index entries.
    if args.board:
        _require_matching_esphome()

    # Abort the sync on the first bad manifest — partial output here
    # would silently ship a board-shaped hole to every install.
    catalog = build_catalog()

    # ``to_dict`` here already applies the omit_default Configs, so
    # body files and index entries both ship the stripped wire shape.
    full_payloads = [board.to_dict() for board in catalog.boards]

    if args.board:
        return _emit_single_board(catalog.boards, full_payloads, args.board)

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


def _emit_single_board(
    boards: list[BoardCatalogEntry], full_payloads: list[dict[str, Any]], board_id: str
) -> int:
    """
    Rewrite one board's body file, then refresh the index and featured map.

    Only the target's ``board_bodies/<id>.json`` is rewritten; the index and
    featured-components files are full rebuilds whose other entries are
    byte-identical (the version guard rules out drift), so the diff stays
    scoped to the edited board. This assumes only *board_id* was edited:
    naming one board while another's manifest is also dirty rewrites the
    other's index entry but not its body, which the consistency test flags.
    """
    idx = next((i for i, board in enumerate(boards) if board.id == board_id), None)
    if idx is None:
        raise SystemExit(
            f"sync_boards: no board with id {board_id!r}; "
            f"expected a folder name under {_DEFINITIONS_DIR / 'boards'}"
        )
    _emit_body_atomically(full_payloads[idx], board_id)
    _write_index(full_payloads)
    _emit_featured_components_index(boards)
    _LOGGER.info("Regenerated board_bodies/%s.json + refreshed index and featured map", board_id)
    return 0


def _emit_body_atomically(payload: dict[str, Any], board_id: str) -> None:
    """Write one body file via stage-then-replace so an interrupted write can't truncate it."""
    staging = _BODIES_DIR.parent / "board_bodies.single"
    prepare_next_bodies_dir(staging)
    try:
        emit_body_with_roundtrip(
            payload, board_id, staging, BoardCatalogEntry, log_label="Board", sort_keys=True
        )
        (staging / f"{board_id}.json").replace(_BODIES_DIR / f"{board_id}.json")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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

    swap_split_catalog_in(
        next_bodies=next_bodies,
        live_bodies=_BODIES_DIR,
        index_payload=_index_payload(full_payloads),
        live_index=_INDEX_FILE,
        index_cls=BoardCatalogIndex,
        index_entries_key="boards",
        sort_keys=True,
    )


def _index_payload(full_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the slim index payload, stamped with the ESPHome it was generated from."""
    return {
        "esphome_version": _installed_esphome_version(),
        "boards": sorted(
            (_strip_body_fields(payload) for payload in full_payloads),
            key=lambda p: p["id"],
        ),
    }


# ESPHome betas/dev builds (``2026.7.0b1``, ``2026.7.0-dev``) share board tables
# with their base release, so canonicalize to the base for both the stamp and
# the guard or a beta would false-mismatch its own release.
_ESPHOME_BASE_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)")


def _canonical_esphome_version(version: str) -> str:
    """Drop a prerelease/dev suffix: ``2026.7.0b1`` -> ``2026.7.0``."""
    match = _ESPHOME_BASE_VERSION_RE.match(version)
    return match.group(1) if match else version


def _installed_esphome_version() -> str:
    from esphome.const import __version__

    return _canonical_esphome_version(__version__)


def _write_index(full_payloads: list[dict[str, Any]]) -> None:
    """Rewrite ``boards.index.json`` only, leaving the body files untouched."""
    index_payload = _index_payload(full_payloads)
    for entry in index_payload["boards"]:
        BoardCatalogIndex.from_dict(entry)
    next_index = _INDEX_FILE.with_suffix(".json.next")
    next_index.write_bytes(
        orjson.dumps(index_payload, option=orjson.OPT_SORT_KEYS | orjson.OPT_APPEND_NEWLINE)
    )
    next_index.replace(_INDEX_FILE)


def _require_matching_esphome() -> None:
    """Abort unless installed ESPHome matches the ``esphome_version`` boards.index.json was built with."""
    try:
        expected = orjson.loads(_INDEX_FILE.read_bytes())["esphome_version"]
    except (OSError, orjson.JSONDecodeError, KeyError):
        raise SystemExit(
            f"sync_boards: could not read esphome_version from {_INDEX_FILE}.\n"
            f"To fix, regenerate the whole catalog first: python script/sync_boards.py"
        ) from None
    assert_installed_esphome(
        expected,
        what="sync_boards single-board mode",
        normalize=_canonical_esphome_version,
        alt_fix=(
            "Or regenerate the whole catalog against your installed ESPHome instead:\n"
            "    python script/sync_boards.py"
        ),
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
