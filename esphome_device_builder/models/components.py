"""Component catalog data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mashumaro.mixins.orjson import DataClassORJSONMixin

from .common import ConfigEntry, PagedResponse


class ComponentCategory(StrEnum):
    """Component categories (ESPHome platform types + infrastructure).

    Values must mirror the strings written by the sync script — anything
    missing here gets coerced to ``MISC`` at load time, which would
    silently break the category filter for that domain.
    """

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SWITCH = "switch"
    LIGHT = "light"
    FAN = "fan"
    COVER = "cover"
    CLIMATE = "climate"
    BUTTON = "button"
    NUMBER = "number"
    SELECT = "select"
    TEXT = "text"
    TEXT_SENSOR = "text_sensor"
    LOCK = "lock"
    VALVE = "valve"
    MEDIA_PLAYER = "media_player"
    SPEAKER = "speaker"
    MICROPHONE = "microphone"
    CAMERA = "camera"
    DISPLAY = "display"
    TOUCHSCREEN = "touchscreen"
    OUTPUT = "output"
    DATETIME = "datetime"
    EVENT = "event"
    UPDATE = "update"
    ALARM = "alarm_control_panel"
    CORE = "core"
    BUS = "bus"
    AUTOMATION = "automation"
    # Platform-domain umbrellas surfaced as their own categories by
    # the sync script. ``ota.*``/``time.*`` components live under
    # these and the regular component selector hides them since the
    # OTA / time blocks belong in the core dialog.
    OTA = "ota"
    TIME = "time"
    # Other platform domains the sync script tags from the schema —
    # listed explicitly so loading doesn't silently coerce them to
    # MISC.
    AUDIO_ADC = "audio_adc"
    AUDIO_DAC = "audio_dac"
    CANBUS = "canbus"
    INFRARED = "infrared"
    MEDIA_SOURCE = "media_source"
    ONE_WIRE = "one_wire"
    PACKET_TRANSPORT = "packet_transport"
    STEPPER = "stepper"
    WATER_HEATER = "water_heater"
    MISC = "misc"
    # Synthetic category for components surfaced as board recommendations.
    # Featured entries are materialised on the fly from the board catalog
    # and only appear in API results when ``category=featured`` is the
    # explicit filter — they are excluded from the regular catalog
    # listing the same way ``core`` / ``ota`` / ``time`` / ``update`` are.
    FEATURED = "featured"


@dataclass
class ComponentCatalogEntry(DataClassORJSONMixin):
    """A component in the catalog.

    Components map 1:1 to ESPHome's `components/` directory. Each entry
    describes how to render and serialize one block in the user's YAML
    config (e.g. `wifi:`, `sensor:`, `i2c:`).
    """

    # Component ID — matches ESPHome's component directory name and the
    # YAML key the user types (e.g. "wifi", "dht", "i2c").
    id: str

    # Human-readable name shown in the UI ("Wi-Fi", "DHT Temperature
    # & Humidity Sensor", "I²C Bus").
    name: str

    # Description shown on the component card and detail view. Sourced
    # from the ESPHome docs frontmatter and first paragraph.
    description: str

    # Group the component is filed under in the catalog UI.
    category: ComponentCategory

    # Direct link to the official ESPHome docs page for this component.
    docs_url: str = ""

    # Optional image / illustration shown on the component card.
    image_url: str = ""

    # Other components this one requires to be configured. ESPHome
    # rejects the YAML if a dependency is missing — the frontend should
    # warn the user and offer to add the missing component.
    dependencies: list[str] = field(default_factory=list)

    # Whether the same component can be added multiple times (e.g.
    # multiple sensors, multiple I²C buses). When False, the component
    # is a singleton (e.g. `wifi:`, `api:`).
    multi_conf: bool = False

    # Empty list = component works on every target platform. Non-empty
    # = component is restricted to those platforms (e.g. ["esp32"] for
    # ESP32-only hardware features). Frontend uses this to filter the
    # available components based on the device's selected board.
    supported_platforms: list[str] = field(default_factory=list)

    # The component's own configuration fields. Nested config blocks
    # (e.g. ``esp32_ble_tracker.scan_parameters``) and entity
    # sub-readings (DHT temperature / humidity) appear here as
    # ConfigEntry instances of type=NESTED that carry their own
    # ``config_entries``.
    config_entries: list[ConfigEntry] = field(default_factory=list)


@dataclass
class AddComponentRequest(DataClassORJSONMixin):
    """Request to add a component to a device config."""

    component_id: str
    # Field values keyed by config-entry key. Nested entries are
    # represented as nested dicts (one level per ConfigEntry of
    # type=NESTED).
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class AddComponentResponse(DataClassORJSONMixin):
    """Response after adding a component."""

    yaml: str


@dataclass
class PagedComponentsResponse(PagedResponse):
    """Paginated component catalog API response."""

    components: list[ComponentCatalogEntry] = field(default_factory=list)
    categories: list[dict[str, str | int]] = field(default_factory=list)
