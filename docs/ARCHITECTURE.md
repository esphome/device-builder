# Architecture

## Principles

1. **ESPHome is a CLI tool.** Firmware operations shell out to `esphome` via subprocess. Device metadata and serial ports use ESPHome Python imports. Board and component definitions come from our own `definitions/` directory.

2. **ESPHome is an optional dependency.** `pip install .[esphome]` pulls it in for standalone use. Plain `pip install .` works inside the ESPHome container.

3. **Frontend and backend are separate repos.** The frontend is a separate pip package. The backend try-imports it and serves the static files.

4. **WS-first API.** Everything goes through a single `/ws` WebSocket with command/response protocol. REST endpoints only for HA backward compat.

5. **Real-time events.** Clients subscribe once via `subscribe_events`, get instant push notifications. No polling needed.

6. **Persistent firmware jobs.** Compile/upload jobs are queued, run one at a time, survive page refreshes and server restarts.

7. **Device discovery.** mDNS browser for instant online/offline detection, ping sweep every 60s as fallback, optional MQTT discovery for devices that opt in via an `mqtt:` block. Source priority: `mdns > mqtt > ping`.

## Project Structure

```
esphome_device_builder/
├── device_builder.py          # Core singleton — owns controllers, event bus, web app
├── __main__.py                # CLI entry point
├── constants.py               # Version + defaults
│
├── models/                    # Data shapes only — no logic
│   ├── common.py              # EventType, ConfigEntry, PagedResponse
│   ├── devices.py             # Device, AdoptableDevice, DevicesResponse
│   ├── boards.py              # Board enums + models
│   ├── components.py          # Component enums + models
│   ├── firmware.py            # FirmwareJob, JobStatus, JobType
│   ├── preferences.py         # UserPreferences, Theme, DashboardView
│   └── api.py                 # WebSocket protocol models
│
├── controllers/               # Business logic — all state lives here
│   ├── boards.py              # BoardCatalog: 559 boards across 7 platforms
│   ├── components.py          # ComponentCatalog: 655 components
│   ├── devices.py             # DevicesController: CRUD, file scanning, logs
│   ├── firmware.py            # FirmwareController: job queue, compile, install
│   ├── automations.py         # AutomationsController: triggers + actions
│   └── config.py              # ConfigController + DashboardSettings + metadata
│
├── helpers/                   # Pure utilities
│   ├── api.py                 # @api_command decorator
│   ├── event_bus.py           # EventBus
│   ├── json.py                # JSON response, CORS
│   └── yaml.py                # YAML generation
│
├── api/                       # Transport layer
│   ├── ws.py                  # /ws WebSocket dispatch
│   └── legacy.py              # HA compat endpoints
│
└── definitions/               # Data files
    ├── boards/                # board YAML manifests
    ├── components.json        # components definitions (auto generated from schema.esphome.io)
    └── schemas/               # JSON schemas
```

## Controllers

| Controller | Responsibility |
|-----------|---------------|
| Devices | Device CRUD, file scanning, YAML validation, live logs |
| Firmware | Job queue, compile, install, upload, download binaries |
| Boards | Board catalog with search, filtering, pin maps |
| Components | Component catalog with search, config entries |
| Automations | Context-aware triggers + actions |
| Config | Version, serial ports, preferences, secrets |
| Onboarding | First-run setup state (welcome flow, default secrets, sample device) |
| RemoteBuild | mDNS browse + manual host entry + token store + first-use binding for the remote-build offload feature (issue #106) |
| Built-in | ping, subscribe_events |

## Event bus

In-process pub/sub, owned by `DeviceBuilder.bus` (an `EventBus` from `helpers/event_bus`). Controllers fire events on state transitions; WS commands subscribe via `subscribe_events` and stream them to connected clients. Event types are declared in `models/common.py` as `EventType(StrEnum)` members.

### Typing event payloads

`Event` and `EventBus.fire` are generic on the data shape so each event flows through with its TypedDict intact:

```python
@dataclass
class Event[DataT]:
    event_type: EventType
    data: DataT


class EventBus:
    def fire[DataT](self, event_type: EventType, data: DataT) -> None: ...
    def add_listener(
        self,
        event_type: EventType,
        listener: Callable[[Event[Any]], None],
    ) -> Callable[[], None]: ...
```

Each event-specific shape is declared as a `TypedDict` next to the controller that fires it. In `models/remote_build.py`:

```python
class RemoteBuildPairRequestReceivedData(TypedDict):
    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str
```

The fire site uses the TypedDict-call syntax so mypy validates the construction:

```python
self._db.bus.fire(
    EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED,
    RemoteBuildPairRequestReceivedData(
        dashboard_id=dashboard_id,
        pin_sha256=pin_sha256,
        label=label,
        peer_ip=peer_ip,
    ),
)
```

The subscriber narrows by typing its callback's `event` parameter:

```python
def _on_pair_status(event: Event[RemoteBuildPairStatusChangedData]) -> None:
    status = event.data["status"]  # mypy: Literal['approved'] | Literal['removed']

bus.add_listener(EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED, _on_pair_status)
```

`add_listener` is *not* generic on `_DataT` — listeners share a bucket type-erased as `Callable[[Event[Any]], None]` and `Any`'s bidirectional compatibility lets a `Callable[[Event[XData]], None]` register cleanly. The alternative — per-event-type overloads keyed on `Literal[EventType.X]` — was rejected: at end-state it would mean ~42 overloads (21 events × `fire`+`add_listener`), past mypy's practical resolution-perf limits. The trade: the type system enforces the *correct* pairing (subscriber typed for the matching event) but doesn't reject the *wrong* pairing (subscriber typed for a different event). Mismatches live in code review.

Mirrors HA core's `Event[_DataT]` / `EventType[_DataT]` pattern. Deliberate divergence: HA bounds `_DataT` to `Mapping[str, Any]` with that as the default so untyped events fall through; we drop the bound entirely. Untyped fire sites pass plain `dict[str, Any]` and mypy infers `DataT` from the call.

`TypedDict` rather than `@dataclass` because:

- The wire shape is a `dict`, not a class instance. `TypedDict` matches the runtime shape; `@dataclass` would need an `asdict()` step on every fire.
- Subscribers that ride the existing `subscribe_events` WS plumbing serialise the payload through `helpers.json.dumps` (orjson), which handles `dict` natively.
- It mirrors HA's convention so contributors moving between this codebase and HA find the same pattern.

`tests/test_event_payload_contracts.py` pins each TypedDict against its emitter at runtime — for every payload class, a factory invokes the production code path (TypedDict-call constructor or a helper that returns the dict literal as a TypedDict alias) and asserts the resulting dict's keys equal the TypedDict's `__annotations__`. A second test walks `models.*` and asserts every `*Data(TypedDict)` discoverable in the namespace is listed in the factory table — so a future PR adding a TypedDict can't silently skip the contract check.

New events should ship with a TypedDict from day one.

## Firmware Job Queue

Jobs are persistent, event-driven, and decoupled from WebSocket connections:

```
firmware/install {configuration} → QUEUED → RUNNING → output... → COMPLETED/FAILED
                                     │                                    │
                                     └──── persisted to disk ─────────────┘
```

- One job runs at a time, others wait in queue
- Output buffered in `FirmwareJob.output` — survives disconnect
- `firmware/follow_job` sends history then streams live
- Error detection scans output for failure patterns (not just exit code)
- Jobs persist across server restarts

## Component Catalog

`definitions/components.json` is generated by `script/sync_components.py`
from ESPHome's pre-built schema bundle (https://schema.esphome.io). Schema +
narrow live `esphome` introspection cover most fields; `multi_conf`,
`platform_defaults`, `supported_platforms`, type refinement (boolean / float
recovery), and `unit_of_measurement` autocomplete options come from the live
package. Component-level descriptions and titles fall back to the docs MDX
(`esphome-docs` shallow clone) when the schema's index is sparse.

The same script runs nightly via
[`.github/workflows/sync-component-catalog.yml`](../.github/workflows/sync-component-catalog.yml)
— it pins the schema version to the dashboard's installed `esphome` to avoid
drift, runs `script/check_catalog.py` as a regression guard, and opens a
PR with a diff summary when the rebuild produces a change.

## CI / Release pipeline

- **`test.yml`** runs lint + the catalog smoke test on every PR, plus pytest
  across the supported Python matrix. Also callable as a preflight from
  `release.yml`.
- **`release.yml`** is the publish entrypoint — `workflow_dispatch` from
  the Actions tab or `workflow_call` from `auto-release.yml`. Inputs:
  - `version` — `X.Y.Z` for stable, `X.Y.ZbN` for beta.
  - `channel` — `release` or `prerelease`. Format must match (e.g.
    `release` rejects a `b`-suffix tag).

  The workflow stamps `pyproject.toml`, builds wheel + sdist, tags +
  creates the GitHub release with notes drafted from merged-PR labels
  (config in [.github/release-drafter.yml](../.github/release-drafter.yml)),
  attaches both artifacts, and publishes to PyPI. The GitHub release is
  an output of the workflow — don't publish one by hand.

  Tagging + release creation use the `ESPHOME_GITHUB_APP_*` org credentials
  so the workflow keeps working under branch protection. PyPI publish uses
  `PYPI_TOKEN` and is currently `continue-on-error: true` — drop that
  flag once a publish has succeeded.
- **`auto-release.yml`** runs nightly. If ≥ 2 commits have landed on
  `main` since the last release, computes the next prerelease version
  (`X.Y.ZbN` → `X.Y.Zb(N+1)`, or `X.Y.Z` → `X.Y.(Z+1)b1`) and calls
  `release.yml` with `channel=prerelease`. Stable releases are always
  manual.
- **`pr-labels.yaml`** enforces exactly-one-of the changelog labels.
- **`dependabot.yml`** keeps actions and pip dependencies fresh; `esphome`
  itself is pinned manually so the catalog smoke test stays a meaningful
  guard.

All workflow files are commented — start there for the source of truth.

## Authentication

Auth is opaque server-issued session tokens, gated by the WebSocket handshake. See [API.md](API.md#authentication) for the wire protocol.

When `--ha-addon` is set, the server binds **two** TCP sites on a shared `DeviceBuilder` singleton:

- **Public site** (`--host:--port`, default `0.0.0.0:6052`) — the standard dashboard. The auth middleware enforces password on REST endpoints, and the WS handler enforces the in-band `auth` handshake. This is what users hit at `http://homeassistant.local:6052`.
- **Trusted ingress site** (`--ingress-host:--ingress-port`, default `0.0.0.0:8099` inside the addon container) — bound to the supervisor's docker network only, never exposed externally. Skips the auth gate because the supervisor has already authenticated the request upstream. The HA add-on `config.yaml` advertises `ingress_port` to the supervisor so the ingress proxy knows where to forward.

This is the Music Assistant pattern: physically separating the listeners is the security boundary, rather than trusting an `X-Ingress-Path` header. It also means HA app users can keep ingress access (no password) while operators can still secure direct access from outside HA with a username/password.

The legacy `DISABLE_HA_AUTHENTICATION=true` env var skips the ingress site entirely — operators get only the password-gated public port.

### Reverse-proxy / cross-origin deployments

When the dashboard is exposed behind a reverse proxy (nginx, Caddy, Traefik, nginx-proxy-manager, …) under a hostname that doesn't match the upstream bind address, the WS handshake's strict `Origin === Host` check rejects the connection. Operators set `--trusted-domains` (or `$ESPHOME_TRUSTED_DOMAINS`, the legacy ESPHome dashboard env var name) to a comma-separated allowlist of hostnames they want the dashboard to accept:

```bash
# CLI
esphome-device-builder /config --username dash --password ... \
  --trusted-domains dashboard.example.com,proxy.example.com

# Env var (matches the legacy ESPHome dashboard's name)
ESPHOME_TRUSTED_DOMAINS=dashboard.example.com esphome-device-builder /config ...
```

The allowlist drives two checks in the WS handshake (both opt-in; empty = strict legacy behaviour):

- **Origin allowlist** — accepts cross-origin connections whose `Origin` header's hostname is in the list. Required for any reverse-proxy deployment where the proxy hostname differs from the upstream Host.
- **Host allowlist** — rejects any connection whose `Host` header isn't in the list. Defense in depth against DNS rebinding (an attacker domain that resolves to the victim's LAN IP would carry an unfamiliar Host).

Both gates apply only to requests that carry an `Origin` header. Browsers always set `Origin` for the WebSocket opening handshake, so DNS-rebinding attempts land inside the gate; non-browser clients (CLI tools, the HA integration, direct `websockets` clients) omit `Origin` and skip both gates. The in-band `auth` handshake does the work for those clients, and gating on `Origin` means an operator hardening against rebinding doesn't accidentally lock out their HA integration.

Match is case-insensitive and port-tolerant: `dashboard.example.com` accepts `Dashboard.Example.com:8443`. IPv6 may be entered with or without brackets (`::1` and `[::1]` both work). Use `*` as the only entry to opt out of the Host restriction while still permitting cross-origin handshakes (handy when the Host varies per request).

## Discovery (mDNS)

Two mDNS surfaces ride the same `AsyncEsphomeZeroconf` instance the device state monitor already owns. Sharing one Zeroconf singleton matters: opening a second responder fights for the same multicast socket and silently drops half the packets.

**Devices** (`_esphomelib._tcp.local.`) — passive browse. ESPHome devices broadcast on this service type; `DeviceStateMonitor`'s browser callback turns `Added` / `Updated` / `Removed` events into ONLINE / OFFLINE state transitions and TXT-driven config-hash / version / api-encryption updates. See "Two mDNS paths with different OFFLINE semantics" in [CLAUDE.md](../CLAUDE.md) for the asymmetric trust rules between the browser callback and the one-off active-resolve path.

**Dashboards** (`_esphomebuilder._tcp.local.`) — bidirectional. The dashboard advertises its own service instance on startup (skipped in HA-addon mode by default; the addon container's docker IP isn't LAN-routable). TXT carries `server_version` + `esphome_version` always; `pin_sha256` + `remote_build_port` are added when the remote-build receiver site is bound. Browse runs in `RemoteBuildController`, populates `remote_build/list_hosts`, and merges with manually-added `(hostname, port)` rows from `_remote_build.manual_hosts`.

The 15-character RFC 6335 §5.1 cap on service-type labels is why the new type is `_esphomebuilder` (14 chars) rather than `_esphomedashboard` (16, would be truncated). Keeps the `_esphome*` prefix consistent with the existing device service type.

## Remote build

Receiver-side surface for the remote-build offload feature (issue #106). The dashboard can play *receiver* (lend its CPU to other dashboards) and *offloader* (delegate compiles to a paired receiver). Phases 3a–3c shipped the receiver half against an HTTPS+bearer auth model that was wound down across phase 4a-r1 (listener body swap to plain-TCP Noise WS) and phase 4a-r2 (helper deletion). The Noise XX peer-link described below is the production shape today.

### Pairing auth flow (Noise XX)

Pairing is a two-side flow, but in the typical case both sides are operated by the same user with two dashboards open in different tabs (HA add-on + ESPHome Desktop, two HA instances they own, etc.). The trust model already concentrates authority on each side: anyone with shell-level access to either dashboard's `<config_dir>` can read or rotate the X25519 peer-link keypair, mint pair_requests, or accept them, so distributing pair-time authority across multiple humans only makes sense when they're already shell co-administrators of the same deployment. The flow is: open the receiver's Pairing requests screen in one tab, click Pair on the offloader in another, OOB-confirm the pin matches both UIs, click Accept back on the receiver. The two-operator case (a shared deployment) is supported and uses the same protocol; it just means switching tabs becomes "ask my colleague to look at theirs."

Out-of-band pin verification defeats a LAN MITM at first contact (the only window where pinning hasn't established trust yet); the **pairing window** narrows when new requests are even accepted (only while the Pairing requests screen on the receiving dashboard is mounted) so an idle receiver doesn't accumulate inbox noise from arbitrary LAN scanners. Already-approved peers connect anytime for real builds; the window only gates new pair_requests.

The cryptographic primitives are `Noise_XX_25519_ChaChaPoly_SHA256` (mutual identity exchange + forward secrecy) over a dedicated peer-link TCP listener (default port 6055, separate from the dashboard UI port; configurable via `--remote-build-port`). Each dashboard holds a long-lived X25519 keypair as its peer-link identity, persisted at `<config_dir>/.device-builder-peer-link-key.bin` (0o600); `pin_sha256` is the lowercase-hex SHA-256 of the static pubkey.

The numbered phases:

All WS commands below use the `remote_build/` namespace and all events use the `remote_build_` prefix (matching the existing convention in `docs/API.md` and `models/common.py`); the diagram further down strips both for readability.

1. **Discovery** — both dashboards advertise on mDNS (`_esphomebuilder._tcp.local`); TXT carries `remote_build_port` + `pin_sha256` (lowercase-hex SHA-256 of the X25519 peer-link pubkey).
2. **Receiver opens pairing window** — the user opens Settings → Build server → Pairing requests on the receiving dashboard; the frontend calls `remote_build/set_pairing_window` with `open=true`; the backend flips an in-process deadline and fires `remote_build_pairing_window_changed`. The window closes automatically on screen-unmount or user-idle timeout.
3. **Preview pair (intent=preview)** — three Noise XX handshake messages. The offloader captures the receiver's static pubkey from the handshake transcript and surfaces `pin_sha256` to the user; no application data crosses the wire.
4. **OOB pin verification** — human-mediated. The user compares the pin shown on the offloader UI against the receiver UI's Build server card.
5. **Pair request (intent=pair_request)** — fresh Noise XX with payload `{label, dashboard_id}`. If the pairing window is open and no APPROVED row exists yet, the receiver adds a PENDING entry to its in-memory `_pending_peers` dict (no disk write), fires `remote_build_pair_request_received`, and returns `intent_response=pending`. If the window is closed, returns `intent_response=no_pairing_window`. If an APPROVED row already exists with a matching pin, returns `intent_response=approved` immediately (re-pair against existing trust, bypasses window gate).
6. **Receiver-side approve** — user OOB-confirms the offloader's pin, clicks Accept on the receiving dashboard; `remote_build/approve_peer` pops the dict entry, persists it to `settings.peers` as APPROVED, fires `remote_build_pair_status_changed`.
7. **Offloader observes approval (event-pushed, no polling)** — when `request_pair` returns PENDING, the offloader controller writes the row into the unified `_pairings` dict (PENDING status) and spawns one `_pair_status_listener` asyncio task. The listener opens a Noise WS to the receiver with `intent=pair_status`; the receiver-side `lookup_peer_for_status` registers a bus listener for `remote_build_pair_status_changed` filtered to the matching `dashboard_id` and parks until admin clicks Accept / Reject (bus event fires → re-snapshot → return `approved` / `rejected`) or window-close fires the same event with status="removed" for each cleared dict entry. The listener flips the row's status to APPROVED in place + schedules a debounced save through the per-file `Store`, then fires `offloader_pair_status_changed` on the offloader's local bus — any client subscribed to the global `subscribe_events` stream picks the event up; no separate subscription channel.
8. **Subsequent real-build sessions** — `intent=peer_link`. **Not gated by the pairing window**; paired peers connect anytime. The receiver looks up the offloader's static-pubkey-hash against its `StoredPeer` table; an APPROVED match returns `intent_response=ok` and the session stays open for application messages.

```mermaid
sequenceDiagram
    autonumber
    participant OF as Offloader frontend
    participant OB as Offloader backend
    participant RB as Receiver backend
    participant RF as Receiver frontend
    participant RU as Receiver user

    RU->>RF: open Pairing requests screen
    RF->>RB: set_pairing_window open=true
    RB-->>RF: pairing_window_changed expires_in=300

    OF->>OB: preview_pair
    OB->>RB: Noise XX msg1 intent=preview
    RB->>OB: Noise XX msg2 responder pubkey
    OB->>RB: Noise XX msg3 finish
    OB-->>OF: pin_sha256

    Note over OF,RF: OOB pin verification

    OF->>OB: request_pair
    OB->>RB: Noise XX intent=pair_request
    alt pairing window open
        RB->>RB: create StoredPeer PENDING
        RB-->>RF: pair_request_received
        RB-->>OB: intent_response=pending
    else window closed
        RB-->>OB: intent_response=no_pairing_window
    end

    RU->>RF: OOB-confirm pin, click Accept
    RF->>RB: approve_peer
    RB->>RB: PENDING to APPROVED
    RB-->>RF: pair_status_changed approved

    Note over OF,OB: live updates ride existing subscribe_events stream
    OB->>RB: Noise XX intent=pair_status (await flip)
    Note over RB: bus.listening on pair_status_changed<br/>filtered to dashboard_id
    RB-->>OB: intent_response=approved (on RU click)
    OB-->>OF: offloader_pair_status_changed status=approved

    OB->>RB: Noise XX intent=peer_link
    RB-->>OB: intent_response=ok
```

**Why two Noise handshakes for one pairing.** The preview handshake (step 3) captures the receiver's static pubkey for OOB display *before* the offloader has decided to trust this receiver; the WS closes immediately, no application data crosses the wire. The pair-request handshake (step 5) is a fresh handshake that re-binds the OOB-confirmed pin (defends against TOCTOU between preview and confirm: if the pubkey-hash on the second handshake doesn't match `pin_sha256` from preview, the offloader aborts). Re-handshakes are cheap because Noise's setup cost is negligible at this cadence (pair flows are rare, not a hot path).

**Why long-poll instead of polling.** The pair-status path holds a Noise WS open with `intent=pair_status` for each PENDING row. The receiver-side `lookup_peer_for_status` parks on its own bus's `pair_status_changed` event filtered to the matching `dashboard_id` and pushes the response when admin clicks Accept / Reject — sub-second flip latency without a poll cadence. Transport errors retry after a 2s backoff; terminal flips (APPROVED / REJECTED) exit the listener.

**PENDING is in-memory only, bounded by the pairing window.** Disk only carries APPROVED rows. Receiver-side: `RemoteBuildController._pending_peers: dict[str, StoredPeer]` holds PENDING peers for the *active pairing window's* lifetime; the dict is cleared on every window-close transition (auto-close timeout, explicit `set_pairing_window(open=False)`, controller `stop()`). The clear path fires `pair_status_changed("removed")` for each cleared entry so any in-flight pair_status long-poll wakes, re-snapshots, and reports REJECTED to its offloader; the offloader's listener then drops its own pending state. Offloader-side: a single `_pairings: dict[tuple[str, int], StoredPairing]` carries both PENDING and APPROVED rows — the per-file `Store` at `<config_dir>/.offloader_pairings.json` filters PENDING out at serialise time so the on-disk shape stays APPROVED-only, and the dict is the canonical source of truth at runtime. Three load-bearing properties fall out of this:

1. **A malicious LAN scanner can't fill the receiver's settings file with junk pair-requests** even within an open window — the dict is RAM-bounded by window lifetime, never persisted, and capped by admin's screen-mounted attention span (typically minutes).
2. **The pair_status long-poll's window-gate is implicit** — closed-window means the dict is empty, so any pair_status query returns REJECTED naturally via the `_lookup_peer_response` dict-then-list lookup. No separate `is_pairing_window_open()` check needed at the snapshot path.
3. **Cold-start has no PENDING state** — a controller restart means the dict starts empty; any in-flight pair attempts have to be re-initiated by the offloader. There is no respawn-on-subscribe path because the offloader doesn't have a separate subscription channel; live updates ride the existing global `subscribe_events` stream as `offloader_pair_status_changed` events fired by the per-row listener task.

**The `pair_request` window-gate.** Lives inside `record_pair_request`, not at the WS dispatcher. New offloaders (no row anywhere) and refresh of an existing PENDING dict entry are gated; `pair_request` against an *already-APPROVED* row + matching pin bypasses the window check (re-pair against existing trust requires no admin authorization, so the network-blip-retry case stops surfacing NO_PAIRING_WINDOW just because admin's screen happens to be closed). APPROVED + drifted pin returns REJECTED regardless of window state — rotation-or-impersonation signal that admin must explicitly handle via `remove_peer` then re-pair.

**Window-state disclosure.** The `no_pairing_window` response from `record_pair_request` only reaches an offloader whose `dashboard_id` doesn't match an APPROVED row (the APPROVED check short-circuits ahead of the window gate). Random callers / unknown peers get the same NO_PAIRING_WINDOW response when window is closed, so the window flag is observable to anyone who can reach the listener — but it's not informationally useful: the listener's mDNS TXT broadcasts `pin_sha256` + `remote_build_port` only while bound, which is itself the strongest signal of the receiver's overall pair-acceptance state.

**Identity rotation.** The peer-link X25519 keypair has its own rotation lifecycle (`rotate_peer_link_identity`), independent of the phase-3a Ed25519 cert. Rotating the 3a cert does NOT change the X25519 pubkey; only `rotate_peer_link_identity` does. When the user rotates, the `dashboard_id` stays stable but `pin_sha256` changes; every paired peer sees a `pin_mismatch` event on the next handshake and has to re-pair (this is the desired behaviour for "operator suspects compromise"). The separate-keypair design was decided during PR #472 review: the alternative (deriving X25519 from Ed25519 via libsodium-style `crypto_sign_ed25519_sk_to_curve25519`) adds non-trivial code for no benefit pre-release, and an implicit cascade would hide a security-relevant rotation event behind a routine cert renewal.

### Listener internals

**Second TCP listener.** When `_remote_build.enabled` is `true`, `DeviceBuilder` binds an aiohttp `TCPSite` on `--remote-build-port` (default 6055) serving `/remote-build/peer-link`. Disabled by default; the listener doesn't bind at all when the toggle is off (a sidecar `enabled=false` skip beats default-deny 404s — nothing to probe). This sits alongside the public + ingress sites from the Authentication section: HA-addon mode with remote-build enabled binds three listeners on three different ports, each with its own role.

**Middleware.** A single `_strip_server_header_middleware` overrides aiohttp's `Server: Python/x.y aiohttp/z.w` banner to empty string on the peer-link site. (Setting to empty wins; `del response.headers["Server"]` doesn't catch the connection-level injection.)

**Identity** (`helpers/dashboard_identity` + `helpers/peer_link_identity`). On first dashboard start, two long-lived identities are minted:

* **Dashboard cert + `dashboard_id`** (phase 3a): an Ed25519 self-signed cert (100-year validity, SAN=localhost, EKU=SERVER_AUTH critical), persisted as `.device-builder-cert.pem` + `.device-builder-key.pem` next to the metadata sidecar, plus a stable random `dashboard_id` under `_remote_build.dashboard_id`. The cert is no longer used by the receiver listener post-pivot; `dashboard_id` is still load-bearing as the offloader-presented identifier on every Noise pair_request / peer_link / pair_status frame. `rotate_certificate` survives as the rotation hook for `dashboard_id` provenance, and as a side effect tears down + rebuilds the listener (which reloads the X25519 peer-link identity from disk).
* **Peer-link X25519 keypair** (phase 4a-r1 part 2): a 32-byte raw X25519 secret persisted at `<config_dir>/.device-builder-peer-link-key.bin` (0o600). This is the keypair the Noise XX handshake exchanges; `pin_sha256` advertised in mDNS TXT is the lowercase-hex SHA-256 of the static pubkey. Loaded once at handler-factory time and captured in the Noise dispatch closure for the listener's lifetime; rotation rebuilds the listener.

The TXT contract — `pin_sha256` + `remote_build_port` appear together iff the listener is currently bound — holds across rotation. When the listener isn't bound, rotation only writes new keys to disk; mDNS isn't updated because there's no listener for peers to connect to.

## Persisted state and security expectations

The dashboard writes a small set of files into `<config_dir>` and treats them as durable per-installation state. A few have non-obvious security expectations.

| File | Sensitivity | Mode |
|---|---|---|
| `.device-builder.json` | Mostly identifier-only (`dashboard_id`, `_remote_build.enabled`, `manual_hosts`, `peers[]`). The `peers[]` rows carry the offloader's `pin_sha256` (X25519 pubkey hash) and `static_x25519_pub` — neither is secret on its own, but a reader of the sidecar can enumerate which `dashboard_id`s have paired. | umask default |
| `.offloader_pairings.json` | Offloader-side pinned receivers (`StoredPairing` rows: `(receiver_hostname, receiver_port, pin_sha256, static_x25519_pub, label, paired_at, status)`). Owned by `helpers.storage.Store` with debounced writes; only APPROVED rows ever reach disk (PENDING is filtered out at serialise time). Same secret-equivalent shape as the receiver's `peers[]`: a reader can enumerate which receivers this offloader has paired with, but neither pin nor pubkey is secret on its own. | 0o600 enforced at write time (default for `Store`) |
| `.device-builder-cert.pem` | Public TLS cert (self-signed at first start). Dormant for transport post-pivot — the listener uses Noise XX (X25519) for transport security; the cert is now read only for `get_identity`'s `pin_sha256` field, which still reports the cert SPKI fingerprint until phase 4b+ swaps it for the X25519 peer-link pin. Not sensitive. | mkstemp default (0o600) |
| `.device-builder-key.pem` | **Private TLS key for the self-signed dashboard cert. Sensitive.** Currently dormant for transport (Noise XX uses X25519 instead), but a reader of this file could impersonate the dashboard to any consumer that started using the cert again. The cert + key get rotated as a unit by `rotate_certificate`, which also tears down + rebuilds the listener (a side-effect that reloads the X25519 peer-link identity from disk). | 0o600 enforced at write time |
| `.device-builder-peer-link-key.bin` | **Private X25519 peer-link key. Sensitive.** A reader of this file can impersonate the dashboard to any paired peer over the Noise XX handshake — this is the load-bearing transport-security key post-pivot. | 0o600 enforced at write time |

**Backup tools must preserve `0o600` on `.device-builder-key.pem`.** The dashboard writes the file at the right mode via `tempfile.mkstemp` + `os.replace`, but a tar-then-restore-as-different-user round-trip can land it at the umask default. Operators backing up `<config_dir>` should use a tool that captures and restores POSIX modes (e.g. `tar --preserve-permissions`, `rsync -p`, `restic`). The dashboard does *not* re-tighten the mode on every load (the load-time chmod was deliberately removed as untested defensive code) — once relaxed it stays relaxed until the next `rotate_certificate` call.

**The dashboard expects — and enforces — exactly one process per `<config_dir>`.** Identity files, the metadata sidecar, and the build tree are all guarded by per-process `threading.Lock`s; two `device-builder` processes running against the same config directory would race on writes. Startup takes an exclusive `fcntl.flock` on `<config_dir>/.device-builder.lock` (see `helpers/single_instance.ensure_single_execution`); a second start refuses with the running PID + start time on stderr. The OS releases the lock on process exit, so a stale lock file with no holder is harmless and re-acquired cleanly. Windows lacks `fcntl` and the check is a silent no-op there; the HA-addon shape (the dominant production target) is POSIX-only, and dev / Desktop on Windows accept the residual race risk in exchange for not needing `msvcrt.locking` plumbing. If a multi-process model is ever needed, the per-process `threading.Lock`s would also need to become cross-process file-locks.

**`dashboard_id` is an identifier, not a secret.** It's shared with paired peers as part of pairing handshakes (sent in the encrypted msg3 payload of the Noise XX handshake on every `pair_request` / `peer_link` / `pair_status` frame). A leaked metadata sidecar reveals the ID but doesn't, on its own, grant access — the X25519 peer-link key (the load-bearing secret) is what the receiver pins against. The `dashboard_id` is **not** published in mDNS TXT — only `pin_sha256` + `remote_build_port` are advertised; peers learn each other's IDs as part of pairing.

## Deployment

### Beta (HA add-on)

Toggle `new_dashboard_beta` in the ESPHome add-on. Pip-installs the device builder and runs it.

### Production

Baked into the ESPHome container. Legacy dashboard deprecated.

## Legacy HA Compatibility

`api/legacy.py` serves: `GET /devices`, `GET /json-config`, `/compile`, `/upload` (spawn protocol).
