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
    BoardTag,
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
                boards.append(
                    _generated_board(
                        Platform(platform),
                        name,
                        _meta_name(meta, name),
                        _derive_pins_from_aliases(pins),
                    )
                )
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
        entry = _generated_board(
            Platform.RP2040, name, _meta_name(meta, name), _derive_rp2040_pins(pins, max_pin)
        )
        boards.append(entry)
        ids.add(name)


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


def _esp32_generic_pins_by_variant(
    boards: list[BoardCatalogEntry],
) -> dict[Esp32Variant, list[BoardPin]]:
    """Index the loaded ``generic-<variant>`` manifests' pins by variant."""
    return {
        b.esphome.variant: b.pins
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
        derived = _esp32_board_pins(generic_pins.get(variant, []), pins)
        boards.append(
            _generated_board(Platform("esp32"), name, _meta_name(meta, name), derived, variant)
        )
        ids.add(name)


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
    otherwise ``find_by_pio_board`` falls back to an arbitrary product — #395).
    """
    module = importlib.import_module("esphome.components.esp8266.boards")
    # Direct access (not getattr-with-default): an upstream rename of these
    # private symbols should fail the sync loudly, not emit a pinless catalog.
    board_list: dict[str, Any] = module.BOARDS
    pin_map: dict[str, Any] = module.ESP8266_BOARD_PINS
    base: dict[str, int] = module.ESP8266_BASE_PINS
    generic = _esp8266_generic_pins(boards)
    ids = {b.id for b in boards}
    for board in boards:
        if board.esphome.platform.value != "esp8266" or board.pins:
            continue
        amap = {**base, **(_resolve_board_pins(pin_map, board.esphome.board) or {})}
        board.pins = _overlay_esp8266_aliases(generic, amap)
    for name, meta in board_list.items():
        if name in ids:
            continue
        amap = {**base, **(_resolve_board_pins(pin_map, name) or {})}
        boards.append(
            _generated_board(
                Platform("esp8266"),
                name,
                _meta_name(meta, name),
                _overlay_esp8266_aliases(generic, amap),
            )
        )
        ids.add(name)


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
    shadowed onto the other platform's pinout.
    """
    platform_by_id = {b.id: b.esphome.platform.value for b in boards}
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
        boards.append(
            _generated_board(
                Platform.NRF52,
                name,
                _NRF52_BOARD_NAMES.get(name, name),
                _derive_nrf52_pins(adc_gpios),
            )
        )
        platform_by_id[name] = _NRF52_PLATFORM


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
    _augment_esp32_boards(catalog.boards)
    _augment_esp8266_boards(catalog.boards)
    _augment_nrf52_boards(catalog.boards)
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
