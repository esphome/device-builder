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

from mashumaro.mixins.orjson import DataClassORJSONMixin

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

    # Device config file changes (detected by disk scanner)
    DEVICE_ADDED = "device_added"
    DEVICE_REMOVED = "device_removed"
    DEVICE_UPDATED = "device_updated"

    # Device online/offline state changes
    DEVICE_STATE_CHANGED = "device_state_changed"

    # Discoverable device changes
    IMPORTABLE_DEVICE_ADDED = "importable_device_added"
    IMPORTABLE_DEVICE_REMOVED = "importable_device_removed"

    # Firmware job lifecycle
    JOB_QUEUED = "job_queued"
    JOB_STARTED = "job_started"
    JOB_OUTPUT = "job_output"
    JOB_PROGRESS = "job_progress"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"


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

    # When True the field accepts a list of values rather than a single
    # value (e.g. multiple SSIDs, multiple radar targets). Frontend
    # renders an add/remove list of inputs of the declared `type`.
    multi_value: bool = False

    # When True the field accepts either a literal value of the
    # declared `type` OR a `!lambda |- ...` block returning that type.
    # Most ESPHome fields are templatable.
    templatable: bool = False

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
    # `depends_on_value` and `depends_on_value_not` are mutually
    # exclusive — set at most one. Frontend hides the entry when the
    # predicate fails.

    # Key of another entry in the same component this entry depends on.
    # When None the entry is always visible.
    depends_on: str | None = None

    # Show this entry only when the dependency's current value equals
    # this. Ignored if `depends_on` is None.
    depends_on_value: ConfigPrimitive | None = None

    # Show this entry only when the dependency's current value does NOT
    # equal this. Ignored if `depends_on` is None.
    depends_on_value_not: ConfigPrimitive | None = None

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

    # === nested entries (only meaningful when type == NESTED) ===

    # Inner config entries when this entry's value is a structured YAML
    # mapping (e.g. ``esp32_ble_tracker.scan_parameters`` →
    # duration / interval / window / active / continuous, or DHT's
    # temperature / humidity readings). Frontend renders the parent
    # field as a collapsible group containing the inner form.
    config_entries: list[ConfigEntry] | None = None

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

    value: ConfigPrimitive | list[Any] | dict[str, Any] | None = None
    locked: bool = False
    suggestions: list[ConfigPrimitive] | None = None
