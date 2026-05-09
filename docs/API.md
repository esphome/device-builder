# API Reference

Base URL: `http://localhost:6052`

## WebSocket API (`/ws`)

The primary API. A single multiplexed WebSocket handles all 44 commands.

### Protocol

**Connect:** `ws://localhost:6052/ws`

On connect, the server sends a [`ServerInfoMessage`](../esphome_device_builder/models/api.py):
```json
{"server_version": "0.0.0", "esphome_version": "2026.3.1", "port": 6052, "ha_addon": false, "requires_auth": false}
```

**Send a [`CommandMessage`](../esphome_device_builder/models/api.py):**
```json
{"command": "devices/list", "message_id": "1", "args": {}}
```

**Receive a [`ResultMessage`](../esphome_device_builder/models/api.py):**
```json
{"message_id": "1", "result": { ... }}
```

**Streaming output ([`EventMessage`](../esphome_device_builder/models/api.py)):**
```json
{"message_id": "1", "event": "output", "data": "Compiling...\n"}
{"message_id": "1", "event": "result", "data": {"success": true, "code": 0}}
```

**Error ([`ErrorMessage`](../esphome_device_builder/models/api.py)):**
```json
{"message_id": "1", "error_code": "unknown_command", "details": "..."}
```

### Error Codes ([`ErrorCode`](../esphome_device_builder/models/api.py))

| Code | Description |
|------|-------------|
| `invalid_message` | Malformed JSON or missing fields |
| `unknown_command` | Command not found |
| `invalid_args` | Missing or invalid arguments |
| `not_found` | Resource not found |
| `internal_error` | Server error |
| `not_authenticated` | Connection has not authenticated; only `auth/login` is accepted |
| `rate_limited` | Too many failed login attempts from this IP |

### Enums

| Enum | Values | Description |
|------|--------|-------------|
| `DeviceState` | `unknown`, `online`, `offline` | Device connectivity state (mDNS + ping) |

---

## Commands

### Authentication

> Controller: [`AuthController`](../esphome_device_builder/controllers/auth.py)

When the dashboard is started with `--username`/`--password` (or `$ESPHOME_USERNAME`/`$ESPHOME_PASSWORD` env vars), every WebSocket connection on the public port must authenticate before any other command will be accepted.

The handshake:

1. Server sends `ServerInfoMessage` with `requires_auth: true`.
2. Client sends `auth/login` (or its alias `auth`) with either `{username, password}` or a previously issued `{token}`.
3. Server replies with `{token, expires_at}`.
4. Subsequent commands on the same connection are accepted normally.

Tokens are opaque random strings, persisted to `<config>/.device-builder-sessions.json`, and auto-refresh on each use (sliding 30-day window). Frontends should store the token in `localStorage` and reuse it on reconnect — only fall back to the password form on `not_authenticated`.

Connections that arrive on the trusted ingress site (HA add-on supervisor proxy) get `requires_auth: false` and skip the handshake entirely.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `auth/login` (alias: `auth`) | `{username, password}` *or* `{token}` | `{token, expires_at}` | Authenticate this connection |
| `auth/logout` | — | `{logged_out: true}` | Revoke the current token; closes the connection |
| `auth/refresh` | — | `{token, expires_at}` | Slide the expiry forward without making another API call |

**Bearer header (non-browser clients).** Anything that can set HTTP headers — the HA `esphome-dashboard-api` client, CLI tools, scripts — may pass `Authorization: Bearer <token>` on the WS handshake or on a REST request. The server treats that as equivalent to a successful in-band `auth/login {token}` call.

**Basic auth (REST only).** Legacy REST endpoints also accept `Authorization: Basic <base64(user:pass)>`. WebSocket clients can't use this because browsers don't allow setting headers on `new WebSocket(...)`.

**Rate limiting.** After 10 failed login attempts from one IP within a 5-minute window, that IP is locked out for 5 minutes. A successful login clears the failure history immediately. Token-based logins (replays) are exempt — brute-forcing 256 bits of token entropy is infeasible, and rate-limiting valid replays would lock legitimate clients out after a network blip.

### Devices

> Models: [`Device`](../esphome_device_builder/models/devices.py), [`DevicesResponse`](../esphome_device_builder/models/devices.py)
>
> Controller: [`DevicesController`](../esphome_device_builder/controllers/devices.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `devices/list` | — | `DevicesResponse` | List configured + importable devices |
| `devices/get_states` | — | `dict` | Get device online/offline states |
| `devices/create` | `{name, board_id, config_type?, ssid?, psk?, file_content?}` | `WizardResponse` | Create device from board definition |
| `devices/update` | `{name, friendly_name?, comment?, board_id?}` | `UpdateDeviceResponse` | Update device metadata |
| `devices/set_labels` | `{configuration, label_ids: string[]}` | `Device` | Replace this device's label assignments. Pass `[]` to clear. Unknown ids return `INVALID_ARGS`. Fires `device_updated` after the scanner reload. |
| `devices/rename` | `{configuration, new_name}` | — | Rename device via ESPHome CLI |
| `devices/delete` | `{configuration}` | — | Delete device and associated files |
| `devices/delete_bulk` | `{configurations: string[]}` | `[{configuration, success, error?}]` | Delete multiple devices |
| `devices/archive` | `{configuration}` | — | Soft-delete: move YAML to `<config_dir>/archive/`, wipe build dir, wipe StorageJSON + device-metadata sidecars. Reversible via `devices/unarchive` (cached IP/version/hash refill from the next mDNS broadcast). |
| `devices/archive_bulk` | `{configurations: string[]}` | `[{configuration, success, error?}]` | Archive multiple devices at once. Same per-item shape as `devices/delete_bulk`. |
| `devices/unarchive` | `{configuration}` | — | Move an archived YAML back into the active config directory. Errors with `INVALID_ARGS` if an active config with the same filename already exists. |
| `devices/list_archived` | — | `[{configuration, name, friendly_name, comment}]` | List archived devices for the dashboard's archived-devices dialog. |
| `devices/delete_archived` | `{configuration}` | — | Permanently delete an archived YAML and its sidecars. The companion to `unarchive` for "I really don't want this back". |
| `devices/get_config` | `{configuration}` | `string` | Read device YAML config |
| `devices/update_config` | `{configuration, content}` | — | Write device YAML config |
| `devices/add_component` | `{configuration, component_id, fields?, sub_entities?}` | `AddComponentResponse` | Add component to device config |
| `devices/import` | `{name, project_name?, package_import_url?, ...}` | `dict` | Import/adopt discovered device |
| `devices/ignore` | `{name, ignore?}` | — | Toggle device visibility |
| `devices/validate` | `{configuration}` | Streaming | Validate YAML config |
| `devices/logs` | `{configuration, port?}` | Streaming | Stream live device logs |

`Device.state`: `DeviceState` — `unknown`, `online`, or `offline` (discovered via mDNS + ping).
`Device.has_pending_changes`: `true` = config changed since last compile, `false` = up to date, `null` = never compiled.
`Device.update_available`: `true` = device was compiled with a different ESPHome version than the server.

### Firmware

> Models: [`FirmwareJob`](../esphome_device_builder/models/firmware.py), [`JobStatus`](../esphome_device_builder/models/firmware.py), [`JobType`](../esphome_device_builder/models/firmware.py)
>
> Controller: [`FirmwareController`](../esphome_device_builder/controllers/firmware.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `firmware/compile` | `{configuration}` | `FirmwareJob` | Queue compile job |
| `firmware/upload` | `{configuration, port?: ""}` | `FirmwareJob` | Queue upload of existing binary. `port` defaults to `""` (no `--device` arg — CLI auto-detects). Also accepts `"OTA"`, a serial path (`/dev/ttyUSB0`, `COM3`), or an explicit IP / hostname for "install to a specific address" — the address-cache shortcut is bypassed when a target is named directly. |
| `firmware/install` | `{configuration, port?: "OTA" \| serial \| ip \| hostname}` | `FirmwareJob` | Queue compile + upload. `port` defaults to `"OTA"` (let the CLI resolve the configured host). Same `port` semantics as `firmware/upload` for non-default values. |
| `firmware/clean` | `{configuration}` | `FirmwareJob` | Queue build clean for one device |
| `firmware/reset_build_env` | — | `FirmwareJob` | Queue full reset of `.esphome/` build dirs and PIO cache |
| `firmware/compile_bulk` | `{configurations: string[]}` | `[FirmwareJob]` | Queue multiple compiles |
| `firmware/install_bulk` | `{configurations: string[], port?: "OTA" \| serial \| ip \| hostname}` | `[FirmwareJob]` | Queue multiple installs. `port` defaults to `"OTA"` and is shared across every queued job — almost always callers want that default rather than a single explicit target across the fleet. Same `port` validation as `firmware/install`. |
| `firmware/get_jobs` | `{status?, configuration?}` | `[FirmwareJob]` | List jobs with filters |
| `firmware/get_job` | `{job_id}` | `FirmwareJob` | Get job with full output |
| `firmware/follow_job` | `{job_id}` | Streaming | Historical output + live stream for one job |
| `firmware/follow_jobs` | `{snapshot?: true}` | Streaming | All jobs' lifecycle + output + progress |
| `firmware/get_binaries` | `{configuration}` | `[{title, file}]` | List compiled firmware files |
| `firmware/download` | `{configuration, file, compressed?}` | `{filename, data, size}` | Download binary (base64) |
| `firmware/cancel` | `{job_id}` | — | Cancel queued or running job |
| `firmware/clear` | `{status?}` | — | Remove finished jobs |

**Job queue**: one job runs at a time, others wait. Jobs persist across server restarts. Output buffered in `FirmwareJob.output` — clients can reconnect via `firmware/follow_job`.

**One active job per device**: queuing a new job for a device cancels any existing queued or running job with the same `configuration` first. The cancelled job fires `JOB_CANCELLED` as usual, then the new job fires `JOB_QUEUED` — frontends following lifecycle events stay consistent with the "show the latest result" UX. `firmware/reset_build_env` is global (empty `configuration`) and is exempt from this rule.

**History retention**: terminal `compile`/`upload`/`install` jobs are kept in a global pool capped at 50, deduplicated to one entry per `configuration` (newest wins). Terminal `clean`/`reset_build_env` jobs sit in a separate pool capped at 5 so they don't crowd device history. Active (queued/running) jobs are exempt from pruning. Each retained job's `output` is trimmed to the last 2000 lines on terminal transition; a synthetic first line `... [output trimmed: N earlier line(s) elided]` indicates how many lines were dropped. `firmware/clear` still wipes terminal jobs on demand.

**`firmware/reset_build_env`**: wipes `.esphome/build/`, `.esphome/external_components/`, and `.esphome/platformio_cache/` so the next compile re-fetches external components and re-downloads PlatformIO toolchains. Returns a `FirmwareJob` with empty `configuration` and `job_type: "reset_build_env"`. Streams progress through the same `JOB_OUTPUT` event as compile jobs. Mid-run cancellation is honoured between the three target directories, not during a single removal.

**Cancel semantics**:
- Queued jobs flip to `cancelled` immediately.
- Running jobs receive SIGTERM, with SIGKILL escalation after a 3 s grace period. The job's status becomes `cancelled` (not `failed`) and `JOB_CANCELLED` fires.

**Progress**: `FirmwareJob.progress` is an `int | null` 0–100 latched from the highest percentage seen in `[ 17%] Compiling …` (PlatformIO) or `Writing at 0x… (45 %)` (esptool) lines. `null` means the tooling hasn't emitted a percentage yet — most early compile output is opaque. The value is monotonically non-decreasing within a job so the UI doesn't appear to regress between phases.

**Job events** (broadcast to all subscribed clients):
- `job_queued`, `job_started`, `job_output`, `job_progress`, `job_completed`, `job_failed`, `job_cancelled`

**`firmware/follow_jobs` stream events** (per WebSocket subscription):
- `snapshot` — initial replay of every retained job (one event per job, payload is the full `FirmwareJob`). Includes both active and the trimmed terminal history, so a client gets the complete picture from a single subscription with no extra `firmware/get_jobs` call. Skipped when `snapshot: false`.
- `job_queued` / `job_started` / `job_completed` / `job_failed` / `job_cancelled` — full `FirmwareJob` payload.
- `job_output` — `{job_id, line}` (line keeps its `\n` or `\r` terminator).
- `job_progress` — `{job_id, progress}` (0–100 integer).

The subscription stays open for the connection's lifetime; closing the WebSocket cancels the stream.

### Boards

> Controller: [`BoardCatalog`](../esphome_device_builder/controllers/boards.py)
>
> Enums: [`Platform`](../esphome_device_builder/models/boards.py), [`Esp32Variant`](../esphome_device_builder/models/boards.py), [`BoardTag`](../esphome_device_builder/models/boards.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `boards/get_boards` | `{query?, platform?, variant?, tag?, offset?, limit?}` | `PagedBoardsResponse` | Search/list boards |
| `boards/get_board` | `{board_id}` | `BoardCatalogEntry` | Get board with pin map |

`BoardCatalogEntry` carries two recommendation lists for the Add Component dialog:

- `featured_components: list[FeaturedComponent]` — components recommended for this board, surfaced in the catalog API as `featured.<board_id>.<local_id>` under category `featured`. Each entry can override the catalog `name`/`description` and pre-fill any subset of the underlying component's `config_entries` via a `fields` map keyed by `ConfigEntry.key`. Three preset modes per field:
  - **default**: a primitive value the frontend pre-fills; user can change it.
  - **locked**: `{value, locked: true}` — frontend disables the input and `devices/add_component` rejects deviating user values.
  - **suggestions**: `{suggestions: [...]}` — frontend renders a picker, user must pick from the list.
- `featured_bundles: list[FeaturedBundle]` — `{id, name, description, component_ids}` groups of featured components (e.g. "Status LED" = `output.gpio` + `light.binary`). The frontend triggers sequential `devices/add_component` calls for each `component_id` when the user adds a bundle.

### Components

> Controller: [`ComponentCatalog`](../esphome_device_builder/controllers/components.py)
>
> Enums: [`ComponentCategory`](../esphome_device_builder/models/components.py), [`ConfigEntryType`](../esphome_device_builder/models/common.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `components/get_categories` | `{board_id?}` | `[{id, name, count}]` | List categories with counts |
| `components/get_components` | `{query?, category?, exclude_category?, platform?, board_id?, offset?, limit?}` | `PagedComponentsResponse` | Search/list components |
| `components/get_component` | `{component_id, platform?, board_id?}` | `ComponentCatalogEntry` | Get component with config entries |

`platform` filters to components compatible with the given target platform; components with an empty `supported_platforms` list are platform-agnostic and always included. `board_id` is a convenience — the boards catalog resolves it to a platform; `platform` wins when both are passed. The platform is also used to materialise each entry's `platform_defaults` into `default_value`.

`category` / `exclude_category` accept either a single category or a list. Use `exclude_category` for the regular catalog selector to hide entries that belong to the dedicated "Add core configuration" dialog.

**Featured components.** The board catalog's `featured_components` are surfaced through this same API under the synthetic category `featured` and ID prefix `featured.<board_id>.<local_id>`. They are **only** returned when `category` explicitly includes `featured` and `board_id` is supplied — the regular catalog listing never mixes them in. `get_categories` adds a `featured` entry with the board's recommended-count when `board_id` is set. A featured `ComponentCatalogEntry` carries the board overrides baked into its `config_entries`: `default_value` reflects the preset, and the new `locked: bool` and `suggestions: list[ConfigPrimitive] | None` fields tell the frontend to disable the input or render a picker. `devices/add_component` recognises `featured.*` ids — the wire shape doesn't change, but the backend resolves the underlying component, validates user input against the locked/suggestion constraints, and merges presets before delegating to the regular merge logic.

### Automations

> Controller: [`AutomationsController`](../esphome_device_builder/controllers/automations.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `automations/get_triggers` | `{platform_type?}` | `[AutomationTrigger]` | List triggers by platform type |
| `automations/get_actions` | — | `[AutomationAction]` | List all actions |
| `automations/get_available` | `{configuration}` | `{triggers, actions, present_platform_types}` | Context-aware for a device |

### Config

> Controller: [`ConfigController`](../esphome_device_builder/controllers/config.py)
>
> Models: [`UserPreferences`](../esphome_device_builder/models/preferences.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `config/version` | — | `{server_version, esphome_version}` | Get versions |
| `config/serial_ports` | — | `[{port, desc}]` | List serial ports |
| `config/get_preferences` | — | `UserPreferences` | Get user preferences |
| `config/set_preferences` | `{theme?, dashboard_view?, ...}` | `UserPreferences` | Update preferences (partial) |
| `config/get_secrets` | — | `[string]` | List secret key names |

### Onboarding

> Controller: [`OnboardingController`](../esphome_device_builder/controllers/onboarding.py)
>
> Models: [`OnboardingState`, `OnboardingStep`, `OnboardingStepId`, `OnboardingStepStatus`](../esphome_device_builder/models/onboarding.py)

First-run setup tracking. Each step's `status` is computed from live data on every `get_state` call (never persisted), so the frontend's "needs attention" indicators clear the moment the user fixes the underlying state — even via a manual `secrets.yaml` edit. `completed_version` is the last onboarding-flow version the user has explicitly acknowledged; bumping `ONBOARDING_VERSION` (server-side constant) re-prompts users at lower versions when new steps are added.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `onboarding/get_state` | — | `OnboardingState` | Snapshot of current vs acknowledged version + per-step `pending` / `done` status. Currently one step (`wifi_credentials`) — pending when `secrets.yaml`'s `wifi_ssid` is missing, empty, whitespace-only, or matches the bootstrap placeholder. |
| `onboarding/set_wifi_credentials` | `{ssid, password?}` | `OnboardingState` | Update `wifi_ssid` / `wifi_password` in `secrets.yaml` via a line-based rewrite that preserves standalone and inline trailing comments and other secrets. Validates against ESPHome's own length limits (32 char SSID, 64 char password) plus a control-character check; empty / whitespace-only SSID, oversize values, and control characters (other than `\t`) raise `INVALID_ARGS`. `password` is optional and defaults to the empty string for open networks. |
| `onboarding/mark_acknowledged` | — | `OnboardingState` | Record that the user has finished the current onboarding flow (sets `onboarding_completed_version` to `ONBOARDING_VERSION`). Idempotent and monotonic — never downgrades a higher stored value. Use this on save AND on explicit decline ("I don't use Wi-Fi") so the wizard stops re-popping; the per-step `pending` status stays accurate so the dedicated `Set up Wi-Fi…` kebab entry still surfaces the re-entry path until the underlying data is set. |

### Labels

> Models: [`Label`](../esphome_device_builder/models/labels.py)
>
> Controller: [`LabelsController`](../esphome_device_builder/controllers/labels.py)

User-defined chips (name + optional `#rrggbb` color) that can be assigned to devices via `devices/set_labels`. The catalog is global; assignments live on each device's `Device.labels` field as a list of label ids.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `labels/list` | — | `[Label]` | Return every label in the global catalog |
| `labels/create` | `{name, color?}` | `Label` | Create a label. `name` 1-50 chars, unique case-insensitive. `color` is `#rrggbb` (lowercased on save) or null. Server generates `id`. Fires `label_created`. |
| `labels/update` | `{label_id, name?, color?}` | `Label` | Rename and / or recolor. Pass `color: null` to clear; omit `color` to leave it unchanged. Fires `label_updated`. |
| `labels/delete` | `{label_id}` | `{deleted: true}` | Delete a label and cascade — every device entry with this id has it removed in the same transaction, then each affected device fires `device_updated`; finally `label_deleted` fires. |

Renaming or recoloring a label leaves device assignments untouched — devices reference labels by id, not by name. The frontend is expected to subscribe to `subscribe_events`, fetch the catalog once via `labels/list`, then resolve ids → name + color at render time.

### Remote Build

> Controller: [`RemoteBuildController`](../esphome_device_builder/controllers/remote_build.py)
>
> Models: [`RemoteBuildSettingsView`](../esphome_device_builder/models/remote_build.py), [`RemoteBuildPeer`](../esphome_device_builder/models/remote_build.py), [`ManualHost`](../esphome_device_builder/models/remote_build.py), [`PeerSummary`](../esphome_device_builder/models/remote_build.py), [`IdentityView`](../esphome_device_builder/models/remote_build.py)

Receiver-side surface for the remote-build offload feature (issue #106). Discovers peer dashboards via mDNS (`_esphomebuilder._tcp.local.`), lets the user add manual peers for cross-subnet LANs, and pairs with offloaders over the peer-link Noise WS (`/remote-build/peer-link`, default port 6055). Receiver-side state persists in `.device-builder.json` under the `_remote_build` key; offloader-side pairings live in their own sibling file `<config_dir>/.offloader_pairings.json` (per-file `helpers.storage.Store` with debounced writes — atomic per-domain, no lock contention against unrelated metadata writers).

The pre-pivot HTTPS+bearer auth surface (phases 3b1-3c) was wound down across phase 4a-r1 (listener body swap to Noise WS) and phase 4a-r2 (helper deletion); only the WS commands below ship today.

#### Surface map: which commands run on which side

A single `device-builder` process can be a *receiver* (accepts Noise WS connections from offloaders, lets a human admin pair them) and an *offloader* (initiates Noise WS connections to receivers it has pinned) at the same time. Each WS command targets one role. The frontend surfaces them on different Settings screens — "Build server" (receiver role) vs "Send builds" (offloader role). All commands run over the dashboard's main `/ws` endpoint and inherit whatever auth that endpoint enforces (today: none — the dashboard `/ws` trusts any local connection); none of these commands run over the peer-link Noise WS, which carries only `intent=...` frames between dashboards, never WS commands.

| Command | Side | Notes |
|---|---|---|
| `list_hosts` / `add_manual_host` / `remove_manual_host` | both | Discovery surface; the same dashboard browses receivers it can offload TO, and lists itself among receivers other dashboards see. |
| `get_settings` / `set_settings` | receiver | Master toggle for whether this dashboard accepts incoming offloader connections. Off-default; toggling requires a restart. |
| `list_peers` / `approve_peer` / `remove_peer` | receiver | Admin manages incoming pairings. |
| `set_pairing_window` | receiver | Frontend-driven; the Pairing requests screen calls `open=true` on mount + extend ticks, `open=false` on unmount. |
| `get_identity` / `rotate_identity` | receiver | Surfaces / rotates the dashboard's identity for OOB pin verification. |
| `preview_pair` | offloader | Open a brief Noise WS to capture a receiver's pin for OOB display. |
| `request_pair` | offloader | Send `intent=pair_request`. Both PENDING and APPROVED rows live in the controller's unified `_pairings` dict; the per-file `Store` debounce-saves APPROVED rows to `<config_dir>/.offloader_pairings.json` (PENDING is filtered out at serialise time). APPROVED result spawns no listener; PENDING result spawns a `_pair_status_listener` task that flips the row's status on flip. |
| `unpair` | offloader | Drop the row from the unified `_pairings` dict and schedule the debounced save. Cancels the row's listener task if any. Idempotent. |

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `remote_build/list_hosts` | — | `[RemoteBuildPeer]` | Discovered (`source=mdns`) and manually-added (`source=manual`) peer dashboards merged into one list. |
| `remote_build/get_settings` | — | `RemoteBuildSettingsView` | Read the receiver-side settings (`enabled`, `manual_hosts`, `peers`). |
| `remote_build/set_settings` | `{enabled}` | `RemoteBuildSettingsView` | Persist the master switch. Strict-bool; rejects truthy strings. |
| `remote_build/add_manual_host` | `{hostname, port}` | `RemoteBuildSettingsView` | Add a manual peer for cross-subnet / non-mDNS LANs. Hostname normalised to lowercase. Duplicate `(hostname, port)` raises `already_exists`. |
| `remote_build/remove_manual_host` | `{hostname, port}` | `RemoteBuildSettingsView` | Remove a manual peer. Unknown pair raises `not_found`. |
| `remote_build/list_peers` | — | `[PeerSummary]` | Receiver-side list of paired (or pending) offloaders, projected to drop the raw X25519 pubkey bytes. |
| `remote_build/approve_peer` | `{dashboard_id}` | `RemoteBuildSettingsView` | Promote a `PENDING` row to `APPROVED`. Fires `remote_build_pair_status_changed`. |
| `remote_build/remove_peer` | `{dashboard_id}` | `RemoteBuildSettingsView` | Drop a peer row. PENDING entries live in the controller's in-memory dict; APPROVED rows live in `settings.peers`. Fires `remote_build_pair_status_changed` with `status="removed"` for either case (the event wakes any in-flight pair_status long-poll, which is needed for the PENDING case to drop the offloader's local state). `not_found` when neither dict nor list has a matching row. |
| `remote_build/set_pairing_window` | `{open}` | — | Open / close the pairing window for the calling WS client. The window narrows when `intent="pair_request"` Noise frames are even accepted; refcounted across clients with auto-close timeout. Fires `remote_build_pairing_window_changed` on transitions. |
| `remote_build/get_identity` | — | `IdentityView` | Read the receiver's stable identity: `{dashboard_id, pin_sha256, server_version, esphome_version, listener_bound}`. The cert + key PEMs are intentionally NOT included; `pin_sha256` is the cert SPKI fingerprint (lowercase hex SHA-256) — a vestige of the pre-pivot bearer flow that the WS surface still returns until phase 4b+ swaps in the peer-link X25519 pubkey hash advertised in mDNS TXT. `listener_bound` reports whether the peer-link Noise WS listener is currently serving traffic. Idempotent (no rotation triggered). |
| `remote_build/rotate_identity` | — | `IdentityView` | Mint a fresh dashboard cert + key pair, replacing whatever's on disk. **Note: this rotates the TLS cert from phase 3a (still used for `dashboard_id` provenance) but is *not* the peer-link rotation; the listener is torn down + rebuilt as a side effect, which reloads the X25519 peer-link identity from disk.** Phase 4b+ replaces this WS surface with peer-link identity rotation. |
| `remote_build/preview_pair` | `{hostname, port}` | `{pin_sha256}` | Open a brief Noise XX WS to a receiver, capture the static pubkey, return the lowercase-hex SHA-256 for OOB display. No state mutated on either side. `unavailable` on transport / handshake failure. |
| `remote_build/request_pair` | `{hostname, port, pin_sha256, receiver_label, offloader_label}` | `PairingSummary` | Re-handshake (defends against TOCTOU between preview and confirm), send `intent="pair_request"` carrying `{label: offloader_label, dashboard_id}` in encrypted msg3. The unified `_pairings` dict holds both PENDING and APPROVED rows; APPROVED rows debounce-save to `<config_dir>/.offloader_pairings.json` via the per-file `Store`, and PENDING rows are filtered out at serialise time so a malicious LAN scanner can't bloat the file. PENDING result spawns a pair-status listener task that flips the row's status in place + schedules a save when the receiver reports the eventual flip; APPROVED result short-circuits the inbox dance. PENDING rows don't survive a controller restart — any in-flight pair attempt has to be re-issued. `precondition_failed` on pin mismatch; `no_pairing_window` when the receiver's window is closed; `unavailable` on transport failure; `internal_error` on an unexpected receiver `intent_response`. |
| `remote_build/unpair` | `{hostname, port}` | `{removed: bool}` | Pop the row from the unified `_pairings` dict and schedule the debounced save. Idempotent — `removed=false` when no row matched. Cancels the row's pair-status listener task. The receiver-side `StoredPeer` is *not* notified; that's the receiver admin's concern (a future `intent="peer_link"` from this offloader will be rejected because the local row is gone). |

#### Peer-link Noise WS receiver site

A separate aiohttp `web.Application` binds on the dashboard's `--remote-build-port` (default `6055`) and serves `/remote-build/peer-link` — a `Noise_XX_25519_ChaChaPoly_SHA256` WebSocket endpoint. Default-off; binds only when `RemoteBuildSettings.enabled` is true. **Toggling `enabled` requires a dashboard restart for the listener to follow** — `set_settings` persists the new value but doesn't live-bind / unbind.

The Noise XX handshake exchanges static X25519 pubkeys mutually; the offloader pins the receiver's pin (out-of-band verified via `intent="preview"`) and the receiver looks up the offloader's pin against its `peers` list. Post-handshake, a single transport frame carries `{intent_response: ...}` and (in phase 4a) closes the WS. Phase 5+ extends `intent="peer_link"` to keep the WS open for application messages.

The receiver advertises the listener's port over mDNS as a TXT property:

| TXT property | Value | When present |
|---|---|---|
| `server_version` | `"1.2.3"` | always |
| `esphome_version` | `"2026.5.0"` | always |
| `pin_sha256` | lowercase-hex SHA-256 of the X25519 peer-link pubkey | when the peer-link listener is bound |
| `remote_build_port` | stringified int (e.g. `"6055"`) | when the peer-link listener is bound (same condition as `pin_sha256`) |

Same-subnet peers read `remote_build_port` from TXT so a `--remote-build-port` override is auto-discovered. Cross-subnet peers (`add_manual_host` flow) provide the port at add time.

### Utility

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `ping` | — | `{pong: true}` | Health check |
| `subscribe_events` | — | Streaming | Subscribe to real-time events |

**`subscribe_events` initial state:**

Right after a client subscribes (and before any live events arrive), the server pushes one `initial_state` event carrying a snapshot of state that's accumulated server-side via background activity (mDNS browser, completed pair flows, etc.) so the frontend can paint the first frame without follow-up reads. Shape: `{devices?: [...], importable?: [...], pairings?: [PairingSummary]}`. Each field is present only when the corresponding controller is up; `pairings` carries both PENDING and APPROVED rows from the offloader-side `_pairings` dict (sync read, no executor hop). Live updates that arrive after the initial state mutate against this seed via the events below.

**`subscribe_events` events:**
- `device_added`, `device_removed`, `device_updated`, `device_state_changed`
- `importable_device_added`, `importable_device_removed`
- `label_created`, `label_updated`, `label_deleted`
- `job_queued`, `job_started`, `job_output`, `job_completed`, `job_failed`
- `remote_build_pair_request_received` — `{dashboard_id, pin_sha256, label, peer_ip}` — fires when an offloader's `intent="pair_request"` Noise frame lands a new `PENDING` row inside the receiver's open pairing window. The Settings UI surfaces the row in the inbox with the offending `dashboard_id`, the peer-link `pin_sha256` (X25519 pubkey hash), the offloader's claimed `label`, and `peer_ip` for sanity-checking.
- `remote_build_pair_status_changed` — `{dashboard_id, status}` (`status: "approved" | "removed"`) — fires from three paths: (a) `approve_peer` promoting a PENDING dict entry to APPROVED (`status="approved"`), (b) `remove_peer` dropping either a PENDING dict entry or an APPROVED list row (`status="removed"`), (c) pairing-window-close clearing the PENDING dict (`status="removed"` per cleared entry). The "removed" event is what wakes any in-flight `intent="pair_status"` long-poll on a paired offloader so its listener task drops the offloader's local state. Subscribers refresh the paired-peers list without polling.
- `remote_build_pairing_window_changed` — `{open, expires_in_seconds}` — fires when the pairing window opens / closes (refcount transitions, auto-close timeout, idle ageing). `expires_in_seconds` is `null` when `open` is `false`; otherwise it's the float remaining lifetime against the latest user-activity extend. Subscribers render the "Pairing window: X seconds remaining" countdown from this value (and tick locally between events).
- `remote_build_identity_rotated` — `{dashboard_id, pin_sha256}` — fires when the operator triggers `remote_build/rotate_identity`. Subscribers refresh their cached pin without polling `get_identity`. Only fires when the on-disk rotation succeeds; the listener rebuild may still fail-soft, in which case the rotater's `IdentityView` response carries `listener_bound=false` while this event reflects only that the cert + key on disk changed.
- `offloader_pair_status_changed` — `{receiver_hostname, receiver_port, status: "approved" | "removed"}` — offloader-side counterpart to `remote_build_pair_status_changed`. Fired by the per-row pair-status listener task (`_await_pair_status_flip` → `_apply_pair_status_result` → `_fire_offloader_pair_status_changed`) when its `intent="pair_status"` round-trip resolves: APPROVED + matching pin → `status="approved"`; APPROVED + drifted pin → `status="removed"` (treat receiver-side identity rotation as peer-revoked); REJECTED → `status="removed"`. Also fired by `remote_build/unpair` when the user removes a row. Keys on `(hostname, port)` because the offloader's `StoredPairing` keys on the receiver coordinates the user dialled, not on a receiver-side identifier the offloader doesn't track. Delivered to clients via the existing global `subscribe_events` stream — no separate subscription channel.

---

## Legacy REST Endpoints (Deprecated)

For Home Assistant ESPHome integration backward compat only.

| Endpoint | Description |
|----------|-------------|
| `GET /devices` | List devices |
| `GET /json-config?configuration=...` | Get parsed YAML as JSON |
| `GET /compile` (WebSocket) | Compile via spawn protocol |
| `GET /upload` (WebSocket) | Upload via spawn protocol |
