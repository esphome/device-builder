"""Component catalog data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from .common import ConfigEntry, DashboardModel, PagedResponse, RequiredGroup


class IntegrationDocEntry(TypedDict):
    """One ``components/get_integration_docs`` map value."""

    url: str
    name: str
    description: str


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
    IMAGE = "image"
    INFRARED = "infrared"
    MEDIA_SOURCE = "media_source"
    MOTION = "motion"
    ONE_WIRE = "one_wire"
    PACKET_TRANSPORT = "packet_transport"
    RADIO_FREQUENCY = "radio_frequency"
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
class ComponentCatalogIndexEntry(DashboardModel):
    """
    Slim catalog entry returned by list / search endpoints.

    Carries every field the catalog UI needs to render the card grid,
    apply the platform / category / query filters, and resolve docs
    URLs — but omits the per-field ``config_entries`` tree. The
    detail-view variant (:class:`ComponentCatalogEntry`) adds that
    tree and is fetched on demand via
    :meth:`ComponentCatalog.get_body` when the user opens a card.
    """

    id: str
    name: str
    description: str
    category: ComponentCategory
    docs_url: str = ""
    image_url: str = ""
    dependencies: list[str] = field(default_factory=list)
    multi_conf: bool = False
    supported_platforms: list[str] = field(default_factory=list)
    # Interface namespaces this component can be referenced *as*: a
    # cross-domain interface (an ``adc`` sensor provides
    # ``voltage_sampler``) or its own domain when the referenceable ids
    # are nested sub-entities (``sensor.aht10`` provides ``sensor`` via
    # ``temperature.id`` / ``humidity.id``). Lets the frontend resolve
    # ``references_component`` fields whose target ids aren't a matching
    # section's own top-level ``id``.
    provides: list[str] = field(default_factory=list)

    # For a provided interface whose id lives at *nested* paths rather than
    # the component's own top-level ``id`` (``usb_uart`` exposes a ``uart``
    # via ``channels[].id``), the YAML key-paths the frontend descends to
    # collect candidate ids. Keyed by interface namespace, one entry per
    # nested location (``sprinkler`` exposes ``switch`` at several); absent
    # for the common own-id case (resolved via the section id). A
    # same-domain provider may also carry the root path ``["id"]`` when the
    # component's own id is itself an entity (``sensor.pulse_counter``).
    provides_id_paths: dict[str, list[list[str]]] = field(default_factory=dict)

    # Real catalog category behind a ``featured`` entry (``bus`` for a
    # featured ``spi``), so the card can chip its type alongside its
    # recommendation status. ``None`` on regular entries.
    underlying_category: ComponentCategory | None = None


@dataclass
class ComponentCatalogEntry(DashboardModel):
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

    # Requirements this component imposes on the bus it attaches to, keyed by
    # bus id ("i2c" / "spi" / "uart"). A value is an exact-match scalar
    # (``parity``), a list of choices (first = default; narrows the field's
    # dropdown, e.g. baud ``[2400, 9600]``), or a range bound (``min_frequency``
    # / ``max_frequency``, Hz). ``require_tx`` / ... mark required pins.
    # Frontend pre-fills dep-added buses.
    bus_constraints: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Empty list = component works on every target platform. Non-empty
    # = component is restricted to those platforms (e.g. ["esp32"] for
    # ESP32-only hardware features). Frontend uses this to filter the
    # available components based on the device's selected board.
    supported_platforms: list[str] = field(default_factory=list)

    # Interface namespaces this component can be referenced *as*: a
    # cross-domain interface (an ``adc`` sensor provides ``voltage_sampler``,
    # so it satisfies ``ct_clamp``'s ``sensor:`` reference) or its own
    # domain when the referenceable ids are nested sub-entities
    # (``sensor.aht10`` provides ``sensor`` via ``temperature.id``).
    # Frontend joins this against a field's ``references_component`` to
    # find valid targets beyond a matching section's own top-level ``id``.
    provides: list[str] = field(default_factory=list)

    # Nested id-path locators for entries in ``provides`` whose id isn't
    # the component's own top-level ``id`` (``usb_uart`` → ``{"uart":
    # [["channels", "id"]]}``). Keyed by interface namespace, one path per
    # nested location; absent for own-id providers. Same-domain providers
    # may include the root path ``["id"]`` for hybrid platforms whose own
    # id is also an entity (``sensor.pulse_counter``).
    provides_id_paths: dict[str, list[list[str]]] = field(default_factory=dict)

    # The component's own configuration fields. Nested config blocks
    # (e.g. ``esp32_ble_tracker.scan_parameters``) and entity
    # sub-readings (DHT temperature / humidity) appear here as
    # ConfigEntry instances of type=NESTED that carry their own
    # ``config_entries``.
    config_entries: list[ConfigEntry] = field(default_factory=list)

    # Cross-field cardinality constraints over ``config_entries``.
    # Empty by default; populated when the upstream component
    # wraps its top-level ``CONFIG_SCHEMA`` in
    # ``cv.has_*_one_key(...)`` (e.g. ``light.esp32_rmt_led_strip``
    # requires exactly one of ``chipset`` / the manual-timing
    # group). Frontend pairs these with each entry's ``group`` to
    # render the full "either X or all of {Y, Z, …}" rule.
    required_groups: list[RequiredGroup] = field(default_factory=list)


@dataclass
class AddComponentRequest(DashboardModel):
    """Request to add a component to a device config."""

    component_id: str
    # Field values keyed by config-entry key. Nested entries are
    # represented as nested dicts (one level per ConfigEntry of
    # type=NESTED).
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class AddComponentResponse(DashboardModel):
    """Response after adding a component."""

    yaml: str


@dataclass
class PagedComponentsResponse(PagedResponse):
    """Paginated component catalog API response.

    Entries are the slim :class:`ComponentCatalogIndexEntry` shape —
    the per-field ``config_entries`` tree is omitted from list /
    search responses and fetched per-component via
    ``components/get_component_bodies`` when the user opens a card.
    """

    components: list[ComponentCatalogIndexEntry] = field(default_factory=list)
    categories: list[dict[str, str | int]] = field(default_factory=list)
