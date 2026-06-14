"""Config controller package — settings, metadata, preferences, labels, chip detection.

This package was split out of the former ``controllers/config.py`` module. The
public surface (and the private symbols the test suite reaches into) are
re-exported here so existing ``from ...controllers.config import X`` callers keep
working unchanged.
"""

from __future__ import annotations

from .chip_detect import (
    _APP_DESC_MAGIC,
    _APP_DESC_OFFSET,
    _APP_DESC_SIZE,
    _CHIP_DETECT_TIMEOUT,
    _DETECT_BUSY,
    _DETECT_NO_ESPTOOL,
    _DETECT_NO_RESPONSE,
    _DETECT_PERMISSION,
    _DETECT_TIMEOUT,
    _DETECT_UNKNOWN,
    _DETECT_UNKNOWN_CHIP,
    _PROJECT_NAME_OFFSET,
    _PROJECT_NAME_SIZE,
    _chip_family_to_descriptor,
    _classify_esptool_failure,
    _detect_chip_via_esptool,
    _detect_failure_message,
    _is_valid_port_name,
    _make_descriptor_tempfile,
    _parse_chip_family_line,
    _parse_project_name,
    _read_app_descriptor_board_id,
    _read_descriptor_file,
    _run_esptool,
    _unlink_quietly,
)
from .controller import ConfigController
from .label_store import (
    _decode_labels,
    delete_label_cascade,
    labels_transaction,
    load_labels,
    save_labels,
    set_device_labels,
)
from .metadata import (
    _load_metadata,
    _save_metadata,
    clear_volatile_device_metadata,
    get_board_id,
    get_device_ip,
    get_device_metadata,
    metadata_transaction,
    remove_device_metadata,
    rename_device_metadata,
    set_device_metadata,
)
from .remote_build_settings import (
    _settings_from_raw,
    has_remote_build_settings_persisted,
    load_remote_build_settings,
    remote_build_settings_transaction,
    save_remote_build_settings,
)
from .settings import _DASHBOARD_SENTINEL_FILE, DashboardSettings

__all__ = [
    "_APP_DESC_MAGIC",
    "_APP_DESC_OFFSET",
    "_APP_DESC_SIZE",
    "_CHIP_DETECT_TIMEOUT",
    "_DASHBOARD_SENTINEL_FILE",
    "_DETECT_BUSY",
    "_DETECT_NO_ESPTOOL",
    "_DETECT_NO_RESPONSE",
    "_DETECT_PERMISSION",
    "_DETECT_TIMEOUT",
    "_DETECT_UNKNOWN",
    "_DETECT_UNKNOWN_CHIP",
    "_PROJECT_NAME_OFFSET",
    "_PROJECT_NAME_SIZE",
    "ConfigController",
    "DashboardSettings",
    "_chip_family_to_descriptor",
    "_classify_esptool_failure",
    "_decode_labels",
    "_detect_chip_via_esptool",
    "_detect_failure_message",
    "_is_valid_port_name",
    "_load_metadata",
    "_make_descriptor_tempfile",
    "_parse_chip_family_line",
    "_parse_project_name",
    "_read_app_descriptor_board_id",
    "_read_descriptor_file",
    "_run_esptool",
    "_save_metadata",
    "_settings_from_raw",
    "_unlink_quietly",
    "clear_volatile_device_metadata",
    "delete_label_cascade",
    "get_board_id",
    "get_device_ip",
    "get_device_metadata",
    "has_remote_build_settings_persisted",
    "labels_transaction",
    "load_labels",
    "load_remote_build_settings",
    "metadata_transaction",
    "remote_build_settings_transaction",
    "remove_device_metadata",
    "rename_device_metadata",
    "save_labels",
    "save_remote_build_settings",
    "set_device_labels",
    "set_device_metadata",
]
