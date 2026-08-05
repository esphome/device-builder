"""
Tests for MQTT detection, broker config parsing, and the multi-broker coordinator.

Covers the parts that don't require a live broker:
* YAML parsing for the ``mqtt:`` opt-in (helpers.device_yaml)
* ``parse_mqtt_block`` — broker extraction with ``!secret`` resolution
* ``DeviceMqttCoordinator`` — start/stop one monitor per unique broker
* Source-priority logic in ``DeviceStateMonitor`` (mdns > mqtt > ping)
"""

from __future__ import annotations

import asyncio
import json
import ssl
import textwrap
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers import (
    _device_mqtt_coordinator as coordinator_module,
)
from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import (
    CLIENT_CERT_UNSUPPORTED,
    _broker_from_mqtt_dict,
    parse_mqtt_block,
)
from esphome_device_builder.controllers._device_mqtt_monitor import (
    DeviceMqttMonitor,
    MqttBrokerConfig,
    _decode_payload,
    _extract_ip,
)
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.helpers.device_yaml import device_uses_mqtt
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence
from esphome_device_builder.models import Device, DeviceState
from tests._mqtt_fixtures import (
    RecordingMonitor,
    build_test_extract,
    make_mqtt_coordinator,
    write_mqtt_device,
)

from .conftest import running_task

# ---------------------------------------------------------------------------
# YAML detection
# ---------------------------------------------------------------------------


def test_device_uses_mqtt_top_level_block() -> None:
    yaml = "esphome:\n  name: foo\n\nmqtt:\n  broker: 192.168.1.10\n"
    assert device_uses_mqtt(yaml) is True


def test_device_uses_mqtt_with_comment_above() -> None:
    yaml = "# notes\n\nmqtt:\n  broker: x\n"
    assert device_uses_mqtt(yaml) is True


def test_device_uses_mqtt_inline_token_does_not_count() -> None:
    yaml = "esphome:\n  name: foo\n  comment: 'uses mqtt for telemetry'\n"
    assert device_uses_mqtt(yaml) is False


def test_device_uses_mqtt_only_indented_block() -> None:
    # Indented ``mqtt:`` is part of another block (e.g. a sensor config),
    # not an opt-in to dashboard MQTT discovery.
    yaml = "esphome:\n  name: foo\n\nsensor:\n  - mqtt:\n      topic: foo\n"
    assert device_uses_mqtt(yaml) is False


def test_device_uses_mqtt_handles_empty_input() -> None:
    assert device_uses_mqtt("") is False


# ---------------------------------------------------------------------------
# parse_mqtt_block — broker extraction
# ---------------------------------------------------------------------------


def test_parse_mqtt_block_simple() -> None:
    yaml = "mqtt:\n  broker: 192.168.1.10\n  username: user\n  password: pass\n"
    config = parse_mqtt_block(yaml)
    assert config == MqttBrokerConfig(
        host="192.168.1.10",
        port=1883,
        username="user",
        password="pass",
    )


def test_parse_mqtt_block_custom_port() -> None:
    yaml = "mqtt:\n  broker: broker.example\n  port: 8883\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 8883


def test_parse_mqtt_block_resolves_secrets() -> None:
    yaml = "mqtt:\n  broker: !secret broker_host\n  port: !secret port\n  password: !secret pw\n"
    secrets = {"broker_host": "192.168.1.5", "port": "8883", "pw": "topsecret"}
    config = parse_mqtt_block(yaml, secrets)
    assert config is not None
    assert config.host == "192.168.1.5"
    assert config.port == 8883
    assert config.password == "topsecret"


def test_parse_mqtt_block_missing_secret_returns_none() -> None:
    # broker is required; if its secret can't be resolved, the whole
    # block is unusable.
    yaml = "mqtt:\n  broker: !secret missing\n"
    assert parse_mqtt_block(yaml, {}) is None


def test_parse_mqtt_block_no_block() -> None:
    yaml = "esphome:\n  name: foo\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_ignores_unknown_tags() -> None:
    # Devices can use ESPHome custom tags (!lambda, !include) that pyyaml
    # doesn't know about — parsing must not raise.
    yaml = (
        "esphome:\n  name: foo\n"
        "sensor:\n  - platform: template\n    lambda: !lambda 'return 1;'\n"
        "mqtt:\n  broker: broker.local\n"
    )
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.host == "broker.local"


def test_parse_mqtt_block_invalid_yaml_returns_none() -> None:
    assert parse_mqtt_block("not: valid: yaml: at all") is None


def test_parse_mqtt_block_resolves_substitutions() -> None:
    yaml = (
        "substitutions:\n  mqtt_host: 192.0.2.10\n  mqtt_user: bob\n  mqtt_port: '8883'\n"
        "mqtt:\n  broker: ${mqtt_host}\n  port: ${mqtt_port}\n  username: $mqtt_user\n"
    )
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.host == "192.0.2.10"
    assert config.port == 8883
    assert config.username == "bob"


def test_parse_mqtt_block_unresolved_substitution_returns_none() -> None:
    # No local substitutions block defines mqtt_host; the token must not
    # become a host, so the caller defers to the seed / reload path.
    yaml = "mqtt:\n  broker: ${mqtt_host}\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_resolves_port_substitution() -> None:
    yaml = "substitutions:\n  mqtt_port: '8883'\nmqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 8883


def test_parse_mqtt_block_unresolved_port_substitution_falls_back_to_default() -> None:
    # An unresolved port token can't gate the monitor (only the host does),
    # so it degrades to the default rather than raising.
    yaml = "mqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 1883


# A real (throwaway) self-signed certificate so ``load_verify_locations``
# accepts it in the TLS-context tests; the parse tests only need the
# PEM marker.
_TEST_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIBlzCCAT2gAwIBAgIUHWu1Lj97F4GFo6xWxlmnSwiQcR0wCgYIKoZIzj0EAwIw
ITEfMB0GA1UEAwwWZGV2aWNlLWJ1aWxkZXIgdGVzdCBDQTAeFw0yNjA3MjEwOTM3
NDFaFw0zNjA3MTgwOTM3NDFaMCExHzAdBgNVBAMMFmRldmljZS1idWlsZGVyIHRl
c3QgQ0EwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASxVkmntEhahEMjwX+pXYi8
W8UHQjeY6du4eJNxIZpAQUllDlEWhz/QbHfOfMcrzuSj7VKMavxFFzRP9JWW5CKY
o1MwUTAdBgNVHQ4EFgQUoQqW8ogEU4ejfrfxeFBguEHcElowHwYDVR0jBBgwFoAU
oQqW8ogEU4ejfrfxeFBguEHcElowDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQD
AgNIADBFAiAqg5r0pEyWUbuNBfLpOIyIxQwRbjBjjTEU+J5UgYukOQIhAOve8VSf
nODERLHpQGYe8XgP17e1hlbmvRUz3m87aC8/
-----END CERTIFICATE-----
"""


def _tls_mqtt_yaml(*, skip_cn: bool = False) -> str:
    """Build an ``mqtt:`` block carrying the test CA as an inline block scalar."""
    lines = ["mqtt:", "  broker: broker.example", "  port: 8883", "  certificate_authority: |"]
    lines.append(textwrap.indent(_TEST_CA_PEM, "    ").rstrip("\n"))
    if skip_cn:
        lines.append("  skip_cert_cn_check: true")
    return "\n".join(lines) + "\n"


def test_parse_mqtt_block_reads_tls_fields() -> None:
    config = parse_mqtt_block(_tls_mqtt_yaml(skip_cn=True))
    assert isinstance(config, MqttBrokerConfig)
    assert config.port == 8883
    assert config.certificate_authority is not None
    assert config.certificate_authority.strip() == _TEST_CA_PEM.strip()
    assert config.skip_cert_cn_check is True


def test_parse_mqtt_block_skip_cn_defaults_false() -> None:
    config = parse_mqtt_block(_tls_mqtt_yaml())
    assert isinstance(config, MqttBrokerConfig)
    assert config.skip_cert_cn_check is False


def test_parse_mqtt_block_resolves_ca_secret() -> None:
    yaml = "mqtt:\n  broker: broker.example\n  certificate_authority: !secret mqtt_ca\n"
    config = parse_mqtt_block(yaml, {"mqtt_ca": _TEST_CA_PEM})
    assert isinstance(config, MqttBrokerConfig)
    assert config.certificate_authority == _TEST_CA_PEM


def test_parse_mqtt_block_ca_include_returns_none() -> None:
    # An ``!include``d CA is invisible to the tolerant loader; a plaintext
    # broker here would be wrong, so the caller must miss the fast tier.
    yaml = "mqtt:\n  broker: broker.example\n  certificate_authority: !include ca.pem\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_ca_path_returns_none() -> None:
    # esphome's certificate_authority carries PEM content; a path would
    # hand paho garbage and loop on SSLError.
    yaml = "mqtt:\n  broker: broker.example\n  certificate_authority: /config/ca.pem\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_corrupt_ca_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    # Carries the PEM marker but isn't loadable; refusing at parse time
    # routes it to the loud unresolved warning instead of an eternal
    # quiet reconnect loop on SSLError.
    yaml = (
        "mqtt:\n  broker: broker.example\n  certificate_authority: |\n"
        "    -----BEGIN CERTIFICATE-----\n"
        "    bm90IGEgcmVhbCBjZXJ0\n"
        "    -----END CERTIFICATE-----\n"
    )
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        assert parse_mqtt_block(yaml) is None
    # The concrete SSL parse failure stays recoverable from the logs.
    assert any("failed to parse" in r.getMessage() for r in caplog.records)


def test_parse_mqtt_block_non_ascii_ca_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    # A smart quote from copy-paste makes ``load_verify_locations`` raise
    # TypeError, not SSLError.
    yaml = (
        "mqtt:\n  broker: broker.example\n  certificate_authority: |\n"
        "    -----BEGIN CERTIFICATE-----\n"
        "    MIIB\u2019x\n"
        "    -----END CERTIFICATE-----\n"
    )
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        assert parse_mqtt_block(yaml) is None
    assert any("failed to parse" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("false", False, id="false"),
        pytest.param("off", False, id="off"),
        pytest.param("yes", True, id="yes"),
        pytest.param("not-a-bool", None, id="unrecognized"),
    ],
)
def test_parse_mqtt_block_skip_cn_value_handling(value: str, expected: bool | None) -> None:
    """The YAML boolean vocabulary coerces; a typo refuses rather than meaning False."""
    yaml = _tls_mqtt_yaml() + f"  skip_cert_cn_check: {value}\n"
    config = parse_mqtt_block(yaml)
    if expected is None:
        assert config is None
    else:
        assert isinstance(config, MqttBrokerConfig)
        assert config.skip_cert_cn_check is expected


@pytest.mark.parametrize(
    "cert_lines",
    [
        pytest.param("  client_certificate: cert\n  client_certificate_key: key\n", id="inline"),
        pytest.param("  client_certificate: !secret cc\n", id="secret"),
        pytest.param("  client_certificate: !include cc.pem\n", id="include"),
    ],
)
def test_parse_mqtt_block_client_cert_returns_sentinel(cert_lines: str) -> None:
    yaml = f"mqtt:\n  broker: broker.example\n{cert_lines}"
    assert parse_mqtt_block(yaml) is CLIENT_CERT_UNSUPPORTED


def test_broker_from_mqtt_dict_reads_tls_and_client_cert() -> None:
    tls_block = {"broker": "b.example", "certificate_authority": _TEST_CA_PEM}
    broker = _broker_from_mqtt_dict(tls_block, {}, {})
    assert isinstance(broker, MqttBrokerConfig)
    assert broker.certificate_authority == _TEST_CA_PEM
    client_cert_block = {
        "broker": "b.example",
        "client_certificate": "x",
        "client_certificate_key": "y",
    }
    assert _broker_from_mqtt_dict(client_cert_block, {}, {}) is CLIENT_CERT_UNSUPPORTED


def test_mqtt_broker_config_key_groups_by_host_port_username() -> None:
    a = MqttBrokerConfig(host="broker", port=1883, username="alice")
    b = MqttBrokerConfig(host="broker", port=1883, username="bob")
    c = MqttBrokerConfig(host="broker", port=8883, username="alice")
    d = MqttBrokerConfig(host="broker", port=1883, username="alice", password="one")
    e = MqttBrokerConfig(host="broker", port=1883, username="alice", password="two")
    assert a.key != b.key  # different username → its own session
    assert a.key != c.key  # different port
    assert d.key == e.key  # same login, password differs → shared session


def test_mqtt_broker_config_key_reflects_tls_identity() -> None:
    plain = MqttBrokerConfig(host="broker")
    tls = MqttBrokerConfig(host="broker", certificate_authority=_TEST_CA_PEM)
    other_ca = MqttBrokerConfig(host="broker", certificate_authority="-----BEGIN CERTIFICATE-----X")
    skip_cn = MqttBrokerConfig(
        host="broker", certificate_authority=_TEST_CA_PEM, skip_cert_cn_check=True
    )
    same = MqttBrokerConfig(host="broker", certificate_authority=_TEST_CA_PEM)
    assert len({plain.key, tls.key, other_ca.key, skip_cn.key}) == 4
    assert tls.key == same.key


# ---------------------------------------------------------------------------
# DeviceMqttCoordinator — broker session lifecycle
# ---------------------------------------------------------------------------


async def test_coordinator_no_mqtt_devices_runs_no_monitors(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [write_mqtt_device(tmp_path, "plain", None)]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 0
    assert stub_monitor.instances == []


async def test_coordinator_groups_devices_with_same_broker(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [
        write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.1.10\n"),
        write_mqtt_device(tmp_path, "beta", "mqtt:\n  broker: 192.168.1.10\n"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert len(stub_monitor.instances) == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.10"


async def test_coordinator_skips_client_cert_device_with_warn_once(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    devices = [
        write_mqtt_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: b.example\n  client_certificate: c\n  client_certificate_key: k\n",
        )
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert stub_monitor.instances == []
    matching = [r for r in caplog.records if "client-certificate" in r.getMessage()]
    assert [r.levelname for r in matching] == ["WARNING", "DEBUG"]


async def test_coordinator_rewarns_client_cert_after_recovery(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dropping the client cert clears the gate; re-adding it warns again."""
    client_cert_block = (
        "mqtt:\n  broker: b.example\n  client_certificate: c\n  client_certificate_key: k\n"
    )
    devices = [write_mqtt_device(tmp_path, "alpha", client_cert_block)]
    coord = make_mqtt_coordinator(tmp_path, devices)
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
        # Re-seed so the carried extract tracks the rewrite; a same-mtime
        # rewrite would otherwise serve the stale block on coarse
        # filesystem clocks (Windows CI).
        devices[0] = write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: b.example\n")
        await coord.reconcile()
        assert coord.active_brokers == 1
        devices[0] = write_mqtt_device(tmp_path, "alpha", client_cert_block)
        await coord.reconcile()
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "client-certificate" in r.getMessage()
    ]
    assert len(warnings) == 2


async def test_coordinator_replaces_monitor_when_tls_added(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """Adding a CA to an existing broker login reads as a new broker key."""
    devices = [
        write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: broker.example\n  port: 8883\n")
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    (first,) = stub_monitor.instances
    assert first.broker.certificate_authority is None

    devices[0] = write_mqtt_device(tmp_path, "alpha", _tls_mqtt_yaml())
    await coord.reconcile()

    assert first.stopped is True
    assert len(stub_monitor.instances) == 2
    assert stub_monitor.instances[1].broker.certificate_authority is not None
    assert coord.active_brokers == 1


async def test_coordinator_starts_a_session_per_login_on_one_broker(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same broker, one MQTT user per device (per-user ACLs): each login
    # needs its own session, and it isn't a credential conflict.
    devices = [
        write_mqtt_device(
            tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n  username: alpha\n  password: a\n"
        ),
        write_mqtt_device(
            tmp_path, "beta", "mqtt:\n  broker: 192.168.0.1\n  username: beta\n  password: b\n"
        ),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
    assert coord.active_brokers == 2
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert warnings == []


async def test_coordinator_designates_one_publisher_per_broker(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """Two logins on one broker → one broadcaster; distinct brokers each broadcast."""
    devices = [
        write_mqtt_device(
            tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n  username: alpha\n  password: a\n"
        ),
        write_mqtt_device(
            tmp_path, "beta", "mqtt:\n  broker: 192.168.0.1\n  username: beta\n  password: b\n"
        ),
        write_mqtt_device(tmp_path, "gamma", "mqtt:\n  broker: 192.168.0.2\n"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 3
    by_login = {(m.broker.host, m.broker.username): m for m in stub_monitor.instances}
    assert by_login[("192.168.0.1", "alpha")].is_publisher is True
    assert by_login[("192.168.0.1", "beta")].is_publisher is False
    assert by_login[("192.168.0.2", None)].is_publisher is True


async def test_coordinator_promotes_publisher_when_broadcaster_drops(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """Losing the designated broadcaster promotes a surviving same-broker login."""
    alpha = write_mqtt_device(
        tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n  username: alpha\n  password: a\n"
    )
    beta = write_mqtt_device(
        tmp_path, "beta", "mqtt:\n  broker: 192.168.0.1\n  username: beta\n  password: b\n"
    )
    devices = [alpha, beta]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()

    devices.remove(alpha)
    (tmp_path / "alpha.yaml").unlink()
    await coord.reconcile()

    survivors = [m for m in stub_monitor.instances if not m.stopped]
    assert [(m.broker.username, m.is_publisher) for m in survivors] == [("beta", True)]


async def test_election_prefers_connected_login_over_down_incumbent(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A login stuck in reconnect loses the broadcaster role to a healthy sibling."""
    devices = [
        write_mqtt_device(
            tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n  username: alpha\n  password: a\n"
        ),
        write_mqtt_device(
            tmp_path, "beta", "mqtt:\n  broker: 192.168.0.1\n  username: beta\n  password: b\n"
        ),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    by_user = {m.broker.username: m for m in stub_monitor.instances}
    assert by_user["alpha"].is_publisher is True

    # beta's session connects; alpha never does. The connection-change
    # callback (wired to _assign_publishers) must hand beta the role.
    beta_cb = by_user["beta"].on_connection_change
    assert beta_cb is not None
    by_user["beta"].connected = True
    beta_cb()
    assert by_user["beta"].is_publisher is True
    assert by_user["alpha"].is_publisher is False

    # alpha coming up later must NOT steal the role back — the healthy
    # incumbent is sticky, so the broadcaster doesn't churn.
    by_user["alpha"].connected = True
    by_user["alpha"].on_connection_change()
    assert by_user["beta"].is_publisher is True
    assert by_user["alpha"].is_publisher is False

    # beta dropping hands the role to the connected alpha.
    by_user["beta"].connected = False
    beta_cb()
    assert by_user["alpha"].is_publisher is True
    assert by_user["beta"].is_publisher is False


async def test_coordinator_passes_presence_to_monitors(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n")]
    presence = SubscriberPresence()
    coord = make_mqtt_coordinator(tmp_path, devices, presence=presence)
    await coord.reconcile()
    assert [m.presence for m in stub_monitor.instances] == [presence]


async def test_coordinator_warns_once_on_same_login_different_password(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same host/port/username but disagreeing passwords is genuinely
    # ambiguous; the first password wins and the WARNING is logged once.
    devices = [
        write_mqtt_device(
            tmp_path, "alpha", "mqtt:\n  broker: 192.168.0.1\n  username: shared\n  password: a\n"
        ),
        write_mqtt_device(
            tmp_path, "beta", "mqtt:\n  broker: 192.168.0.1\n  username: shared\n  password: b\n"
        ),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
        await coord.reconcile()
        await coord.reconcile()
    assert coord.active_brokers == 1
    warnings = [
        r
        for r in caplog.records
        if r.name == target and r.levelname == "WARNING" and "different passwords" in r.getMessage()
    ]
    debugs = [
        r
        for r in caplog.records
        if r.name == target and r.levelname == "DEBUG" and "different passwords" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert len(debugs) >= 2


async def test_coordinator_starts_one_monitor_per_unique_broker(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [
        write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: broker-a.local\n"),
        write_mqtt_device(tmp_path, "beta", "mqtt:\n  broker: broker-b.local\n  port: 8883\n"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 2
    hosts = sorted(m.broker.host for m in stub_monitor.instances)
    assert hosts == ["broker-a.local", "broker-b.local"]


async def test_coordinator_stops_monitors_when_devices_drop_mqtt(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: broker.local\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1

    # Simulate the user editing the YAML to remove the mqtt: block.
    devices[0].uses_mqtt = False
    await coord.reconcile()
    assert coord.active_brokers == 0
    assert stub_monitor.instances[0].stopped is True


async def test_coordinator_stop_cleans_up_all_monitors(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [
        write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: broker-a.local\n"),
        write_mqtt_device(tmp_path, "beta", "mqtt:\n  broker: broker-b.local\n"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    await coord.stop()
    assert coord.active_brokers == 0
    assert all(m.stopped for m in stub_monitor.instances)


async def test_coordinator_skips_devices_with_unresolvable_secrets(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: !secret missing\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 0


async def test_coordinator_resolves_secrets_from_secrets_yaml(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    (tmp_path / "secrets.yaml").write_text("mqtt_broker: 10.0.0.5\nmqtt_pw: shh\n")
    devices = [
        write_mqtt_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n  password: !secret mqtt_pw\n",
        )
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "10.0.0.5"
    assert stub_monitor.instances[0].broker.password == "shh"


async def test_coordinator_resolves_secrets_via_included_secrets_file(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # secrets.yaml pulls in a shared file via the merge-key +
    # ``!include`` pattern (HA-shared secrets); the plain SafeLoader
    # rejects it, so the ESPHome-loader fallback must resolve it.
    (tmp_path / "shared.yaml").write_text("mqtt_broker: 10.0.0.9\nmqtt_pw: shh\n")
    (tmp_path / "secrets.yaml").write_text("<<: !include shared.yaml\n")
    devices = [
        write_mqtt_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n  password: !secret mqtt_pw\n",
        )
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "10.0.0.9"
    assert stub_monitor.instances[0].broker.password == "shh"
    assert "Could not parse secrets.yaml" not in caplog.text


async def test_coordinator_warns_when_secrets_unparseable_by_both_loaders(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "secrets.yaml").write_text("<<: !include does_not_exist.yaml\n")
    devices = [
        write_mqtt_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n",
        )
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "Could not read secrets.yaml" in caplog.text


async def test_coordinator_empty_secrets_yaml_does_not_warn(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An empty / comment-only secrets.yaml parses to None; that is a
    # legitimate file and must not spam a warning on every poll.
    (tmp_path / "secrets.yaml").write_text("# only a comment\n")
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: !secret mqtt_broker\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "secrets.yaml" not in caplog.text


async def test_coordinator_warns_when_secrets_yaml_not_a_mapping(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A secrets.yaml that parses to a list/scalar is structurally wrong;
    # warn distinctly from a parse failure rather than degrade silently.
    (tmp_path / "secrets.yaml").write_text("- not\n- a\n- mapping\n")
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: !secret mqtt_broker\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "secrets.yaml is not a mapping" in caplog.text


async def test_coordinator_resolves_broker_pulled_in_via_packages(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    # Issue #893: mqtt block lives in a shared package, not the
    # device file. The coordinator requests a deep reload, then
    # resolves from the refreshed extract's package-merged block.
    (tmp_path / "common.yaml").write_text("mqtt:\n  broker: 192.168.1.203\n")
    alpha_yaml = "esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n"
    alpha_path = tmp_path / "alpha.yaml"
    alpha_path.write_text(alpha_yaml)
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    devices = [device]
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, devices, reload_requests=reloads)

    await coord.reconcile()
    assert coord.active_brokers == 0
    assert reloads == ["alpha.yaml"]

    devices[0] = replace(
        device,
        mqtt_extract=build_test_extract(
            alpha_path, alpha_yaml, {"mqtt": {"broker": "192.168.1.203"}}
        ),
    )
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.203"


async def test_coordinator_resolves_broker_from_local_substitution(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    # Issue #1643: ${var} broker resolves from the file's own
    # substitutions block in the fast path, no reload needed.
    devices = [
        write_mqtt_device(
            tmp_path,
            "alpha",
            "substitutions:\n  mqtt_host: 192.0.2.10\nmqtt:\n  broker: ${mqtt_host}\n",
        )
    ]
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, devices, reload_requests=reloads)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.0.2.10"
    assert reloads == []


async def test_coordinator_resolves_port_from_substitution(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    mqtt_yaml = (
        "substitutions:\n  mqtt_port: '8883'\nmqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    )
    devices = [write_mqtt_device(tmp_path, "alpha", mqtt_yaml)]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.port == 8883


async def test_coordinator_skips_unresolved_substitution(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An undefined substitution must not start a monitor on the literal
    # token; warn once instead of looping on DNS failure.
    device = write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: ${mqtt_host}\n")
    coord = make_mqtt_coordinator(tmp_path, [device])

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("WARNING", logger=target):
        await coord.reconcile()

    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert len(warnings) == 1


async def test_coordinator_warns_once_per_unresolved_device(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A fresh deep extract that still can't resolve is genuinely
    # unresolvable — warn once, and never loop on reload requests.
    device = write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: ${mqtt_host}\n")
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, [device], reload_requests=reloads)

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
        await coord.reconcile()
        await coord.reconcile()

    assert reloads == []

    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    debugs = [
        r
        for r in caplog.records
        if r.name == target
        and r.levelname == "DEBUG"
        and "still could not be resolved" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert len(debugs) >= 2


async def test_coordinator_re_warns_after_broker_recovers_and_breaks_again(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Dedupe must reset on a successful resolve so a later
    # regression surfaces a fresh WARNING, not a DEBUG.
    broken = "mqtt:\n  broker: ${mqtt_host}\n"
    devices = [write_mqtt_device(tmp_path, "alpha", broken)]
    coord = make_mqtt_coordinator(tmp_path, devices)

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("WARNING", logger=target):
        await coord.reconcile()  # unresolved → WARNING #1
        await coord.reconcile()  # unresolved → DEBUG (suppressed)
        devices[0] = write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: broker.local\n")
        await coord.reconcile()  # resolved → flag cleared
        devices[0] = write_mqtt_device(tmp_path, "alpha", broken)
        await coord.reconcile()  # unresolved again → WARNING #2

    warnings = [
        r
        for r in caplog.records
        if r.name == target
        and r.levelname == "WARNING"
        and "could not be resolved" in r.getMessage()
    ]
    assert len(warnings) == 2


def test_broker_from_mqtt_dict_returns_none_for_non_dict() -> None:
    assert _broker_from_mqtt_dict(None, {}, {}) is None
    assert _broker_from_mqtt_dict("not-a-dict", {}, {}) is None


async def test_coordinator_skips_device_whose_yaml_vanishes_at_stat(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The file disappears at the fast tier's stat(). Skip silently —
    # the WARNING is reserved for fixable configs.
    yaml_path = tmp_path / "alpha.yaml"
    yaml_path.write_text("esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n")
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = make_mqtt_coordinator(tmp_path, [device])

    real_stat = Path.stat

    def _stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == yaml_path:
            raise OSError("simulated stat race")
        return real_stat(self, *args, **kwargs)

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with patch.object(Path, "stat", _stat), caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()

    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert warnings == [], [r.getMessage() for r in warnings]


async def test_coordinator_caches_resolved_broker_across_polls(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    # Once seed-resolved, polls hit the cache until an mtime moves —
    # even if a later rebuild drops the seed.
    alpha_yaml = "esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n"
    alpha_path = tmp_path / "alpha.yaml"
    alpha_path.write_text(alpha_yaml)
    seeded = build_test_extract(alpha_path, alpha_yaml, {"mqtt": {"broker": "192.168.1.50"}})
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
        mqtt_extract=seeded,
    )
    devices = [device]
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, devices, reload_requests=reloads)

    await coord.reconcile()
    assert coord.active_brokers == 1

    devices[0] = replace(device, mqtt_extract=replace(seeded, resolved_block=None))
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert reloads == []


async def test_coordinator_recovers_when_negative_resolve_fixed_in_secrets(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    # Failure must not be cached — fix to secrets.yaml has to
    # recover on the next poll without a restart.
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\nmqtt:\n  broker: !secret mqtt_host\n"
    )
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = make_mqtt_coordinator(tmp_path, [device])

    await coord.reconcile()
    assert coord.active_brokers == 0

    (tmp_path / "secrets.yaml").write_text("mqtt_host: 192.168.1.42\n")
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.42"


async def test_coordinator_skips_devices_with_missing_yaml(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # YAML deleted between scans — skip silently, don't fire
    # the broker-unresolvable WARNING (reserved for fixable YAMLs).
    device = Device(
        name="ghost",
        friendly_name="ghost",
        configuration="ghost.yaml",
        uses_mqtt=True,
    )
    coord = make_mqtt_coordinator(tmp_path, [device])
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert warnings == [], [r.getMessage() for r in warnings]


def test_broker_from_mqtt_dict_handles_invalid_port() -> None:
    broker = _broker_from_mqtt_dict({"broker": "broker.local", "port": "not-a-number"}, {}, {})
    assert broker is not None
    assert broker.port == 1883


def test_broker_from_mqtt_dict_returns_none_when_broker_missing() -> None:
    assert _broker_from_mqtt_dict({"username": "u"}, {}, {}) is None


def test_broker_from_mqtt_dict_resolves_substitutions() -> None:
    # The seed carries the package-merged substitutions separately.
    broker = _broker_from_mqtt_dict(
        {"broker": "${mqtt_host}", "port": "${mqtt_port}"},
        {},
        {"mqtt_host": "192.168.1.203", "mqtt_port": "8883"},
    )
    assert broker is not None
    assert broker.host == "192.168.1.203"
    assert broker.port == 8883


def test_broker_from_mqtt_dict_unresolved_substitution_returns_none() -> None:
    assert _broker_from_mqtt_dict({"broker": "${mqtt_host}"}, {}, {}) is None


def test_broker_from_mqtt_dict_resolves_port_substitution() -> None:
    broker = _broker_from_mqtt_dict(
        {"broker": "10.0.0.5", "port": "${mqtt_port}"}, {}, {"mqtt_port": "8883"}
    )
    assert broker is not None
    assert broker.port == 8883


def test_broker_from_mqtt_dict_reads_resolved_block() -> None:
    # Post-resolver shape — every field a plain scalar.
    block = {
        "broker": "192.168.1.203",
        "port": 1883,
        "username": "mquser",
        "password": "topsecret",
    }
    broker = _broker_from_mqtt_dict(block, {}, {})
    assert broker == MqttBrokerConfig(
        host="192.168.1.203",
        port=1883,
        username="mquser",
        password="topsecret",
    )


# ---------------------------------------------------------------------------
# DeviceMqttMonitor — solo lifecycle
# ---------------------------------------------------------------------------


def test_monitor_running_flag_is_false_before_start() -> None:
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
    )
    assert monitor.running is False


async def test_monitor_stop_without_start_is_noop() -> None:
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
    )
    await monitor.stop()
    assert monitor.running is False


# ---------------------------------------------------------------------------
# DeviceStateMonitor — source priority
# ---------------------------------------------------------------------------


def _build_state_monitor() -> tuple[
    DeviceStateMonitor, list[Device], list[tuple[str, DeviceState, str]]
]:
    devices = [Device(name="alpha", friendly_name="Alpha", configuration="alpha.yaml")]
    transitions: list[tuple[str, DeviceState, str]] = []

    def record(name: str, state: DeviceState, source: str) -> None:
        transitions.append((name, state, source))
        for device in devices:
            if device.name == name:
                device.runtime_state.state = state

    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=record,
        on_ip_change=lambda _n, _ip: None,
    )
    return monitor, devices, transitions


def test_priority_mdns_blocks_lower_sources() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "mdns") is True
    assert monitor.apply("alpha", DeviceState.OFFLINE, "mqtt") is False
    assert monitor.apply("alpha", DeviceState.OFFLINE, "ping") is False
    assert transitions == [("alpha", DeviceState.ONLINE, "mdns")]


def test_priority_mqtt_overrides_ping() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert monitor.apply("alpha", DeviceState.OFFLINE, "mqtt") is True
    assert transitions[-1] == ("alpha", DeviceState.OFFLINE, "mqtt")


def test_priority_same_source_replays_are_noop_for_identical_state() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "mqtt") is True
    assert monitor.apply("alpha", DeviceState.ONLINE, "mqtt") is False
    assert len(transitions) == 1


def test_priority_unknown_source_stamped_after_first_observation() -> None:
    monitor, _, _ = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert monitor.priority_for("alpha") == "ping"


def test_unknown_device_observation_is_ignored() -> None:
    monitor, _, transitions = _build_state_monitor()
    assert monitor.apply("missing", DeviceState.ONLINE, "mqtt") is False
    assert transitions == []


def test_ping_can_rescue_after_mdns_offline() -> None:
    """After mDNS pops its source, ping must be allowed to re-mark ONLINE."""
    monitor, _, transitions = _build_state_monitor()
    monitor.apply("alpha", DeviceState.ONLINE, "mdns")
    monitor.apply("alpha", DeviceState.OFFLINE, "mdns")
    # The mDNS Removed handler clears the source so a different source can take over.
    monitor.state.state_source.pop("alpha", None)
    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert transitions[-1] == ("alpha", DeviceState.ONLINE, "ping")


# ---------------------------------------------------------------------------
# DeviceMqttMonitor._listen — retained-message filtering
# ---------------------------------------------------------------------------


async def test_listen_drops_retained_discover_messages() -> None:
    """A retained ``esphome/discover/<name>`` must not flip the device online.

    Retained messages get delivered the moment we subscribe — they're a
    snapshot of the device's *last* publish, not proof that it's reachable
    now. Treating one as an online observation ghost-onlines a dead
    device until the offline timeout catches up.

    Synchronisation: queue a retained message followed by a fresh one
    and only assert after the fresh message's callback fires. That
    proves ``_listen`` actually drained the queue past the retained
    entry rather than racing the cancel — no ``sleep(0)`` heuristics.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _RetainedMessage:
        topic = "esphome/discover/stress-esp32"
        payload = json.dumps({"name": "stress-esp32", "ip": "10.0.0.1"}).encode()
        retain = True

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.2"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_RetainedMessage())
    await queue.put(_FreshMessage())

    async with running_task(monitor._listen(queue)):
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)

    # Only the fresh message produced a callback — the retained one was dropped.
    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_skips_empty_payload() -> None:
    """A message with an empty/None payload is silently skipped.

    ``_decode_payload`` returns ``""`` for ``None`` / unsupported
    shapes. The listen loop short-circuits on the falsy return so
    a misbehaving broker that sends headers without a payload
    doesn't crash the JSON parser. Pin: an empty fresh message
    followed by a real one only fires the real one's callback.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _EmptyPayloadMessage:
        topic = "esphome/discover/ghost"
        payload = None  # _decode_payload returns ""
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.7"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_EmptyPayloadMessage())
    await queue.put(_FreshMessage())

    async with running_task(monitor._listen(queue)):
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)

    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_drops_non_json_payload(caplog: pytest.LogCaptureFixture) -> None:
    """A payload that fails ``json.loads`` is logged and skipped, not raised.

    Misbehaving devices or unrelated retained messages on the
    discover topic shouldn't tank the listener. Pin: malformed
    JSON before a clean message, only the clean one fires.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _BadJsonMessage:
        topic = "esphome/discover/garbled"
        payload = b"not-json-at-all{"
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_BadJsonMessage())
    await queue.put(_FreshMessage())

    with caplog.at_level("DEBUG", logger="esphome_device_builder.controllers._device_mqtt_monitor"):
        async with running_task(monitor._listen(queue)):
            await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)

    assert state_calls == [("kitchen", DeviceState.ONLINE)]
    # Pin the log emission too — without this, a regression that
    # silently swallows the JSONDecodeError without recording it
    # would still pass the "fresh message wins" check.
    assert any(
        "Ignoring non-JSON payload" in rec.message and rec.levelname == "DEBUG"
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


async def test_listen_skips_payload_with_missing_or_invalid_name() -> None:
    """A payload that doesn't carry a non-empty string ``name`` is skipped.

    Defensive: a malformed firmware publishing
    ``{"ip": "..."}`` without a name has no key to associate the
    state change with. The listener silently drops it rather
    than calling the state callback with ``None`` (which the
    downstream monitor would then index by).
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _NoNameMessage:
        topic = "esphome/discover/anonymous"
        payload = json.dumps({"ip": "10.0.0.1"}).encode()  # no ``name``
        retain = False

    class _EmptyNameMessage:
        topic = "esphome/discover/blank"
        payload = json.dumps({"name": "", "ip": "10.0.0.2"}).encode()
        retain = False

    class _NumericNameMessage:
        topic = "esphome/discover/typo"
        payload = json.dumps({"name": 42}).encode()  # not a string
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_NoNameMessage())
    await queue.put(_EmptyNameMessage())
    await queue.put(_NumericNameMessage())
    await queue.put(_FreshMessage())

    async with running_task(monitor._listen(queue)):
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)

    # Only the well-formed message fired the callback.
    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_processes_fresh_discover_messages() -> None:
    """A fresh (non-retained) discover message updates state and IP."""
    state_calls: list[tuple[str, DeviceState]] = []
    ip_calls: list[tuple[str, str]] = []
    seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda n, ip: ip_calls.append((n, ip)),
    )

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.5"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_FreshMessage())

    async with running_task(monitor._listen(queue)):
        await asyncio.wait_for(seen.wait(), timeout=1.0)

    assert state_calls == [("kitchen", DeviceState.ONLINE)]
    assert ip_calls == [("kitchen", "10.0.0.5")]


# ---------------------------------------------------------------------------
# DeviceMqttMonitor — start / stop / running / is_available / _ping_loop
# ---------------------------------------------------------------------------


def test_is_available_tracks_paho_module_presence() -> None:
    """``is_available`` is exactly ``paho_mqtt is not None``.

    Bidirectional contract — locks the predicate regardless of
    whether the test environment actually has paho-mqtt installed.
    The CI matrix that includes the [esphome] extra exercises the
    True branch; a stripped install (e.g. a minimal Docker image
    without the extra) running this same test would exercise the
    False branch. The ``test_is_available_false_when_paho_missing``
    test below pins the False branch unconditionally via
    monkeypatch.
    """
    expected = monitor_module.paho_mqtt is not None
    assert DeviceMqttMonitor.is_available() is expected


def test_is_available_false_when_paho_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``is_available`` returns ``False`` when paho-mqtt isn't importable.

    The dashboard ships with the import wrapped in ``try / except
    ImportError`` so a stripped install (e.g. a Docker image without
    the [esphome] extra) doesn't crash at import time. ``start()``
    consults ``is_available()`` and skips the listener with a
    helpful warning when paho is gone.
    """
    monkeypatch.setattr(monitor_module, "paho_mqtt", None)
    assert DeviceMqttMonitor.is_available() is False


async def test_running_reflects_task_state() -> None:
    """``running`` is True between ``start`` and ``stop``, False outside.

    Exposed for the coordinator's idempotency check ("is this
    monitor already up?") so a duplicate ``start`` doesn't spawn
    a second connect loop.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    assert monitor.running is False  # before start

    # Stand-in for the listener task — never resolves so the
    # monitor stays in the "running" state until we cancel it.
    parked = asyncio.Event()
    async with running_task(parked.wait()) as task:
        monitor._task = task
        assert monitor.running is True

    # A done task no longer counts as running.
    assert monitor.running is False


async def test_start_warns_and_returns_when_paho_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No paho → log warning, don't spawn the listener task.

    Without this early return ``_run`` would crash on the very
    first ``paho_mqtt.Client(...)`` call. The warning is the
    user-facing breadcrumb pointing at the optional ``[esphome]``
    extra.
    """
    monkeypatch.setattr(monitor_module, "paho_mqtt", None)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    with caplog.at_level("WARNING"):
        await monitor.start()

    assert monitor._task is None
    assert any("paho-mqtt not installed" in rec.message for rec in caplog.records)


async def test_start_is_idempotent_when_already_running() -> None:
    """A second ``start`` while running is a no-op — doesn't replace the task.

    Pin the contract so a regression that always re-creates the
    task would orphan the original (which keeps holding the
    paho client + thread) and double-publish discover messages.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    parked = asyncio.Event()
    async with running_task(parked.wait()) as task:
        monitor._task = task
        await monitor.start()
        assert monitor._task is task  # no replacement


async def test_stop_cancels_task_and_clears_last_seen() -> None:
    """``stop`` cancels the runner and forgets every observation.

    Last-seen entries are paired with a live broker subscription;
    keeping them after stop would feed the next ``start`` stale
    timestamps and immediately mark the device offline (they're
    older than ``_OFFLINE_TIMEOUT``).
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    parked = asyncio.Event()
    monitor._task = asyncio.create_task(parked.wait())
    monitor._last_seen["kitchen"] = 12345.0

    await monitor.stop()

    assert monitor._task is None
    assert monitor._last_seen == {}


async def test_stop_is_no_op_when_never_started() -> None:
    """``stop`` on a never-started monitor is a clean no-op.

    Pairs with the coordinator's "drop a broker that no devices
    use" path — it calls ``stop`` unconditionally, which mustn't
    crash on a monitor that never reached ``start``.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    await monitor.stop()
    assert monitor._task is None


async def test_ping_loop_marks_stale_devices_offline_and_republishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale ``_last_seen`` entries flip OFFLINE; broker gets a re-publish each tick.

    The ping loop is the failsafe that fires when MQTT silently
    stops delivering — devices' last-seen ages past
    ``_OFFLINE_TIMEOUT`` and they switch to OFFLINE without a
    fresh subscribe-side signal. The re-publish on every tick
    pokes the broker so any device that quietly came back gets
    a chance to announce again.

    Speed up the loop by patching ``_PING_INTERVAL`` and
    ``_OFFLINE_TIMEOUT`` — the production values (2s / 10s)
    would make this test wait ten seconds for an offline flip.
    """
    # 50ms / 100ms: well under any plausible test-host scheduler
    # jitter while still letting "stale" form between ticks.
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 0.1)

    state_calls: list[tuple[str, DeviceState]] = []
    offline_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        if state == DeviceState.OFFLINE:
            offline_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )
    fake = _CountingClient()

    # Seed a stale entry that's already past the (patched) offline
    # timeout. The first tick should sweep it.
    loop = asyncio.get_running_loop()
    monitor._last_seen["ghost"] = loop.time() - 1.0

    async with running_task(monitor._ping_loop(fake)):
        await asyncio.wait_for(offline_seen.wait(), timeout=2.0)

    assert ("ghost", DeviceState.OFFLINE) in state_calls
    assert "ghost" not in monitor._last_seen
    # Each tick republishes the discover trigger.
    assert fake.publishes
    topic, _payload, retain = fake.publishes[0]
    assert topic == "esphome/discover"
    assert retain is False


class _PublishInfo:
    rc = 0


class _CountingClient:
    def __init__(self) -> None:
        self.publishes: list[tuple[str, Any, bool]] = []

    def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
        self.publishes.append((topic, payload, retain))
        return _PublishInfo()


async def test_ping_loop_idle_publishes_nothing_and_freezes_aging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no dashboard subscriber the loop parks: no broadcasts, no OFFLINE flips."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 0.1)

    state_calls: list[tuple[str, DeviceState]] = []
    presence = SubscriberPresence()
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda n, s: state_calls.append((n, s)),
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    fake = _CountingClient()

    loop = asyncio.get_running_loop()
    monitor._last_seen["ghost"] = loop.time() - 1.0

    async with running_task(monitor._ping_loop(fake)):
        # Several would-be intervals pass; the parked loop stays silent.
        await asyncio.sleep(0.3)
        assert fake.publishes == []
        assert state_calls == []
        assert "ghost" in monitor._last_seen


async def test_ping_loop_resume_publishes_immediately_and_rebases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscriber arriving wakes the loop: instant broadcast, stale entries rebased."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 10.0)

    state_calls: list[tuple[str, DeviceState]] = []
    presence = SubscriberPresence()
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda n, s: state_calls.append((n, s)),
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    fake = _CountingClient()

    loop = asyncio.get_running_loop()
    stale_stamp = loop.time() - 100.0
    monitor._last_seen["sleeper"] = stale_stamp

    async with running_task(monitor._ping_loop(fake)):
        await asyncio.sleep(0.1)
        assert fake.publishes == []

        with presence.subscriber():
            for _ in range(100):
                if fake.publishes:
                    break
                await asyncio.sleep(0.01)
            assert fake.publishes, "no broadcast after a subscriber arrived"
            assert monitor._last_seen["sleeper"] > stale_stamp
            assert state_calls == []


async def test_ping_loop_non_publisher_is_a_pure_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-broadcaster monitor neither publishes nor ages entries offline."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 0.1)

    state_calls: list[tuple[str, DeviceState]] = []
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda n, s: state_calls.append((n, s)),
        on_ip_change=lambda *_: None,
    )
    monitor.is_publisher = False
    fake = _CountingClient()

    loop = asyncio.get_running_loop()
    monitor._last_seen["ghost"] = loop.time() - 1.0

    async with running_task(monitor._ping_loop(fake)):
        await asyncio.sleep(0.3)
        assert fake.publishes == []
        assert state_calls == []
        assert "ghost" in monitor._last_seen


async def test_ping_loop_failed_broadcast_pauses_aging_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A tick whose broadcast failed neither ages devices nor spams the log."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 0.1)

    class _FailingClient(_CountingClient):
        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
            super().publish(topic, payload, retain=retain)
            info = _PublishInfo()
            info.rc = 4
            return info

    state_calls: list[tuple[str, DeviceState]] = []
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda n, s: state_calls.append((n, s)),
        on_ip_change=lambda *_: None,
    )
    fake = _FailingClient()
    loop = asyncio.get_running_loop()
    monitor._last_seen["ghost"] = loop.time() - 1.0

    with caplog.at_level("DEBUG", logger="esphome_device_builder.controllers._device_mqtt_monitor"):
        async with running_task(monitor._ping_loop(fake)):
            for _ in range(100):
                if len(fake.publishes) >= 3:
                    break
                await asyncio.sleep(0.01)

    assert state_calls == []
    assert "ghost" in monitor._last_seen
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Discover broadcast" in warnings[0].getMessage()
    assert any(
        "still failing" in rec.message and rec.levelname == "DEBUG" for rec in caplog.records
    )


async def test_broadcast_recovery_rebases_and_rearms_warning() -> None:
    """The first successful broadcast after a failed stretch rebases the ledger."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    monitor._publish_error_logged = True
    loop = asyncio.get_running_loop()
    stale_stamp = loop.time() - 100.0
    monitor._last_seen["sleeper"] = stale_stamp

    assert await monitor._broadcast(_CountingClient()) is True
    assert monitor._last_seen["sleeper"] > stale_stamp
    assert monitor._publish_error_logged is False


async def test_stop_unsubscribes_presence_wake_callback() -> None:
    """stop() detaches the wake callback so a dropped monitor can't leak into the gate."""
    presence = SubscriberPresence()
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    assert len(presence._subscriber_callbacks) == 1
    await monitor.stop()
    assert presence._subscriber_callbacks == []


async def test_set_connected_fires_connection_change_on_transitions_only() -> None:
    """_set_connected notifies once per edge, not per call."""
    calls: list[bool] = []
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        on_connection_change=lambda: calls.append(True),
    )
    monitor._set_connected(value=True)
    monitor._set_connected(value=True)
    monitor._set_connected(value=False)
    assert calls == [True, True]


async def test_promotion_rebases_last_seen() -> None:
    """set_publisher(False→True) rebases stamps aged during the no-broadcaster gap."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    monitor.is_publisher = False
    loop = asyncio.get_running_loop()
    stale_stamp = loop.time() - 100.0
    monitor._last_seen["sleeper"] = stale_stamp

    monitor.set_publisher(value=True)
    assert monitor._last_seen["sleeper"] > stale_stamp

    # Re-granting an already-held role must not touch the ledger.
    monitor._last_seen["sleeper"] = stale_stamp
    monitor.set_publisher(value=True)
    assert monitor._last_seen["sleeper"] == stale_stamp


async def test_ping_loop_subscriber_return_cuts_interval_sleep_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dashboard reopening mid-interval triggers a broadcast without the full wait."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 30.0)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 65.0)

    presence = SubscriberPresence()
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    fake = _CountingClient()

    async with running_task(monitor._ping_loop(fake)):
        with presence.subscriber():
            for _ in range(100):
                if fake.publishes:
                    break
                await asyncio.sleep(0.01)
            assert len(fake.publishes) == 1
        # Tab closed mid-interval, then reopened — the wake callback
        # must abort the 30s sleep and broadcast promptly.
        await asyncio.sleep(0.05)
        with presence.subscriber():
            for _ in range(100):
                if len(fake.publishes) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert len(fake.publishes) >= 2


# ---------------------------------------------------------------------------
# DeviceMqttMonitor._run — reconnect-on-error loop
# ---------------------------------------------------------------------------


async def test_start_spawns_run_task_when_paho_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first ``start()`` call actually creates the ``_run`` task.

    The ``test_start_is_idempotent_when_already_running`` case
    pre-seeds ``_task`` and asserts it isn't replaced — but the
    happy-path branch (``self._task = asyncio.create_task(self._run())``)
    was uncovered. Stub ``_run`` to a fast-resolving coroutine
    so the test doesn't actually try to talk to a broker, then
    verify ``running`` flipped True.

    Force ``paho_mqtt`` non-None for the duration of the test so
    ``start()``'s ``is_available()`` guard doesn't short-circuit
    on a stripped install (CI without the ``[esphome]`` extra,
    or a Docker base image that omits paho).
    """
    if monitor_module.paho_mqtt is None:
        # Stand-in module — only the truthiness matters here, the
        # stubbed ``_run`` never actually touches it.
        monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {}))

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    parked = asyncio.Event()

    async def _fake_run() -> None:
        await parked.wait()

    monkeypatch.setattr(monitor, "_run", _fake_run)

    await monitor.start()

    try:
        assert monitor.running is True
        assert monitor._task is not None
    finally:
        parked.set()
        await monitor.stop()
    assert monitor.running is False


class _FakePahoClient:
    """Configurable paho stand-in; subclasses override only what varies."""

    connack_rc = 0

    def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
        self.on_connect: Any = None
        self.on_subscribe: Any = None
        self.on_message: Any = None
        self._record("init", (client_id, clean_session))

    def _record(self, op: str, args: tuple[Any, ...]) -> None:
        return None

    def username_pw_set(self, username: str, password: str) -> None:
        self._record("username_pw_set", (username, password))

    def tls_set_context(self, context: ssl.SSLContext) -> None:
        self._record("tls_set_context", (context,))

    def tls_insecure_set(self, value: bool) -> None:
        self._record("tls_insecure_set", (value,))

    def connect(self, host: str, port: int) -> None:
        self._record("connect", (host, port))

    def loop_start(self) -> None:
        self._record("loop_start", ())
        # Fire on_connect the way paho's network thread would; tests
        # run it directly since the call reaches them via the executor.
        self.on_connect(self, None, None, self.connack_rc)

    def loop_stop(self) -> None:
        self._record("loop_stop", ())

    def subscribe(self, topic: str) -> tuple[int, int]:
        self._record("subscribe", (topic,))
        # Model a healthy broker: the SUBACK grants the subscription.
        if self.on_subscribe is not None:
            self.on_subscribe(self, None, 1, [0])
        return (0, 1)

    def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
        self._record("publish", (topic, payload, retain))
        return _PublishInfo()

    def disconnect(self) -> None:
        self._record("disconnect", ())


async def test_connect_and_listen_subscribes_publishes_and_runs_listen_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_connect_and_listen`` wires paho callbacks, subscribes, and runs the inner tasks.

    Drive the full body without a real broker by stubbing
    ``paho_mqtt.Client`` and the inner ``_listen`` / ``_ping_loop``
    coroutines. Pin: ``connect`` / ``loop_start`` / ``subscribe``
    are called in order with no connect-time publish (broadcasts
    belong to the gated ping loop), the inner tasks fire, and
    teardown runs ``disconnect`` + ``loop_stop`` even on cancel.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local", port=1883, username="alice", password="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    calls: list[tuple[str, Any]] = []
    listen_started = asyncio.Event()
    ping_started = asyncio.Event()

    class _FakeClient(_FakePahoClient):
        def _record(self, op: str, args: tuple[Any, ...]) -> None:
            calls.append((op, args))

        def loop_start(self) -> None:
            super().loop_start()
            # Fire one on_message so the queue-bridge closure gets
            # exercised.
            fake_msg = type("M", (), {"topic": "x", "payload": b"", "retain": False})()
            self.on_message(self, None, fake_msg)

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    async def _fake_listen(_queue: Any) -> None:
        listen_started.set()
        await asyncio.Event().wait()

    async def _fake_ping(_client: Any) -> None:
        ping_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_listen", _fake_listen)
    monkeypatch.setattr(monitor, "_ping_loop", _fake_ping)

    async with running_task(monitor._connect_and_listen("test-id")):
        await asyncio.wait_for(listen_started.wait(), timeout=2.0)
        await asyncio.wait_for(ping_started.wait(), timeout=2.0)

    op_names = [c[0] for c in calls]
    # Ordered: init → username/pw → connect → loop_start (whose CONNACK
    # callback subscribes) → disconnect → loop_stop. No publish here —
    # the ping loop owns every broadcast so the subscriber gate can
    # hold them all.
    assert op_names == [
        "init",
        "username_pw_set",
        "connect",
        "loop_start",
        "subscribe",
        "disconnect",
        "loop_stop",
    ]
    assert ("subscribe", ("esphome/discover/#",)) in calls


async def _drive_one_session(
    monitor: DeviceMqttMonitor, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Any]]:
    """Run ``_connect_and_listen`` against the fake client; return its call log."""
    calls: list[tuple[str, Any]] = []
    started = asyncio.Event()

    class _FakeClient(_FakePahoClient):
        def _record(self, op: str, args: tuple[Any, ...]) -> None:
            calls.append((op, args))

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    async def _park(_arg: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_listen", _park)
    monkeypatch.setattr(monitor, "_ping_loop", _park)

    async with running_task(monitor._connect_and_listen("test-id")):
        await asyncio.wait_for(started.wait(), timeout=2.0)
    return calls


async def test_connect_wraps_ca_broker_in_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local", port=8883, certificate_authority=_TEST_CA_PEM),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    calls = await _drive_one_session(monitor, monkeypatch)
    op_names = [c[0] for c in calls]
    assert op_names.index("tls_set_context") < op_names.index("connect")
    assert "tls_insecure_set" not in op_names
    (context,) = next(args for op, args in calls if op == "tls_set_context")
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert len(context.get_ca_certs()) == 1


async def test_connect_skip_cn_check_uses_tls_insecure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the ``tls_insecure_set`` path.

    A direct ``check_hostname`` flip hits the ``ssl.match_hostname``
    removal on Python 3.12+.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(
            host="broker.local",
            port=8883,
            certificate_authority=_TEST_CA_PEM,
            skip_cert_cn_check=True,
        ),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    calls = await _drive_one_session(monitor, monkeypatch)
    op_names = [c[0] for c in calls]
    assert (
        op_names.index("tls_set_context")
        < op_names.index("tls_insecure_set")
        < op_names.index("connect")
    )
    assert ("tls_insecure_set", (True,)) in calls


async def test_run_treats_ssl_error_as_expected_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A TLS handshake failure reconnects quietly under its own actionable label."""
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x", certificate_authority=_TEST_CA_PEM),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    attempts = 0
    third_attempt = asyncio.Event()

    async def _fail(_client_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            third_attempt.set()
            await asyncio.Event().wait()
        raise ssl.SSLError(1, "certificate verify failed")

    monkeypatch.setattr(monitor, "_connect_and_listen", _fail)
    target = "esphome_device_builder.controllers._device_mqtt_monitor"
    with caplog.at_level("DEBUG", logger=target):
        async with running_task(monitor._run()):
            await asyncio.wait_for(third_attempt.wait(), timeout=2.0)
    tls_records = [r for r in caplog.records if "TLS handshake" in r.getMessage()]
    assert [r.levelname for r in tls_records][:2] == ["WARNING", "DEBUG"]
    # The misconfiguration label, not "unreachable", and it points at the knobs.
    assert "certificate_authority" in tls_records[0].getMessage()
    assert not [r for r in caplog.records if "unreachable" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def _fail_rc(_topic: str) -> tuple[int, int]:
    return (7, 1)


def _fail_raise(_topic: str) -> tuple[int, int]:
    raise RuntimeError("boom")


@pytest.mark.parametrize(
    ("subscribe_impl", "match"),
    [
        pytest.param(_fail_rc, "subscribe failed \\(rc=7\\)", id="nonzero_rc"),
        pytest.param(_fail_raise, "subscribe raised", id="raises"),
    ],
)
async def test_subscribe_failure_fails_the_session_loud(
    monkeypatch: pytest.MonkeyPatch,
    subscribe_impl: Callable[[str], tuple[int, int]],
    match: str,
) -> None:
    """A failed or raising subscribe surfaces as ConnectionError, never a silent timeout."""

    class _FakeClient(_FakePahoClient):
        def subscribe(self, topic: str) -> tuple[int, int]:
            return subscribe_impl(topic)

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    with pytest.raises(ConnectionError, match=match):
        await monitor._connect_and_listen("test-id")


async def test_reconnect_subscribe_failure_tears_the_session_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed re-subscribe on paho's auto-reconnect rebuilds the session, not a dead list."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    session_running = asyncio.Event()
    loop = asyncio.get_running_loop()
    instances: list[Any] = []

    class _FakeClient(_FakePahoClient):
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            super().__init__(client_id, clean_session)
            self.subscribe_calls = 0
            instances.append(self)

        def subscribe(self, topic: str) -> tuple[int, int]:
            self.subscribe_calls += 1
            return (0, 1) if self.subscribe_calls == 1 else (7, 2)

        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
            # The ping loop broadcasting proves the session TaskGroup
            # is running, so the re-fired CONNACK below exercises the
            # watcher path, not the initial handshake check.
            loop.call_soon_threadsafe(session_running.set)
            return _PublishInfo()

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    task = asyncio.create_task(monitor._connect_and_listen("test-id"))
    await asyncio.wait_for(session_running.wait(), timeout=2.0)
    # paho's auto-reconnect re-fires on_connect; this re-subscribe fails.
    instances[0].on_connect(instances[0], None, None, 0)

    with pytest.raises(ConnectionError, match="subscribe failed \\(rc=7\\)"):
        await asyncio.wait_for(task, timeout=2.0)


async def test_acl_denied_subscription_tears_the_session_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SUBACK 0x80 (broker ACL denial) fails the session instead of going dark."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    session_running = asyncio.Event()
    loop = asyncio.get_running_loop()
    instances: list[Any] = []

    class _FakeClient(_FakePahoClient):
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            super().__init__(client_id, clean_session)
            instances.append(self)

        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
            loop.call_soon_threadsafe(session_running.set)
            return _PublishInfo()

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    task = asyncio.create_task(monitor._connect_and_listen("test-id"))
    await asyncio.wait_for(session_running.wait(), timeout=2.0)
    # The broker's SUBACK arrives after the handshake looked healthy.
    instances[0].on_subscribe(instances[0], None, 1, [0x80])

    with pytest.raises(ConnectionError, match="subscription denied"):
        await asyncio.wait_for(task, timeout=2.0)


async def test_missing_suback_fails_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker that never SUBACKs fails the session instead of staying dark."""
    monkeypatch.setattr(monitor_module, "_CONNECT_TIMEOUT", 0.2)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    class _FakeClient(_FakePahoClient):
        def subscribe(self, topic: str) -> tuple[int, int]:
            return (0, 1)  # accepted, but no SUBACK ever arrives

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    with pytest.raises(ConnectionError, match="no SUBACK"):
        await asyncio.wait_for(monitor._connect_and_listen("test-id"), timeout=2.0)


async def test_missing_suback_on_reconnect_fails_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SUBACK guard re-arms per handshake — a silent reconnect SUBACK also fails."""
    monkeypatch.setattr(monitor_module, "_CONNECT_TIMEOUT", 0.3)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    session_running = asyncio.Event()
    loop = asyncio.get_running_loop()
    instances: list[Any] = []

    class _FakeClient(_FakePahoClient):
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            super().__init__(client_id, clean_session)
            self.subscribe_calls = 0
            instances.append(self)

        def subscribe(self, topic: str) -> tuple[int, int]:
            self.subscribe_calls += 1
            if self.subscribe_calls == 1:
                return super().subscribe(topic)  # healthy: SUBACK granted
            return (0, 1)  # accepted, but the reconnect SUBACK never arrives

        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> _PublishInfo:
            loop.call_soon_threadsafe(session_running.set)
            return _PublishInfo()

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    task = asyncio.create_task(monitor._connect_and_listen("test-id"))
    await asyncio.wait_for(session_running.wait(), timeout=2.0)
    # paho's auto-reconnect re-fires on_connect; this handshake's
    # SUBACK is silently dropped.
    instances[0].on_connect(instances[0], None, None, 0)

    with pytest.raises(ConnectionError, match="no SUBACK"):
        await asyncio.wait_for(task, timeout=2.0)


async def test_idle_monitor_still_applies_spontaneous_announcements() -> None:
    """The listen path is not presence-gated — announcements apply while parked."""
    presence = SubscriberPresence()
    state_calls: list[tuple[str, DeviceState]] = []
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda n, s: state_calls.append((n, s)),
        on_ip_change=lambda *_: None,
        presence=presence,
    )
    assert not presence.has_subscribers()

    queue: asyncio.Queue[Any] = asyncio.Queue()
    await queue.put(
        type(
            "M",
            (),
            {
                "topic": "esphome/discover/kitchen",
                "payload": json.dumps({"name": "kitchen"}).encode(),
                "retain": False,
            },
        )()
    )
    async with running_task(monitor._listen(queue)):
        for _ in range(100):
            if state_calls:
                break
            await asyncio.sleep(0.01)

    assert state_calls == [("kitchen", DeviceState.ONLINE)]
    assert "kitchen" in monitor._last_seen


@pytest.mark.parametrize("failing_call", ["disconnect", "loop_stop"])
async def test_teardown_failure_does_not_mask_the_session_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failing_call: str
) -> None:
    """A raising teardown call is logged, its sibling still runs, and the session error surfaces."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    torn_down: list[str] = []

    class _FakeClient(_FakePahoClient):
        connack_rc = 4  # "bad username/password" — any non-zero rejects

        def _record(self, op: str, args: tuple[Any, ...]) -> None:
            if op in ("disconnect", "loop_stop"):
                torn_down.append(op)

        def disconnect(self) -> None:
            super().disconnect()
            if failing_call == "disconnect":
                raise RuntimeError("boom")

        def loop_stop(self) -> None:
            super().loop_stop()
            if failing_call == "loop_stop":
                raise RuntimeError("boom")

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    with (
        caplog.at_level("ERROR", logger="esphome_device_builder.controllers._device_mqtt_monitor"),
        pytest.raises(ConnectionError, match="rc=4"),
    ):
        await monitor._connect_and_listen("test-id")
    assert monitor.connected is False
    # A raising disconnect must not skip the thread join, or the paho
    # thread leaks every reconnect cycle.
    assert torn_down == ["disconnect", "loop_stop"]
    assert any("teardown failed" in rec.message for rec in caplog.records)


def test_unwrap_session_error_keeps_mixed_groups() -> None:
    """Only a lone expected connection error unwraps; anything else stays grouped."""
    lone = ExceptionGroup("g", [ConnectionError("x")])
    assert isinstance(monitor_module._unwrap_session_error(lone), ConnectionError)
    paired = ExceptionGroup("g", [ConnectionError("x"), OSError("y")])
    assert isinstance(monitor_module._unwrap_session_error(paired), ConnectionError)
    mixed = ExceptionGroup("g", [ConnectionError("x"), ValueError("y")])
    assert monitor_module._unwrap_session_error(mixed) is mixed


async def test_reconnect_refires_subscribe_via_on_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every CONNACK resubscribes, so paho's auto-reconnect can't lose the topic."""
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    subscribes: list[str] = []
    subscribed = asyncio.Event()
    loop = asyncio.get_running_loop()
    instances: list[Any] = []

    class _FakeClient(_FakePahoClient):
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            super().__init__(client_id, clean_session)
            instances.append(self)

        def subscribe(self, topic: str) -> tuple[int, int]:
            subscribes.append(topic)
            loop.call_soon_threadsafe(subscribed.set)
            return (0, 1)

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    async with running_task(monitor._connect_and_listen("test-id")):
        await asyncio.wait_for(subscribed.wait(), timeout=2.0)
        assert subscribes == ["esphome/discover/#"]
        # paho's auto-reconnect re-fires on_connect from its thread;
        # the callback alone must re-establish the subscription.
        instances[0].on_connect(instances[0], None, None, 0)
        assert subscribes == ["esphome/discover/#", "esphome/discover/#"]


async def test_connect_and_listen_raises_on_broker_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero CONNACK rc raises ``ConnectionError`` so ``_run`` retries.

    The retry path (``test_run_reconnects_on_connect_and_listen_failure``)
    proves the loop catches the error; this test pins that the
    error is actually raised when paho reports a rejected connect.
    Disconnect + loop_stop must still run in the finally block.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    teardown_calls: list[str] = []

    class _FakeClient(_FakePahoClient):
        connack_rc = 4  # "bad username/password" — any non-zero rejects

        def _record(self, op: str, args: tuple[Any, ...]) -> None:
            if op in ("loop_stop", "disconnect"):
                teardown_calls.append(op)

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    with pytest.raises(ConnectionError, match="rc=4"):
        await monitor._connect_and_listen("test-id")

    # Teardown ran even though we raised, in paho's documented order.
    assert teardown_calls == ["disconnect", "loop_stop"]


async def test_run_reconnects_on_connect_and_listen_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker error in ``_connect_and_listen`` triggers a delayed retry.

    ``_run``'s reconnect loop is what survives transient broker
    blips (network glitch, broker restart). A bare exception
    inside ``_connect_and_listen`` would otherwise kill the
    monitor permanently. The test patches the underlying
    coroutine to raise once, then succeed — and asserts the
    second call happened.

    Speed up via ``_RECONNECT_DELAY = 0`` so the test doesn't
    wait the production 5s between attempts.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    # Seed last_seen so we can verify it gets cleared on error
    # (production keeps device state alone — only ``_last_seen``
    # is reset — so a brief blip doesn't trigger an offline storm).
    monitor._last_seen["kitchen"] = 0.0

    call_count = 0
    second_call = asyncio.Event()

    async def _fake_connect(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            msg = "broker rejected"
            raise ConnectionError(msg)
        second_call.set()
        # Park to keep the runner alive until cancelled.
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_connect_and_listen", _fake_connect)

    async with running_task(monitor._run()):
        await asyncio.wait_for(second_call.wait(), timeout=2.0)

    assert call_count >= 2
    # First-attempt error cleared last_seen — pin the contract
    # so a regression that leaves stale entries (which would
    # then immediately mark the device offline on the next ping
    # tick) surfaces here.
    assert monitor._last_seen == {}


async def test_run_collapses_repeat_unreachable_errors_to_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeat unreachable-broker errors stay at DEBUG, not ERROR+traceback.

    When the broker is offline for a long time the reconnect loop
    fires every ``_RECONNECT_DELAY`` seconds. Logging a full ERROR
    with traceback on each tick floods journalctl / Home Assistant's
    log view (issue #324). The first failure should still be loud
    (WARNING, no traceback for expected ``TimeoutError`` /
    ``OSError`` / ``ConnectionError``) so the operator sees the
    broker went away; subsequent identical failures collapse to
    DEBUG so the file doesn't fill with copies of the same trace.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    call_count = 0
    third_call = asyncio.Event()

    async def _always_timeout(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            third_call.set()
        raise TimeoutError("timed out")

    monkeypatch.setattr(monitor, "_connect_and_listen", _always_timeout)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    async with running_task(monitor._run()):
        await asyncio.wait_for(third_call.wait(), timeout=2.0)

    unreachable = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and "unreachable" in r.message
    ]
    # Exactly one WARNING for the first transition into "unreachable",
    # the rest collapsed to DEBUG. ``exc_info`` must be None on every
    # such record — pin that there's no traceback being attached.
    warnings = [r for r in unreachable if r.levelname == "WARNING"]
    debugs = [r for r in unreachable if r.levelname == "DEBUG"]
    assert len(warnings) == 1, [r.levelname for r in unreachable]
    assert len(debugs) >= 1
    for record in unreachable:
        assert record.exc_info is None


async def test_run_resets_log_gate_after_successful_connect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful CONNACK re-arms the loud-warning gate.

    Without the reset, a broker that goes down → up → down again
    would only WARN once (on the very first failure) and silently
    DEBUG every subsequent outage forever, defeating the point of
    surfacing it in the operator's log. The reset trigger is
    ``self._connected_this_session = True`` (set inside
    ``_connect_and_listen`` right after CONNACK), not a clean
    return — production almost never sees a clean return because
    the inner TaskGroup parks until cancelled or raises.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    # Sequence of behaviours per ``_connect_and_listen`` call:
    # 1. fail (TimeoutError) — first WARNING
    # 2. simulate a session that reached CONNACK and then was
    #    closed by the broker (sets the in-session flag, then
    #    raises an expected error). Production's equivalent is a
    #    broker that accepted the connection, ran for a while, and
    #    then dropped us — the gate must re-arm.
    # 3. fail (TimeoutError) — should be a *second* WARNING, not DEBUG
    behaviours = ["fail", "connect-then-drop", "fail"]
    third_failure = asyncio.Event()

    async def _scripted(_client_id: str) -> None:
        if not behaviours:
            third_failure.set()
            await asyncio.Event().wait()
        action = behaviours.pop(0)
        if not behaviours:
            third_failure.set()
        if action == "fail":
            raise TimeoutError("timed out")
        # ``connect-then-drop``: signal CONNACK success, then
        # raise as if the broker dropped the session.
        monitor._connected_this_session = True
        raise ConnectionError("broker dropped session")

    monkeypatch.setattr(monitor, "_connect_and_listen", _scripted)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    async with running_task(monitor._run()):
        await asyncio.wait_for(third_failure.wait(), timeout=2.0)
        # Give the loop one extra tick to log the third failure
        # before we tear it down.
        await asyncio.sleep(0.05)

    warnings = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "WARNING"
        and "unreachable" in r.message
    ]
    # Two WARNINGs: the first failure (start of outage A) and the
    # connect-then-drop (start of outage B — gate re-armed by the
    # CONNACK in between). The third failure is a continuation of
    # outage B with no successful connect between them, so it
    # collapses to DEBUG. Pinning this also catches the inverse
    # regression: dropping the gate-reset entirely would only emit
    # one WARNING here instead of two.
    assert len(warnings) == 2, [r.message for r in warnings]
    debugs = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "DEBUG"
        and "unreachable" in r.message
    ]
    assert len(debugs) == 1, [r.message for r in debugs]


async def test_run_loud_logs_unexpected_after_expected_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new unexpected exception after a connect-error loop still logs ERROR+traceback.

    The two log gates (expected vs unexpected) are tracked
    separately so a long ``TimeoutError`` outage can't suppress
    the first appearance of an *unexpected* exception class —
    that would hide a genuine bug behind the offline-broker
    spam-suppression. Pin: after a TimeoutError WARNING, a
    subsequent ``RuntimeError`` (unrelated category) logs at
    ERROR level with traceback (``exc_info``) attached.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    # First call raises TimeoutError (expected, WARNING),
    # second call raises RuntimeError (unexpected, must be loud).
    behaviours: list[str] = ["timeout", "unexpected"]
    second_call = asyncio.Event()

    async def _scripted(_client_id: str) -> None:
        if not behaviours:
            second_call.set()
            await asyncio.Event().wait()
        action = behaviours.pop(0)
        if not behaviours:
            second_call.set()
        if action == "timeout":
            raise TimeoutError("timed out")
        msg = "kaboom"
        raise RuntimeError(msg)

    monkeypatch.setattr(monitor, "_connect_and_listen", _scripted)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    async with running_task(monitor._run()):
        await asyncio.wait_for(second_call.wait(), timeout=2.0)
        await asyncio.sleep(0.05)

    errors = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and r.levelname == "ERROR" and "error" in r.message
    ]
    assert len(errors) == 1, [(r.levelname, r.message) for r in errors]
    # ``logger.exception`` attaches exc_info — pin the traceback is
    # actually present so a regression that drops the exception
    # context (or routes through DEBUG) surfaces here.
    assert errors[0].exc_info is not None


async def test_run_collapses_repeat_unexpected_errors_to_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeat unexpected exceptions log DEBUG with class+message, no traceback.

    Covers the suppressed-traceback DEBUG branch in
    ``_log_reconnect_failure``'s unexpected-error path. The first
    occurrence still emits one ERROR with traceback (proven in
    ``test_run_loud_logs_unexpected_after_expected_failure``);
    every subsequent occurrence with the gate already tripped
    falls back to DEBUG so a tight failure loop doesn't dump the
    same trace into the log every ``_RECONNECT_DELAY``. Pin: the
    DEBUG line still includes the exception class name and message
    so the operator can tell what's repeating without raising the
    log level back to ERROR.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    call_count = 0
    third_call = asyncio.Event()

    async def _always_runtime_error(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            third_call.set()
        msg = "kaboom"
        raise RuntimeError(msg)

    monkeypatch.setattr(monitor, "_connect_and_listen", _always_runtime_error)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    async with running_task(monitor._run()):
        await asyncio.wait_for(third_call.wait(), timeout=2.0)

    errors = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and r.levelname == "ERROR" and "error" in r.message
    ]
    debugs = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "DEBUG"
        and "suppressed traceback" in r.message
    ]
    # Exactly one ERROR (first hit) and at least one DEBUG-suppressed
    # follow-up — the repeats. Anything more than one ERROR means the
    # gate didn't trip; zero DEBUG means the suppressed branch was
    # never reached.
    assert len(errors) == 1, [(r.levelname, r.message) for r in errors]
    assert len(debugs) >= 1
    # Every DEBUG must include the exception class + message — that's
    # the whole point of capturing ``as err`` in the broad branch.
    for record in debugs:
        assert "RuntimeError" in record.message
        assert "kaboom" in record.message
        # And no traceback should be attached at this level — the
        # promise of "suppressed traceback" is meaningful only if it
        # actually drops the exc_info too.
        assert record.exc_info is None


# ---------------------------------------------------------------------------
# Pure helpers — _extract_ip / _decode_payload
# ---------------------------------------------------------------------------


def test_extract_ip_returns_first_present_address() -> None:
    """``_extract_ip`` returns the first ``ip``/``ip0``/``ip1``/``ip2`` set.

    Some ESPHome firmwares expose multiple IPs (Wi-Fi + Ethernet,
    Wi-Fi + AP). The dashboard only needs one to dial back; the
    first is the canonical primary, secondaries are fallbacks
    when it's unreachable. Pin the iteration order ``ip`` →
    ``ip0`` → ``ip1`` → ``ip2`` so a regression that flips it
    surfaces here.
    """
    # ``ip`` wins when present.
    assert _extract_ip({"ip": "10.0.0.1", "ip0": "192.168.1.1", "ip1": "172.16.0.1"}) == "10.0.0.1"
    # Falls through to ``ip0`` when ``ip`` missing.
    assert _extract_ip({"ip0": "192.168.1.1", "ip1": "172.16.0.1"}) == "192.168.1.1"
    # And to ``ip1`` / ``ip2`` in turn.
    assert _extract_ip({"ip1": "172.16.0.1", "ip2": "10.10.10.10"}) == "172.16.0.1"
    assert _extract_ip({"ip2": "10.10.10.10"}) == "10.10.10.10"


def test_extract_ip_skips_empty_and_non_string_values() -> None:
    """Empty strings / non-strings are skipped; missing all → ``""``.

    Defensive: a misbehaving firmware that publishes ``"ip": null``
    or ``"ip": ""`` shouldn't shadow the next address candidate.
    """
    # Empty + non-string ``ip`` skipped, falls through to ``ip1``.
    assert _extract_ip({"ip": "", "ip0": None, "ip1": "172.16.0.1"}) == "172.16.0.1"
    # Numeric-shaped non-string skipped (devices shouldn't do this
    # but the helper guards against it anyway).
    assert _extract_ip({"ip": 12345}) == ""
    # Nothing present at all.
    assert _extract_ip({}) == ""
    assert _extract_ip({"name": "kitchen", "version": "2026.5.0"}) == ""


def test_extract_ip_skips_unspecified_and_unparseable_values() -> None:
    """Unspecified / non-IP candidates are skipped; the next real IP wins."""
    assert _extract_ip({"ip": "0.0.0.0", "ip0": "10.0.0.5"}) == "10.0.0.5"
    assert _extract_ip({"ip": "::", "ip0": "10.0.0.5"}) == "10.0.0.5"
    assert _extract_ip({"ip": "not-an-ip", "ip0": "10.0.0.5"}) == "10.0.0.5"
    assert _extract_ip({"ip": "0.0.0.0", "ip0": "::"}) == ""


def test_decode_payload_handles_str_bytes_and_garbage() -> None:
    """``_decode_payload`` accepts ``str`` / ``bytes`` / ``bytearray`` / ``memoryview``.

    paho-mqtt's payload type isn't strictly typed at the wire —
    the helper has to tolerate every shape paho might produce.
    Malformed UTF-8 falls back to ``backslashreplace`` so the
    debug log line stays readable.
    """
    assert _decode_payload("already-text") == "already-text"
    assert _decode_payload(b"raw bytes") == "raw bytes"
    assert _decode_payload(bytearray(b"mutable")) == "mutable"
    assert _decode_payload(memoryview(b"viewed")) == "viewed"
    # Malformed UTF-8: the leading 0x80 isn't a valid start byte;
    # ``backslashreplace`` keeps it visible without raising.
    decoded = _decode_payload(b"\x80hello")
    assert "hello" in decoded


def test_decode_payload_returns_empty_for_unsupported_types() -> None:
    """``None`` and other unsupported payload shapes return ``""``.

    The caller guards against a falsy return so an empty string
    safely short-circuits the JSON parse without raising.
    """
    assert _decode_payload(None) == ""
    assert _decode_payload(12345) == ""
    assert _decode_payload({"not": "supported"}) == ""
    assert _decode_payload(["nope"]) == ""


async def test_coordinator_cancel_mid_stop_keeps_monitor_registered(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A cancel during a monitor's teardown leaves it registered for ``stop()`` to find."""
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.1.10\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    monitor = stub_monitor.instances[0]

    async def _cancelled_stop() -> None:
        raise asyncio.CancelledError

    devices[0] = write_mqtt_device(tmp_path, "alpha", None)

    with (
        patch.object(monitor, "stop", _cancelled_stop),
        pytest.raises(asyncio.CancelledError),
    ):
        await coord.reconcile()

    assert coord.active_brokers == 1

    await coord.stop()
    assert coord.active_brokers == 0
    assert monitor.stopped is True


async def test_coordinator_stop_bounded_when_reconcile_lock_held(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged reconcile can't stall shutdown; ``stop()`` times out and still stops monitors."""
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.1.10\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    monitor = stub_monitor.instances[0]
    monkeypatch.setattr(coordinator_module, "_STOP_LOCK_TIMEOUT_SECONDS", 0.01)
    await coord._reconcile_lock.acquire()

    await coord.stop()

    assert monitor.stopped is True
    assert coord.active_brokers == 0
    # The holder's lock is untouched — stop() never acquired it.
    assert coord._reconcile_lock.locked()


async def test_coordinator_reconcile_after_stop_starts_nothing(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A reconcile that outlives ``stop()`` must not restart monitors."""
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.1.10\n")]
    coord = make_mqtt_coordinator(tmp_path, devices)

    await coord.stop()
    await coord.reconcile()

    assert coord.active_brokers == 0
    assert stub_monitor.instances == []


async def test_coordinator_reload_lands_and_next_pass_starts_the_broker(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A shallow-extract device defers via a reload request; the next pass starts it."""
    alpha_yaml = "esphome:\n  name: alpha\nmqtt:\n  broker: ${mqtt_host}\n"
    alpha_path = tmp_path / "alpha.yaml"
    alpha_path.write_text(alpha_yaml)
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
        mqtt_extract=build_test_extract(alpha_path, alpha_yaml, shallow=True),
    )
    devices = [device]
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, devices, reload_requests=reloads)

    await coord.reconcile()
    assert coord.active_brokers == 0
    assert reloads == ["alpha.yaml"]

    devices[0] = replace(
        device,
        mqtt_extract=build_test_extract(
            alpha_path,
            alpha_yaml,
            {"mqtt": {"broker": "${mqtt_host}"}},
            {"mqtt_host": "192.168.1.77"},
        ),
    )
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.77"


async def test_coordinator_stale_extract_requests_reload(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An edited-since-scan unresolvable device defers to a reload, silently."""
    device = write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 10.0.0.5\n")
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\n\nmqtt:\n  broker: ${package_host}\n"
    )
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, [device], reload_requests=reloads)

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("WARNING", logger=target):
        await coord.reconcile()

    assert coord.active_brokers == 0
    assert reloads == ["alpha.yaml"]
    assert [r for r in caplog.records if r.name == target and r.levelname == "WARNING"] == []


async def test_coordinator_defers_teardown_while_reloads_pending(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A pass with unresolved devices never stops monitors it can't account for."""
    devices = [write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 10.0.0.5\n")]
    reloads: list[str] = []
    coord = make_mqtt_coordinator(tmp_path, devices, reload_requests=reloads)
    await coord.reconcile()
    assert coord.active_brokers == 1

    beta_yaml = "esphome:\n  name: beta\nmqtt:\n  broker: ${package_host}\n"
    beta_path = tmp_path / "beta.yaml"
    beta_path.write_text(beta_yaml)
    devices[0] = write_mqtt_device(tmp_path, "alpha", None)
    devices.append(
        Device(
            name="beta",
            friendly_name="beta",
            configuration="beta.yaml",
            uses_mqtt=True,
            mqtt_extract=build_test_extract(beta_path, beta_yaml, shallow=True),
        )
    )

    await coord.reconcile()
    assert reloads == ["beta.yaml"]
    # Alpha's monitor survives the deferred pass.
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].stopped is False

    devices[1] = replace(
        devices[1],
        mqtt_extract=build_test_extract(
            beta_path,
            beta_yaml,
            {"mqtt": {"broker": "${package_host}"}},
            {"package_host": "10.9.9.9"},
        ),
    )
    await coord.reconcile()
    # Beta resolved and started; alpha's stale monitor finally torn down.
    assert stub_monitor.instances[0].stopped is True
    assert coord.active_brokers == 1
    assert stub_monitor.instances[1].broker.host == "10.9.9.9"


async def test_coordinator_reconcile_survives_stop_during_teardown(
    tmp_path: Path,
    stub_monitor: type[RecordingMonitor],
) -> None:
    """A ``stop()`` landing mid-teardown neither crashes the pass nor restarts monitors."""
    devices = [
        write_mqtt_device(tmp_path, "alpha", "mqtt:\n  broker: 10.0.0.1\n"),
        write_mqtt_device(tmp_path, "beta", "mqtt:\n  broker: 10.0.0.2\n"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 2

    async def _concurrent_stop() -> None:
        coord._stopped = True
        coord._monitors.clear()

    devices[:] = [
        write_mqtt_device(tmp_path, "alpha", None),
        write_mqtt_device(tmp_path, "beta", None),
    ]
    with (
        patch.object(stub_monitor.instances[0], "stop", _concurrent_stop),
        patch.object(stub_monitor.instances[1], "stop", _concurrent_stop),
    ):
        await coord.reconcile()

    assert coord.active_brokers == 0
    assert len(stub_monitor.instances) == 2
