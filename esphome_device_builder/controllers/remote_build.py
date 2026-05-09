"""
Remote-build feature; peer dashboard discovery + settings + tokens.

Browses ``_esphomebuilder._tcp.local.`` to list other dashboards
reachable on the LAN; persists the receiver-side ``enabled``
master switch, the user-supplied manual-host list for
cross-subnet / non-multicast LANs, and the receiver-issued
bearer-token list; merges discovery sources into a single
``remote_build/list_hosts`` snapshot.

The ``enabled`` flag gates the HTTPS receiver site
:class:`DeviceBuilder` binds at startup (``/remote-build/v1/*``,
default port 6055). Toggling ``enabled`` at runtime persists
the new value but does NOT live-bind / unbind the listener;
flipping it requires a dashboard restart for the listener
state to follow. The 3c Settings UI surfaces this constraint;
a future PR can wire the start / stop hooks if interactive
toggling matters.

Tokens are validated by the auth middleware on that site
against an in-memory index seeded from disk in :meth:`start`
and refreshed on every CRUD mutation. **The frontend
generates the bearer (token_id + secret) client-side** and
submits only ``SHA-256(secret)`` to the backend; the
cleartext bearer never crosses the wire from frontend to
dashboard. This closes the leak that would otherwise occur
when the dashboard is reachable on plain HTTP (standalone
``--host 0.0.0.0`` without a reverse-proxy TLS terminator).
Only the hash lands on disk; if the user loses the cleartext,
the recovery is to remove the token and register a fresh one.

Pairing flow + peer-link WS arrive in later phases. The
listener currently serves only ``/remote-build/v1/health`` as
a smoke endpoint; phase 5+ adds the real bundle / build /
firmware RPCs against the same auth surface.

Manual hosts have no version / fingerprint resolution yet;
they land in ``list_hosts`` with empty ``server_version`` /
``esphome_version`` until pairing attempts the connection.

Browser uses the existing ``AsyncEsphomeZeroconf`` instance owned by
:class:`~esphome_device_builder.controllers._device_state_monitor.DeviceStateMonitor`,
so the dashboard ships one mDNS responder per process and this
controller adds a second :class:`~zeroconf.asyncio.AsyncServiceBrowser`
on the same instance for the new service type. The state monitor's
own browsers (``_esphomelib._tcp.local.`` for devices,
``_http._tcp.local.`` for adoptable web UIs) are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ..helpers.api import CommandError, api_command
from ..helpers.dashboard_advertise import SERVICE_TYPE
from ..models import (
    ErrorCode,
    ManualHost,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredToken,
    TokenSummary,
)
from .config import load_remote_build_settings, remote_build_settings_transaction

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Timeout for the cache-miss resolve path. Longer than
# ``DeviceStateMonitor._MDNS_RESOLVE_TIMEOUT_MS`` (2s) because peer
# dashboards typically run on full hosts (laptop, desktop, addon
# container) that may be a few hops further away on the LAN than
# an ESPHome device, and the user-visible cost of a slow first
# discovery is "the peer doesn't appear in Settings for a few
# seconds"; not the device-state miss the shorter timeout
# protects against.
_RESOLVE_TIMEOUT_MS = 3000


def _decode_txt_value(raw: bytes | None) -> str:
    """Decode a TXT value as UTF-8, falling back to the empty string."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _peer_from_service_info(name: str, info: AsyncServiceInfo) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a resolved ``AsyncServiceInfo``.

    Keeps the parsing in one place so ``_apply_service_info`` and
    the cache-hit branch produce identical shapes.

    Uses ``parsed_scoped_addresses(IPVersion.All)`` rather than
    ``parsed_addresses()`` so IPv6 link-local entries keep their
    ``%<interface>`` scope suffix. Without the scope, an
    ``fe80::xxx`` address parses but isn't connectable; the OS
    needs to know which interface to send the packet out on.
    Mirrors the choice already made in
    :class:`DeviceStateMonitor` (line 901).
    """
    properties = info.properties or {}
    server_version = _decode_txt_value(properties.get(b"server_version"))
    esphome_version = _decode_txt_value(properties.get(b"esphome_version"))
    # ``info.name`` comes back as ``<instance>.<service_type>``; we
    # only want the leftmost label as the friendly name.
    instance = (info.name or name).split(".", 1)[0]
    server = info.server or ""
    return RemoteBuildPeer(
        name=instance,
        hostname=server,
        port=info.port or 0,
        source=RemoteBuildPeerSource.MDNS,
        addresses=info.parsed_scoped_addresses(IPVersion.All) or [],
        server_version=server_version,
        esphome_version=esphome_version,
    )


def _peer_from_manual_host(entry: ManualHost) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a stored :class:`ManualHost`.

    Manual hosts skip resolution; phase 2b just surfaces the
    user-entered ``(hostname, port)`` so the frontend can render
    the row alongside mDNS-discovered ones. Phase 4 attempts the
    actual connection and fills the version fields.

    ``name`` is the hostname verbatim (rather than the leftmost
    label) so an IP-only entry still reads sensibly in the UI;
    the frontend can render a "Manual" badge to distinguish it
    from an mDNS-discovered row.
    """
    return RemoteBuildPeer(
        name=entry.hostname,
        hostname=entry.hostname,
        port=entry.port,
        source=RemoteBuildPeerSource.MANUAL,
    )


def _validate_hostname(raw: object) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Lowercase normalisation
    matches the duplicate-check semantics; hostnames are
    case-insensitive per RFC 1035 §2.3.3, so ``Desktop.local`` and
    ``desktop.local`` should be the same entry. The stored form
    is the trimmed, lowercased string (so two adds with different
    casing collapse to one entry rather than registering twice).
    Phase 4 attempts the actual connection (and discovers DNS /
    TLS validity); phase 2b deliberately doesn't pre-flight an
    "is this resolvable now?" check, which would fail on offline
    laptops adding a peer for later.
    """
    if not isinstance(raw, str):
        msg = "manual host: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = "manual host: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


# Wire format for the bearer the offloader presents is
# ``{token_id}.{secret}``: a fixed 11-char base64url ``token_id``
# (the textual form of 8 random bytes — 64 bits, plenty against
# birthday collisions at the ``_MAX_TOKENS = 100`` cap) plus a
# 43-char base64url ``secret`` (the textual form of 32 random
# bytes — 256 bits, infeasible to brute force). Both halves are
# base64url so the wire form has no shell-quoting hazards.
# ``_validate_token_id`` enforces the exact 11-char length so
# the collision math stays load-bearing — without that pin, a
# client could ship arbitrary-length ids and the backend's
# bookkeeping would still work but the entropy guarantee
# wouldn't.
#
# **The cleartext bearer is never sent to the backend.** The
# frontend generates ``token_id`` + ``secret`` client-side
# (``crypto.getRandomValues`` — the only acceptable source;
# any fallback to ``Math.random`` or similar is a security
# regression because the backend has no way to verify the
# entropy of a hash, only its shape), computes
# ``SHA-256(secret)`` locally, and POSTs
# ``{label, token_id, secret_sha256}``. The backend stores only
# the hash; the cleartext bearer stays on the user's screen
# long enough to copy into the offloader, then discarded. This
# closes the leak that would otherwise occur when the dashboard
# is reachable on plain HTTP (for example a standalone
# ``--host 0.0.0.0`` deployment without a reverse-proxy TLS
# terminator).

# Cap label length to keep ``list_tokens`` payloads bounded and
# prevent a misbehaving frontend from accidentally storing a
# multi-megabyte string. Generous enough for "Green dashboard
# (kitchen)" style labels.
_TOKEN_LABEL_MAX = 128

# ``secrets.token_urlsafe`` emits the base64url alphabet only;
# any other character means the caller is sending something that
# isn't a token_id (most likely the full bearer or a typo). Pin
# the exact length to ``base64url(8 bytes) = 11`` so the
# collision math stays load-bearing — a client that shipped
# longer / shorter ids would still work for storage but the
# 64-bit entropy guarantee wouldn't.
_TOKEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_ID_LEN = 11

# Soft cap on the receiver-side token list so a misbehaving
# frontend looping ``add_token`` can't grow the metadata file
# unboundedly. 100 is well above any realistic
# pairings-per-receiver count and gives the UI a clean upper
# bound to render.
_MAX_TOKENS = 100


def _validate_label(raw: object) -> str:
    """
    Normalise a user-entered token label to a stripped, length-bounded form.

    Rejects non-string, empty / whitespace-only, and too-long
    inputs with :class:`CommandError(INVALID_ARGS)`. Duplicate
    labels are NOT rejected: ``token_id`` is the unique key, and
    a user might legitimately want two tokens both labelled
    "Green" (different machines or different purposes).
    """
    if not isinstance(raw, str):
        msg = "token: 'label' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not trimmed:
        msg = "token: 'label' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) > _TOKEN_LABEL_MAX:
        msg = f"token: 'label' must be at most {_TOKEN_LABEL_MAX} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


def _validate_token_id(raw: object) -> str:
    """
    Validate a user-supplied token_id.

    Pins the exact 11-char length so the 64-bit entropy
    guarantee in the bearer-format docstring stays accurate;
    the frontend's ``crypto.getRandomValues(new Uint8Array(8))``
    + base64url encode produces 11 chars, and ``secrets.token_urlsafe(8)``
    on the test side does the same. A shorter id shrinks the
    namespace; a longer one isn't generated by any honest
    client and almost certainly indicates the caller stuffed
    extra data in.

    Also rejects values containing ``.``: the bearer wire form
    is ``{token_id}.{secret}``, so a value with a dot is most
    likely the full bearer mistakenly passed instead of the id
    half. Rejecting before the value lands in any error message
    keeps the cleartext secret out of logs / DevTools / frontend
    telemetry.

    Shape-checks only; the existence check happens in the mutator
    under the metadata lock so look-up and delete are atomic.
    """
    if not isinstance(raw, str):
        msg = "token: 'token_id' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not trimmed:
        msg = "token: 'token_id' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if "." in trimmed:
        # Specifically don't echo ``trimmed`` back: it might be a
        # full bearer, in which case the secret half is in this
        # variable.
        msg = "token: 'token_id' must be the id half only, not the full bearer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) != _TOKEN_ID_LEN:
        msg = f"token: 'token_id' must be exactly {_TOKEN_ID_LEN} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not _TOKEN_ID_PATTERN.fullmatch(trimmed):
        msg = "token: 'token_id' must contain base64url characters only"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


_SECRET_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_secret_sha256(raw: object) -> str:
    """
    Validate a client-supplied SHA-256 hash of the bearer secret half.

    Must be exactly 64 lowercase hex chars (the textual form of
    SHA-256). Catches frontend bugs that send the cleartext
    secret instead of the hash, send an uppercase / mixed-case
    digest, or send a different-length string.

    The frontend computes ``sha256(secret)`` client-side so the
    cleartext bearer never crosses the wire to the backend; the
    backend persists only this hash. Defending against
    malformed-input here is a sanity check, not a security
    boundary — even a valid-shape hash with no matching cleartext
    is just an unusable token row.
    """
    if not isinstance(raw, str):
        msg = "token: 'secret_sha256' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not _SECRET_SHA256_PATTERN.fullmatch(trimmed):
        msg = "token: 'secret_sha256' must be 64 lowercase hex characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


def _summarise_token(token: StoredToken) -> TokenSummary:
    """Project a :class:`StoredToken` to its public-facing :class:`TokenSummary`."""
    return TokenSummary(
        token_id=token.token_id,
        label=token.label,
        created_at=token.created_at,
        bound_dashboard_id=token.bound_dashboard_id,
    )


def _to_view(settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
    """
    Project a :class:`RemoteBuildSettings` to its wire :class:`RemoteBuildSettingsView`.

    Drops ``secret_sha256`` from each token row. Every controller
    method that returns settings to a client routes through here
    so the stored hash never leaves the server, even when a CRUD
    response also touches the tokens list.
    """
    return RemoteBuildSettingsView(
        enabled=settings.enabled,
        manual_hosts=list(settings.manual_hosts),
        tokens=[_summarise_token(t) for t in settings.tokens],
    )


def _validate_port(raw: object) -> int:
    """
    Validate a user-entered port number.

    ``bool`` is rejected even though ``isinstance(True, int)`` is
    true; accepting ``True`` for a port number is a footgun
    (silently coerces to 1, which IANA reserves for tcpmux).
    Range is the IANA-registered ephemeral plus
    well-known: 1-65535.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = "manual host: 'port' must be an integer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not 1 <= raw <= 65535:
        msg = "manual host: 'port' must be between 1 and 65535"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


class RemoteBuildController:
    """
    Discover peer dashboards and own the receiver-side settings.

    Constructed once in :meth:`DeviceBuilder.start`. The browser
    lifetime is tied to :meth:`start` / :meth:`stop`; the controller's
    own start happens after :class:`DevicesController.start` so the
    shared zeroconf instance is already up.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._browser: AsyncServiceBrowser | None = None
        self._peers: dict[str, RemoteBuildPeer] = {}
        # Strong refs for fire-and-forget resolve tasks so the
        # garbage collector can't reap them mid-await.
        self._tasks: set[asyncio.Task[None]] = set()
        # The mDNS service-instance name our own ``DashboardAdvertiser``
        # publishes; captured at start so we can filter our own
        # broadcast out of the discovered list. ``None`` when the
        # advertiser was skipped (HA addon mode, zeroconf failed),
        # in which case there's nothing to filter.
        self._own_instance_name: str | None = None
        # In-memory token index keyed off ``token_id``. Built from
        # disk at start; refreshed after every CRUD mutation so
        # the auth middleware's lookup is constant-time and
        # never has to hit the filesystem on the request hot
        # path. Empty until ``start`` runs.
        self._tokens_by_id: dict[str, StoredToken] = {}

    async def start(self) -> None:
        """
        Wire the browser onto the shared zeroconf and capture self-name.

        No-op when zeroconf failed to start; peer discovery is a
        nice-to-have, not load-bearing, and the controller stays in
        a "no peers, never will be" state until the next dashboard
        restart. Same fail-soft contract as
        :class:`DashboardAdvertiser`.
        """
        # Seed the token index from disk before the zeroconf gate
        # below: the index is consumed by the HTTPS auth middleware
        # (phase 3b2), not by the zeroconf browser. Even on a
        # zeroconf-disabled deployment (HA addon, container with
        # mDNS broken) the index needs to be live so the listener
        # can validate bearers.
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        self._tokens_by_id = {t.token_id: t for t in settings.tokens}

        if self._db.devices is None:
            _LOGGER.debug("RemoteBuildController.start called before devices controller")
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("zeroconf unavailable; remote-build discovery disabled")
            return
        # Capture own service-instance name so our own advertise
        # doesn't show up in ``list_hosts``. Reads through the
        # public ``service_instance_name`` accessor on
        # ``DashboardAdvertiser`` rather than reaching into
        # ``_info``; keeps this controller decoupled from the
        # advertiser's private layout.
        advertiser = self._db._dashboard_advertiser
        if advertiser is not None:
            self._own_instance_name = advertiser.service_instance_name
        # Wrap browser construction so a zeroconf-side failure (e.g.
        # the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in an
        # unexpected state) doesn't abort dashboard startup. Peer
        # discovery is fail-soft; same contract as the advertise.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
            self._browser = None

    async def stop(self) -> None:
        """Cancel the browser and drain in-flight resolve tasks."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        self._peers.clear()

    # ------------------------------------------------------------------
    # mDNS plumbing
    # ------------------------------------------------------------------

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """
        Browser callback; resolve the service info and update the peer map.

        Filters our own service-instance name so the advertise we
        publish doesn't show up in ``list_hosts``. ``Removed`` events
        delete the peer immediately; ``Added`` / ``Updated`` resolve
        either from the zeroconf cache (sync) or via a fire-and-forget
        task (async).
        """
        if name == self._own_instance_name:
            return
        if state_change == ServiceStateChange.Removed:
            self._peers.pop(name, None)
            return
        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._peers[name] = _peer_from_service_info(name, info)
            return
        task = asyncio.create_task(self._resolve_and_apply(zeroconf, info, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve_and_apply(self, zeroconf: Any, info: AsyncServiceInfo, name: str) -> None:
        """Async resolve path for cache misses."""
        try:
            resolved = await info.async_request(zeroconf, timeout=_RESOLVE_TIMEOUT_MS)
        except Exception:
            _LOGGER.debug("Resolve failed for %s", name, exc_info=True)
            return
        if not resolved:
            return
        self._peers[name] = _peer_from_service_info(name, info)

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/list_hosts")
    async def list_hosts(self, **kwargs: Any) -> list[RemoteBuildPeer]:
        """
        Return every peer dashboard known to this receiver.

        Merges two sources into a single snapshot:

        * mDNS-discovered peers from the browser (``source=MDNS``,
          full version + address info).
        * Manually-added peers from
          ``_remote_build.manual_hosts`` (``source=MANUAL``, blank
          version fields until phase 4 fills them in).

        Manual hosts are placed AFTER mDNS hits so the UI's
        primary content is the auto-discovered list. A
        manually-added entry that's also reachable via mDNS shows
        up twice for now (once per source); phase 4's pairing
        flow will introduce the deduplication logic alongside the
        actual connection attempt.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [
            *self._peers.values(),
            *(_peer_from_manual_host(entry) for entry in settings.manual_hosts),
        ]

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettingsView:
        """Return the receiver-side remote-build settings (wire view)."""
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return _to_view(settings)

    async def _modify_settings(
        self, mutator: Callable[[RemoteBuildSettings], None]
    ) -> RemoteBuildSettingsView:
        """
        Run ``mutator`` against the current settings and persist the result.

        Wraps :func:`remote_build_settings_transaction` so the
        whole read-modify-write happens under the metadata lock,
        so two concurrent callers can't both read the same starting
        value and have the second save wipe the first's change.
        Runs in the default executor since the transaction does
        blocking JSON I/O. Returns the wire view so the response
        leaving this method can never carry ``secret_sha256``.

        ``mutator`` is invoked with the freshly-loaded settings
        and is expected to mutate it in place. A
        :class:`CommandError` raised inside the mutator (e.g.
        duplicate-detection on add) propagates out and discards
        the pending write; same exception-on-discard contract as
        :func:`metadata_transaction`.
        """

        def _txn() -> RemoteBuildSettings:
            with remote_build_settings_transaction(self._db.settings.config_dir) as settings:
                mutator(settings)
                return settings

        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(None, _txn)
        # Keep the auth middleware's lookup index in sync with the
        # post-write state. Add / remove / first-use-binding all
        # route through here, so this is the one place that needs
        # to refresh.
        self._tokens_by_id = {t.token_id: t for t in settings.tokens}
        return _to_view(settings)

    def lookup_token(self, token_id: str) -> StoredToken | None:
        """
        Return the matching :class:`StoredToken` for ``token_id``, or ``None``.

        Public accessor for the phase-3b2 auth middleware. Reads
        the in-memory index (constant-time dict hit, no I/O on
        the request hot path). The index is seeded in
        :meth:`start` and kept in sync via
        :meth:`_modify_settings`.
        """
        return self._tokens_by_id.get(token_id)

    async def bind_token_first_use(self, token_id: str, dashboard_id: str) -> StoredToken | None:
        """
        Atomically bind ``token_id`` to ``dashboard_id`` on first authenticated use.

        Returns the post-write :class:`StoredToken` (with
        ``bound_dashboard_id`` populated), or ``None`` if the
        token has been removed in the meantime.

        Idempotent: if the token is already bound, the existing
        ``bound_dashboard_id`` is preserved and the call is a
        no-op write. Two concurrent first-use requests with
        different ``dashboard_id`` values race for the slot; the
        winner's id sticks, the loser's call returns the
        winner-bound token. Callers compare the returned
        ``bound_dashboard_id`` against the value they presented
        to detect a race-loss → 403 mismatch.

        Phase-3b2's auth middleware is the only caller. The
        write hops through ``run_in_executor`` because the
        underlying ``metadata_transaction`` is sync filesystem
        I/O.
        """

        def _bind(settings: RemoteBuildSettings) -> StoredToken | None:
            for token in settings.tokens:
                if token.token_id != token_id:
                    continue
                if token.bound_dashboard_id is None:
                    token.bound_dashboard_id = dashboard_id
                return token
            return None

        captured: list[StoredToken | None] = []

        def _capture(settings: RemoteBuildSettings) -> None:
            captured.append(_bind(settings))

        # ``_modify_settings`` either runs the mutator exactly
        # once and returns, or it raises (which propagates).
        # Either way ``captured`` has length 1 on the success
        # path; ``[0]`` is safe.
        await self._modify_settings(_capture)
        return captured[0]

    @api_command("remote_build/set_settings")
    async def set_settings(self, *, enabled: bool, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Persist the receiver-side ``enabled`` master switch.

        Read-modify-write so manual hosts, tokens, and any future
        phase-3+ fields stay intact; a client toggling just
        ``enabled`` doesn't reset every other field to its default.

        Validates ``enabled`` is strictly a ``bool`` rather than
        coercing truthiness; a client sending the string ``"false"``
        for example would otherwise persist as ``True``, which is
        the opposite of what the user intended on a security-
        sensitive toggle.

        **Listener bind requires restart.** The HTTPS receiver
        site (``/remote-build/v1/*``) is bound once in
        :meth:`DeviceBuilder.start` based on the value at startup;
        flipping ``enabled`` here persists the new value but does
        NOT live-rebind. The frontend should surface a "restart
        required" hint to the operator. A future PR can wire
        ``set_settings`` into the lifecycle hooks if interactive
        toggling becomes a real UX concern.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled

        return await self._modify_settings(_set)

    # ------------------------------------------------------------------
    # Manual hosts (phase 2b)
    # ------------------------------------------------------------------

    @api_command("remote_build/add_manual_host")
    async def add_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettingsView:
        """
        Add a manually-entered peer for cross-subnet / non-mDNS LANs.

        Validates ``hostname`` (non-empty string, normalised to
        lowercase per RFC 1035 §2.3.3) and ``port`` (integer,
        1-65535). Rejects duplicates by ``(hostname, port)``:
        adding the same pair twice raises ``ALREADY_EXISTS`` so
        the frontend can render a "this dashboard is already in
        your list" message without string-matching the details
        field.

        Returns the post-write settings so the caller can re-render
        the manual-hosts list without a separate ``get_settings``
        round-trip.
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _add(settings: RemoteBuildSettings) -> None:
            for entry in settings.manual_hosts:
                if entry.hostname == host and entry.port == port_num:
                    msg = f"manual host {host}:{port_num} is already registered"
                    raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
            settings.manual_hosts.append(ManualHost(hostname=host, port=port_num))

        return await self._modify_settings(_add)

    @api_command("remote_build/remove_manual_host")
    async def remove_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettingsView:
        """
        Remove a previously-added manual peer.

        Hostname normalisation matches :meth:`add_manual_host` so a
        case-different removal request finds the entry. A
        non-existent ``(hostname, port)`` pair raises
        ``NOT_FOUND`` so the caller knows the operation was a no-op
        rather than silently succeeding (matters for the
        Settings UI: "Removed Foo" toast vs no feedback).
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _remove(settings: RemoteBuildSettings) -> None:
            kept = [
                entry
                for entry in settings.manual_hosts
                if not (entry.hostname == host and entry.port == port_num)
            ]
            if len(kept) == len(settings.manual_hosts):
                msg = f"manual host {host}:{port_num} is not registered"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.manual_hosts = kept

        return await self._modify_settings(_remove)

    # ------------------------------------------------------------------
    # Token CRUD (phase 3b1)
    # ------------------------------------------------------------------

    @api_command("remote_build/list_tokens")
    async def list_tokens(self, **kwargs: Any) -> list[TokenSummary]:
        """
        Return every issued bearer token, by ``TokenSummary``.

        ``TokenSummary`` rows never carry the secret hash; the
        cleartext bearer is generated client-side at
        ``add_token`` time and never crosses the wire to the
        backend, so this list intentionally has no path to
        recover it. The frontend renders the token_id + label +
        bound dashboard_id (if any) so the operator can audit
        which peers are paired.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [_summarise_token(token) for token in settings.tokens]

    @api_command("remote_build/add_token")
    async def add_token(
        self, *, label: str, token_id: str, secret_sha256: str, **kwargs: Any
    ) -> TokenSummary:
        """
        Register a client-generated bearer token under *label*.

        The CLIENT generates ``token_id`` + the cleartext secret,
        computes ``SHA-256(secret)`` locally, and submits
        ``{label, token_id, secret_sha256}``. The cleartext bearer
        never crosses the wire to the backend; only the hash is
        persisted. The frontend keeps the cleartext on screen
        long enough for the user to copy it into the offloader,
        then discards it.

        This wire shape closes the leak that would otherwise
        occur on plain-HTTP standalone deployments: the bearer
        is never present in any backend log, response, or stored
        request body. ``list_tokens`` returns
        :class:`TokenSummary` rows that don't carry the hash;
        if the user loses the cleartext, the only recovery is
        to remove the token and register a fresh one.

        Duplicate labels are allowed (``token_id`` is the unique
        key); a user may legitimately want two tokens both
        labelled "Green". Duplicate ``token_id`` is rejected
        with ``ALREADY_EXISTS`` — the client should retry with a
        freshly-generated id (collision is improbable at 64
        bits but not impossible at the soft cap).
        """
        clean_label = _validate_label(label)
        clean_token_id = _validate_token_id(token_id)
        clean_secret_sha256 = _validate_secret_sha256(secret_sha256)
        created_at = time.time()

        def _add(settings: RemoteBuildSettings) -> None:
            if len(settings.tokens) >= _MAX_TOKENS:
                msg = (
                    f"token list at capacity ({_MAX_TOKENS}); "
                    "remove an unused token before issuing a new one"
                )
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            for existing in settings.tokens:
                if existing.token_id == clean_token_id:
                    # Don't echo the token_id (already in the
                    # caller's hand; not a credential, but no
                    # need to mirror it back through error logs).
                    msg = "token_id collides with an existing token; retry with a fresh id"
                    raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
            settings.tokens.append(
                StoredToken(
                    token_id=clean_token_id,
                    label=clean_label,
                    secret_sha256=clean_secret_sha256,
                    created_at=created_at,
                )
            )

        await self._modify_settings(_add)
        return TokenSummary(
            token_id=clean_token_id,
            label=clean_label,
            created_at=created_at,
        )

    @api_command("remote_build/remove_token")
    async def remove_token(self, *, token_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Revoke a previously-issued token.

        Removing a bound token immediately disconnects the
        offloader it's paired to: the next request the offloader
        sends presents a token_id the receiver no longer
        recognises and gets a 401. A non-existent ``token_id``
        raises ``NOT_FOUND`` so the caller knows the call was a
        no-op.
        """
        clean_id = _validate_token_id(token_id)

        def _remove(settings: RemoteBuildSettings) -> None:
            kept = [token for token in settings.tokens if token.token_id != clean_id]
            if len(kept) == len(settings.tokens):
                # Don't echo ``clean_id`` (or any user-supplied
                # input) here: validation rejects bearers up
                # front, but the principle is to keep credential-
                # adjacent input out of error messages by default.
                msg = "token is not registered"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.tokens = kept

        return await self._modify_settings(_remove)
