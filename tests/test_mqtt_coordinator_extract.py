"""Coverage for the scan-time ``mqtt:`` extraction consumed by the coordinator."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers import _device_mqtt_coordinator as coordinator_module
from esphome_device_builder.helpers.device_yaml import _loading as loading_module
from esphome_device_builder.helpers.device_yaml import extract_mqtt_block
from esphome_device_builder.helpers.device_yaml._loading import load_device_from_storage
from esphome_device_builder.models import Device
from tests._mqtt_fixtures import (
    RecordingMonitor,
    build_test_extract,
    make_mqtt_coordinator,
    write_mqtt_device,
)

_BROKER_YAML = "mqtt:\n  broker: 192.168.1.10\n"


def _seed_package_device(
    tmp_path: Path,
    resolved_block: dict[str, Any] | None,
    resolved_substitutions: dict[str, str] | None = None,
) -> Device:
    """Build a package-sourced mqtt device whose extract carries *resolved_block*."""
    yaml = "esphome:\n  name: pkg\n\npackages:\n  core: !include core.yaml\n"
    path = tmp_path / "pkg.yaml"
    path.write_text(yaml)
    resolved = {"mqtt": resolved_block} if resolved_block is not None else None
    return Device(
        name="pkg",
        friendly_name="pkg",
        configuration="pkg.yaml",
        uses_mqtt=True,
        mqtt_extract=build_test_extract(path, yaml, resolved, resolved_substitutions),
    )


async def test_fresh_extract_skips_reading_the_yaml(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A fresh carried extract resolves the broker without touching the file."""
    device = write_mqtt_device(tmp_path, "kitchen", _BROKER_YAML)
    # Rewrite with a same-length broker and restore the original mtime —
    # the carried extract must win, proving no re-read happened.
    path = tmp_path / "kitchen.yaml"
    original_mtime = path.stat().st_mtime
    path.write_text("esphome:\n  name: kitchen\n\nmqtt:\n  broker: 192.168.1.99\n")
    os.utime(path, (original_mtime, original_mtime))

    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["192.168.1.10"]


async def test_stale_extract_falls_back_and_sees_the_edit(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """An extract whose mtime no longer matches re-reads the edited YAML."""
    device = write_mqtt_device(tmp_path, "kitchen", _BROKER_YAML)
    path = tmp_path / "kitchen.yaml"
    path.write_text("esphome:\n  name: kitchen\n\nmqtt:\n  broker: 10.0.0.9\n")
    future = path.stat().st_mtime + 5
    os.utime(path, (future, future))

    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.0.0.9"]


async def test_resolved_seed_avoids_the_full_parse(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A package-sourced broker derives from the scan seed with no esphome parse."""
    device = _seed_package_device(tmp_path, {"broker": "10.1.1.1"})
    parses = {"n": 0}

    def _no_parse(_path: Path) -> dict:
        parses["n"] += 1
        return {}

    monkeypatch.setattr(coordinator_module, "load_device_yaml", _no_parse)
    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.1.1.1"]
    assert parses["n"] == 0


async def test_seed_resolves_package_substitutions(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seed resolves ``${var}`` against the package-merged substitutions."""
    device = _seed_package_device(tmp_path, {"broker": "${host}"}, {"host": "10.4.4.4"})

    def _no_parse(_path: Path) -> dict:
        raise AssertionError("seed should have resolved without the full parse")

    monkeypatch.setattr(coordinator_module, "load_device_yaml", _no_parse)
    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.4.4.4"]


async def test_stale_seed_falls_back_to_the_full_parse(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secrets-mtime bump invalidates the seed; the full parse runs once then caches."""
    device = _seed_package_device(tmp_path, {"broker": "10.1.1.1"})
    (tmp_path / "secrets.yaml").write_text("added: after-scan\n")

    parses = {"n": 0}

    def _parse(_path: Path) -> dict:
        parses["n"] += 1
        return {"mqtt": {"broker": "10.2.2.2"}}

    monkeypatch.setattr(coordinator_module, "load_device_yaml", _parse)
    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.2.2.2"]
    assert parses["n"] == 1


async def test_seed_deriving_none_falls_through_to_the_full_parse(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolvable seed keeps the full-parse freshness path."""
    device = _seed_package_device(tmp_path, {"broker": "${never_defined}"})

    def _parse(_path: Path) -> dict:
        return {"mqtt": {"broker": "10.3.3.3"}}

    monkeypatch.setattr(coordinator_module, "load_device_yaml", _parse)
    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.3.3.3"]


@pytest.mark.parametrize(
    ("mqtt_yaml", "expected_hosts"),
    [
        pytest.param("mqtt:\n  broker: 192.168.1.10\n", ["192.168.1.10"], id="plain"),
        pytest.param("mqtt:\n  broker: !secret mqtt_host\n", ["172.16.0.2"], id="secret"),
        pytest.param(
            "substitutions:\n  host: 10.9.9.9\n\nmqtt:\n  broker: ${host}\n",
            ["10.9.9.9"],
            id="substitution",
        ),
        pytest.param(
            "mqtt:\n  broker: h\n  client_certificate: c\n  client_certificate_key: k\n",
            [],
            id="client-cert",
        ),
    ],
)
async def test_extract_and_fallback_paths_agree(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    mqtt_yaml: str,
    expected_hosts: list[str],
) -> None:
    """Both tiers produce the expected brokers, so monitors never churn on a rescan."""
    (tmp_path / "secrets.yaml").write_text("mqtt_host: 172.16.0.2\n")
    carried = write_mqtt_device(tmp_path, "kitchen", mqtt_yaml)
    fallback = write_mqtt_device(tmp_path, "kitchen", mqtt_yaml)
    fallback.mqtt_extract = None

    coord_a = make_mqtt_coordinator(tmp_path, [carried])
    await coord_a.reconcile()
    brokers_a = sorted(m.broker for m in coord_a._monitors.values())

    coord_b = make_mqtt_coordinator(tmp_path, [fallback])
    await coord_b.reconcile()

    assert sorted(b.host for b in brokers_a) == expected_hosts
    assert sorted(m.broker for m in coord_b._monitors.values()) == brokers_a


def test_secrets_edit_during_parse_stamps_the_pre_edit_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secrets write racing the esphome parse leaves a stale stamp, not a poisoned seed."""
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text("mqtt_host: 10.0.0.1\n")
    pre_edit_mtime = secrets.stat().st_mtime
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n\nmqtt:\n  broker: x\n")

    def _parse_and_race(_path: Path) -> dict:
        secrets.write_text("mqtt_host: 10.0.0.2\n")
        future = secrets.stat().st_mtime + 5
        os.utime(secrets, (future, future))
        return {"mqtt": {"broker": "10.0.0.1"}}

    monkeypatch.setattr(loading_module, "load_device_yaml", _parse_and_race)
    device = load_device_from_storage(tmp_path / "kitchen.yaml", "", "", "", "", 0, ())

    assert device.mqtt_extract is not None
    assert device.mqtt_extract.secrets_mtime == pre_edit_mtime


async def test_seed_rejected_when_yaml_size_changes_under_same_mtime(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-mtime size change invalidates the seed like the scanner's own cache key."""
    device = _seed_package_device(tmp_path, {"broker": "10.1.1.1"})
    path = tmp_path / "pkg.yaml"
    original_mtime = path.stat().st_mtime
    path.write_text(path.read_text() + "# grew\n")
    os.utime(path, (original_mtime, original_mtime))

    def _parse(_path: Path) -> dict:
        return {"mqtt": {"broker": "10.5.5.5"}}

    monkeypatch.setattr(coordinator_module, "load_device_yaml", _parse)
    coord = make_mqtt_coordinator(tmp_path, [device])
    await coord.reconcile()

    assert [m.broker.host for m in stub_monitor.instances] == ["10.5.5.5"]


def test_extract_mqtt_block_rejects_non_mapping_yaml() -> None:
    """A YAML document that isn't a mapping yields no block."""
    assert extract_mqtt_block("- just\n- a\n- list\n") == (None, {})


def test_scan_populates_and_omits_the_extract(tmp_path: Path) -> None:
    """The producer carries the extract for mqtt devices only, and never serializes it."""
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n\nmqtt:\n  broker: !secret mqtt_host\n"
    )
    (tmp_path / "plain.yaml").write_text("esphome:\n  name: plain\n")

    mqtt_device = load_device_from_storage(tmp_path / "kitchen.yaml", "", "", "", "", 0, ())
    plain_device = load_device_from_storage(tmp_path / "plain.yaml", "", "", "", "", 0, ())

    assert mqtt_device.mqtt_extract is not None
    assert mqtt_device.mqtt_extract.main_block is not None
    assert "mqtt_extract" not in mqtt_device.to_dict()
    assert plain_device.mqtt_extract is None
