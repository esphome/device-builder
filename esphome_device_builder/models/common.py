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

    # Per-device reachability detail change — fired alongside
    # ``DEVICE_STATE_CHANGED`` (and on its own when only the per-signal
    # last-seen / rtt move) for any subscriber filtering by device.
    # The drawer subscribes to these via the ``devices/subscribe_reachability``
    # WS command; the broadcast ``subscribe_events`` excludes this
    # type explicitly (see ``_cmd_subscribe_events``) so a connected
    # client doesn't receive a freshness event for every device on
    # every mDNS announce.
    DEVICE_REACHABILITY = "device_reachability"

    # Discoverable device changes
    IMPORTABLE_DEVICE_ADDED = "importable_device_added"
    IMPORTABLE_DEVICE_REMOVED = "importable_device_removed"

    # Label catalog mutations. Per-device label assignment changes
    # ride the existing ``DEVICE_UPDATED`` event (fired automatically
    # by the scanner reload after the sidecar write).
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

    # Receiver rotated its TLS keypair via
    # ``remote_build/rotate_identity``. Payload carries
    # ``{dashboard_id, pin_sha256}``: subscribers (the offloader-
    # side peer-link in phase 4+, the receiver Settings UI in
    # 3c2) can refresh their cached pin without polling
    # ``get_identity``. The event fires after the on-disk
    # rotation succeeds; the listener rebuild may still
    # fail-soft, in which case the rotater's ``IdentityView``
    # response carries ``listener_bound=False`` while the event
    # itself reflects only that the cert + key on disk
    # changed.
    REMOTE_BUILD_IDENTITY_ROTATED = "remote_build_identity_rotated"

    # A pair_request Noise frame landed for a previously-unknown
    # peer while the receiver's pairing window was open (phase 4
    # auth flow; see issue #106 design choice (b)/(c)). Payload:
    # ``{dashboard_id, pin_sha256, label, peer_ip}``. The receiver
    # Settings UI surfaces this in the Pairing requests inbox.
    # Fires only when the pairing window is open; closed-window
    # pair_requests are rejected at the listener with
    # ``intent_response=no_pairing_window`` and don't create a
    # row. The listener (part 4) is the actual emitter; this
    # entry is added in part 3 alongside the receiver-UI WS
    # commands so the model surface lands together.
    REMOTE_BUILD_PAIR_REQUEST_RECEIVED = "remote_build_pair_request_received"

    # A peer entry's status changed. Payload:
    # ``{dashboard_id, status: "approved" | "removed"}``. Fires
    # from three paths: (a) ``remote_build/approve_peer``
    # promoting a PENDING in-memory dict entry to APPROVED on
    # disk (``status="approved"``), (b) ``remote_build/remove_peer``
    # dropping either a PENDING dict entry or an APPROVED list
    # row (``status="removed"``), (c) pairing-window-close
    # clearing the in-memory PENDING dict (``status="removed"``
    # per cleared entry). The ``status="removed"`` event is
    # what wakes any in-flight ``intent="pair_status"`` long-poll
    # on a paired offloader so its listener task drops the
    # offloader's local state.
    #
    # Receiver Settings UI updates the inbox + approved-peers
    # list on this event. The offloader-side counterpart event
    # is :attr:`OFFLOADER_PAIR_STATUS_CHANGED`, fired on the
    # offloader's local bus by the offloader's pair-status
    # listener task after observing the receiver's response; the
    # two share a wire shape but live on different buses
    # (receiver vs offloader) and carry different identifiers
    # (offloader's dashboard_id vs the offloader's own
    # ``(receiver_hostname, receiver_port)`` coordinates).
    REMOTE_BUILD_PAIR_STATUS_CHANGED = "remote_build_pair_status_changed"

    # Offloader-side counterpart to ``REMOTE_BUILD_PAIR_STATUS_CHANGED``.
    # Payload: ``{receiver_hostname, receiver_port,
    # status: "approved" | "removed"}``. Fired by the offloader's
    # per-row pair-status listener task
    # (``RemoteBuildController._fire_offloader_pair_status_changed``,
    # called from ``_apply_pair_status_result`` once an
    # ``intent="pair_status"`` round-trip resolves), and also by
    # ``RemoteBuildController.unpair`` when the user removes a
    # row. Delivered to clients via the existing global
    # ``subscribe_events`` stream — no separate subscription
    # channel. Receiver-side keys aren't carried because the
    # offloader's :class:`StoredPairing` never stores the
    # receiver's ``dashboard_id`` — the receiver coordinates the
    # offloader knows are the ``(hostname, port)`` it dialled.
    OFFLOADER_PAIR_STATUS_CHANGED = "offloader_pair_status_changed"

    # Pairing window opened, extended, or closed. Payload:
    # ``{open: bool, expires_in_seconds: float | None}``. Fires
    # on every state transition: window open (open=true,
    # expires_in_seconds=300), activity-driven extension
    # (open=true, expires_in_seconds=300 again, deadline
    # bumped), explicit close from the frontend (open=false,
    # expires_in_seconds=null), or auto-close on deadline reach
    # (open=false). The receiver frontend renders a live
    # countdown from ``expires_in_seconds``; idempotent calls
    # that don't change state (e.g. close-while-already-closed)
    # do NOT fire the event.
    REMOTE_BUILD_PAIRING_WINDOW_CHANGED = "remote_build_pairing_window_changed"

    # An mDNS-discovered peer dashboard appeared (or its TXT /
    # SRV info was refreshed). Payload: a full
    # :class:`RemoteBuildPeer` dict. The receiver-side controller
    # holds the discovered set in RAM (``self._peers``) and fires
    # this event from ``_on_service_state_change`` /
    # ``_resolve_and_apply`` whenever the row is upserted; the
    # frontend's pair-dialog "discovered dashboards" list mutates
    # against this stream rather than re-polling. The
    # ``subscribe_events`` initial-state push carries the full
    # current set under ``hosts`` so a fresh tab paints without a
    # round-trip.
    REMOTE_BUILD_HOST_ADDED = "remote_build_host_added"

    # An mDNS-discovered peer dashboard left the LAN (zeroconf
    # ``Removed`` callback — TTL expiry without renewal, or an
    # explicit goodbye). Payload: ``{name: str}`` matching the
    # service-instance name carried on the corresponding
    # ``REMOTE_BUILD_HOST_ADDED`` event. The frontend drops the
    # row from its discovered set on this event.
    REMOTE_BUILD_HOST_REMOVED = "remote_build_host_removed"


class StreamEvent(StrEnum):
    """Per-stream frame names sent via ``WebSocketClient.send_event``.

    Distinct from :class:`EventType` (the global event-bus channel
    name): a ``StreamEvent`` is the ``event`` field of a single
    streaming command's response frames (``follow_job``,
    ``stream_logs``, ``validate_config``, ``follow_jobs``'s initial
    snapshot). Two-tier model — bus events get fanned out to per-
    connection streams, where the controller may relabel them
    (e.g. ``EventType.JOB_OUTPUT`` becomes ``StreamEvent.OUTPUT``
    inside a ``follow_job`` stream that's already scoped to a
    specific job_id).

    The wire bytes coincide with some ``EventType`` values
    (``"job_output"``, ``"job_progress"``) for the all-jobs
    follower path, where the stream simply forwards the bus event
    name through. Those call sites pass the ``EventType`` member
    directly (it's a ``StrEnum``, so it serialises to the same
    string) rather than redeclaring the constant here.
    """

    # Per-line subprocess output (``follow_job`` / ``stream_logs`` /
    # ``validate_config``).
    OUTPUT = "output"
    # Terminal frame — final status / exit code, ends a streaming
    # command. Sent priority so a backlog of output frames can't
    # drop the close signal.
    RESULT = "result"
    # Initial replay of buffered state at the start of a stream
    # (``follow_jobs`` snapshots the job table; ``follow_job``
    # replays the job's output ring before live tail).
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

    # ``dict[str, Any]`` must precede ``list[Any]`` in the union:
    # mashumaro dispatches in declaration order and ``list(some_dict)``
    # would otherwise win for dict inputs (returning the keys).
    value: ConfigPrimitive | dict[str, Any] | list[Any] | None = None
    locked: bool = False
    suggestions: list[ConfigPrimitive] | None = None
