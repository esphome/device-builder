"""Device-related data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro.mixins.orjson import DataClassORJSONMixin


class DeviceState(StrEnum):
    """Device connectivity state."""

    UNKNOWN = "unknown"
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass
class Device(DataClassORJSONMixin):
    """A configured ESPHome device."""

    name: str
    friendly_name: str
    configuration: str  # filename (e.g. "my_device.yaml")
    comment: str | None = None
    board_id: str = ""
    target_platform: str = ""
    address: str = ""  # mDNS hostname from StorageJSON (e.g. "my_device.local")
    # Last-known resolved IP. Populated by mDNS resolution and DNS
    # pre-resolve in the ping sweep, persisted through the device-builder
    # metadata sidecar so the OTA address cache survives a restart.
    ip: str = ""
    web_port: int | None = None
    current_version: str = ""
    deployed_version: str = ""
    loaded_integrations: list[str] = field(default_factory=list)  # from StorageJSON after compile
    state: DeviceState = DeviceState.UNKNOWN
    has_pending_changes: bool = True  # True until successfully compiled + deployed
    update_available: bool = False  # True if compiled with older ESPHome version
    uses_mqtt: bool = False  # True if the YAML declares a top-level mqtt: block


@dataclass
class AdoptableDevice(DataClassORJSONMixin):
    """A discoverable device available for import/adoption."""

    name: str
    friendly_name: str
    package_import_url: str
    project_name: str
    project_version: str
    network: str
    ignored: bool


@dataclass
class DevicesResponse(DataClassORJSONMixin):
    """Response for devices/list command."""

    configured: list[Device]
    importable: list[AdoptableDevice]


@dataclass
class WizardResponse(DataClassORJSONMixin):
    """Response after creating a new device."""

    configuration: str


@dataclass
class UpdateDeviceResponse(DataClassORJSONMixin):
    """Response after updating device metadata."""

    name: str
    friendly_name: str
    comment: str | None
    board_id: str | None
