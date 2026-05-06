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

from ..helpers.yaml import FastestSafeLoader
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
        seen: dict[tuple[str, int], MqttBrokerConfig] = {}
        for device in self._get_devices():
            if not device.uses_mqtt:
                continue
            yaml_path = self._config_dir / device.configuration
            try:
                yaml_content = yaml_path.read_text(encoding="utf-8")
            except OSError:
                _LOGGER.debug("Could not read %s for MQTT broker config", device.configuration)
                continue
            broker = parse_mqtt_block(yaml_content, secrets_map)
            if broker is None:
                _LOGGER.warning(
                    "Device %s declares mqtt: but broker could not be resolved "
                    "(missing secret or invalid config)",
                    device.configuration,
                )
                continue
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
        return list(seen.values())


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

    host = _resolve(mqtt.get("broker"), secrets_map)
    if not host:
        return None
    port_raw = _resolve(mqtt.get("port"), secrets_map)
    try:
        port = int(port_raw) if port_raw else _DEFAULT_PORT
    except (TypeError, ValueError):
        port = _DEFAULT_PORT
    username = _resolve(mqtt.get("username"), secrets_map) or None
    password = _resolve(mqtt.get("password"), secrets_map) or None

    return MqttBrokerConfig(host=host, port=port, username=username, password=password)


def _load_secrets(config_dir: Path) -> dict[str, Any]:
    secrets_path = config_dir / "secrets.yaml"
    if not secrets_path.exists():
        return {}
    try:
        with secrets_path.open("r", encoding="utf-8") as f:
            # ``FastestSafeLoader`` is libyaml's CSafeLoader — the C
            # equivalent of SafeLoader. Same noqa rationale as the
            # ``_TolerantYamlLoader`` call above.
            data = yaml.load(f, Loader=FastestSafeLoader)  # noqa: S506
    except yaml.YAMLError:
        _LOGGER.warning("Could not parse secrets.yaml — MQTT broker secrets unavailable")
        return {}
    return data if isinstance(data, dict) else {}


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
