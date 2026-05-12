"""Config controller — settings, preferences, secrets, version, serial ports."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.const import __version__ as esphome_version
from esphome.core import CORE
from esphome.helpers import get_bool_env
from esphome.helpers import write_file as atomic_write_file
from esphome.storage_json import StorageJSON
from esphome.util import get_serial_ports

from ..constants import DEFAULT_INGRESS_PORT, DEFAULT_REMOTE_BUILD_PORT
from ..constants import __version__ as server_version
from ..helpers.api import CommandError, api_command
from ..helpers.auth import hash_password
from ..helpers.json import JSONDecodeError, dumps_indent, loads
from ..helpers.secrets_state import (
    PLACEHOLDER_WIFI_PASSWORD,
    PLACEHOLDER_WIFI_SSID,
    read_secrets_yaml,
)
from ..helpers.storage_path import resolve_storage_path
from ..helpers.subprocess import run_subprocess_capture
from ..models import (
    ErrorCode,
    Label,
    RemoteBuildSettings,
    UserPreferences,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

_DASHBOARD_SENTINEL_FILE = "___DASHBOARD_SENTINEL___.yaml"
_METADATA_FILE = ".device-builder.json"
_PREFS_KEY = "_preferences"
_LABELS_KEY = "_labels"
_REMOTE_BUILD_KEY = "_remote_build"


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
    # Plain-TCP port for the remote-build peer-link receiver site
    # (issue #106). The transport is Noise XX over plain HTTP/WS,
    # not TLS — Noise provides mutual auth + forward secrecy +
    # confidentiality at the application layer. The
    # site is only bound when ``RemoteBuildSettings.enabled`` is set;
    # default-off keeps the listener inactive on installs that
    # haven't opted in. Lives separately from ``port`` because the
    # peer-link's auth gate (Noise + pre-shared pin pairing) is
    # independent from the dashboard's WS gate (loopback / login).
    remote_build_port: int = DEFAULT_REMOTE_BUILD_PORT
    # Bind address for the remote-build peer-link receiver. Defaults
    # to all interfaces — the feature's whole point is letting paired
    # peers on the LAN reach this dashboard, and the security gate is
    # Noise + pre-shared pin (not bind address). Lives separately from
    # ``host`` because the HTTP/WS dashboard often binds to
    # ``127.0.0.1`` (desktop app loopback security model) while the
    # peer-link still needs to be LAN-reachable. Operators who want
    # to lock the receiver to a specific NIC can override via
    # ``--remote-build-host`` / ``$ESPHOME_REMOTE_BUILD_HOST``. Accepts
    # an IP literal or a local interface name (e.g. ``eth0``); the
    # latter is resolved at bind time to every IPv4 / IPv6 address
    # on the interface (see :func:`helpers.network_interfaces.resolve_bind_host`).
    remote_build_host: str = "0.0.0.0"
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
        # Env-var fallback uses ``ESPHOME_*`` rather than the legacy
        # dashboard's bare ``USERNAME`` / ``PASSWORD``: the bare names
        # collide with login-shell / Windows system vars (``$USERNAME``
        # is the OS user on both), which would silently promote the
        # OS user to the dashboard username when only ``--password``
        # / ``$ESPHOME_PASSWORD`` is set. Intentional divergence from
        # ``esphome/dashboard/settings.py``.
        username = getattr(args, "username", None) or os.getenv("ESPHOME_USERNAME") or ""
        password = getattr(args, "password", None) or os.getenv("ESPHOME_PASSWORD") or ""
        self.username = username
        self.using_password = bool(username and password)
        if self.using_password:
            self.password_hash = hash_password(password)
        self.config_dir = Path(args.configuration)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.absolute_config_dir = self.config_dir.resolve()
        # Ensure secrets.yaml exists (ESPHome fails if !secret references
        # can't find it). Atomic write — a crash mid-write would leave the
        # user with a half-bootstrap'd secrets file and the next startup
        # would see ``not exists() == False`` on the partial and skip
        # this branch, leaving them stuck. ``write_file`` stages in a
        # sibling tempfile + ``shutil.move`` so the file is either fully
        # there or not at all.
        #
        # Use non-empty placeholder strings rather than ``""``: ESPHome's
        # ``wifi`` validator rejects an empty SSID with
        # "SSID can't be empty.", so a fresh-install ``create_device``
        # whose generated YAML uses ``!secret wifi_ssid`` would
        # validation-fail before the device is even saved
        # ("Failed to create device: SSID can't be empty."). The
        # placeholders validate clean and clearly signal to the user
        # that the values need to be replaced before flashing —
        # ``OnboardingController`` reads the same constants from
        # ``helpers.secrets_state`` to detect the unconfigured state
        # and surface the setup wizard.
        secrets_path = self.config_dir / "secrets.yaml"
        if not secrets_path.exists():
            atomic_write_file(
                secrets_path,
                "# Secrets — referenced from device configs via !secret\n"
                "# Replace these placeholders with your real Wi-Fi\n"
                "# credentials before flashing or installing OTA.\n"
                f'wifi_ssid: "{PLACEHOLDER_WIFI_SSID}"\n'
                f'wifi_password: "{PLACEHOLDER_WIFI_PASSWORD}"\n',
            )
        self.log_level = getattr(args, "log_level", "info")
        self.port = getattr(args, "port", 6052)
        self.host = getattr(args, "host", "0.0.0.0")
        self.ingress_port = getattr(args, "ingress_port", DEFAULT_INGRESS_PORT)
        self.ingress_host = getattr(args, "ingress_host", "") or ""
        # ``--remote-build-port`` (or ``$ESPHOME_REMOTE_BUILD_PORT``).
        # Precedence mirrors ``--trusted-domains`` below: an explicit
        # CLI value (including the default) wins; ``None`` means
        # "flag not set, consult the env var". Container deployments
        # that fix the CMD in the Dockerfile and override via env
        # can flip the listener port without rebuilding the image.
        cli_remote_build_port = getattr(args, "remote_build_port", None)
        if cli_remote_build_port is not None:
            self.remote_build_port = cli_remote_build_port
        else:
            env_remote_build_port = os.getenv("ESPHOME_REMOTE_BUILD_PORT", "")
            try:
                self.remote_build_port = (
                    int(env_remote_build_port)
                    if env_remote_build_port
                    else DEFAULT_REMOTE_BUILD_PORT
                )
            except ValueError:
                _LOGGER.warning(
                    "Invalid ESPHOME_REMOTE_BUILD_PORT=%r; falling back to %d",
                    env_remote_build_port,
                    DEFAULT_REMOTE_BUILD_PORT,
                )
                self.remote_build_port = DEFAULT_REMOTE_BUILD_PORT
        # ``--remote-build-host`` (or ``$ESPHOME_REMOTE_BUILD_HOST``).
        # Same precedence pattern as the port: an explicit CLI value
        # wins; absence (or empty / whitespace-only) means "consult
        # the env var, then fall back to the default". Empty-string
        # falls through to the env var rather than passing ``""`` to
        # ``TCPSite`` — aiohttp would translate that to a low-level
        # ``getaddrinfo`` failure with a cryptic error rather than
        # the obvious "0.0.0.0 default". The default is ``0.0.0.0``
        # (all interfaces) — binding the peer-link receiver to the
        # same interface as the HTTP dashboard would break the
        # desktop-app shape, where ``--host 127.0.0.1`` is the
        # dashboard's security boundary but the peer-link still needs
        # to be LAN-reachable so paired peers can actually dial the
        # IPs the mDNS announce broadcasts.
        cli_remote_build_host_raw = getattr(args, "remote_build_host", None)
        cli_remote_build_host = (
            cli_remote_build_host_raw.strip()
            if isinstance(cli_remote_build_host_raw, str)
            else None
        )
        if cli_remote_build_host:
            self.remote_build_host = cli_remote_build_host
        else:
            env_remote_build_host = os.getenv("ESPHOME_REMOTE_BUILD_HOST", "").strip()
            self.remote_build_host = env_remote_build_host or "0.0.0.0"
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
# Plain (non-reentrant) ``Lock`` is intentional: nested
# ``metadata_transaction`` calls on the same thread are unsafe even
# under an ``RLock`` because each call does its own load/save, so
# the inner write is overwritten by the outer write at the outer's
# exit. The deadlock on attempted re-entry is the loud failure;
# silently losing updates would be worse. See the docstring below.
_METADATA_LOCK = threading.Lock()


@contextmanager
def metadata_transaction(config_dir: Path) -> Iterator[dict[str, Any]]:
    """
    Atomic read-modify-write context for the metadata sidecar.

    Yields the current metadata dict. Mutate it in place; on a clean
    exit the changes are persisted atomically. Exceptions raised
    inside the block discard the pending mutation. Concurrent
    transactions are serialised so updates can't clobber each other.

    Do not call from inside another ``metadata_transaction`` on the
    same thread. The lock is non-reentrant and will deadlock; this
    is intentional. Each call loads / saves its own snapshot, so
    nested calls would lose updates even under a reentrant lock
    (the outer save would overwrite the inner save). Helpers that
    take the same lock (e.g. ``get_or_create_identity``) must be
    called outside any open transaction.
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
    # tempfile + Path.replace so lock-free readers never observe a partial write.
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_METADATA_FILE}.", suffix=".tmp", dir=str(config_dir))
    tmp_path = Path(tmp_name)
    try:
        # ``dumps_indent`` yields bytes, so open the temp file in
        # binary mode. The on-disk file stays readable / diffable.
        with os.fdopen(fd, "wb") as fh:
            fh.write(dumps_indent(data))
        tmp_path.replace(path)
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
    mac_address: str | None = None,
    regen_failed_mtime: float | None = None,
    regen_failed_at: float | None = None,
    build_size_bytes: int | None = None,
    build_size_dir_mtime: int | None = None,
    build_size_info_mtime: int | None = None,
    labels: list[str] | None = None,
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

    ``mac_address`` is the canonical ``XX:XX:XX:XX:XX:XX`` MAC
    from the mDNS ``mac`` TXT record (normalized at ingest).
    Persisted so the dashboard renders the address immediately on
    startup, before the first mDNS probe response. Passing an
    empty string clears it.

    ``regen_failed_mtime`` is the YAML's mtime when the last
    ``--only-generate`` storage-regen attempt failed; pair it with
    ``regen_failed_at`` (the wall-clock time the failure was
    recorded). Together they let a backend restart skip retrying
    the same broken config (missing ``!secret`` / ``!include`` /
    unreachable git package) — the next attempt only runs when
    the YAML's mtime has actually moved past the cached stamp,
    OR when the cached stamp is older than the controller's
    failure-TTL (so transient external problems eventually get
    re-checked). The two fields are written together by
    :meth:`DevicesController._stamp_regen_failure`; the
    success / archive paths clear them by passing ``0.0`` to
    *both* — clearing only one half leaves the other behind, so
    callers should always touch the pair as a unit.

    ``build_size_bytes`` caches the total size of the per-device
    ``.esphome/build/<name>/`` tree at the freshness pair
    captured by the last walk. The pair is split because each
    half catches a class of compile-time changes the other
    misses: ``build_size_dir_mtime`` moves on entry-set churn
    (PlatformIO atomic-replaces, sibling add/remove),
    ``build_size_info_mtime`` moves on every real ESPHome
    recompile (``write_file_if_changed`` rewrites
    ``build_info.json``). Either side moving counts as stale,
    so a freshly-restarted dashboard re-walks any device whose
    pair drifted from what was persisted. Pass ``0`` for any
    field to clear (used by the archive flow's volatile-field
    scrub).

    ``labels`` is the list of label IDs assigned to this device
    (opaque ``uuid.uuid4().hex`` references into the global
    ``_labels`` catalog). ``None`` leaves the persisted list
    alone; ``[]`` clears it (drops the key entirely so empty
    entries don't bloat the file); a populated list replaces
    the assignments wholesale.
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
        if labels is not None:
            if labels:
                entry["labels"] = list(labels)
            else:
                entry.pop("labels", None)
        # Tri-state fields: ``None`` means "leave alone", a truthy
        # value writes, an explicit falsy (``""`` / ``0``) clears.
        # The numeric stamps below (``regen_failed_*`` /
        # ``build_size_*``) all carry timestamps or sizes whose
        # legitimate values are strictly positive — ``0`` is
        # therefore safe as the explicit-clear sentinel.
        # Loop over the (key, value) pairs so adding a new
        # tri-state field doesn't bump this function's branch
        # count (ruff PLR0912 caps at 12).
        for key, value in (
            ("expected_config_hash", expected_config_hash),
            ("mac_address", mac_address),
            ("regen_failed_mtime", regen_failed_mtime),
            ("regen_failed_at", regen_failed_at),
            ("build_size_bytes", build_size_bytes),
            ("build_size_dir_mtime", build_size_dir_mtime),
            ("build_size_info_mtime", build_size_info_mtime),
        ):
            if value is None:
                continue
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)


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
        # Last MAC observed via mDNS. Stable per device but tied
        # to the running firmware — on archive the YAML may later
        # be redeployed to a different physical board, and a
        # stale persisted MAC would render until the next mDNS
        # announce overwrote it.
        "mac_address",
        # YAML mtime + wall-clock timestamp at the last failed
        # storage-regen attempt. Archive moves the YAML; a future
        # unarchive may put it back with a fresh mtime, so any
        # cached failure stamp would be meaningless. Cleared so
        # the next scan retries the regen.
        "regen_failed_mtime",
        "regen_failed_at",
        # Cached size of the per-device build directory at the
        # freshness pair captured alongside it. Archive wipes the
        # build tree (``_wipe_device_build_dir``) so the cached
        # triple would describe a directory that no longer
        # exists.
        "build_size_bytes",
        "build_size_dir_mtime",
        "build_size_info_mtime",
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
    except (ValueError, TypeError, LookupError):
        # mashumaro runtime-shape errors → defaults. Hand-edited
        # sidecar with a malformed prefs blob shouldn't take the
        # whole dashboard down.
        return UserPreferences()


def save_preferences(config_dir: Path, prefs: UserPreferences) -> None:
    """Save user preferences to disk."""
    with metadata_transaction(config_dir) as data:
        data[_PREFS_KEY] = prefs.to_dict()


_REMOTE_BUILD_FAIL_SAFE = RemoteBuildSettings(enabled=False)


def _settings_from_raw(raw: Any) -> RemoteBuildSettings:
    """
    Decode a ``_remote_build`` blob, failing safe on shape mismatch.

    With the default ``RemoteBuildSettings.enabled=True``, a
    malformed blob can no longer silently fall through to
    "defaults" — that path would enable the listener on a
    corrupted sidecar without any operator opt-in. Instead:

    * A non-dict raw value (None / list / scalar from a hand-
      edit or partial-write) returns ``enabled=False``.
    * A dict that fails ``from_dict`` decode (schema break,
      type-incompatible value on a known field) returns
      ``enabled=False``.

    Both paths log a warning at the call site so the operator
    can spot the corrupted sidecar.

    Legacy ``tokens`` / ``manual_hosts`` / ``peers`` entries
    on older ``.device-builder.json`` files are silently
    dropped — mashumaro's ``DataClassORJSONMixin`` ignores
    unknown keys by default. The ``tokens`` field went with
    the pre-Noise bearer machinery; ``manual_hosts`` was
    removed once the pair dialog started typing hostnames
    straight into ``request_pair``; ``peers`` moved to its
    own per-file ``Store`` at ``.receiver_peers.json``.
    """
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Malformed ``_remote_build`` block in metadata "
            "(expected dict, got %s); failing safe to enabled=False. "
            "Fix or remove the block to recover default behaviour.",
            type(raw).__name__,
        )
        return _REMOTE_BUILD_FAIL_SAFE
    # Drop the legacy ``tokens`` key explicitly so a corrupt
    # token row in an older sidecar can't crash the whole
    # ``from_dict`` decode of an otherwise-valid blob.
    cleaned = {k: v for k, v in raw.items() if k != "tokens"}
    try:
        return RemoteBuildSettings.from_dict(cleaned)
    except Exception:
        _LOGGER.exception(
            "Failed to decode ``_remote_build`` block in metadata; "
            "failing safe to enabled=False. Fix or remove the block "
            "to recover default behaviour."
        )
        return _REMOTE_BUILD_FAIL_SAFE


def load_remote_build_settings(config_dir: Path) -> RemoteBuildSettings:
    """
    Load the receiver-side remote-build settings.

    Returns defaults (``RemoteBuildSettings()``, i.e.
    ``enabled=True``) when the metadata file is missing or the
    ``_remote_build`` key isn't present (fresh install). A
    present-but-malformed block fails safe to
    ``enabled=False`` rather than silently inheriting the
    permissive default — see :func:`_settings_from_raw` for
    the corruption-path rationale.

    HA-addon callers that need to suppress the auto-bind on a
    fresh install should pair this with
    :func:`has_remote_build_settings_persisted` and gate
    accordingly — the load function returns the dataclass
    semantically; the deployment-mode rule lives at the bind
    site so the toggle's "operator opted in" signal isn't
    lost.
    """
    metadata = _load_metadata(config_dir)
    if _REMOTE_BUILD_KEY not in metadata:
        return RemoteBuildSettings()
    return _settings_from_raw(metadata[_REMOTE_BUILD_KEY])


def has_remote_build_settings_persisted(config_dir: Path) -> bool:
    """
    Return ``True`` when ``_remote_build`` has been explicitly written.

    Distinguishes "fresh install, never touched the toggle"
    (returns ``False``) from "operator deliberately set a value,
    even if that value matches the dataclass default" (returns
    ``True``). The HA-addon default-off rule keys on this so a
    fresh addon install doesn't bind port 6055 (the container
    doesn't expose it anyway) but an operator who flips the
    toggle in Settings still gets the receiver bound regardless
    of deployment mode.

    The block must also have the expected on-disk shape (a
    dict). A malformed ``_remote_build`` value (list, scalar,
    null) doesn't count as opt-in — ``set_settings`` writes
    ``RemoteBuildSettings.to_dict()`` which is always a dict,
    so any non-dict value reached the sidecar via a hand-edit
    or partial-write, not an operator interaction with the
    toggle. Returning ``False`` for that shape keeps the
    HA-addon gate consistent with the fail-safe shape in
    :func:`_settings_from_raw`.
    """
    return isinstance(_load_metadata(config_dir).get(_REMOTE_BUILD_KEY), dict)


def save_remote_build_settings(config_dir: Path, settings: RemoteBuildSettings) -> None:
    """Persist the receiver-side remote-build settings."""
    with metadata_transaction(config_dir) as data:
        data[_REMOTE_BUILD_KEY] = settings.to_dict()


@contextmanager
def remote_build_settings_transaction(
    config_dir: Path,
) -> Iterator[RemoteBuildSettings]:
    """
    Atomic read-modify-write context for the remote-build settings.

    Yields the current :class:`RemoteBuildSettings` (defaults if
    missing or corrupt). Mutate it in place; on a clean exit the
    changes are persisted under the same ``metadata_transaction``
    lock, so the whole RMW is atomic against concurrent
    transactions. Exceptions raised inside the block discard the
    pending mutation.

    Use this whenever an operation "depends on the current state
    to compute the next state": add / remove a manual host, flip
    ``enabled`` while preserving the rest. A bare ``load + save``
    pair is racy because two concurrent callers can both read the
    same starting value and the second save wipes the first's
    change.
    """
    with metadata_transaction(config_dir) as data:
        settings = _settings_from_raw(data.get(_REMOTE_BUILD_KEY, {}))
        yield settings
        data[_REMOTE_BUILD_KEY] = settings.to_dict()


def _decode_labels(raw: Any) -> list[Label]:
    """Parse the on-disk ``_labels`` list, dropping malformed entries."""
    if not isinstance(raw, list):
        return []
    out: list[Label] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(Label.from_dict(entry))
        except (ValueError, TypeError, LookupError) as err:
            # A hand-edited sidecar that landed a malformed entry
            # shouldn't take the whole catalog down — labels are
            # advisory. Debug-log so a developer hunting "why did
            # my label disappear?" can find a paper trail without
            # noisy WARN-level chatter on every load.
            _LOGGER.debug("Skipping malformed label entry %r: %s", entry, err)
    return out


def load_labels(config_dir: Path) -> list[Label]:
    """
    Load the global label catalog.

    Returns an empty list when the ``_labels`` key is missing or
    corrupt. Individual entries that fail to round-trip through
    :class:`Label` are skipped silently so a single bad entry can't
    take the whole catalog down — labels are advisory metadata, not
    load-bearing state.
    """
    return _decode_labels(_load_metadata(config_dir).get(_LABELS_KEY, []))


def save_labels(config_dir: Path, labels: list[Label]) -> None:
    """Replace the global label catalog atomically."""
    with metadata_transaction(config_dir) as data:
        data[_LABELS_KEY] = [label.to_dict() for label in labels]


@contextmanager
def labels_transaction(config_dir: Path) -> Iterator[list[Label]]:
    """
    Atomic read-modify-write context for the global label catalog.

    Yields a mutable list of :class:`Label` instances decoded from the
    ``_labels`` key. Mutate the list in place; on a clean exit the
    catalog is re-encoded and persisted atomically alongside the rest
    of the metadata file. Exceptions raised inside the block discard
    the pending mutation. Use when you need uniqueness / existence
    checks and the write to share a single transaction — the validate
    happens inside the lock so a concurrent writer can't slip in
    between.
    """
    with metadata_transaction(config_dir) as data:
        catalog = _decode_labels(data.get(_LABELS_KEY))
        yield catalog
        data[_LABELS_KEY] = [label.to_dict() for label in catalog]


def set_device_labels(config_dir: Path, configuration: str, label_ids: list[str]) -> None:
    """
    Replace a device's label assignments atomically.

    Validates *label_ids* against the live catalog inside the same
    metadata transaction as the write so a concurrent
    ``labels/delete`` cascade can't leave the device with a
    dangling reference. ``label_ids`` is treated as a set in
    semantics — duplicate IDs in the input are deduplicated while
    preserving first-seen order. Pass ``[]`` to clear all
    assignments. Raises :class:`ValueError` for non-string entries
    in *label_ids* and for ids that aren't in the catalog (caller
    translates to ``CommandError(INVALID_ARGS)`` at the API surface).
    """
    deduped: list[str] = []
    seen: set[str] = set()
    for lid in label_ids:
        if not isinstance(lid, str):
            # Silent skipping would let a payload of all-bad types
            # become an effective ``[]`` (clear-all) write — surprising
            # and user-hostile. Surface a clear error instead so the
            # frontend can fix the payload.
            raise TypeError(f"label_ids must be strings, got {type(lid).__name__}: {lid!r}")
        if lid in seen:
            continue
        deduped.append(lid)
        seen.add(lid)
    with metadata_transaction(config_dir) as data:
        catalog = data.get(_LABELS_KEY, [])
        if isinstance(catalog, list):
            known = {
                entry["id"]
                for entry in catalog
                if isinstance(entry, dict) and isinstance(entry.get("id"), str)
            }
        else:
            known = set()
        unknown = [lid for lid in deduped if lid not in known]
        if unknown:
            raise ValueError(f"Unknown label id(s): {', '.join(repr(u) for u in unknown)}")
        entry = data.setdefault(configuration, {})
        if not isinstance(entry, dict):
            # A non-dict entry shouldn't survive long — overwrite to
            # restore the expected shape rather than crash here.
            entry = {}
            data[configuration] = entry
        if deduped:
            entry["labels"] = deduped
        else:
            entry.pop("labels", None)


def delete_label_cascade(config_dir: Path, label_id: str) -> tuple[bool, set[str]]:
    """
    Drop *label_id* from the catalog and every device entry.

    Performed inside a single ``metadata_transaction`` so the
    existence check, catalog removal, and per-device cleanup all
    share one lock — a concurrent writer can't reintroduce the
    deleted ID, and the existence check works against the *raw*
    on-disk dict so a corrupt catalog entry (one that
    ``Label.from_dict`` would skip) is still removable.

    Returns a tuple ``(found, affected)`` where *found* is ``True``
    when the catalog actually contained an entry with this id (so
    the caller can raise ``NOT_FOUND`` otherwise) and *affected* is
    the set of configuration filenames whose ``labels`` list
    changed — callers use this to schedule a per-device scanner
    reload so live ``Device`` objects pick up the cleaned state
    without having to wait for the next disk-driven scan.
    """
    affected: set[str] = set()
    found = False
    with metadata_transaction(config_dir) as data:
        existing = data.get(_LABELS_KEY)
        if isinstance(existing, list):
            new_catalog: list[Any] = []
            for entry in existing:
                if isinstance(entry, dict) and entry.get("id") == label_id:
                    found = True
                    continue
                new_catalog.append(entry)
            if found:
                data[_LABELS_KEY] = new_catalog
        if not found:
            return False, set()
        for filename, entry in data.items():
            if filename.startswith("_") or not isinstance(entry, dict):
                continue
            current = entry.get("labels")
            if not isinstance(current, list) or label_id not in current:
                continue
            new = [lid for lid in current if lid != label_id]
            if new:
                entry["labels"] = new
            else:
                entry.pop("labels", None)
            affected.add(filename)
    return True, affected


# ---------------------------------------------------------------------------
# Chip / variant detection helpers (config/detect_chip)
# ---------------------------------------------------------------------------

# Maps esptool's chip family string (lower-cased) to
# ``(chip_family, variant, platform)``. ``chip_family`` matches a
# ``WIZARD_BOARD_PLATFORMS.label`` value in the frontend so callers
# can hand it straight to the board-picker filter; ``variant`` and
# ``platform`` mirror ESPHome's own keys. Families not in this
# table cause ``_chip_family_to_descriptor`` to return ``None``,
# which the WS handler surfaces as ``_DETECT_UNKNOWN_CHIP``.
#
# esptool can only identify ESP chips. Non-ESP platforms (RP2040 /
# RP2350, BK72xx, RTL87xx, LN882x, nRF52) need their own probe path;
# they're not in this table.
_CHIP_FAMILY_MAP: dict[str, tuple[str, str, str]] = {
    "esp32": ("ESP32", "esp32", "esp32"),
    "esp32-s2": ("ESP32-S2", "esp32s2", "esp32"),
    "esp32-s3": ("ESP32-S3", "esp32s3", "esp32"),
    "esp32-c2": ("ESP32-C2", "esp32c2", "esp32"),
    "esp32-c3": ("ESP32-C3", "esp32c3", "esp32"),
    "esp32-c5": ("ESP32-C5", "esp32c5", "esp32"),
    "esp32-c6": ("ESP32-C6", "esp32c6", "esp32"),
    "esp32-c61": ("ESP32-C61", "esp32c61", "esp32"),
    "esp32-h2": ("ESP32-H2", "esp32h2", "esp32"),
    "esp32-p4": ("ESP32-P4", "esp32p4", "esp32"),
    "esp8266": ("ESP8266", "", "esp8266"),
}

# ESP-IDF ``esp_app_desc_t`` lives at the start of every IDF app
# image. With ESPHome's default partition layout the app partition
# starts at 0x10000 and the descriptor sits at offset 0x20 within,
# i.e. 0x10020 in flash. The layout is:
#
#   magic         u32       offset 0      0xabcd5432, little-endian
#   secure_ver    u32       offset 4
#   reserved      u8[8]     offset 8
#   version       char[32]  offset 16
#   project_name  char[32]  offset 48
#   …             (more fields we don't need)
#
# ESPHome populates ``project_name`` from ``esphome.name``, which
# vendors flashing factory firmware set to a catalogue board id —
# that's how the wizard auto-routes a starter-kit straight to its
# specific setup screen.
_APP_DESC_OFFSET = 0x10020
_APP_DESC_SIZE = 256
_APP_DESC_MAGIC = 0xABCD5432
_PROJECT_NAME_OFFSET = 48
_PROJECT_NAME_SIZE = 32

# Windows ``COM<n>`` port names. Linux / macOS ports start with
# ``/dev/`` and are validated separately. Catches accidental command
# injection via the port arg — only well-formed names reach esptool.
_WINDOWS_PORT_RE = re.compile(r"^COM\d{1,3}$", re.IGNORECASE)


def _is_valid_port_name(port: str) -> bool:
    """Reject port strings that don't look like a real device path.

    Defence-in-depth — esptool ultimately validates the port itself,
    but accepting arbitrary strings here would let a malicious caller
    pass an argv that triggers esptool to read from a path it
    shouldn't (e.g. a config file). Restrict to ``/dev/<basename>``
    (POSIX serial nodes) or ``COM<n>`` (Windows).
    """
    if port.startswith("/dev/"):
        # Reject path traversal and shell metacharacters.
        rest = port[len("/dev/") :]
        return (
            bool(rest)
            and "/" not in rest
            and ".." not in rest
            and all(c.isalnum() or c in "-_." for c in rest)
        )
    return bool(_WINDOWS_PORT_RE.match(port))


def _chip_family_to_descriptor(esptool_family: str) -> dict[str, str] | None:
    """Map ``"ESP32-C3"`` → ``{chip_family, variant, platform}``."""
    key = esptool_family.strip().lower()
    entry = _CHIP_FAMILY_MAP.get(key)
    if entry is None:
        return None
    family, variant, platform = entry
    return {"chip_family": family, "variant": variant, "platform": platform}


def _parse_project_name(blob: bytes) -> str | None:
    """Pull ``project_name`` out of a 256-byte ``esp_app_desc_t`` blob.

    Returns ``None`` whenever the magic word doesn't match (not an
    IDF app, or partition-layout drift) or the field is empty.
    Callers treat this as "no factory firmware present" and fall
    through to chip-family filtering.
    """
    if len(blob) < _PROJECT_NAME_OFFSET + _PROJECT_NAME_SIZE:
        return None
    magic = int.from_bytes(blob[0:4], "little")
    if magic != _APP_DESC_MAGIC:
        return None
    raw = blob[_PROJECT_NAME_OFFSET : _PROJECT_NAME_OFFSET + _PROJECT_NAME_SIZE]
    nul = raw.find(b"\x00")
    if nul != -1:
        raw = raw[:nul]
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name or None


# Timeouts for the two esptool subcommands ``detect_chip_cmd``
# invokes. ``chip-id`` against a healthy ESP usually completes in
# 2-3 s (reset pulse + ROM handshake + read MAC); the 30 s ceiling
# leaves headroom for slow USB hubs, macOS re-enumeration delays,
# and the occasional retry esptool does internally before giving up.
# ``read-flash`` of 256 B is similar but adds stub upload (a few
# hundred ms) — same ceiling is fine.
_CHIP_DETECT_TIMEOUT = 30.0
_READ_FLASH_TIMEOUT = 30.0


def _classify_esptool_failure(output: str) -> str:
    """Map an esptool error blob to one of the ``_DETECT_*`` reasons.

    Pattern-matches on substrings the esptool CLI prints today —
    fragile in principle, but the patterns have been stable across
    v4 → v5 (the underlying errors come from pyserial / the OS,
    not esptool itself).
    """
    lower = output.lower()
    if "no module named" in lower or "modulenotfounderror" in lower:
        return _DETECT_NO_ESPTOOL
    # POSIX EACCES (errno 13) and pyserial's PermissionError typically
    # mean the user isn't in the dialout group on Linux — different
    # fix from EBUSY (close another app), so they get their own bucket.
    if "errno 13" in lower or "permissionerror" in lower or "permission denied" in lower:
        return _DETECT_PERMISSION
    if (
        "resource busy" in lower
        or "could not open port" in lower
        or "port is busy" in lower
        or "errno 16" in lower
        or "access is denied" in lower  # Windows equivalent of EBUSY
    ):
        return _DETECT_BUSY
    if (
        "failed to connect" in lower
        or "no serial data received" in lower
        or "wrong boot mode detected" in lower
    ):
        return _DETECT_NO_RESPONSE
    return _DETECT_UNKNOWN


def _parse_chip_family_line(output: str) -> dict[str, str] | None:
    r"""Pull the chip family out of an esptool stdout blob.

    esptool prints the family in three places we can target, listed
    here from most to least reliable:

    1. ``"Chip type:          ESP32-C3 (QFN32) (revision v0.3)"`` —
       prints unconditionally after a successful detect+connect,
       happens *after* the collapsing stage finishes (so escape
       codes never overwrite it).
    2. ``"Connected to ESP32-C3 on /dev/..."`` — same post-stage
       guarantee, but the family is embedded mid-line so the
       extraction is slightly more fragile.
    3. ``"Detecting chip type... ESP32-C3"`` — what ``_verify_chip``
       in the firmware controller parses. Lives *inside* the
       collapsible stage; when esptool's "smart features" are
       active (TERM set + colours enabled) the line is still in
       the byte stream but the post-stage ``\x1b[1A\x1b[2K``
       sequence visually erases it. The bytes themselves survive,
       so the parser still finds the line — kept as a final
       fallback for completeness.
    """
    # 1) "Chip type:" line — most reliable, immune to stage collapsing.
    for line in output.splitlines():
        idx = line.find("Chip type:")
        if idx != -1:
            after = line[idx + len("Chip type:") :].strip()
            # Strip the parenthesised package / revision suffix
            # (``ESP32-C3 (QFN32) (revision v0.3)`` → ``ESP32-C3``).
            family = after.split("(")[0].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    # 2) "Connected to X on" line.
    for line in output.splitlines():
        idx = line.find("Connected to ")
        if idx != -1:
            after = line[idx + len("Connected to ") :]
            family = after.split(" on ")[0].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    # 3) "Detecting chip type..." legacy fallback.
    for line in output.splitlines():
        if "Detecting chip type" in line:
            family = line.split("...")[-1].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    return None


async def _run_esptool(args: list[str], timeout: float) -> tuple[int, bytes, bool]:
    """Spawn esptool with *args* and capture stdout+stderr.

    Uses :func:`controllers.firmware.helpers._find_esptool_cmd` to
    pick the right invocation (sibling script preferred over
    ``python -m esptool``) and runs through
    :func:`helpers.subprocess.run_subprocess_capture` — the same
    one-shot helper :func:`_verify_esphome_importable` uses.

    Returns ``(returncode, stdout, timed_out)``. The caller treats
    ``timed_out`` separately from a normal non-zero exit so the WS
    error message can recommend an unplug/replug rather than
    pointing at the cable.

    Lazy-imported to avoid a ``config`` ↔ ``firmware.persistence``
    circular import (persistence reaches back into config for
    ``_load_metadata`` / ``metadata_transaction``).
    """
    from .firmware.helpers import _find_esptool_cmd  # noqa: PLC0415

    cmd = _find_esptool_cmd()
    result = await run_subprocess_capture(*cmd, *args, timeout=timeout)
    rc = result.returncode if result.returncode is not None else -1
    return rc, result.stdout, result.timed_out


async def _detect_chip_via_esptool(
    port: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Run ``esptool chip-id`` and parse the chip family.

    Returns ``(descriptor, None)`` on success or
    ``(None, failure_reason)`` on failure. ``failure_reason`` is one
    of the ``_DETECT_*`` constants — the handler maps it to a
    user-facing message.
    """
    returncode, stdout, timed_out = await _run_esptool(
        ["--port", port, "chip-id"], _CHIP_DETECT_TIMEOUT
    )
    if timed_out:
        _LOGGER.debug("esptool chip-id on %s timed out after %ss", port, _CHIP_DETECT_TIMEOUT)
        return None, _DETECT_TIMEOUT
    output = stdout.decode("utf-8", errors="replace")
    if returncode != 0:
        _LOGGER.debug("esptool chip-id on %s exited %d: %s", port, returncode, output)
        return None, _classify_esptool_failure(output)
    descriptor = _parse_chip_family_line(output)
    if descriptor is None:
        _LOGGER.debug(
            "esptool chip-id on %s succeeded but family wasn't in our map: %s",
            port,
            output,
        )
        return None, _DETECT_UNKNOWN_CHIP
    return descriptor, None


def _make_descriptor_tempfile() -> str:
    """Allocate (and close) a tempfile for esptool's ``read-flash`` output."""
    fd, path = tempfile.mkstemp(prefix="esp_app_desc_", suffix=".bin")
    os.close(fd)
    return path


def _read_descriptor_file(path: str) -> str | None:
    """Read *path* and decode ``project_name`` from the app descriptor."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    return _parse_project_name(blob)


def _unlink_quietly(path: str) -> None:
    """``Path(path).unlink()`` swallowing ``OSError``."""
    with suppress(OSError):
        Path(path).unlink()


async def _read_app_descriptor_board_id(port: str) -> str | None:
    """Best-effort: read 256 B at 0x10020 and decode project_name.

    Failure here is non-fatal — the caller still has chip-family
    info to narrow the picker with. Uses a tempfile because
    esptool's ``read-flash`` writes the binary payload to a named
    file, not stdout. The tempfile-create / read / unlink are sync
    FS calls so they run via ``asyncio.to_thread`` to keep
    blockbuster happy.
    """
    path = await asyncio.to_thread(_make_descriptor_tempfile)
    try:
        returncode, _stdout, timed_out = await _run_esptool(
            [
                "--port",
                port,
                "read-flash",
                hex(_APP_DESC_OFFSET),
                str(_APP_DESC_SIZE),
                path,
            ],
            _READ_FLASH_TIMEOUT,
        )
        if timed_out or returncode != 0:
            return None
        return await asyncio.to_thread(_read_descriptor_file, path)
    finally:
        await asyncio.to_thread(_unlink_quietly, path)


# Failure classifications for ``_detect_chip_via_esptool``. The
# handler in ``detect_chip_cmd`` maps each to a user-facing message
# — they all surface as ``UNAVAILABLE`` to the WS client, the
# distinction is in the human text. ``BUSY`` is the load-bearing one
# (a serial monitor or stale WebSerial session is the single most
# common reason detection fails); without it the user gets a
# misleading "is a device connected?" message even though the cable
# is plugged in.
_DETECT_BUSY = "busy"
_DETECT_PERMISSION = "permission"
_DETECT_NO_RESPONSE = "no_response"
_DETECT_TIMEOUT = "timeout"
_DETECT_NO_ESPTOOL = "no_esptool"
_DETECT_UNKNOWN_CHIP = "unknown_chip"
_DETECT_UNKNOWN = "unknown"


# Per-reason message templates. ``{port}`` is interpolated by
# ``_detect_failure_message`` so each template stays a plain string
# literal — easier to scan than nested f-strings inside a branchy
# function (and keeps ruff happy about the if/elif chain length).
_DETECT_FAILURE_MESSAGES: dict[str, str] = {
    _DETECT_BUSY: (
        "{port} is already in use by another application. Close any "
        "browser tab using Web Serial or any serial monitor connected "
        "to this port, then try again."
    ),
    _DETECT_PERMISSION: (
        "Permission denied opening {port}. On Linux your user may "
        "need to be in the ``dialout`` group "
        "(``sudo usermod -a -G dialout $USER`` and log back in)."
    ),
    _DETECT_NO_RESPONSE: (
        "No response from a chip on {port}. Check the USB cable, "
        "and on boards without auto-reset try holding the BOOT button "
        "while you plug it in."
    ),
    _DETECT_TIMEOUT: (
        "esptool didn't finish in time on {port}. The chip may be "
        "unresponsive — unplug and replug, then try again."
    ),
    _DETECT_UNKNOWN_CHIP: (
        "Detected a device on {port}, but it isn't a recognised ESP "
        "chip family. This command only supports ESP32 / ESP8266 "
        "variants — pick a board manually from the list."
    ),
    _DETECT_NO_ESPTOOL: (
        "Could not run esptool on the server. Make sure esptool is "
        "installed in the dashboard's Python environment."
    ),
}


def _detect_failure_message(reason: str | None, port: str) -> str:
    """Translate a ``_DETECT_*`` reason into a user-facing message."""
    template = _DETECT_FAILURE_MESSAGES.get(
        reason or "",
        "Could not detect a chip on {port}. Is a supported ESP device connected?",
    )
    return template.format(port=port)


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

    @api_command("config/detect_chip")
    async def detect_chip_cmd(self, **kwargs: Any) -> dict:
        """Identify what's plugged into a server-side serial port.

        Runs ``esptool chip-id`` to detect the chip family, then
        best-effort reads the IDF ``esp_app_desc_t`` at flash
        offset ``0x10020`` for the ``project_name`` field (the
        board_id baked in by ESPHome at compile time when factory
        firmware is present). Closes the parity gap with WebSerial,
        which already does the same locally via esptool-js.

        Returns ``{chip_family, variant, platform, board_id?}``.
        ``board_id`` is omitted whenever the manifest read fails or
        the device isn't running an IDF image — callers treat that
        as "narrow the picker by chip family" and let the user pick
        the specific board.

        Failures all surface as ``UNAVAILABLE`` but with distinct
        messages so the user can act: "port busy" (close the
        offending app), "no response" (check the cable / BOOT
        button), "unknown chip" (the device responded but isn't
        an ESP variant we recognise), etc.
        """
        port = kwargs.get("port")
        if not isinstance(port, str) or not port:
            raise CommandError(ErrorCode.INVALID_ARGS, "port is required")
        if not _is_valid_port_name(port):
            raise CommandError(ErrorCode.INVALID_ARGS, f"invalid port: {port!r}")

        chip_info, failure = await _detect_chip_via_esptool(port)
        if chip_info is None:
            raise CommandError(ErrorCode.UNAVAILABLE, _detect_failure_message(failure, port))

        result: dict = dict(chip_info)
        board_id = await _read_app_descriptor_board_id(port)
        if board_id:
            result["board_id"] = board_id
        return result

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
        config_dir = self._db.settings.config_dir
        data = await loop.run_in_executor(None, read_secrets_yaml, config_dir)
        if not data:
            return []
        # ``secrets.yaml`` could legitimately have non-string keys
        # (a YAML scalar like ``42:`` parses to ``int``). ``sorted()``
        # on mixed types raises ``TypeError`` in Python 3, so filter
        # to string keys before sorting — non-string keys aren't
        # usable in ``!secret`` references anyway.
        return sorted(k for k in data if isinstance(k, str))

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
            storage = StorageJSON.load(resolve_storage_path(configuration))
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
