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
> Models: [`RemoteBuildSettingsView`](../esphome_device_builder/models/remote_build.py), [`RemoteBuildPeer`](../esphome_device_builder/models/remote_build.py), [`ManualHost`](../esphome_device_builder/models/remote_build.py), [`TokenSummary`](../esphome_device_builder/models/remote_build.py)

Receiver-side surface for the remote-build offload feature (issue #106). Discovers peer dashboards via mDNS (`_esphomebuilder._tcp.local.`), lets the user add manual peers for cross-subnet LANs, and issues bearer tokens that paired offloaders present to the receiver's HTTPS listener (the listener itself lands in phase 3b2). Settings persist in `.device-builder.json` under `_remote_build`.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `remote_build/list_hosts` | — | `[RemoteBuildPeer]` | Discovered (`source=mdns`) and manually-added (`source=manual`) peer dashboards merged into one list. |
| `remote_build/get_settings` | — | `RemoteBuildSettingsView` | Read the receiver-side settings (enabled, manual_hosts, tokens). |
| `remote_build/set_settings` | `{enabled}` | `RemoteBuildSettingsView` | Persist the master switch. Strict-bool; rejects truthy strings. |
| `remote_build/add_manual_host` | `{hostname, port}` | `RemoteBuildSettingsView` | Add a manual peer for cross-subnet / non-mDNS LANs. Hostname normalised to lowercase. Duplicate `(hostname, port)` raises `already_exists`. |
| `remote_build/remove_manual_host` | `{hostname, port}` | `RemoteBuildSettingsView` | Remove a manual peer. Unknown pair raises `not_found`. |
| `remote_build/list_tokens` | — | `[TokenSummary]` | Issued bearer tokens, projected to omit the secret hash. |
| `remote_build/add_token` | `{label, token_id, secret_sha256}` | `TokenSummary` | Register a client-generated bearer. **The frontend generates `token_id` + cleartext secret locally** and submits only `SHA-256(secret)`; the cleartext bearer never crosses the wire to the backend. Label 1-128 chars; `token_id` is base64url, **exactly 11 chars** (the textual form of 8 random bytes from `crypto.getRandomValues(new Uint8Array(8))`); `secret_sha256` is 64 lowercase hex chars. Duplicate labels allowed; duplicate `token_id` rejected with `already_exists`. |
| `remote_build/remove_token` | `{token_id}` | `RemoteBuildSettingsView` | Revoke a token. Unknown / blank `token_id` raises `not_found` / `invalid_args` respectively. |

**Bearer wire format**: `{token_id}.{secret}` where `token_id` is the lookup key (the textual form of 8 random bytes after base64url encoding — exactly 11 chars, validated strictly by the backend) and `secret` is the verification value (32 random bytes after base64url encoding — 43 chars). The auth middleware splits on `.`, looks up by `token_id`, and `hmac.compare_digest`s `SHA-256(secret)` against the stored hash.

**Cleartext bearer is generated client-side** by the frontend and never crosses the wire to the backend; only the SHA-256 of the secret half is sent in `add_token` and persisted. This closes the leak that would otherwise occur on plain-HTTP standalone deployments where the main port (default `6052`) carries the WS API in cleartext.

**`bound_dashboard_id`** on `StoredToken` / `TokenSummary` is reserved for phase 3b3's first-use binding; it stays `null` until the first authenticated request arrives carrying the offloader's `X-Dashboard-ID` header.

#### HTTPS receiver site (phase 3b2)

A separate aiohttp `web.Application` binds on the dashboard's `--remote-build-port` (default `6055`) over TLS using the cert + key from phase 3a. Default-off; binds only when `RemoteBuildSettings.enabled` is true. **Toggling `enabled` requires a dashboard restart for the listener to follow** — `set_settings` persists the new value but doesn't live-bind / unbind the listener.

| Endpoint | Auth | Notes |
|---|---|---|
| `GET /remote-build/v1/health` | `Authorization: Bearer {token_id}.{secret}` + `X-Dashboard-ID: {offloader_dashboard_id}` | Returns `{"ok": true}` on a valid bearer paired with the bound (or first-binding) dashboard_id; 401 without bearer; 400 without `X-Dashboard-ID`; 403 on dashboard_id mismatch; 429 with `Retry-After` after rate-limit lockout. |

The bearer scheme is RFC 7235 case-insensitive (`Bearer`, `bearer`, `BEARER` all work) and tolerant of any RFC 7230 BWS (space or tab) between scheme and credentials.

**First-use binding** (phase 3b3): every authenticated request must carry `X-Dashboard-ID` (the offloader's `dashboard_id` from phase 3a, base64url, ≤ 64 chars). On the first authenticated request for a token whose `bound_dashboard_id` is `null`, the receiver atomically writes the presented value as the binding. Subsequent requests with a mismatched `X-Dashboard-ID` are rejected with 403 (the token is valid; the peer is wrong). A `remote_build_binding_mismatch` event fires on every 403 mismatch so the receiver Settings UI can surface the attempt to the operator. Concurrent first-use requests with different dashboard_ids race for the slot under the metadata lock; the loser observes the winner's binding and gets 403.

Per-IP rate limiter on failed attempts: 10 failures per 60 seconds triggers a 5-minute lockout for that source IP. Successful auth doesn't clear the limiter — there's no notion of "this peer is trustworthy now"; per-pairing trust is the binding step in phase 3b3.

The receiver advertises the listener's port over mDNS as a TXT property:

| TXT property | Value | When present |
|---|---|---|
| `server_version` | `"1.2.3"` | always |
| `esphome_version` | `"2026.5.0"` | always |
| `pin_sha256` | lowercase-hex SPKI fingerprint | when the remote-build receiver site is bound (the listener post-bind sets this; a misconfigured `enabled=true` that fails to bind leaves it absent) |
| `remote_build_port` | stringified int (e.g. `"6055"`) | when the remote-build receiver site is bound (same condition as `pin_sha256`) |

Same-subnet peers read `remote_build_port` from TXT so a `--remote-build-port` override is auto-discovered. Cross-subnet peers (`add_manual_host` flow) provide the port at add time.

### Utility

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `ping` | — | `{pong: true}` | Health check |
| `subscribe_events` | — | Streaming | Subscribe to real-time events |

**`subscribe_events` events:**
- `device_added`, `device_removed`, `device_updated`, `device_state_changed`
- `importable_device_added`, `importable_device_removed`
- `label_created`, `label_updated`, `label_deleted`
- `job_queued`, `job_started`, `job_output`, `job_completed`, `job_failed`
- `remote_build_binding_mismatch` — `{token_id, presented_dashboard_id, bound_dashboard_id, peer_ip, race_loss}` — fires when an authenticated `/remote-build/v1/*` request's `X-Dashboard-ID` doesn't match the token's bound value. `race_loss` is `true` when the mismatch happened during a first-use bind (concurrent legitimate-paste-into-two-offloaders is a likely cause), `false` when the token was already bound (more suspicious — points at a stolen-bearer or operator-paste-into-wrong-machine attempt). The Settings UI uses the flag to soften the wording on the race-loss case while keeping the mismatch path loud.

---

## Legacy REST Endpoints (Deprecated)

For Home Assistant ESPHome integration backward compat only.

| Endpoint | Description |
|----------|-------------|
| `GET /devices` | List devices |
| `GET /json-config?configuration=...` | Get parsed YAML as JSON |
| `GET /compile` (WebSocket) | Compile via spawn protocol |
| `GET /upload` (WebSocket) | Upload via spawn protocol |
