"""Device-related data models."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Literal, TypedDict

from .common import DashboardModel


class DeviceState(StrEnum):
    """Device connectivity state."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


class ReachabilitySource(StrEnum):
    """Channel a device's online state was last observed on.

    The state monitor's source priority — ``mdns`` > ``mqtt`` >
    ``ping`` > ``unknown`` — is implemented in the explicit
    ``_SOURCE_PRIORITY`` mapping in
    ``controllers/_device_state_monitor.py``; the enum just names
    the string values that map flows through. The drawer surfaces
    the current ``active_source`` next to the active Reachability
    row so the user can see which channel is driving the
    indicator. ``StrEnum`` so the value crosses the WS boundary as
    a plain string without an extra serialization layer.
    """

    UNKNOWN = "unknown"
    PING = "ping"
    MQTT = "mqtt"
    MDNS = "mdns"


@dataclass
class DeviceRuntimeState(DashboardModel):
    """
    Monitor-observed device state, grouped so a rebuild carries it whole.

    Populated by the mDNS / MQTT / ping monitors after startup; the
    fields the metadata sidecar persists (``deployed_version``,
    ``deployed_config_hash``, ``queued_update``,
    ``api_encryption_active``) are also seeded from disk on cold load,
    while ``state`` / ``active_source`` / ``ip_addresses`` start empty
    and repopulate on the next announce. A Device rebuild carries the
    whole object from the previous in-memory instance so a re-scan
    doesn't wipe what the monitors have discovered since.
    """

    state: DeviceState = DeviceState.UNKNOWN
    # Reachability channel currently driving online state
    # (``mdns`` > ``mqtt`` > ``ping``); UNKNOWN until a source claims
    # the device.
    active_source: ReachabilitySource = ReachabilitySource.UNKNOWN
    # Every IP currently known for the device. mDNS populates from
    # zeroconf's ``parsed_scoped_addresses`` (in practice IPv4 first,
    # then any scoped IPv6 — link-local addresses keep the ``%scope``
    # suffix); single-IP sources (MQTT discovery, DNS fallback) carry
    # just the one address they know. ``Device.ip`` always holds the
    # primary picked for OTA cache args.
    ip_addresses: list[str] = field(default_factory=list)
    deployed_version: str = ""
    # 8-char hex hash of the running firmware, read from the mDNS
    # ``config_hash`` TXT record (esphome/esphome#16145). When this
    # and ``Device.expected_config_hash`` are both known they drive
    # ``has_pending_changes`` instead of the mtime fallback — that's
    # how we tell "flashed with the latest compile" apart from
    # "compile succeeded but device still runs older firmware".
    deployed_config_hash: str = ""
    # True once a local offline compile finished successfully and is
    # waiting to be flashed via OTA upon the next mDNS check-in.
    queued_update: bool = False
    # Tri-state cipher observed on the wire: truthy string = encryption
    # confirmed, ``""`` = TXT seen with the key absent (confirmed
    # plaintext), ``None`` = no broadcast yet.
    api_encryption_active: str | None = None


# Canonical name set for routing flat attr names onto ``runtime_state``.
RUNTIME_STATE_FIELD_NAMES = frozenset(f.name for f in fields(DeviceRuntimeState))


@dataclass
class Device(DashboardModel):
    """A configured ESPHome device."""

    name: str
    friendly_name: str
    configuration: str  # filename (e.g. "my_device.yaml")
    comment: str | None = None
    # Optional ``esphome.area`` from the YAML — a free-form room /
    # location label (Home Assistant uses the same key as a device-area
    # hint). Empty string when the YAML doesn't carry an ``area:`` line.
    # Surfaced in the dashboard's drawer and as an opt-in table column.
    area: str = ""
    board_id: str = ""
    target_platform: str = ""
    address: str = ""  # mDNS hostname from StorageJSON (e.g. "my_device.local")
    # Last-known resolved IP — primary IPv4 when available, else the
    # first scoped IPv6. Populated by mDNS resolution and DNS
    # pre-resolve in the ping sweep, persisted through the device-builder
    # metadata sidecar so the OTA address cache survives a restart.
    ip: str = ""
    web_port: int | None = None
    current_version: str = ""
    # 8-char hex hash of the YAML as last successfully compiled.
    # Persisted in the metadata sidecar; matches what ESPHome's
    # runtime publishes via ``App.get_config_hash()``.
    expected_config_hash: str = ""
    loaded_integrations: list[str] = field(default_factory=list)  # from StorageJSON after compile
    # Subset of ``loaded_integrations`` the user directly wrote in
    # YAML — top-level keys (``api:``, ``wifi:``, ``sensor:``) plus
    # the platform stems from ``- platform: <name>`` references
    # (``gpio`` under ``binary_sensor``, ``homeassistant`` /
    # ``sntp`` under ``time``, ``esphome`` under ``ota``). Anything
    # in ``loaded_integrations`` but NOT here is auto-loaded as a
    # dependency (``md5`` from WPA2 password hashing, ``mdns``
    # from ``api``, ``web_server_base`` from ``web_server``,
    # ``voltage_sampler`` from ADC sensors, etc.). Computed from
    # the resolved YAML at storage-load time so packages /
    # ``!include`` contents count as direct (the user imported
    # them; auto-loaded dependencies of those imports are still
    # indirect). Empty list when the YAML couldn't be resolved
    # (mid-edit drafts) — frontend falls back to rendering the
    # whole ``loaded_integrations`` list flat.
    directly_referenced_integrations: list[str] = field(default_factory=list)
    # Monitor-observed state; carried whole through rebuilds.
    runtime_state: DeviceRuntimeState = field(default_factory=DeviceRuntimeState)
    has_pending_changes: bool = True  # True until successfully compiled + deployed
    # True when ``has_pending_changes`` came from the mDNS-sourced config-hash
    # compare (vs the local mtime fallback).
    pending_changes_via_hash: bool = False
    update_available: bool = False  # True if compiled with older ESPHome version
    uses_mqtt: bool = False  # True if the YAML declares a top-level mqtt: block
    # Native API surface flags — drive the lock-icon indicator in
    # the device list. Both fields are computed in
    # ``helpers.device_yaml.load_device_from_storage`` as the
    # union of multiple signals; ``True`` if any of them fires.
    # The union shape is what makes the indicator stable across
    # mid-edit drafts, packages-only ``api:`` blocks, and
    # configurations whose YAML resolution diverges from the
    # actual compiled firmware.
    #
    # ``api_enabled`` — the device exposes a Native API at all:
    #   1. Resolved YAML has a top-level ``api:`` block (handles
    #      local ``!include`` / package contents).
    #   2. Raw-text scan has an ``^api:`` line (keeps the flag
    #      stable mid-edit when ``yaml_util.load_yaml`` fails on
    #      an invalid draft).
    #   3. ``StorageJSON.loaded_integrations`` from the last
    #      successful compile lists ``api`` (catches remote
    #      ``dashboard_import`` packages whose YAML resolution
    #      requires a ``git clone`` the dashboard doesn't run).
    #
    # ``api_encrypted`` — the device's Native API runs Noise
    # encryption:
    #   1. Resolved YAML has an ``api: encryption:`` block.
    #   2. Raw-text scan matches the same shape (the ``api:`` /
    #      ``encryption:`` indented pair).
    #   3. Live mDNS broadcast is a truthy cipher string
    #      (``api_encryption_active``). Authoritative when the
    #      YAML pass diverges from the compiled firmware — e.g.
    #      ESPHome's Jinja-templated packages
    #      (``api: |\n  # set ... ${ns.cfg}``), which the
    #      dashboard's ``yaml_util.load_yaml`` doesn't run but
    #      ESPHome's compile pipeline does (issue #437).
    #
    # Symmetric "wire confirms plaintext" (empty-string mDNS
    # broadcast) deliberately doesn't *clear* ``api_encrypted`` —
    # the four-state lock indicator already encodes
    # YAML-yes / wire-no as ``"mismatch"`` / ``"pending"``, not
    # as a flatten-to-False signal.
    #
    # The actual key is fetched on demand via
    # ``devices/get_api_key``.
    api_enabled: bool = False
    api_encrypted: bool = False
    # Encryption status as observed from the device's
    # ``_esphomelib._tcp.local.`` mDNS broadcast.
    #   None  → mDNS not seen yet. The frontend trusts ``api_encrypted``
    #           verbatim (assume the YAML matches what's on the device).
    #   ""    → mDNS seen, ``api_encryption`` TXT absent. The device is
    #           running plaintext regardless of what the YAML says.
    #   "..." → mDNS seen, ``api_encryption`` TXT present (e.g.
    #           ``Noise_NNpsk0_25519_ChaChaPoly_SHA256``). Encryption is
    #           confirmed live on the device.
    # Drives the four-state lock indicator on the device card / table:
    # active, pending-flash, mismatch, plaintext.
    # Canonical ``XX:XX:XX:XX:XX:XX`` MAC observed in the device's
    # ``_esphomelib._tcp.local.`` ``mac`` TXT record (e.g.
    # ``"94:C9:60:1F:8C:F1"``). Empty string when mDNS hasn't
    # surfaced one yet — the broadcast is reliable for ESPHome
    # firmware so a blank typically means "device hasn't been
    # seen this session". The wire form ESPHome currently
    # broadcasts is lowercase 12-hex-char with no separators; we
    # normalize at ingest (``_normalize_mac``) so the in-memory
    # model, sidecar, and frontend wire all carry one canonical
    # form regardless of what the firmware happens to send. On
    # ESP32 this is the Wi-Fi STA MAC (which equals the eFuse
    # base MAC for the 4-universally-administered default); on
    # RP2040 / RP2350 there's only one MAC across interfaces and
    # that's it.
    mac_address: str = ""
    # Derived ethernet MAC for devices whose YAML loads the
    # ``ethernet`` integration. Empty string when the device has no
    # ethernet integration or no primary MAC has been observed yet.
    # On ESP32 this is the base MAC + 3 to the last octet, per
    # Espressif's MAC allocation table; on RP2040 / RP2350 it
    # equals ``mac_address`` (single-MAC platforms). The drawer
    # renders this row only when present and distinct from
    # ``mac_address``.
    ethernet_mac: str = ""
    # Derived Bluetooth MAC for devices whose YAML loads any
    # ``esp32_ble*`` / ``bluetooth_*`` integration. Empty string
    # when no bluetooth integration is loaded or no primary MAC
    # has been observed yet. ESP32 only — RP2040 bluetooth
    # support routes through a separate radio chip with its own
    # allocation scheme, so we don't derive there. Per
    # Espressif's table this is base + 2 to the last octet.
    bluetooth_mac: str = ""
    # Total bytes under the per-device ``.esphome/build/<name>/``
    # tree at last walk. ``0`` when the device hasn't been compiled
    # yet (no StorageJSON / no build artifacts on disk) or when the
    # cached value hasn't been populated since startup. The walk is
    # gated on a freshness pair (the build dir's top-level mtime
    # *and* ``build_info.json``'s mtime) — either side moving
    # counts as stale. Both halves are persisted alongside the
    # cached total in the metadata sidecar so a backend restart
    # picks up the value without an N-device cold-start walk; only
    # devices whose pair drifted from what was persisted get
    # re-walked. See ``helpers/build_size.py`` for the empirical
    # matrix that drove the pair-vs-single-stat decision.
    build_size_bytes: int = 0
    # User-assigned label IDs (opaque ``uuid.uuid4().hex`` strings
    # from the global catalog at ``.device-builder.json``'s
    # ``_labels`` key). Frontend joins against the catalog from
    # ``labels/list`` to render colored chips. The list itself is
    # the assignment record; the canonical name and color live on
    # the catalog entry, so a label rename / recolor needs no
    # device-level write.
    labels: list[str] = field(default_factory=list)
    # Resolved ``logger: baud_rate`` for the Web Serial log port. ``None`` ⇒
    # unset (frontend uses 115200); ``0`` ⇒ UART logging disabled; positive ⇒
    # that baud.
    logger_baud_rate: int | None = None
    # esp32 whose ``ota: platform: esphome`` sets ``allow_partition_access``
    # — gates the install dialog's OTA bootloader-update action. Whether the
    # *running* firmware has it compiled in is the frontend's half of the
    # gate (deployed hash == expected hash).
    ota_partition_access: bool = False


@dataclass
class AdoptableDevice(DashboardModel):
    """A discoverable device available for import/adoption."""

    name: str
    friendly_name: str
    package_import_url: str
    project_name: str
    project_version: str
    network: str
    ignored: bool
    # Pre-built URL to the device's web UI when it advertises a
    # ``_http._tcp.local.`` mDNS service. Empty string when no web
    # server was found — the discovered card then hides the
    # Visit-web-UI affordance.
    web_url: str = ""


@dataclass
class DevicesResponse(DashboardModel):
    """Response for devices/list command."""

    configured: list[Device]
    importable: list[AdoptableDevice]


@dataclass
class WizardResponse(DashboardModel):
    """Response after creating a new device."""

    configuration: str


@dataclass
class ImportBundleResponse(DashboardModel):
    """
    Result of a ``devices/import_bundle`` call.

    ``status="conflicts"`` means nothing was written: *conflicts*
    lists the bundle files that already exist on disk so the user
    can pick which to overwrite and re-submit. ``status="imported"``
    means the tree landed; *configuration* is the device's YAML, and
    *written* / *kept* report which files were placed vs left untouched
    (a non-empty *kept* means a partial import). ``secrets.yaml`` is
    always merged, never a conflict.
    """

    status: Literal["imported", "conflicts"]
    configuration: str
    conflicts: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    has_secrets: bool = False
    esphome_version: str = ""


@dataclass
class UpdateDeviceResponse(DashboardModel):
    """Response after updating device metadata."""

    name: str
    friendly_name: str
    comment: str | None
    board_id: str | None


# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is
# type-checked at the call site without changing the wire shape).
# See ``docs/ARCHITECTURE.md`` "Event bus → Typing event payloads"
# for the subscriber-side narrowing pattern.
# ---------------------------------------------------------------------------


class DeviceEventData(TypedDict):
    """
    Payload for ``EventType.DEVICE_ADDED`` / ``DEVICE_REMOVED`` / ``DEVICE_UPDATED``.

    The three CRUD events share a single shape — the disk
    scanner forwards ``ScanChange`` events through this payload
    and subscribers differentiate by the ``EventType`` carried
    alongside, not by inspecting the payload. The full
    ``Device`` rides through so the frontend's device-table
    renderer has every field it needs without an additional
    fetch.
    """

    device: Device


class DeviceStateChangedData(TypedDict):
    """
    Payload for ``EventType.DEVICE_STATE_CHANGED``.

    Flat ``{configuration, state}`` shape — deliberately *not* the
    full ``Device`` object. The frontend's
    ``DeviceStateChangedEventData`` destructures these two fields
    by name; sending the full device object made both resolve to
    ``undefined`` and the table never updated when ping (or any
    other source) flipped a device online. ``state`` ships as the
    serialised ``DeviceState.value`` string for the same reason —
    the JSON encoder for ``DeviceState`` produces the same form
    but firing the enum object directly leaks an enum into the
    listener's ``data["state"]``.
    """

    configuration: str
    state: str


class DeviceReachabilityData(TypedDict):
    """
    Payload for ``EventType.DEVICE_REACHABILITY``.

    Per-device freshness snapshot fired every time a reachability
    signal (mDNS announce / ICMP success / MQTT discover) lands
    for a configured device. Wire shape mirrored on the frontend
    by ``DeviceReachabilityEventData``; the device drawer's per-
    device subscription keys on ``device`` and pushes the
    snapshot to the client. Optional fields are ``None`` when the
    corresponding signal hasn't been observed yet — the drawer
    hides the row entirely in that case.
    """

    device: str
    state: str
    active_source: str
    ip: str
    mdns_last_seen_seconds_ago: float | None
    mdns_ttl_remaining_seconds: float | None
    mdns_ptr_ttl_seconds: float | None
    mdns_txt_records: dict[str, str] | None
    ping_last_seen_seconds_ago: float | None
    mqtt_last_seen_seconds_ago: float | None
    ping_rtt_ms: float | None


class ImportableDeviceAddedData(TypedDict):
    """
    Payload for ``EventType.IMPORTABLE_DEVICE_ADDED``.

    Carries the full ``AdoptableDevice`` so the frontend's
    discoverable-device list has every field it needs (mDNS host,
    port, version) without an additional fetch. Distinct from
    ``IMPORTABLE_DEVICE_REMOVED`` (which only ships the name)
    because the ADD event has to land a complete row whereas
    REMOVE only needs the id key to delete the row.
    """

    device: AdoptableDevice


class ImportableDeviceRemovedData(TypedDict):
    """
    Payload for ``EventType.IMPORTABLE_DEVICE_REMOVED``.

    Carries only the removed device's mDNS name — the catalog row
    is already gone, and clients track importable devices by name
    (not by full payload) so the id key is all that's needed to
    drop the row.
    """

    name: str
