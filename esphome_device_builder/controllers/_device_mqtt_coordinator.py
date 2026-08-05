"""
Coordinator for per-broker MQTT discovery monitors.

Consumes each MQTT-using device's scan-time ``mqtt:`` extraction (falling
back to reading the YAML when the extraction is stale), resolves
``!secret`` references via ``secrets.yaml``, groups by broker
host/port/username, and runs one :class:`DeviceMqttMonitor` per unique
broker login. Re-runs lifecycle on each poll so monitors track edits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from esphome.core import EsphomeError

from ..constants import SECRETS_FILENAME
from ..helpers.async_ import run_in_executor
from ..helpers.atomic_io import read_text_with_stat
from ..helpers.device_yaml import (
    _UNRESOLVED_SUBSTITUTION_RE,
    SecretRef,
    _extract_resolved_substitutions,
    _resolve_substitutions,
    extract_mqtt_block,
    load_device_yaml,
    safe_stat_key,
)
from ..helpers.subscriber_presence import SubscriberPresence
from ..helpers.yaml import load_yaml_fast_then_esphome
from ..models import Device
from ..models.devices import DeviceMqttExtract
from ._device_mqtt_monitor import (
    BrokerKey,
    DeviceMqttMonitor,
    IPCallback,
    MqttBrokerConfig,
    StateCallback,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_PORT = 1883

# ``mqtt:`` fields read into an MqttBrokerConfig, each resolved through
# the same !secret + substitution pipeline.
_BROKER_FIELDS = ("broker", "port", "username", "password", "certificate_authority")
# TLS client-certificate auth has no paho path that accepts in-memory
# PEM (``load_cert_chain`` wants files), so these blocks are skipped
# with a warning instead of looping unreachable.
_CLIENT_CERT_FIELDS = ("client_certificate", "client_certificate_key")

_PEM_CERT_MARKER = "-----BEGIN CERTIFICATE-----"

_UNREADABLE_DEBUG = "Could not read %s for MQTT broker config"


class _ClientCertUnsupported:
    """Sentinel: the ``mqtt:`` block uses client-certificate auth."""

    __slots__ = ()


CLIENT_CERT_UNSUPPORTED = _ClientCertUnsupported()


class DeviceMqttCoordinator:
    """
    Manage one :class:`DeviceMqttMonitor` per unique broker login.

    ``reconcile()`` is idempotent — call it after every device scan to
    pick up YAML edits. Adds monitors for new ``(host, port, username)``
    logins, stops monitors for logins no longer referenced.
    """

    def __init__(
        self,
        config_dir: Path,
        get_devices: Callable[[], list[Device]],
        on_state_change: StateCallback,
        on_ip_change: IPCallback,
        presence: SubscriberPresence | None = None,
    ) -> None:
        self._config_dir = config_dir
        self._get_devices = get_devices
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._presence = presence
        self._monitors: dict[BrokerKey, DeviceMqttMonitor] = {}
        # Positive-only slow-path cache keyed on the YAML's
        # ``(mtime_ns, size)`` plus the secrets file's. Package /
        # ``!include`` edits on a previously-cached device won't
        # invalidate — user needs a device-YAML touch or restart.
        self._broker_cache: dict[
            str, tuple[tuple[int, int, int, int], MqttBrokerConfig | _ClientCertUnsupported]
        ] = {}
        # Per-device dedupe for the broker-unresolvable WARNING —
        # WARNING once, DEBUG on repeats.
        self._unresolved_logged: set[str] = set()
        # Per-device dedupe for the client-certificate-unsupported
        # WARNING — WARNING once, DEBUG on repeats.
        self._client_cert_logged: set[str] = set()
        # Per-login dedupe for the same-username/different-password
        # WARNING — WARNING once, DEBUG on repeats.
        self._conflict_logged: set[BrokerKey] = set()
        # Two concurrent reconciles (cold-start refine vs poll) would
        # both see a broker absent and leak one running monitor on the
        # dict overwrite.
        self._reconcile_lock = asyncio.Lock()

    @property
    def active_brokers(self) -> int:
        """Return the number of brokers currently being monitored."""
        return len(self._monitors)

    async def reconcile(self) -> None:
        """Sync running monitors to the brokers referenced by device YAML."""
        if not DeviceMqttMonitor.is_available():
            if any(d.uses_mqtt for d in self._get_devices()):
                _LOGGER.warning(
                    "paho-mqtt not installed — MQTT device discovery disabled despite "
                    "devices declaring mqtt: blocks"
                )
            return

        async with self._reconcile_lock:
            brokers = await run_in_executor(self._collect_brokers)
            wanted_keys = {b.key for b in brokers}
            existing_keys = set(self._monitors.keys())

            for key in existing_keys - wanted_keys:
                _LOGGER.info(
                    "Stopping MQTT monitor for %s:%s user %r", key.host, key.port, key.username
                )
                monitor = self._monitors.pop(key)
                try:
                    await monitor.stop()
                except asyncio.CancelledError:
                    # A cancel mid-teardown (refine drained during
                    # shutdown) must not orphan a started monitor
                    # outside the registry ``stop()`` walks.
                    self._monitors[key] = monitor
                    raise

            new_monitors: list[DeviceMqttMonitor] = []
            for broker in brokers:
                if broker.key in self._monitors:
                    continue
                monitor = DeviceMqttMonitor(
                    broker,
                    self._on_state_change,
                    self._on_ip_change,
                    presence=self._presence,
                    on_connection_change=self._assign_publishers,
                )
                self._monitors[broker.key] = monitor
                new_monitors.append(monitor)

            # Election runs before start() so a new monitor never connects
            # wearing the default publisher flag, and again on every
            # connection change via the callback above.
            self._assign_publishers()
            for monitor in new_monitors:
                await monitor.start()

    async def stop(self) -> None:
        """Stop every active monitor and clear state."""
        async with self._reconcile_lock:
            for monitor in list(self._monitors.values()):
                await monitor.stop()
            self._monitors.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assign_publishers(self) -> None:
        """
        Designate one discover broadcaster per (host, port).

        Same-broker monitors under other logins subscribe but stay
        silent — the fleet answers every broadcast, so N logins must
        not mean N× the traffic. Connected sessions win over down ones
        (a login stuck in reconnect must not silence the broker), the
        incumbent wins over healthy siblings (no churn), then
        anonymous-first / lowest-username keeps the pick stable.
        """

        def order(key: BrokerKey) -> tuple[bool, bool, bool, str, str, bool]:
            monitor = self._monitors[key]
            incumbent = monitor.is_publisher
            return (
                not monitor.connected,
                not incumbent,
                key.username is not None,
                key.username or "",
                key.ca_digest or "",
                key.skip_cn,
            )

        elected: dict[tuple[str, int], DeviceMqttMonitor] = {}
        for key in sorted(self._monitors, key=order):
            elected.setdefault(key[:2], self._monitors[key])
        for key, monitor in self._monitors.items():
            monitor.set_publisher(value=elected[key[:2]] is monitor)

    def _collect_brokers(self) -> list[MqttBrokerConfig]:
        mqtt_devices = [d for d in self._get_devices() if d.uses_mqtt]
        if not mqtt_devices:
            # Equivalent to an empty loop pass: clear the warn gates and
            # cache so a re-added device warns fresh, and skip the
            # secrets read + parse the common no-mqtt fleet never needs.
            self._unresolved_logged.clear()
            self._client_cert_logged.clear()
            self._conflict_logged.clear()
            self._broker_cache.clear()
            return []
        secrets_map = _load_secrets(self._config_dir)
        secrets_key = safe_stat_key(self._config_dir / SECRETS_FILENAME)
        seen: dict[BrokerKey, MqttBrokerConfig] = {}
        seen_devices: set[str] = set()
        client_cert_devices: set[str] = set()
        conflicts: set[BrokerKey] = set()
        for device in mqtt_devices:
            seen_devices.add(device.configuration)
            yaml_path = self._config_dir / device.configuration
            try:
                yaml_stat = yaml_path.stat()
            except OSError:
                # Skip silently — the WARNING is reserved for
                # present-but-unresolvable YAMLs, not deleted ones.
                _LOGGER.debug(_UNREADABLE_DEBUG, device.configuration)
                continue
            if (extract := device.mqtt_extract) is not None and _extract_fresh(extract, yaml_stat):
                broker = _broker_from_mqtt_dict(
                    extract.main_block, secrets_map, extract.main_substitutions
                )
            else:
                # Scan raced an edit (or the device predates the scanner
                # carrying extractions) — fall back to reading the file;
                # the handle stat keys _resolve_slow's extract-seed
                # comparison.
                try:
                    yaml_stat, yaml_content = read_text_with_stat(yaml_path)
                except OSError:
                    _LOGGER.debug(_UNREADABLE_DEBUG, device.configuration)
                    continue
                broker = parse_mqtt_block(yaml_content, secrets_map)
            if broker is None:
                broker = self._resolve_slow(yaml_path, device.mqtt_extract, yaml_stat, secrets_key)
            self._track_outcome(device.configuration, broker, seen, client_cert_devices, conflicts)
        # Drop tracking for devices no longer declaring ``mqtt:`` and
        # logins that no longer conflict, so a recurrence re-warns.
        self._unresolved_logged &= seen_devices
        self._client_cert_logged &= client_cert_devices
        self._conflict_logged &= conflicts
        self._broker_cache = {k: v for k, v in self._broker_cache.items() if k in seen_devices}
        return list(seen.values())

    def _track_outcome(
        self,
        configuration: str,
        broker: MqttBrokerConfig | _ClientCertUnsupported | None,
        seen: dict[BrokerKey, MqttBrokerConfig],
        client_cert_devices: set[str],
        conflicts: set[BrokerKey],
    ) -> None:
        """Record one device's resolution outcome into the reconcile accumulators."""
        if isinstance(broker, _ClientCertUnsupported):
            client_cert_devices.add(configuration)
            self._log_client_cert_unsupported(configuration)
            return
        if broker is None:
            self._log_broker_unresolved(configuration)
            return
        self._unresolved_logged.discard(configuration)
        self._client_cert_logged.discard(configuration)
        existing = seen.get(broker.key)
        if existing is None:
            seen[broker.key] = broker
            return
        # Same host/port/username but a different password: the login
        # is ambiguous, so the first device's password wins.
        if existing.password != broker.password:
            conflicts.add(broker.key)
            self._log_credential_conflict(broker)

    def _resolve_slow(
        self,
        yaml_path: Path,
        extract: DeviceMqttExtract | None,
        yaml_stat: os.stat_result,
        secrets_key: tuple[int, int],
    ) -> MqttBrokerConfig | _ClientCertUnsupported | None:
        """Resolve a package-sourced broker: cache, then scan seed, then full parse."""
        cache_key = (yaml_stat.st_mtime_ns, yaml_stat.st_size, *secrets_key)
        cached = self._broker_cache.get(yaml_path.name)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        # A fresh scan-time extraction of the package-merged config
        # stands in for the full parse; only a positive outcome is
        # cached so a broken package edit keeps re-resolving.
        if (
            extract is not None
            and extract.resolved_block is not None
            and (
                extract.yaml_mtime_ns,
                extract.yaml_size,
                extract.secrets_mtime_ns,
                extract.secrets_size,
            )
            == cache_key
        ):
            broker = _broker_from_mqtt_dict(
                extract.resolved_block, {}, extract.resolved_substitutions
            )
            if broker is not None:
                self._broker_cache[yaml_path.name] = (cache_key, broker)
                return broker
        resolved = load_device_yaml(yaml_path)
        broker = _extract_broker_from_config(resolved)
        if broker is not None:
            self._broker_cache[yaml_path.name] = (cache_key, broker)
        else:
            self._broker_cache.pop(yaml_path.name, None)
        return broker

    def _log_broker_unresolved(self, configuration: str) -> None:
        self._warn_once(
            self._unresolved_logged,
            configuration,
            "Device %s declares mqtt: but broker could not be resolved "
            "(missing secret, invalid config, or a certificate_authority "
            "that is not valid inline PEM content)",
            "Device %s declares mqtt: but broker still could not be resolved",
            configuration,
        )

    def _log_client_cert_unsupported(self, configuration: str) -> None:
        self._warn_once(
            self._client_cert_logged,
            configuration,
            "Device %s uses MQTT client-certificate authentication, which is not "
            "supported for MQTT discovery — skipping this device's broker",
            "Device %s still uses MQTT client-certificate auth — discovery skipped",
            configuration,
        )

    def _log_credential_conflict(self, broker: MqttBrokerConfig) -> None:
        self._warn_once(
            self._conflict_logged,
            broker.key,
            "Multiple devices reference broker %s:%s as user %r with different passwords — "
            "using the password from the first device",
            "Broker %s:%s user %r still referenced with different passwords — using the first",
            broker.host,
            broker.port,
            broker.username,
        )

    @staticmethod
    def _warn_once(gate: set[Any], key: Any, warning: str, debug: str, *args: Any) -> None:
        """WARNING the first time *key* lands in *gate*, DEBUG while it persists."""
        if key in gate:
            _LOGGER.debug(debug, *args)
            return
        _LOGGER.warning(warning, *args)
        gate.add(key)


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------


def parse_mqtt_block(
    yaml_content: str,
    secrets_map: dict[str, Any] | None = None,
) -> MqttBrokerConfig | _ClientCertUnsupported | None:
    """
    Extract broker connection parameters from a device YAML.

    Returns ``None`` when the YAML has no ``mqtt:`` block, when the
    block has no resolvable ``broker:`` field, or when the YAML fails
    to parse. ``!secret xyz`` references and ``${var}`` / ``$var``
    substitutions from the file's own ``substitutions:`` block are
    resolved; a broker still carrying an unresolved token returns
    ``None`` so the caller falls through to the package-aware slow path.
    ``CLIENT_CERT_UNSUPPORTED`` flags a block using client-certificate
    auth, which discovery cannot do.
    """
    mqtt, subs = extract_mqtt_block(yaml_content)
    return _broker_from_mqtt_dict(mqtt, secrets_map or {}, subs)


def _extract_broker_from_config(
    config: dict | None,
) -> MqttBrokerConfig | _ClientCertUnsupported | None:
    """Extract broker parameters from a fully-resolved ESPHome config.

    ``load_device_yaml`` merges ``packages:`` / ``!include`` but skips the
    substitution pass, so resolve ``${var}`` against the merged
    ``substitutions:`` block here too.
    """
    if not isinstance(config, dict):
        return None
    return _broker_from_mqtt_dict(config.get("mqtt"), {}, _extract_resolved_substitutions(config))


def _extract_fresh(extract: DeviceMqttExtract, yaml_stat: os.stat_result) -> bool:
    """Whether the scan-time extraction still matches the file on disk."""
    return extract.yaml_mtime_ns == yaml_stat.st_mtime_ns and extract.yaml_size == yaml_stat.st_size


def _broker_from_mqtt_dict(
    mqtt: Any, secrets_map: dict[str, Any], subs: dict[str, str]
) -> MqttBrokerConfig | _ClientCertUnsupported | None:
    """Shared per-item core: verdict for one ``mqtt:`` mapping."""
    if not isinstance(mqtt, dict):
        return None
    # Presence alone signals intent — the value may be an ignored
    # ``!include`` (None) or an unresolved ``!secret``, and neither
    # changes the verdict.
    if any(f in mqtt for f in _CLIENT_CERT_FIELDS):
        return CLIENT_CERT_UNSUPPORTED
    return _broker_from_block(mqtt, secrets_map, subs)


def _broker_from_block(
    raw_mqtt: dict, secrets_map: dict[str, Any], subs: dict[str, str]
) -> MqttBrokerConfig | None:
    """
    Resolve *raw_mqtt*'s broker fields and build an :class:`MqttBrokerConfig`.

    A declared ``certificate_authority`` that resolves to nothing or to
    non-PEM content fails the parse instead of degrading to plaintext.
    """
    mqtt = {
        k: _resolve_substitutions(_resolve(raw_mqtt.get(k), secrets_map), subs)
        for k in _BROKER_FIELDS
    }
    host = mqtt.get("broker")
    if not host:
        return None
    # An unresolved ``${var}`` / ``$var`` token would otherwise become a
    # bogus host and loop the monitor on DNS failure.
    if isinstance(host, str) and _UNRESOLVED_SUBSTITUTION_RE.search(host):
        return None
    certificate_authority = mqtt.get("certificate_authority") or None
    # esphome's ``certificate_authority`` is PEM content; a file path or
    # a corrupt cert would hand paho garbage and loop on SSLError forever.
    if "certificate_authority" in raw_mqtt and (
        certificate_authority is None
        or _PEM_CERT_MARKER not in certificate_authority
        or not _ca_pem_is_loadable(certificate_authority)
    ):
        return None
    # The YAML loader already resolves the boolean vocabulary
    # (true/yes/on/...), so anything non-bool is a typo or an
    # indirection this flag doesn't support — refuse, don't silently
    # keep hostname verification on for a user who mistyped the value.
    skip_cert_cn_check = raw_mqtt.get("skip_cert_cn_check", False)
    if not isinstance(skip_cert_cn_check, bool):
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
        certificate_authority=certificate_authority,
        skip_cert_cn_check=skip_cert_cn_check,
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


def _ca_pem_is_loadable(certificate_authority: str) -> bool:
    """
    Validate the CA at parse time so a corrupt PEM fails loudly.

    A cert that only fails inside the monitor's connect loop reads as
    an unreachable broker forever; refusing here routes it to the gated
    unresolved warning instead. Blocking (cert parsing), so callers run
    on the executor.
    """
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=certificate_authority)
    except (ssl.SSLError, TypeError, ValueError) as err:
        # ``load_verify_locations`` raises TypeError for non-ASCII cadata
        # and ValueError for empty data, not SSLError.
        # The gated unresolved warning is generic; keep the concrete
        # parse failure recoverable from the logs.
        _LOGGER.debug("certificate_authority failed to parse: %s", err)
        return False
    return True


def _resolve(value: Any, secrets_map: dict[str, Any]) -> str | None:
    """Return the resolved scalar value, or None when unresolvable."""
    if value is None:
        return None
    if isinstance(value, SecretRef):
        secret = secrets_map.get(value.name)
        if secret is None:
            _LOGGER.warning("Secret %r referenced by mqtt: block is not defined", value.name)
            return None
        return str(secret)
    if isinstance(value, (str, int, float)):
        return str(value)
    return None
