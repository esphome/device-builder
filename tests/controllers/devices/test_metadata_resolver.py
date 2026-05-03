"""Tests for ``DevicesController._resolve_device_metadata``.

The metadata resolver is what threads ``board_id`` / ``ip`` /
``expected_config_hash`` through to every reload of a device's
in-memory state. The hash side specifically has to read
``build_info.json`` first (firmware-canonical) and only fall back to
the sidecar's persisted value when the build directory is wiped —
otherwise a stale sidecar (e.g. left over from a previous bug) would
keep mis-rendering the drawer's "Local config hash" until the user
re-flashed every device.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import Device, EventType

from .conftest import RecordingStateMonitor, capture_devices_events


def _make_controller(monkeypatch: Any, board_id: str = "esp32-c3-devkitm-1") -> Any:
    """Build a controller with the YAML-parsing path stubbed out.

    The board-id derivation reads StorageJSON / parses YAML — neither
    relevant to the hash-priority tests, and both heavy to set up. A
    single stub keeps the resolver focused on the metadata + hash
    sources we actually want to assert against.
    """
    controller = DevicesController.__new__(DevicesController)
    monkeypatch.setattr(
        controller,
        "_derive_board_id_from_yaml",
        lambda _config_dir, _filename: board_id,
        raising=False,
    )
    return controller


def _stub_get_metadata(monkeypatch: Any, payload: dict[str, Any]) -> None:
    """Make ``get_device_metadata`` return *payload* — no JSON IO."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.get_device_metadata",
        lambda _config_dir, _filename: payload,
    )


def _write_storage_pointer(config_dir: Path, filename: str, build_path: Path) -> None:
    """Write the ESPHome ``StorageJSON`` sidecar pointing at *build_path*.

    ``read_build_info_hash`` resolves the build directory by loading
    ``StorageJSON`` and reading ``storage.build_path`` — these tests
    write a real sidecar (instead of mocking) so the resolver
    exercises the same disk path it does in production.
    """
    storage_dir = config_dir / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / f"{filename}.json").write_text(
        json.dumps(
            {
                "storage_version": 1,
                "name": filename.removesuffix(".yaml"),
                "comment": None,
                "esphome_version": "2026.5.0-dev",
                "src_version": 1,
                "address": "",
                "web_port": None,
                "esp_platform": "esp32",
                "board": "esp32-c3-devkitm-1",
                "build_path": str(build_path),
                "firmware_bin_path": str(build_path / ".pioenvs" / "firmware.bin"),
                "loaded_integrations": [],
                "loaded_platforms": [],
                "no_mdns": False,
                "framework": "esp-idf",
                "core_platform": "esp32",
            }
        ),
        encoding="utf-8",
    )


def _write_build_info(build_path: Path, config_hash: int) -> None:
    """Drop a ``build_info.json`` carrying *config_hash* under *build_path*."""
    build_path.mkdir(parents=True, exist_ok=True)
    (build_path / "build_info.json").write_text(
        json.dumps(
            {
                "config_hash": config_hash,
                "build_time": 1700000000,
                "build_time_str": "2025-11-14 12:00:00 -0500",
                "esphome_version": "2026.5.0-dev",
            }
        ),
        encoding="utf-8",
    )


def test_build_info_hash_wins_over_stale_sidecar(tmp_path: Path, monkeypatch: Any) -> None:
    """``build_info.json`` is authoritative; sidecar's stale value is ignored."""
    config_dir = tmp_path
    filename = "kitchen.yaml"
    (config_dir / filename).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    # Sidecar carries a wrong value left over from the pre-codegen
    # subprocess bug (the user-visible regression on
    # ``acfloatmonitor32.yaml``: ``f3e21d5a``).
    _stub_get_metadata(
        monkeypatch,
        {"board_id": "", "ip": "192.168.1.42", "expected_config_hash": "f3e21d5a"},
    )

    # build_info.json carries the firmware-canonical value.
    build_path = config_dir / ".esphome" / "build" / "kitchen"
    _write_storage_pointer(config_dir, filename, build_path)
    _write_build_info(build_path, config_hash=0x5A94A12D)

    controller = _make_controller(monkeypatch)
    metadata = controller._resolve_device_metadata(config_dir, filename)

    assert metadata.expected_config_hash == "5a94a12d"
    assert metadata.ip == "192.168.1.42"  # untouched


def test_falls_back_to_sidecar_when_build_dir_wiped(tmp_path: Path, monkeypatch: Any) -> None:
    """No build_info.json (e.g. after ``clean``) → use the sidecar's hash."""
    config_dir = tmp_path
    filename = "kitchen.yaml"
    (config_dir / filename).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    _stub_get_metadata(
        monkeypatch,
        {"board_id": "", "ip": "", "expected_config_hash": "abcd1234"},
    )

    # StorageJSON points at a build dir that no longer exists — clean
    # job wipes ``.esphome/build/<name>``, and the sidecar is the
    # only remaining trace of the previous compile's hash.
    build_path = config_dir / ".esphome" / "build" / "kitchen"
    _write_storage_pointer(config_dir, filename, build_path)
    # Intentionally NOT calling _write_build_info — directory empty.

    controller = _make_controller(monkeypatch)
    metadata = controller._resolve_device_metadata(config_dir, filename)

    assert metadata.expected_config_hash == "abcd1234"


def test_no_hash_anywhere_returns_empty_string(tmp_path: Path, monkeypatch: Any) -> None:
    """Brand-new device, never compiled, no sidecar entry → empty string.

    Empty rather than ``None`` keeps the dataclass shape stable and
    lets ``compute_has_pending_changes`` fall through to the mtime
    check without a special-case branch.
    """
    config_dir = tmp_path
    filename = "kitchen.yaml"
    (config_dir / filename).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    _stub_get_metadata(monkeypatch, {})  # nothing in the sidecar
    # No StorageJSON sidecar either → no build_path → no
    # build_info.json read.

    controller = _make_controller(monkeypatch)
    metadata = controller._resolve_device_metadata(config_dir, filename)

    assert metadata.expected_config_hash == ""


def test_build_info_hash_used_even_when_sidecar_empty(tmp_path: Path, monkeypatch: Any) -> None:
    """First sight of a regenerated device: only build_info.json has the hash.

    Mirrors what the dashboard sees right after
    ``_schedule_storage_regenerate`` runs ``--only-generate`` for a
    newly-added YAML — the sidecar's ``expected_config_hash`` is
    only written on success of that regenerate, but the resolver
    runs on every scan, so the build_info.json read has to carry
    the value through until the persist completes.
    """
    config_dir = tmp_path
    filename = "kitchen.yaml"
    (config_dir / filename).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    _stub_get_metadata(monkeypatch, {})  # sidecar not yet written

    build_path = config_dir / ".esphome" / "build" / "kitchen"
    _write_storage_pointer(config_dir, filename, build_path)
    _write_build_info(build_path, config_hash=0x12345678)

    controller = _make_controller(monkeypatch)
    metadata = controller._resolve_device_metadata(config_dir, filename)

    assert metadata.expected_config_hash == "12345678"


def test_added_device_without_hash_triggers_regenerate(monkeypatch: Any) -> None:
    """An imported device with integrations but no hash gets its hash backfilled.

    Symptom from the field: an Apollo R_PRO-1 added before
    build_info.json existed in the dashboard had ``loaded_integrations``
    populated (so the original "first-sight" trigger didn't fire) but
    no ``expected_config_hash`` — the drawer then showed an em-dash
    forever, because nothing else nudges ``--only-generate`` until
    the YAML is edited. Extending the trigger condition to
    ``not loaded_integrations or not expected_config_hash`` schedules
    the regenerate so the next scan picks up the canonical hash.
    """
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._regenerate_failed = set()
    controller._state_monitor = RecordingStateMonitor()
    schedule = MagicMock()
    monkeypatch.setattr(controller, "_schedule_storage_regenerate", schedule, raising=False)
    captured = capture_devices_events(controller, EventType.DEVICE_ADDED)

    device = Device(
        name="apollo",
        friendly_name="Apollo R_PRO-1",
        configuration="apollo-r-pro-1.yaml",
        loaded_integrations=["api", "wifi"],  # populated, *not* the empty case
        expected_config_hash="",  # but no hash yet
    )
    controller._on_scan_change(ScanChange.ADDED, device)

    schedule.assert_called_once_with("apollo-r-pro-1.yaml")
    # Sanity: the bus fire still happens — the trigger is additive,
    # not a replacement.
    assert [(e.event_type, e.data) for e in captured] == [
        (EventType.DEVICE_ADDED, {"device": device})
    ]
    # Probe fires too — the eager mDNS probe on ADDED is what catches
    # YAMLs dropped on disk outside the API path.
    assert controller._state_monitor.calls == [("probe_device", "apollo", None)]


def test_added_device_fully_populated_does_not_regenerate(
    monkeypatch: Any,
) -> None:
    """A device that already carries integrations + hash skips the regenerate.

    Without this guard, every dashboard restart would needlessly
    spawn an ``--only-generate`` per device on a fully-warmed
    config_dir.
    """
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._regenerate_failed = set()
    controller._state_monitor = MagicMock()
    schedule = MagicMock()
    monkeypatch.setattr(controller, "_schedule_storage_regenerate", schedule, raising=False)

    device = Device(
        name="apollo",
        friendly_name="Apollo R_PRO-1",
        configuration="apollo-r-pro-1.yaml",
        loaded_integrations=["api", "wifi"],
        expected_config_hash="039818dc",
    )
    controller._on_scan_change(ScanChange.ADDED, device)

    schedule.assert_not_called()


def test_board_id_from_sidecar_takes_priority_over_yaml(tmp_path: Path, monkeypatch: Any) -> None:
    """A pinned board_id from the sidecar isn't clobbered by YAML re-derivation.

    Locks down the existing precedence even though the diff under
    test is about the hash side — board_id and the hash are
    resolved together, so a refactor that broke the precedence in
    one direction would likely break the other.
    """
    config_dir = tmp_path
    filename = "kitchen.yaml"
    (config_dir / filename).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    _stub_get_metadata(monkeypatch, {"board_id": "esp32-poe"})

    derive = MagicMock(return_value="should-not-be-called")
    controller = DevicesController.__new__(DevicesController)
    monkeypatch.setattr(controller, "_derive_board_id_from_yaml", derive, raising=False)

    metadata = controller._resolve_device_metadata(config_dir, filename)

    assert metadata.board_id == "esp32-poe"
    derive.assert_not_called()
