"""Config controller — settings, preferences, secrets, version, serial ports."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome import yaml_util
from esphome.const import __version__ as esphome_version
from esphome.core import CORE
from esphome.helpers import get_bool_env
from esphome.storage_json import StorageJSON, ext_storage_path
from esphome.util import get_serial_ports

from ..constants import DEFAULT_INGRESS_PORT
from ..constants import __version__ as server_version
from ..helpers.api import CommandError, api_command
from ..helpers.auth import hash_password
from ..helpers.json import JSONDecodeError, dumps_indent, loads
from ..models import ErrorCode, UserPreferences

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_SENTINEL_FILE = "___DASHBOARD_SENTINEL___.yaml"
_METADATA_FILE = ".device-builder.json"
_PREFS_KEY = "_preferences"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class DashboardSettings:
    """Application settings parsed from CLI args and environment."""

    config_dir: Path = field(default_factory=Path)
    absolute_config_dir: Path | None = None
    username: str = ""
    password_hash: bytes = field(default_factory=bytes)
    using_password: bool = False
    on_ha_addon: bool = False
    log_level: str = "info"
    port: int = 6052
    host: str = "0.0.0.0"
    ingress_port: int = DEFAULT_INGRESS_PORT
    ingress_host: str = ""
    # In dev mode the SPA shell is served with ``Cache-Control: no-cache``
    # so a re-deployed wheel isn't masked by a browser-cached
    # ``index.html`` pointing at a now-deleted hashed bundle. In
    # production we let the browser apply its default heuristic; the
    # hashed bundles are still served as ``immutable`` regardless.
    dev_mode: bool = False
    # Hostnames we trust for cross-origin / Host validation in the
    # WebSocket handshake. Carries the legacy
    # ``ESPHOME_TRUSTED_DOMAINS`` semantics from the upstream
    # dashboard, plus a DNS-rebinding-defense Host check:
    #
    #   * Origin allowlist - when the browser's Origin header
    #     doesn't match the request's Host (reverse-proxy hostname
    #     mismatch), accept the connection if Origin's hostname is
    #     in this list. Fixes the
    #     "lose-dashboard-access-behind-nginx" papercut.
    #   * Host allowlist - reject the request entirely if its Host
    #     header isn't in this list. Defense in depth against DNS
    #     rebinding, on top of the existing per-IP-rate-limited
    #     ``auth/login`` gate.
    #
    # Empty list = both checks disabled (existing strict
    # Origin/Host equality is the only gate; no Host allowlist).
    # ``"*"`` is the explicit "match anything" escape hatch for
    # operators who want to acknowledge the knob without
    # restricting hosts.
    trusted_domains: list[str] = field(default_factory=list)

    def parse_args(self, args: Any) -> None:
        """Parse CLI arguments into settings."""
        self.on_ha_addon = getattr(args, "ha_addon", False)
        username = getattr(args, "username", None) or os.getenv("USERNAME") or ""
        password = getattr(args, "password", None) or os.getenv("PASSWORD") or ""
        self.username = username
        self.using_password = bool(username and password)
        if self.using_password:
            self.password_hash = hash_password(password)
        self.config_dir = Path(args.configuration)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.absolute_config_dir = self.config_dir.resolve()
        # Ensure secrets.yaml exists (ESPHome fails if !secret references can't find it)
        secrets_path = self.config_dir / "secrets.yaml"
        if not secrets_path.exists():
            secrets_path.write_text(
                "# Secrets — referenced from device configs via !secret\n"
                "# Update these values for your network\n"
                'wifi_ssid: ""\n'
                'wifi_password: ""\n',
                encoding="utf-8",
            )
        self.log_level = getattr(args, "log_level", "info")
        self.port = getattr(args, "port", 6052)
        self.host = getattr(args, "host", "0.0.0.0")
        self.ingress_port = getattr(args, "ingress_port", DEFAULT_INGRESS_PORT)
        self.ingress_host = getattr(args, "ingress_host", "") or ""
        self.dev_mode = bool(getattr(args, "dev", False))
        # ``--trusted-domains a,b,c`` (or ``$ESPHOME_TRUSTED_DOMAINS``).
        # Comma-separated. Lower-cased for the case-insensitive match
        # in the WS handshake. Empty list = both Origin and Host
        # allowlists disabled.
        #
        # Precedence: a CLI flag value of ``None`` (argparse default
        # when ``--trusted-domains`` wasn't passed) means "flag not
        # set, consult the env var"; any string value, including the
        # empty string, is an explicit override and wins over the
        # env var. Lets operators say ``--trusted-domains ""`` to
        # disable the checks even when ``$ESPHOME_TRUSTED_DOMAINS``
        # is set in the environment (e.g. inherited from a parent).
        cli_value = getattr(args, "trusted_domains", None)
        raw_trusted = (
            cli_value if cli_value is not None else os.getenv("ESPHOME_TRUSTED_DOMAINS", "")
        )
        self.trusted_domains = [
            host.strip().lower() for host in raw_trusted.split(",") if host.strip()
        ]
        CORE.config_path = self.config_dir / _DASHBOARD_SENTINEL_FILE

    def rel_path(self, *parts: str) -> Path:
        """
        Return a path relative to the config dir, validated against path traversal.

        ``relative_to`` raises ``ValueError`` when ``parts`` resolve outside
        the config dir; we translate that into a ``CommandError`` so the
        WS dispatcher surfaces it as ``INVALID_ARGS`` instead of the
        generic ``INTERNAL_ERROR`` that an unclassified ``ValueError``
        would produce. Single chokepoint for every handler that builds
        a configuration path.
        """
        joined = self.config_dir.joinpath(*parts)
        assert self.absolute_config_dir is not None  # type narrowing
        try:
            joined.resolve().relative_to(self.absolute_config_dir)
        except ValueError as err:
            # ``!r`` quotes + escapes the offending value so embedded
            # CR/LF/control bytes can't break the error string when
            # the frontend echoes it back to the user. ``!r`` *first*,
            # then truncate, so the bound holds even for control-heavy
            # payloads (a single ``\x00`` repr's to 4 chars, so an
            # 80-byte raw value can otherwise blow past 200 chars).
            display = repr("/".join(parts))
            if len(display) > 100:
                display = f"{display[:97]}..."
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"Invalid configuration filename: {display}",
            ) from err
        return joined

    @property
    def status_use_mqtt(self) -> bool:
        return bool(get_bool_env("ESPHOME_DASHBOARD_USE_MQTT"))

    @property
    def create_ingress_site(self) -> bool:
        """Whether to bind the trusted HA Ingress TCP site alongside the public site."""
        if not self.on_ha_addon:
            return False
        # DISABLE_HA_AUTHENTICATION lets operators force ingress users
        # through the password-gated public port too.
        return not get_bool_env("DISABLE_HA_AUTHENTICATION")

    def check_password(self, username: str, password: str) -> bool:
        """
        Verify *username* and *password* in constant time.

        Returns ``False`` when no password is configured — check
        ``using_password`` separately to know whether the gate is active.
        """
        if not self.using_password:
            return False
        username_ok = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        password_ok = hmac.compare_digest(self.password_hash, hash_password(password))
        return username_ok and password_ok


# ---------------------------------------------------------------------------
# Metadata persistence (device-builder.json)
# ---------------------------------------------------------------------------

# Several controllers (firmware queue, device CRUD, preferences, IP
# cache) all RMW this file from the executor pool. Without serialisation
# two writers landing in the same window lose each other's updates.
_METADATA_LOCK = threading.Lock()


@contextmanager
def metadata_transaction(config_dir: Path) -> Iterator[dict[str, Any]]:
    """
    Atomic read-modify-write context for the metadata sidecar.

    Yields the current metadata dict. Mutate it in place; on a clean
    exit the changes are persisted atomically. Exceptions raised
    inside the block discard the pending mutation. Concurrent
    transactions are serialised so updates can't clobber each other.
    """
    with _METADATA_LOCK:
        data = _load_metadata(config_dir)
        yield data
        _save_metadata(config_dir, data)


def _load_metadata(config_dir: Path) -> dict[str, Any]:
    path = config_dir / _METADATA_FILE
    try:
        # orjson decodes bytes directly, so skip the read_text → encode
        # round-trip. JSONDecodeError is a subclass of ValueError.
        data = loads(path.read_bytes())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, JSONDecodeError):
        return {}


def _save_metadata(config_dir: Path, data: dict[str, Any]) -> None:
    path = config_dir / _METADATA_FILE
    # tempfile + os.replace so lock-free readers never observe a partial write.
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_METADATA_FILE}.", suffix=".tmp", dir=str(config_dir))
    tmp_path = Path(tmp_name)
    try:
        # ``dumps_indent`` yields bytes, so open the temp file in
        # binary mode. The on-disk file stays readable / diffable.
        with os.fdopen(fd, "wb") as fh:
            fh.write(dumps_indent(data))
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            tmp_path.unlink()
        raise


def get_board_id(config_dir: Path, filename: str) -> str:
    """Get the board_id for a device."""
    return str(_load_metadata(config_dir).get(filename, {}).get("board_id", ""))


def set_device_metadata(
    config_dir: Path,
    filename: str,
    *,
    board_id: str | None = None,
    friendly_name: str | None = None,
    comment: str | None = None,
    ip: str | None = None,
    expected_config_hash: str | None = None,
) -> None:
    """
    Set metadata fields for a device.

    ``ip`` is the last-known resolved IP — persisted so the address
    cache survives backend restarts. Pass an empty string to leave the
    persisted value unchanged (mDNS clears the in-memory IP whenever a
    device drops off the network, but the cache is still useful).

    ``expected_config_hash`` is the 8-char hex FNV-1a-32 hash of the
    YAML as last successfully compiled — pair it with the mDNS
    ``config_hash`` TXT record (esphome/esphome#16145) to tell whether
    the running firmware matches the compiled config. Passing an empty
    string clears it (e.g. after a YAML edit invalidates the prior
    compile).
    """
    with metadata_transaction(config_dir) as data:
        entry = data.setdefault(filename, {})
        if board_id is not None:
            entry["board_id"] = board_id
        if friendly_name is not None:
            entry["friendly_name"] = friendly_name
        if comment is not None:
            entry["comment"] = comment
        if ip:
            entry["ip"] = ip
        if expected_config_hash is not None:
            if expected_config_hash:
                entry["expected_config_hash"] = expected_config_hash
            else:
                entry.pop("expected_config_hash", None)


def get_device_metadata(config_dir: Path, filename: str) -> dict[str, Any]:
    """Get all metadata for a device."""
    result = _load_metadata(config_dir).get(filename, {})
    return result if isinstance(result, dict) else {}


def get_device_ip(config_dir: Path, filename: str) -> str:
    """Return the last-known resolved IP for a device, or ``""`` if unknown."""
    return str(_load_metadata(config_dir).get(filename, {}).get("ip", ""))


def remove_device_metadata(config_dir: Path, filename: str) -> None:
    """Remove metadata for a device."""
    with metadata_transaction(config_dir) as data:
        data.pop(filename, None)


# Fields on a device-metadata entry whose meaning is tied to the
# *running* firmware / network state, not to the YAML's identity.
# Used by ``clear_volatile_device_metadata`` (and its caller in
# the archive flow) to scrub these on archive while preserving
# the stable identity fields. New volatile fields added here
# must also be added to ``set_device_metadata`` (and any
# accessors) so the cleared / un-cleared shapes stay aligned.
_VOLATILE_DEVICE_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        # Last resolved IP — meaningless once the device is no
        # longer on the network.
        "ip",
        # Last successfully-compiled config hash. Pairs with the
        # mDNS ``config_hash`` TXT record to detect "running
        # firmware out of sync with YAML"; on archive the build
        # dir is wiped so the hash no longer corresponds to any
        # available artifact and would be misleading on the next
        # compile.
        "expected_config_hash",
    }
)


def clear_volatile_device_metadata(config_dir: Path, filename: str) -> None:
    """Drop runtime / observed state fields, keep stable identity fields.

    On archive the dashboard removes the YAML's compile output
    and the StorageJSON sidecar (both are build artifacts), but
    the device-metadata entry carries a mix of:

    - Stable identity fields (``board_id``, ``friendly_name``,
      ``comment``) — set by the user or derived from the YAML
      itself, still meaningful on unarchive.
    - Volatile fields (``ip``, ``expected_config_hash``) —
      describe the firmware / network state at archive time and
      go stale immediately.

    The earlier shape removed the entire entry on archive, which
    closed the "future same-name device inherits stale state"
    risk but also lost the identity fields. The catalog → YAML
    match key is ``board_id``; losing it on every archive →
    unarchive cycle forced a re-derive (or a re-pick by the
    user) that wasn't necessary. This helper preserves identity
    + clears volatile so unarchive restores the user-visible
    state unchanged. Same-name new-device leakage of identity
    fields is acceptable: the new device's create flow either
    derives or supplies its own ``board_id``, and friendly_name
    / comment are user labels the new device's editor can
    overwrite if desired.
    """
    with metadata_transaction(config_dir) as data:
        entry = data.get(filename)
        if entry is None:
            return
        if not isinstance(entry, dict):
            # Treat a non-dict value as corrupt — leaving it in place
            # would later break ``set_device_metadata`` (which assumes
            # the existing entry is a dict and item-assigns into it).
            # Drop the bad value so the next write starts from a
            # clean shape.
            data.pop(filename, None)
            return
        for field_name in _VOLATILE_DEVICE_METADATA_FIELDS:
            entry.pop(field_name, None)
        # If the entry is now empty (no identity fields ever
        # set) drop it entirely so we don't leave dead keys
        # behind in the metadata file.
        if not entry:
            data.pop(filename, None)


def load_preferences(config_dir: Path) -> UserPreferences:
    """Load user preferences, returning defaults for missing fields."""
    raw = _load_metadata(config_dir).get(_PREFS_KEY, {})
    try:
        return UserPreferences.from_dict(raw)
    except Exception:
        return UserPreferences()


def save_preferences(config_dir: Path, prefs: UserPreferences) -> None:
    """Save user preferences to disk."""
    with metadata_transaction(config_dir) as data:
        data[_PREFS_KEY] = prefs.to_dict()


# ---------------------------------------------------------------------------
# ConfigController
# ---------------------------------------------------------------------------


class ConfigController:
    """Manages application configuration, preferences, and system info."""

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder

    @api_command("config/version")
    async def get_version(self, **kwargs: Any) -> dict:
        """Get ESPHome and server version."""
        return {"server_version": server_version, "esphome_version": esphome_version}

    @api_command("config/serial_ports")
    async def get_serial_ports_cmd(self, **kwargs: Any) -> list[dict]:
        """List available serial ports."""
        loop = asyncio.get_running_loop()
        ports = await loop.run_in_executor(None, get_serial_ports)
        return [
            {"port": p.path, "desc": p.description if p.description != "n/a" else p.path}
            for p in ports
        ]

    @api_command("config/get_preferences")
    async def get_prefs(self, **kwargs: Any) -> UserPreferences:
        """Get user preferences."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, load_preferences, self._db.settings.config_dir)

    @api_command("config/set_preferences")
    async def set_prefs(self, **kwargs: Any) -> UserPreferences:
        """Update user preferences.

        Accepts partial updates — only provided fields are changed,
        others keep their current values.
        """
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        # Load current, merge with provided fields, validate, save
        current = await loop.run_in_executor(None, load_preferences, config_dir)
        update_fields = {k: v for k, v in kwargs.items() if k not in ("client", "message_id")}

        # Merge into current preferences
        current_dict = current.to_dict()
        current_dict.update(update_fields)
        updated = UserPreferences.from_dict(current_dict)

        await loop.run_in_executor(None, save_preferences, config_dir, updated)
        return updated

    @api_command("config/get_secrets")
    async def get_secrets(self, **kwargs: Any) -> list[str]:
        """Get secret key names from secrets.yaml."""
        loop = asyncio.get_running_loop()

        def _read_secrets() -> list[str]:
            secrets_path = self._db.settings.config_dir / "secrets.yaml"
            if not secrets_path.exists():
                return []
            try:
                # ``yaml_util.load_yaml`` expects a ``Path``, not a
                # ``str`` — passing a string raises ``AttributeError``
                # at ``fname.open(...)`` and the bare except below
                # would silently swallow it, leaving the secrets
                # dropdown permanently empty.
                data = yaml_util.load_yaml(secrets_path)
                return sorted(data.keys()) if isinstance(data, dict) else []
            except Exception:
                return []

        return await loop.run_in_executor(None, _read_secrets)

    @api_command("config/get_info")
    async def get_info(self, *, configuration: str, **kwargs: Any) -> dict | None:
        """Get compiled device metadata (StorageJSON) for a configuration."""
        loop = asyncio.get_running_loop()

        def _load_info() -> dict | None:
            # ``rel_path`` calls ``Path.resolve`` (an ``os.path.abspath``
            # syscall under the hood) and the StorageJSON load below
            # opens the sidecar from disk — both block the event loop
            # if run inline. Do them together inside the executor so
            # a slow filesystem (NFS-mounted config dir, EBS-backed
            # Docker volume) can't stall the dashboard. ``rel_path``
            # raises ``CommandError`` on traversal; the awaited future
            # propagates that out to the WS dispatcher unchanged.
            self._db.settings.rel_path(configuration)
            storage = StorageJSON.load(ext_storage_path(configuration))
            if storage is None:
                return None
            return {
                "name": storage.name,
                "friendly_name": storage.friendly_name,
                "comment": storage.comment,
                "address": storage.address,
                "web_port": storage.web_port,
                "target_platform": storage.target_platform,
                "current_version": storage.esphome_version,
                "deployed_version": storage.firmware_bin_path,
                "loaded_integrations": storage.loaded_integrations,
            }

        return await loop.run_in_executor(None, _load_info)
