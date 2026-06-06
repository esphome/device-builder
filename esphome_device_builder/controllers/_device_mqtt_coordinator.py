"""
Coordinator for per-broker MQTT discovery monitors.

Reads each MQTT-using device's YAML, resolves ``!secret`` references via
``secrets.yaml``, groups by broker host/port, and runs one
:class:`DeviceMqttMonitor` per unique broker. Re-runs lifecycle on each
poll so monitors track YAML edits.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from esphome.core import EsphomeError

from ..constants import SECRETS_FILENAME
from ..helpers.device_yaml import load_device_yaml
from ..helpers.yaml import FastestSafeLoader, load_yaml_fast_then_esphome
from ..models import Device
from ._device_mqtt_monitor import (
    DeviceMqttMonitor,
    IPCallback,
    MqttBrokerConfig,
    StateCallback,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_PORT = 1883


class DeviceMqttCoordinator:
    """
    Manage one :class:`DeviceMqttMonitor` per unique broker.

    ``reconcile()`` is idempotent — call it after every device scan to
    pick up YAML edits. Adds monitors for new brokers, stops monitors
    for brokers no longer referenced.
    """

    def __init__(
        self,
        config_dir: Path,
        get_devices: Callable[[], list[Device]],
        on_state_change: StateCallback,
        on_ip_change: IPCallback,
    ) -> None:
        self._config_dir = config_dir
        self._get_devices = get_devices
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._monitors: dict[tuple[str, int], DeviceMqttMonitor] = {}
        # Positive-only slow-path cache keyed on ``(yaml_mtime,
        # secrets_mtime)``. Package / ``!include`` edits on a
        # previously-cached device won't invalidate — user needs a
        # device-YAML touch or dashboard restart for those.
        self._broker_cache: dict[str, tuple[tuple[float, float], MqttBrokerConfig]] = {}
        # Per-device dedupe for the broker-unresolvable WARNING —
        # WARNING once, DEBUG on repeats.
        self._unresolved_logged: set[str] = set()

    @property
    def active_brokers(self) -> int:
        """Return the number of brokers currently being monitored."""
        return len(self._monitors)

    async def reconcile(self) -> None:
        """Sync running monitors to the brokers referenced by device YAML."""
        if not DeviceMqttMonitor.is_available():
            if any(d.uses_mqtt for d in self._get_devices()):
                _LOGGER.warning(
                    "aiomqtt not installed — MQTT device discovery disabled despite "
                    "devices declaring mqtt: blocks"
                )
            return

        loop = asyncio.get_running_loop()
        brokers = await loop.run_in_executor(None, self._collect_brokers)
        wanted_keys = {b.key for b in brokers}
        existing_keys = set(self._monitors.keys())

        for key in existing_keys - wanted_keys:
            host, port = key
            _LOGGER.info("Stopping MQTT monitor for %s:%s", host, port)
            await self._monitors.pop(key).stop()

        for broker in brokers:
            if broker.key in self._monitors:
                continue
            monitor = DeviceMqttMonitor(broker, self._on_state_change, self._on_ip_change)
            self._monitors[broker.key] = monitor
            await monitor.start()

    async def stop(self) -> None:
        """Stop every active monitor and clear state."""
        for monitor in list(self._monitors.values()):
            await monitor.stop()
        self._monitors.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_brokers(self) -> list[MqttBrokerConfig]:
        secrets_map = _load_secrets(self._config_dir)
        secrets_mtime = _safe_mtime(self._config_dir / SECRETS_FILENAME)
        seen: dict[tuple[str, int], MqttBrokerConfig] = {}
        seen_devices: set[str] = set()
        for device in self._get_devices():
            if not device.uses_mqtt:
                continue
            seen_devices.add(device.configuration)
            yaml_path = self._config_dir / device.configuration
            try:
                yaml_content = yaml_path.read_text(encoding="utf-8")
                yaml_mtime = yaml_path.stat().st_mtime
            except OSError:
                # Skip silently — the WARNING is reserved for
                # present-but-unresolvable YAMLs, not deleted ones.
                _LOGGER.debug("Could not read %s for MQTT broker config", device.configuration)
                continue
            broker = self._resolve_broker(
                yaml_path, yaml_content, yaml_mtime, secrets_mtime, secrets_map
            )
            if broker is None:
                self._log_broker_unresolved(device.configuration)
                continue
            self._unresolved_logged.discard(device.configuration)
            existing = seen.get(broker.key)
            if existing is None:
                seen[broker.key] = broker
                continue
            if (existing.username, existing.password) != (broker.username, broker.password):
                _LOGGER.warning(
                    "Multiple devices reference broker %s:%s with different credentials — "
                    "using credentials from the first device",
                    broker.host,
                    broker.port,
                )
        # Drop tracking for devices no longer declaring ``mqtt:``.
        self._unresolved_logged &= seen_devices
        self._broker_cache = {k: v for k, v in self._broker_cache.items() if k in seen_devices}
        return list(seen.values())

    def _resolve_broker(
        self,
        yaml_path: Path,
        yaml_content: str,
        yaml_mtime: float,
        secrets_mtime: float,
        secrets_map: dict[str, Any],
    ) -> MqttBrokerConfig | None:
        """Return the broker for *yaml_path*, or None if unresolvable."""
        broker = parse_mqtt_block(yaml_content, secrets_map)
        if broker is not None:
            return broker
        cache_key = (yaml_mtime, secrets_mtime)
        cached = self._broker_cache.get(yaml_path.name)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        resolved = load_device_yaml(yaml_path)
        broker = _extract_broker_from_config(resolved)
        if broker is not None:
            self._broker_cache[yaml_path.name] = (cache_key, broker)
        else:
            self._broker_cache.pop(yaml_path.name, None)
        return broker

    def _log_broker_unresolved(self, configuration: str) -> None:
        if configuration in self._unresolved_logged:
            _LOGGER.debug(
                "Device %s declares mqtt: but broker still could not be resolved",
                configuration,
            )
            return
        _LOGGER.warning(
            "Device %s declares mqtt: but broker could not be resolved "
            "(missing secret or invalid config)",
            configuration,
        )
        self._unresolved_logged.add(configuration)


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------


class _SecretRef:
    """Marker for an unresolved ``!secret <name>`` reference."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class _TolerantYamlLoader(FastestSafeLoader):
    """SafeLoader that captures ``!secret`` and ignores other custom tags.

    Subclasses ``FastestSafeLoader`` (libyaml-backed CSafeLoader
    when available) so the per-device MQTT-block parse pays the
    fast path. The custom-constructor mechanism is identical
    between the C and pure-Python loaders, so the ``!secret`` /
    unknown-tag handlers wired below work either way.
    """


def _construct_secret(loader: yaml.Loader, node: yaml.ScalarNode) -> _SecretRef:
    return _SecretRef(loader.construct_scalar(node))


def _ignore_unknown_tag(_loader: yaml.Loader, _tag_suffix: str, _node: yaml.Node) -> None:
    return None


_TolerantYamlLoader.add_constructor("!secret", _construct_secret)
_TolerantYamlLoader.add_multi_constructor("!", _ignore_unknown_tag)


def parse_mqtt_block(
    yaml_content: str,
    secrets_map: dict[str, Any] | None = None,
) -> MqttBrokerConfig | None:
    """
    Extract broker connection parameters from a device YAML.

    Returns ``None`` when the YAML has no ``mqtt:`` block, when the
    block has no resolvable ``broker:`` field, or when the YAML fails
    to parse. ``!secret xyz`` references are resolved via *secrets_map*.
    Reads literal contents only — ``packages:`` / ``!include`` go
    through :func:`load_device_yaml` + ``_extract_broker_from_config``.
    """
    secrets_map = secrets_map or {}
    try:
        # _TolerantYamlLoader subclasses FastestSafeLoader (libyaml's
        # CSafeLoader when available, the pure-Python SafeLoader
        # otherwise — both are safe). The custom !secret constructor
        # only emits a marker dataclass, never instantiates arbitrary
        # types.
        data = yaml.load(yaml_content, Loader=_TolerantYamlLoader)  # noqa: S506
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    mqtt = data.get("mqtt")
    if not isinstance(mqtt, dict):
        return None
    return _broker_from_block(
        {
            "broker": _resolve(mqtt.get("broker"), secrets_map),
            "port": _resolve(mqtt.get("port"), secrets_map),
            "username": _resolve(mqtt.get("username"), secrets_map),
            "password": _resolve(mqtt.get("password"), secrets_map),
        }
    )


def _extract_broker_from_config(config: dict | None) -> MqttBrokerConfig | None:
    """Extract broker parameters from a fully-resolved ESPHome config."""
    if not isinstance(config, dict):
        return None
    mqtt = config.get("mqtt")
    if not isinstance(mqtt, dict):
        return None
    return _broker_from_block(mqtt)


def _broker_from_block(mqtt: dict) -> MqttBrokerConfig | None:
    """Build an :class:`MqttBrokerConfig` from a resolved ``mqtt:`` block."""
    host = mqtt.get("broker")
    if not host:
        return None
    port_raw = mqtt.get("port")
    try:
        port = int(port_raw) if port_raw else _DEFAULT_PORT
    except (TypeError, ValueError):
        port = _DEFAULT_PORT
    username = mqtt.get("username") or None
    password = mqtt.get("password") or None
    return MqttBrokerConfig(
        host=str(host),
        port=port,
        username=str(username) if username is not None else None,
        password=str(password) if password is not None else None,
    )


def _load_secrets(config_dir: Path) -> dict[str, Any]:
    secrets_path = config_dir / SECRETS_FILENAME
    if not secrets_path.exists():
        return {}
    try:
        data = load_yaml_fast_then_esphome(secrets_path)
    except (EsphomeError, yaml.YAMLError, OSError, UnicodeDecodeError) as err:
        _LOGGER.warning("Could not read secrets.yaml (%s) — MQTT broker secrets unavailable", err)
        return {}
    # An empty or comment-only secrets.yaml parses to None; that is a
    # legitimate file, not a failure, so degrade silently.
    if data is None:
        return {}
    if not isinstance(data, dict):
        _LOGGER.warning("secrets.yaml is not a mapping — MQTT broker secrets unavailable")
        return {}
    return data


def _safe_mtime(path: Path) -> float:
    """Return *path*'s mtime, or ``0.0`` when the file is missing."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _resolve(value: Any, secrets_map: dict[str, Any]) -> str | None:
    """Return the resolved scalar value, or None when unresolvable."""
    if value is None:
        return None
    if isinstance(value, _SecretRef):
        secret = secrets_map.get(value.name)
        if secret is None:
            _LOGGER.warning("Secret %r referenced by mqtt: block is not defined", value.name)
            return None
        return str(secret)
    if isinstance(value, (str, int, float)):
        return str(value)
    return None
