"""Common/shared data models.

Hosts shared types referenced from multiple domains (boards, components,
devices) — ConfigEntry, EventType, hardware pin enums, paged-response
base, etc. Anything in this module must remain free of imports from
sibling models to keep the dependency graph acyclic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mashumaro.config import BaseConfig
from mashumaro.mixins.orjson import DataClassORJSONMixin

# ---------------------------------------------------------------------------
# Catalog-shared mashumaro Config
# ---------------------------------------------------------------------------


class _CatalogConfig(BaseConfig):
    """Omit fields whose runtime value equals the declared default or ``None``."""

    omit_default = True
    omit_none = True


# ---------------------------------------------------------------------------
# Paged response base
# ---------------------------------------------------------------------------


@dataclass
class PagedResponse(DataClassORJSONMixin):
    """Base for paginated API responses."""

    total: int = 0
    offset: int = 0
    limit: int = 50


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """Events pushed to connected clients via subscribe_events."""

    # Device config file changes (disk scanner)
    DEVICE_ADDED = "device_added"
    DEVICE_REMOVED = "device_removed"
    DEVICE_UPDATED = "device_updated"

    # Device online/offline state change
    DEVICE_STATE_CHANGED = "device_state_changed"

    # Per-device reachability detail; excluded from the broadcast
    # subscribe_events, delivered via devices/subscribe_reachability.
    DEVICE_REACHABILITY = "device_reachability"

    # Discoverable device changes
    IMPORTABLE_DEVICE_ADDED = "importable_device_added"
    IMPORTABLE_DEVICE_REMOVED = "importable_device_removed"

    # Label catalog mutations (assignment changes ride DEVICE_UPDATED)
    LABEL_CREATED = "label_created"
    LABEL_UPDATED = "label_updated"
    LABEL_DELETED = "label_deleted"

    # Firmware job lifecycle
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_OUTPUT = "job_output"
    JOB_PROGRESS = "job_progress"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"

    # Receiver rotated its X25519 peer-link identity
    REMOTE_BUILD_IDENTITY_ROTATED = "remote_build_identity_rotated"
    # pair_request landed for an unknown peer while pairing window open
    REMOTE_BUILD_PAIR_REQUEST_RECEIVED = "remote_build_pair_request_received"
    # Receiver-side peer status change (approved/removed)
    REMOTE_BUILD_PAIR_STATUS_CHANGED = "remote_build_pair_status_changed"
    # Offloader-side counterpart to REMOTE_BUILD_PAIR_STATUS_CHANGED
    OFFLOADER_PAIR_STATUS_CHANGED = "offloader_pair_status_changed"
    # Offloader pairing row created (request_pair); STATUS_CHANGED only flips
    OFFLOADER_PAIRING_ADDED = "offloader_pairing_added"
    # Offloader-side mDNS auto-rebind to a moved receiver endpoint
    OFFLOADER_PAIR_ENDPOINT_REBOUND = "offloader_pair_endpoint_rebound"
    # Pairing window opened/extended/closed
    REMOTE_BUILD_PAIRING_WINDOW_CHANGED = "remote_build_pairing_window_changed"
    # mDNS-discovered peer dashboard appeared/refreshed
    REMOTE_BUILD_HOST_ADDED = "remote_build_host_added"
    # mDNS-discovered peer dashboard left the LAN
    REMOTE_BUILD_HOST_REMOVED = "remote_build_host_removed"
    # Offloader peer-link Noise WS session opened
    OFFLOADER_PEER_LINK_OPENED = "offloader_peer_link_opened"
    # Offloader peer-link session closed (payload carries reason)
    OFFLOADER_PEER_LINK_CLOSED = "offloader_peer_link_closed"
    # Receiver-side peer-link session opened
    RECEIVER_PEER_LINK_SESSION_OPENED = "receiver_peer_link_session_opened"
    # Receiver-side peer-link session closed
    RECEIVER_PEER_LINK_SESSION_CLOSED = "receiver_peer_link_session_closed"
    # Offloader detected receiver pin drift (identity rotated under us)
    OFFLOADER_PAIR_PIN_MISMATCH = "offloader_pair_pin_mismatch"
    # Offloader detected the receiver rejected/revoked the pairing
    OFFLOADER_PAIR_PEER_REVOKED = "offloader_pair_peer_revoked"
    # Offloader pair alert cleared (re-pair or unpair)
    OFFLOADER_PAIR_ALERT_DISMISSED = "offloader_pair_alert_dismissed"
    # Receiver pushed a queue_status snapshot over peer-link
    OFFLOADER_QUEUE_STATUS_CHANGED = "offloader_queue_status_changed"
    # Receiver pushed a job_state_changed frame for a remote job
    OFFLOADER_JOB_STATE_CHANGED = "offloader_job_state_changed"
    # Receiver pushed a job_output frame for a remote job
    OFFLOADER_JOB_OUTPUT = "offloader_job_output"
    # Offloader master "remote builds enabled" toggle changed
    OFFLOADER_REMOTE_BUILDS_TOGGLED = "offloader_remote_builds_toggled"
    # Offloader per-pairing enable toggle changed
    OFFLOADER_PAIRING_ENABLED_CHANGED = "offloader_pairing_enabled_changed"
    # Cross-tab sync for the master version-match policy
    OFFLOADER_VERSION_MATCH_POLICY_CHANGED = "offloader_version_match_policy_changed"


class StreamEvent(StrEnum):
    """Per-stream frame names sent via ``WebSocketClient.send_event``.

    Distinct from :class:`EventType` (the global event-bus channel
    name): a ``StreamEvent`` is the ``event`` field of a single
    streaming command's response frames. Some wire values coincide
    with ``EventType`` (``"job_output"``); those call sites pass the
    ``EventType`` member directly rather than redeclaring it here.
    """

    # Per-line subprocess output (follow_job / stream_logs / validate_config)
    OUTPUT = "output"
    # Terminal frame — final status / exit code; sent priority
    RESULT = "result"
    # Initial replay of buffered state at stream start
    SNAPSHOT = "snapshot"


# ---------------------------------------------------------------------------
# Hardware enums (shared between board metadata and config-entry constraints)
# ---------------------------------------------------------------------------


class PinFeature(StrEnum):
    """Known GPIO pin features/capabilities.

    Used in two places:
    1. Board manifests describe which features each physical pin exposes.
    2. ConfigEntry of type PIN declares which features it requires;
       the frontend filters board pins to those that match.
    """

    ADC = "adc"
    DAC = "dac"
    TOUCH = "touch"
    PWM = "pwm"
    I2C_SDA = "i2c_sda"
    I2C_SCL = "i2c_scl"
    SPI_MOSI = "spi_mosi"
    SPI_MISO = "spi_miso"
    SPI_CLK = "spi_clk"
    SPI_CS = "spi_cs"
    UART_TX = "uart_tx"
    UART_RX = "uart_rx"
    USB_DP = "usb_dp"
    USB_DM = "usb_dm"
    RGB_LED = "rgb_led"
    JTAG = "jtag"
    STRAPPING = "strapping"
    INPUT_ONLY = "input_only"
    BOOT_BUTTON = "boot_button"


class PinMode(StrEnum):
    """Direction a GPIO pin will be used in.

    Used by ConfigEntry of type PIN to constrain pin selection.
    """

    INPUT = "input"
    OUTPUT = "output"
    INPUT_OUTPUT = "input_output"


# ---------------------------------------------------------------------------
# Config entries
# ---------------------------------------------------------------------------


class ConfigEntryType(StrEnum):
    """Primitive value type of a config entry.

    Drives the base UI control. Two flags layer additional behaviour on
    top without needing extra enum values:

    - `options` populated → render a dropdown of the listed values; the
      value type still reflects what those values are (usually STRING).
    - `multi_value=True` → render an add/remove list of inputs of the
      base type (e.g. STRING + multi_value = list of strings).
    """

    # Single-line text input
    STRING = "string"
    # Single-line text input that masks the value (passwords, API keys)
    SECURE_STRING = "secure_string"
    # Whole-number spinner / numeric input
    INTEGER = "integer"
    # Decimal-number spinner / numeric input
    FLOAT = "float"
    # Toggle / checkbox
    BOOLEAN = "boolean"
    # GPIO pin picker — see `pin_features` and `pin_mode` to filter choices
    PIN = "pin"
    # Duration like "30s", "5min" — frontend renders a value+unit input
    TIME_PERIOD = "time_period"
    # Numeric value carrying a unit: frequency ("50kHz"), data size
    # ("500KB"), framerate ("10 fps"), voltage ("3.3V"), distance
    # ("2m"), temperature ("4°C"), etc. ESPHome's coercer multiplies
    # by the unit at compile time, but the YAML shape the user types
    # is a string — so the frontend renders a number input plus a
    # unit picker, round-trips the value as ``"<value><unit>"``, and
    # validates the numeric portion against ``range``. Unit choices
    # come from ``unit_options`` on the entry. ``TIME_PERIOD`` is
    # kept separate because its grammar (``1h30s``) and unit set are
    # richer; this type is for the simpler single-unit measurements.
    FLOAT_WITH_UNIT = "float_with_unit"
    # Material Design icon picker (mdi:foo)
    ICON = "icon"
    # Component ID reference — links to another component instance
    ID = "id"
    # Automation trigger reference (rare, advanced)
    TRIGGER = "trigger"
    # Color picker — accepts hex (#RRGGBB) or named color
    COLOR = "color"
    # MAC address input (xx:xx:xx:xx:xx:xx)
    MAC_ADDRESS = "mac_address"
    # Multi-line code editor for raw `!lambda |- C++` blocks
    LAMBDA = "lambda"
    # Multi-line JSON editor (HTTP request bodies, custom payloads)
    JSON = "json"
    # Structured value: the entry's value is itself a YAML mapping
    # whose own fields are described by ``config_entries``. Frontend
    # renders the field as a collapsible group containing the nested
    # form. Used for nested config blocks (e.g.
    # ``esp32_ble_tracker.scan_parameters``) and entity sub-readings
    # (e.g. ``dht.temperature`` and ``dht.humidity``).
    NESTED = "nested"
    # User-keyed mapping: the value is a YAML dict whose keys are
    # supplied by the user (component names, substitution names, ...)
    # and whose values all follow the same template schema. The single
    # entry inside ``config_entries`` describes that value template.
    # Frontend renders this as a dynamic list of (key, value) rows
    # with an "Add entry" button. Used for ``logger.logs`` (per-
    # component log levels), ``substitutions:``, ``globals:`` etc.
    MAP = "map"

    # Polymorphic list of single-key items drawn from a named
    # registry. Each item is ``{<registry_id>: <params> | null}``.
    # The frontend's REGISTRY_LIST renderer fetches the catalog
    # named by ``ConfigEntry.registry`` (``"light_effects"`` and
    # ``"filter"`` are populated; per-row parameter editing is V2)
    # and renders one row per item with a per-row type picker.
    # Used by light ``effects:`` and sensor / binary_sensor /
    # text_sensor ``filters:`` (#941).
    REGISTRY_LIST = "registry_list"

    # Layout / decoration entries (no value, used to structure the form)
    LABEL = "label"
    DIVIDER = "divider"
    ALERT = "alert"

    # Fallback for fields whose type couldn't be determined during sync
    UNKNOWN = "unknown"


# Primitive values that can appear as defaults, current values, and
# constants in the visibility predicate. Excludes containers.
ConfigPrimitive = str | int | float | bool


@dataclass
class ConfigValueOption(DataClassORJSONMixin):
    """A single choice for a SELECT-type config entry."""

    label: str
    value: str


class RequiredGroupKind(StrEnum):
    """
    Cross-field cardinality constraint over a group of sibling keys.

    Mirrors the four ``cv.has_*_one_key`` validators upstream
    esphome exposes: a schema decorated with one of these must
    satisfy the cardinality rule across the named keys at
    validation time. The wire form is the StrEnum value; the
    frontend renders an inline hint and validates client-side.
    """

    # Exactly one of the listed keys must be present (e.g.
    # ``esp32_rmt_led_strip`` requires either ``chipset`` *or* the
    # manual-timing fields, never both).
    EXACTLY_ONE = "exactly_one"
    # At least one of the listed keys must be present (e.g.
    # ``wifi.networks[].eap`` requires either an ``identity`` or a
    # client certificate).
    AT_LEAST_ONE = "at_least_one"
    # At most one of the listed keys may be present.
    AT_MOST_ONE = "at_most_one"
    # Either none or all of the listed keys must be present.
    NONE_OR_ALL = "none_or_all"


@dataclass
class RequiredGroup(DataClassORJSONMixin):
    """
    Cross-field "must specify one of these" constraint.

    Lives on the schema that owns the referenced keys —
    ``ComponentCatalogEntry.required_groups`` for component-level
    constraints, ``ConfigEntry.required_groups`` (when
    ``type=NESTED``) for nested-schema constraints. ``keys`` lists
    the sibling YAML key names the cardinality rule applies to.
    """

    kind: RequiredGroupKind
    keys: list[str] = field(default_factory=list)


@dataclass
class ConfigEntry(DataClassORJSONMixin):
    """A single field in a component's configuration schema.

    Drives both the visual editor (rendering, validation, conditional
    visibility) and YAML serialization. Inspired by the Music Assistant
    ConfigEntry pattern.
    """

    # === core ===

    # YAML key name (e.g. "update_interval", "ssid", "pin"). This is
    # what gets serialized into the user's config file.
    key: str

    # Primitive type drives the UI control: text input, number spinner,
    # select dropdown, pin picker, lambda editor, etc.
    type: ConfigEntryType

    # Short human-readable label shown next to the input. When empty,
    # the frontend should derive one from `key` (e.g. "update_interval"
    # → "Update Interval").
    label: str

    # Longer help text shown as a tooltip or below the input. Often
    # extracted from the component documentation. May contain markdown.
    description: str | None = None

    # When True the YAML is invalid without this field set. Frontend
    # marks the input with a required indicator.
    required: bool = False

    # Default value used when the field is omitted from YAML. For
    # `multi_value` entries this is the default *list* of values.
    default_value: ConfigPrimitive | list[ConfigPrimitive] | None = None

    # Per-target-platform default values for fields that use
    # ``cv.SplitDefault`` (e.g. wifi.power_save_mode is "light" on
    # ESP32 but "none" on ESP8266). Frontend should look up the
    # device's target platform here and fall back to ``default_value``
    # when the platform isn't listed (which means the field has no
    # built-in default for that platform — usually because it isn't
    # commonly used there).
    platform_defaults: dict[str, ConfigPrimitive] | None = None

    # Target chips this field is valid on. Empty list = no
    # restriction (the common case); non-empty = the field is
    # restricted to the listed chips and the frontend's form
    # renderer hides it on incompatible boards. Same wire shape
    # as ``ComponentCatalogEntry.supported_platforms`` (which
    # carries the *whole component*'s restriction) — this one
    # gates a single field within an otherwise platform-portable
    # component, e.g. ``sensor.debug.psram`` which is ESP32-only
    # while the rest of the debug sensors are platform-portable.
    # Recovered from upstream's declarative ``cv.only_on``
    # validators by the sync script's schema introspection.
    supported_platforms: list[str] = field(default_factory=list)

    # === value constraints ===

    # Constrains the value to a fixed set of choices. When populated the
    # frontend renders a dropdown rather than a free-form input — the
    # underlying value type (`type`) is unchanged.
    options: list[ConfigValueOption] | None = None

    # When True, ``options`` are treated as suggestions rather than a
    # closed enum: the frontend should render an autocomplete /
    # combobox that allows typing arbitrary values in addition to
    # picking from the list. Used for fields like
    # ``unit_of_measurement`` where ESPHome ships canonical unit
    # symbols but accepts any string.
    allow_custom_value: bool = False

    # Min/max bounds for INTEGER / FLOAT entries. None = unbounded.
    range: tuple[int | float, int | float] | None = None

    # Display-formatting hint for INTEGER entries. Currently only
    # ``"hex"`` is defined, applied to fields whose upstream
    # validator is one of the ``cv.hex_uint*_t`` family
    # (``i2c_address`` is the canonical case — every i2c-platform
    # component sets ``address`` to ``cv.hex_uint8_t`` because i2c
    # addresses are conventionally written as ``0x76`` / ``0x77``,
    # and decimal display is borderline unreadable). Frontend
    # renders the input as hex (``0x76``) and accepts both
    # ``0x76`` and ``118`` on entry. None = decimal display
    # (the default for plain ``cv.int_range`` integers).
    display_format: str | None = None

    # Catalog name for ``REGISTRY_LIST`` entries. Currently
    # ``"light_effects"`` (light.effects) and ``"filter"``
    # (sensor / binary_sensor / text_sensor filters) are populated;
    # new registries plug into the frontend's REGISTRY_OPS table.
    # Null on every other entry type. #941.
    registry: str | None = None

    # Unit choices for ``FLOAT_WITH_UNIT`` entries. The frontend
    # renders a unit picker populated from this list; each option's
    # string is what the YAML serialization appends after the
    # numeric value (e.g. ``["Hz", "kHz", "MHz", "GHz"]`` for
    # ``cv.frequency``). The first entry is the canonical unit —
    # range bounds and any user-typed bare number default to it.
    # None for non-FLOAT_WITH_UNIT entries.
    unit_options: list[str] | None = None

    # When True the field accepts a list of values rather than a single
    # value (e.g. multiple SSIDs, multiple radar targets). Frontend
    # renders an add/remove list of inputs of the declared `type`.
    multi_value: bool = False

    # When True the field accepts either a literal value of the
    # declared `type` OR a `!lambda |- ...` block returning that type.
    # Most ESPHome fields are templatable.
    templatable: bool = False

    # Sibling entries sharing a non-null value are mutually exclusive —
    # exactly one may be set (a remote_receiver binary_sensor's protocol).
    # The frontend renders them as one pick-one dropdown.
    exclusive_group: str | None = None

    # === featured-component overlays ===
    # Populated only on materialised featured components — the regular
    # catalog never sets these. ``locked=True`` tells the frontend to
    # disable the input (the value comes from a board-side preset and
    # the backend rejects deviating user input on add). ``suggestions``,
    # when non-None, limits the user's choice to this list — most often
    # used on PIN entries for addon modules whose pin can land on one
    # of a few GPIOs.

    locked: bool = False
    suggestions: list[ConfigPrimitive] | None = None

    # === conditional visibility ===
    # `depends_on_value`, `depends_on_value_not` and
    # `depends_on_value_any` are mutually exclusive — set at most one.
    # Frontend hides the entry when the predicate fails. New catalog
    # output emits only `depends_on_value_any`; the scalar forms remain
    # for catalogs generated before it existed.

    # Key of another entry in the same component this entry depends on.
    # When None the entry is always visible.
    depends_on: str | None = None

    # Show this entry only when the dependency's current value equals
    # this. Ignored if `depends_on` is None.
    depends_on_value: ConfigPrimitive | None = None

    # Show this entry only when the dependency's current value does NOT
    # equal this. Ignored if `depends_on` is None.
    depends_on_value_not: ConfigPrimitive | None = None

    # Show this entry only when the dependency's current value is in
    # this list. Ignored if `depends_on` is None.
    depends_on_value_any: list[ConfigPrimitive] | None = None

    # Hide this entry unless the named component is configured on the
    # same device. Used for cross-cutting fields that are only
    # meaningful when a specific transport / gateway is configured —
    # e.g. ``qos`` / ``retain`` are only relevant when the device has
    # an ``mqtt:`` block; ``zigbee_*`` fields require a ``zigbee:``
    # block. None = always visible (the default).
    depends_on_component: str | None = None

    # When ``type`` is ID, identifies the component domain the value
    # must reference. The frontend renders a dropdown of existing
    # components of that domain in the device's YAML — e.g.
    # ``rtttl.output`` references "output", ``integration.sensor``
    # references "sensor", many sensors reference "i2c" / "spi" /
    # "uart" buses. None when the field is a free-form ID.
    references_component: str | None = None

    # === pin selection (only meaningful when type == PIN) ===

    # Pin capabilities required for this field. Frontend filters the
    # board's pin map to entries whose features include all of these.
    pin_features: list[PinFeature] = field(default_factory=list)

    # Direction the pin will be used in. None = no constraint.
    pin_mode: PinMode | None = None

    # === UI / i18n ===

    # When True frontend collapses this entry under an "Advanced" section.
    advanced: bool = False

    # When True frontend hides the entry entirely (used for fields the
    # backend tracks but the user shouldn't edit directly).
    hidden: bool = False

    # Optional URL pointing to documentation specific to this field
    # (often an anchor inside the component's docs page).
    help_link: str | None = None

    # i18n override key. None means the frontend should fall back to
    # `component.{component_id}.config.{key}` at render time.
    translation_key: str | None = None

    # Substitution params for the translation string (e.g.
    # `{"min": 0, "max": 100}` for a range message).
    translation_params: dict[str, Any] | None = None

    # === cross-field constraints ===

    # Inclusive-group name (esphome's ``cv.Inclusive(key, group)``):
    # fields sharing the same ``group`` value are all-or-nothing —
    # the user must set every member of the group, or none. None
    # when the field stands alone. Frontend pairs this with the
    # parent schema's ``required_groups`` to render the full
    # "either X or all of {Y, Z, …}" rule (e.g. the manual-timing
    # fields on ``light.esp32_rmt_led_strip``).
    group: str | None = None

    # === nested entries (only meaningful when type == NESTED) ===

    # Inner config entries when this entry's value is a structured YAML
    # mapping (e.g. ``esp32_ble_tracker.scan_parameters`` →
    # duration / interval / window / active / continuous, or DHT's
    # temperature / humidity readings). Frontend renders the parent
    # field as a collapsible group containing the inner form.
    config_entries: list[ConfigEntry] | None = None

    # Cross-field cardinality constraints over this entry's
    # ``config_entries``. Empty by default; populated when the
    # nested schema is wrapped in ``cv.has_*_one_key(...)``
    # upstream (e.g. ``wifi.networks[].eap`` requires at least one
    # of ``identity`` / ``certificate``). Only meaningful when
    # ``type == NESTED``.
    required_groups: list[RequiredGroup] = field(default_factory=list)

    # Set when the nested entry represents an ESPHome entity (sensor,
    # binary_sensor, ...) rather than a plain config group. The
    # frontend should apply platform-default fields (name,
    # device_class, ...) on top of `config_entries` for these. None
    # means a plain structured group.
    platform_type: str | None = None


# ---------------------------------------------------------------------------
# Featured-component presets (board-side)
# ---------------------------------------------------------------------------


@dataclass
class FieldPreset(DataClassORJSONMixin):
    """
    Pre-filled value for a single config-entry on a featured component.

    Three modes, expressed by which fields are populated:

    - ``value`` only: pre-filled default, user can change it.
    - ``value`` + ``locked=True``: fixed value. Frontend disables the input;
      backend rejects deviating user input on add.
    - ``suggestions``: short list of allowed values (frontend renders a
      picker). ``value`` (if also set) is the initial selection.

    ``locked`` and ``suggestions`` are mutually exclusive. ``value`` can be
    a primitive, list, or dict — the latter for nested config entries.
    """

    # ``dict[str, Any]`` must precede ``list[Any]`` in the union:
    # mashumaro dispatches in declaration order and ``list(some_dict)``
    # would otherwise win for dict inputs (returning the keys).
    value: ConfigPrimitive | dict[str, Any] | list[Any] | None = None
    locked: bool = False
    suggestions: list[ConfigPrimitive] | None = None

    class Config(_CatalogConfig):
        """Omit ``locked=False`` / ``suggestions=None``; see :class:`_CatalogConfig`."""
