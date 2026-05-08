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
from esphome.helpers import write_file as atomic_write_file
from esphome.storage_json import StorageJSON, ext_storage_path
from esphome.util import get_serial_ports

from ..constants import DEFAULT_INGRESS_PORT
from ..constants import __version__ as server_version
from ..helpers.api import CommandError, api_command
from ..helpers.auth import hash_password
from ..helpers.json import JSONDecodeError, dumps_indent, loads
from ..models import ErrorCode, Label, RemoteBuildSettings, StoredToken, UserPreferences

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
        # that the values need to be replaced before flashing — same
        # UX contract ESPHome's CLI ``wizard`` enforces by prompting.
        secrets_path = self.config_dir / "secrets.yaml"
        if not secrets_path.exists():
            atomic_write_file(
                secrets_path,
                "# Secrets — referenced from device configs via !secret\n"
                "# Replace these placeholders with your real Wi-Fi\n"
                "# credentials before flashing or installing OTA.\n"
                'wifi_ssid: "REPLACE_WITH_YOUR_WIFI_NETWORK"\n'
                'wifi_password: "REPLACE_WITH_YOUR_WIFI_PASSWORD"\n',
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
    except Exception:
        return UserPreferences()


def save_preferences(config_dir: Path, prefs: UserPreferences) -> None:
    """Save user preferences to disk."""
    with metadata_transaction(config_dir) as data:
        data[_PREFS_KEY] = prefs.to_dict()


def _decode_tokens(raw: Any) -> list[StoredToken]:
    """
    Parse the on-disk ``_remote_build.tokens`` list, dropping malformed rows.

    Mirrors :func:`_decode_labels`: one bad entry shouldn't blank
    every other token from the receiver's view, since the operator
    would lose access to all paired peers until disk repair. Each
    row is validated independently; a row that fails to round-trip
    through :class:`StoredToken` is skipped with a debug log.

    The skip log only carries the non-sensitive ``token_id`` (the
    public lookup key, never a secret), not the full entry dict.
    A hand-edited sidecar could carry credential-adjacent fields
    (e.g. an operator pasted the cleartext secret into the wrong
    field by mistake), and ``%r``-dumping the whole row would
    surface that material in logs / log shippers.
    """
    if not isinstance(raw, list):
        return []
    out: list[StoredToken] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(StoredToken.from_dict(entry))
        except Exception as err:
            safe_id = entry.get("token_id")
            safe_id_repr = (
                safe_id if isinstance(safe_id, str) and len(safe_id) <= 64 else "<unknown>"
            )
            _LOGGER.debug("Skipping malformed token entry (token_id=%s): %s", safe_id_repr, err)
    return out


def _settings_from_raw(raw: Any) -> RemoteBuildSettings:
    """
    Decode a ``_remote_build`` blob with row-level token tolerance.

    The whole-blob fallback is preserved for corrupt
    ``manual_hosts`` / ``enabled`` (those fields are atomic and a
    schema break should reset to defaults loudly), but tokens
    decode row-by-row so one bad token row doesn't disconnect
    every paired peer.
    """
    if not isinstance(raw, dict):
        return RemoteBuildSettings()
    # Pre-clean the tokens list so mashumaro's all-or-nothing
    # ``from_dict`` doesn't refuse the whole blob over one bad row.
    cleaned: dict[str, Any] = {
        **raw,
        "tokens": [t.to_dict() for t in _decode_tokens(raw.get("tokens"))],
    }
    try:
        return RemoteBuildSettings.from_dict(cleaned)
    except Exception:
        return RemoteBuildSettings()


def load_remote_build_settings(config_dir: Path) -> RemoteBuildSettings:
    """
    Load the receiver-side remote-build settings.

    Returns defaults (``enabled=False``) when the metadata file is
    missing or the ``_remote_build`` key isn't present. Token rows
    that fail to round-trip are dropped individually (see
    :func:`_decode_tokens`); a wholly malformed blob falls back to
    defaults rather than crashing dashboard startup.
    """
    return _settings_from_raw(_load_metadata(config_dir).get(_REMOTE_BUILD_KEY, {}))


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
        except Exception as err:
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
            raise ValueError(f"label_ids must be strings, got {type(lid).__name__}: {lid!r}")
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
