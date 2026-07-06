"""Board catalog data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .common import (
    DashboardModel,
    FieldPreset,
    PagedResponse,
    PinFeature,
    _CatalogConfig,
)

# `PinFeature` lives in .common (it's shared with config-entry pin
# constraints), but is re-imported here for stability of existing
# `from .boards import PinFeature` paths.

_ = PinFeature  # suppress "imported but unused" — this is a re-export


class Connectivity(StrEnum):
    """Known connectivity types."""

    WIFI = "wifi"
    BLUETOOTH = "bluetooth"
    ETHERNET = "ethernet"
    ZIGBEE = "zigbee"
    THREAD = "thread"
    OPENTHREAD = "openthread"
    CAN = "can"
    MATTER = "matter"
    LORA = "lora"


class Platform(StrEnum):
    """ESPHome target platforms."""

    ESP32 = "esp32"
    ESP8266 = "esp8266"
    RP2040 = "rp2040"
    BK72XX = "bk72xx"
    RTL87XX = "rtl87xx"
    LN882X = "ln882x"
    NRF52 = "nrf52"
    HOST = "host"


# Inbound platform strings may use the renamed ``rp2`` key; the catalog stays
# keyed on ``rp2040``, so both names fold to it at every ingestion boundary.
RP2_PLATFORM_ALIASES: frozenset[str] = frozenset({"rp2", "rp2040"})


def normalize_platform(name: str) -> str:
    """Fold the renamed ``rp2`` platform key onto the catalog's ``rp2040``."""
    return "rp2040" if name.lower() == "rp2" else name


class Esp32Variant(StrEnum):
    """ESP32 chip variants."""

    ESP32 = "esp32"
    ESP32S2 = "esp32s2"
    ESP32S3 = "esp32s3"
    ESP32S31 = "esp32s31"
    ESP32C2 = "esp32c2"
    ESP32C3 = "esp32c3"
    ESP32C5 = "esp32c5"
    ESP32C6 = "esp32c6"
    ESP32C61 = "esp32c61"
    ESP32H2 = "esp32h2"
    ESP32H4 = "esp32h4"
    ESP32H21 = "esp32h21"
    ESP32P4 = "esp32p4"


class BoardTag(StrEnum):
    """Board tags for unique features not captured by other fields."""

    # Form factor
    COMPACT = "compact"
    DEV_KIT = "dev-kit"
    STARTER_KIT = "starter-kit"
    MODULE = "module"
    BREAKOUT = "breakout"

    # Onboard peripherals
    DISPLAY = "display"
    CAMERA = "camera"
    RGB_LED = "rgb-led"
    RELAY = "relay"
    MOTOR_DRIVER = "motor-driver"
    SD_CARD = "sd-card"
    MICROPHONE = "microphone"
    SPEAKER = "speaker"
    IMU = "imu"

    # Power / connectivity
    WIFI = "wifi"
    LIPO = "lipo"
    POE = "poe"
    USB_C = "usb-c"
    EXTERNAL_ANTENNA = "external-antenna"
    SOLAR = "solar"
    BATTERY = "battery"

    # Ecosystem / OEM
    SONOFF = "sonoff"
    TUYA = "tuya"
    SHELLY = "shelly"


@dataclass
class BoardPin(DashboardModel):
    """A single GPIO pin on a board."""

    gpio: int
    label: str = ""
    features: list[PinFeature] = field(default_factory=list)
    available: bool | None = None  # True=exposed, False=internal, None=unknown
    occupied_by: str | None = None  # e.g. "Built-in LED", "SPI Flash"
    notes: str | None = None
    aliases: list[str] = field(default_factory=list)  # named forms, e.g. RX, D1

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class BoardEsphomeConfig(DashboardModel):
    """Maps this board to an ESPHome YAML platform configuration."""

    platform: Platform
    board: str  # PlatformIO board ID
    variant: Esp32Variant | None = None
    framework: str | None = None  # "arduino" or "esp-idf"
    # Chip series within an ESPHome platform that lumps several under one
    # key: currently rp2040 ("rp2040" / "rp2350"). Reusable to split the
    # libretiny families (e.g. bk72xx chips) the same way later. None where
    # the platform needs no split; esp32 uses ``variant`` for the same role.
    mcu: str | None = None
    # Pre-rev3 ESP32-P4 silicon (esphome's "engineering sample"); stamped by
    # sync_boards from esphome's BOARDS table, never hand-set in manifests.
    engineering_sample: bool = False
    # Explicit logger ``hardware_uart`` for generated configs; may restate the
    # chip default (imports carry the upstream page's value verbatim). None
    # emits a bare ``logger:`` and ESPHome's default applies.
    logger_hardware_uart: str | None = None

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class BoardHardware(DashboardModel):
    """Hardware specifications of a board."""

    flash_size: str | None = None
    ram_size: int | None = None
    cpu_frequency: str | None = None
    connectivity: list[Connectivity] = field(default_factory=list)

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class FeaturedComponent(DashboardModel):
    """
    A component recommended for this board.

    Surfaced in the catalog API under id ``featured.<board_id>.<id>`` and
    category ``featured``. ``component_id`` points at the underlying
    catalog entry the user is actually adding (``switch.gpio``,
    ``binary_sensor.gpio``, ...) — the featured entry contributes
    name/description overrides plus per-field presets keyed by
    ``ConfigEntry.key``.
    """

    # Local id, unique within this board (e.g. "relay", "pir_motion").
    id: str
    component_id: str
    name: str | None = None
    description: str | None = None
    fields: dict[str, FieldPreset] = field(default_factory=dict)
    # Photo of the physical module this entry maps to; overrides the
    # underlying component's generic image on the recommended card.
    # Resolved at sync time (local path -> /boards/images/..., URL as-is).
    image_url: str = ""
    # Mirrors the underlying component's ``multi_conf``. Default True so only
    # the single-instance exceptions serialise (omit_default).
    multi_conf: bool = True
    # Canonical occupied-pin identity per locked pin field, computed at sync time
    # from the underlying component's PIN config_entries. The key is the dot-joined
    # config-entry path to the pin: flat for a top-level pin (``mdc_pin``), dotted
    # for one nested under a composite field (ethernet ``clk.pin``). A board GPIO is
    # an int (``{"scl": 0, "sda": 1}``); an I/O-expander channel is a namespaced
    # token ``"<provider>:<hub_id>:<channel>"`` so it never aliases a board GPIO of
    # the same number. Lets the catalog hide this card when an existing same-domain
    # instance already occupies these exact pins. Empty (omitted) for non-pin
    # components.
    locked_pins: dict[str, int | str] = field(default_factory=dict)
    # Local ids of other featured components on this board that must be added
    # first, in order (e.g. an i2c bus then the pcf8574 hub a gpio pin sits on).
    # The frontend adds any missing prerequisite before this component so a
    # preset whose pin references a hub by id lands a working config.
    requires: list[str] = field(default_factory=list)

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class FeaturedBundle(DashboardModel):
    """
    A logical group of featured components added together.

    Models hardware addons that span multiple ESPHome components — e.g.
    a status LED that needs both ``output.gpio`` and ``light.binary``,
    or an RGB+buzzer module. ``component_ids`` references the local id
    of entries in ``featured_components`` on the same board; the
    frontend triggers sequential ``devices/add_component`` calls for
    each.
    """

    # Local id, unique within this board.
    id: str
    name: str
    description: str = ""
    component_ids: list[str] = field(default_factory=list)
    # Photo of the physical module this bundle maps to; rendered on the
    # bundle card in place of the box icon. Resolved at sync time
    # (local path -> /boards/images/..., URL as-is).
    image_url: str = ""

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class DefaultComponent(DashboardModel):
    """A component installed by default in every new device on this board.

    ``id`` resolves through the same two-step lookup the
    ``default_components`` string form uses: first as a local
    ``featured_components.id`` (picks up that entry's full field
    presets), falling through to a bare catalog ``component_id``.
    ``fields`` carries plain ``key: value`` overrides — no
    ``locked`` / ``suggestions`` wrapping — that supplement (or
    override) the featured component's presets.
    """

    id: str
    fields: dict[str, Any] = field(default_factory=dict)

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class BoardCatalogEntry(DashboardModel):
    """A board definition in the catalog."""

    id: str
    name: str
    description: str
    manufacturer: str
    esphome: BoardEsphomeConfig
    hardware: BoardHardware = field(default_factory=BoardHardware)
    images: list[str] = field(default_factory=list)
    tags: list[BoardTag] = field(default_factory=list)
    pins: list[BoardPin] = field(default_factory=list)
    docs_url: str = ""
    product_url: str = ""
    featured: bool = False
    is_generic: bool = False
    # True when the featured components are a complete onboard config (a
    # devices.esphome.io import, or a hand-curated product opting in) rather
    # than optional add-ons. Gates the synthesized "full setup" bundle.
    # Resolved at manifest load: the manifest's optional ``full_config`` wins,
    # else defaults to "is a devices.esphome.io import".
    full_config: bool = False
    # Components recommended for this board, surfaced in the Add
    # Component dialog as a "Recommended" section.
    featured_components: list[FeaturedComponent] = field(default_factory=list)
    # Logical groups of featured components that the frontend adds
    # together (e.g. a status LED = output.gpio + light.binary).
    featured_bundles: list[FeaturedBundle] = field(default_factory=list)
    # Components installed by default in every new device on this
    # board. Each entry's ``id`` resolves either to a local
    # ``featured_components.id`` (picks up that entry's full field
    # presets) or a catalog ``component_id``; the entry's own
    # ``fields`` dict supplements or overrides those presets.
    default_components: list[DefaultComponent] = field(default_factory=list)
    # True when Wi-Fi is the board's only built-in network (native Wi-Fi, no
    # onboard Ethernet/Thread provider): the create wizard must collect Wi-Fi —
    # a no-network config wouldn't validate. Derived at serve time (from the
    # network providers above + native-Wi-Fi capability); not stored in the JSON.
    requires_wifi: bool = False

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class BoardCatalogIndex(DashboardModel):
    """Slim card-view of :class:`BoardCatalogEntry` (no body fields).

    Picker / list endpoints (``boards/get_boards``) return this shape:
    the picker needs identity + search-key + thumbnail + sort flags but
    not pins / hardware / featured-component presets. Full bodies
    hydrate on demand via the boards ``LazyBodyStore``.
    """

    id: str
    name: str
    description: str
    manufacturer: str
    esphome: BoardEsphomeConfig
    tags: list[BoardTag] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    docs_url: str = ""
    product_url: str = ""
    featured: bool = False
    is_generic: bool = False

    class Config(_CatalogConfig):
        """Skip empty defaults on serialise; see :class:`_CatalogConfig`."""


@dataclass
class BoardCatalogResponse(DashboardModel):
    """Internal: raw board list from definitions loader."""

    boards: list[BoardCatalogEntry]


@dataclass
class PagedBoardsResponse(PagedResponse):
    """Paginated board catalog API response — slim entries only."""

    boards: list[BoardCatalogIndex] = field(default_factory=list)
