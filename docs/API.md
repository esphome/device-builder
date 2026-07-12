# API Reference

Base URL: `http://localhost:6052`

## WebSocket API (`/ws`)

The primary API. A single multiplexed WebSocket handles all 46 commands.

### Protocol

**Connect:** `ws://localhost:6052/ws`

On connect, the server sends a [`ServerInfoMessage`](../esphome_device_builder/models/api.py):
```json
{"server_version": "0.0.0", "esphome_version": "2026.3.1", "port": 6052, "ha_addon": false, "ha_ingress": false, "requires_auth": false, "desktop_version": "", "desktop_update_capable": false}
```

`ha_ingress` is `true` only when the connection is proxied through the HA Supervisor ingress (the `X-Ingress-Path` header is present); the frontend slims its header in that case so HA's own panel bar isn't doubled up. An add-on reached directly on its exposed port reports `ha_addon: true` but `ha_ingress: false`.

`desktop_version` carries the ESPHome Desktop wrapper version (from the `ESPHOME_DESKTOP_VERSION` env var) when the dashboard runs inside the desktop app, and is `""` otherwise; the value is sanitized server-side (blank, non-printable, or over-length is treated as unset). The frontend renders it as a footer line only when non-empty.

`desktop_update_capable` is `true` only when the dashboard runs inside an ESPHome Desktop app that exposes its update `api` (0.14.0+, which sets `ESPHOME_DESKTOP_BIN`). Older desktop apps set `desktop_version` but not this, so the frontend gates the "Check for updates" menu item on `desktop_update_capable`, not on `desktop_version`.

### Desktop update commands

Available only when `desktop_update_capable` is `true`; both shell out to the desktop app's `esphome-desktop` CLI.

- `desktop/check_update` returns whether an update is available for any component, without installing: `{"any_available": bool, "app": {…}, "esphome": {…}, "device_builder": {…}}`, where each component carries `available`, `installed`, `latest`, and `error`.
- `desktop/update` triggers the full update (desktop app, ESPHome, device builder) and returns `{"started": true}` immediately. It is fire-and-forget: the desktop app stops and restarts this backend to install, so the WebSocket drops mid-update; re-run `desktop/check_update` after reconnecting to confirm the new versions.

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
| `unavailable` | Transient external dependency failed (remote unreachable, handshake / decrypt failure) |
| `precondition_failed` | State precondition not met (pin mismatch, receiver rejected) |
| `no_pairing_window` | Receiver's pair-request acceptance window is closed |
| `no_compatible_peer` | Offloader's `version_match_policy="exact_required"` and no APPROVED + enabled pairing was eligible (filtered on version mismatch *or* closed peer-link session); install refuses to fall back to LOCAL. Briefly-offline build servers will surface this error too — operator may want to retry once the session reconnects, or relax the policy in Settings → Build server. |

### Enums

| Enum | Values | Description |
|------|--------|-------------|
| `DeviceState` | `unknown`, `online`, `offline` | Device connectivity state (mDNS + ping) |

---

## Commands

### Authentication

> Controller: [`AuthController`](../esphome_device_builder/controllers/auth.py)

When the dashboard is started with `--username`/`--password` (or `$ESPHOME_USERNAME`/`$ESPHOME_PASSWORD` env vars), every WebSocket connection on the public port must authenticate before any other command will be accepted.

The legacy bare `$USERNAME`/`$PASSWORD` pair is still accepted as a deprecated fallback, so dashboards configured before the `ESPHOME_*` rename stay protected; it is adopted only when `$PASSWORD` is set and no newer source supplied credentials, and the dashboard logs a deprecation warning at startup. Rename to `$ESPHOME_USERNAME`/`$ESPHOME_PASSWORD`; the bare names will be removed in a future release.

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
| `devices/create` | `{name, board_id?, ssid?, psk?, file_content?, overwrite?}` | `WizardResponse` | Create device. `name` is the user's raw display label — capitalisation, inter-word spaces, and unicode are preserved; surrounding whitespace is trimmed. The backend slugifies the cleaned value for `esphome.name:` and the YAML filename and writes the cleaned original into `esphome.friendly_name:`. Callers that already pass a slugified value get the same hostname/friendly_name pair as before (slug-of-slug is a no-op). Three flows: `file_content` writes the supplied YAML as-is; `board_id` generates from a board template; with neither, emits a minimal esp32 stub for the "empty configuration" path. **Wi-Fi handling:** a supplied `ssid` / `psk` is written to `secrets.yaml` (validated, shared with `config/set_wifi_credentials`) and the generated YAML references `!secret wifi_ssid` / `!secret wifi_password` — bare credentials are never written into the device YAML, and the next device reuses the shared secret. With no `ssid`, the generator emits `!secret` when Wi-Fi secrets already exist, else (for a board with no other network) a no-network stub. A board offering onboard-ethernet suggested hardware is **wired by default** — its `ethernet:` block is auto-pulled and the `wifi:` block dropped (see the network-providers note under [Boards](#boards)). A filename collision returns `ALREADY_EXISTS`; pass `overwrite: true` to replace the YAML in place, preserving the existing device's metadata (labels / comment / board_id) and StorageJSON. |
| `devices/import_bundle` | `{file_content_b64, overwrite?: string[]}` | `ImportBundleResponse` | Import an `esphome bundle` archive (`.esphomebundle.tar.gz`) as a device. `file_content_b64` is the base64 of the raw `.tar.gz`. Lands the main YAML plus its `!include`s and local external components into the config dir; `secrets.yaml` is merged (absent keys added, existing values kept). With no `overwrite`, a first call whose files already exist returns `status: "conflicts"` and a `conflicts` list and writes nothing; the caller re-submits the same bytes with the chosen paths in `overwrite` to replace them (paths left out are kept). `status: "imported"` means the tree landed; the response then carries `written` (paths placed) and `kept` (existing paths left untouched), so a non-empty `kept` tells the caller the import was partial (e.g. the main config was not replaced). **Omitting `overwrite` is not the same as `overwrite: []`:** omitting it is the first (planning) pass that reports conflicts, while *any* present array (including empty) is a resolved pass that writes, treating every conflict not in the array as "keep existing". An empty array therefore means "keep all conflicting files", not "report conflicts"; the response's `kept` then lists those files. The resolved pass trusts the submitted decision and does not re-report a conflict that appears between the two calls (it surfaces as `kept`). |
| `devices/update` | `{configuration, friendly_name?, comment?, board_id?}` | `UpdateDeviceResponse` | Update device metadata (sidecar JSON) |
| `devices/set_labels` | `{configuration, label_ids: string[]}` | `Device` | Replace this device's label assignments. Pass `[]` to clear. Unknown ids return `INVALID_ARGS`. Fires `device_updated` after the scanner reload. |
| `devices/set_labels_bulk` | `{updates: [{configuration, label_ids: string[]}, ...]}` | `[{configuration, success, error?}]` | Replace label assignments across multiple devices. One result per entry preserving input order (duplicates in `updates` produce duplicate rows). Per-entry failures (unknown id, missing device, malformed row) don't block the rest. Rows whose `configuration` can't be extracted (non-dict row, non-string value) surface as `{configuration: "", success: false, error}` so the frontend dialog can identify malformed entries from its own request payload. |
| `devices/rename` | `{configuration, new_name, config_only?: bool}` | `{configuration, job, tail_job}` (config-only / in-place: `{configuration, job: null}`) | Rename device. The online path writes the renamed YAML up-front and queues a **two-job chain**: `job` is the `COMPILE` of the new YAML (remote-eligible via the build scheduler, exactly like an install's) and `tail_job` is the dependent `RENAME` that OTA-flashes the *old* device address on the upload lane and swaps the files on success. A failed / cancelled chain deletes the new YAML so the old device is untouched. `config_only: true` (offline) and in-place renames rewrite the YAML with no jobs. A non-retargetable `esphome.name` (from packages / `!include` / an embedded substitution) refuses with `INVALID_ARGS` on every path. |
| `devices/delete` | `{configuration}` | — | Delete device and associated files. A missing YAML surfaces `not_found` (same as `devices/archive`), not `internal_error`. |
| `devices/delete_bulk` | `{configurations: string[]}` | `[{configuration, success, error?}]` | Delete multiple devices |
| `devices/archive` | `{configuration}` | — | Soft-delete: move YAML to `<config_dir>/archive/`, wipe build dir, wipe StorageJSON + device-metadata sidecars. Reversible via `devices/unarchive` (cached IP/version/hash refill from the next mDNS broadcast). |
| `devices/archive_bulk` | `{configurations: string[]}` | `[{configuration, success, error?}]` | Archive multiple devices at once. Same per-item shape as `devices/delete_bulk`. |
| `devices/unarchive` | `{configuration}` | — | Move an archived YAML back into the active config directory. Errors with `INVALID_ARGS` if an active config with the same filename already exists. |
| `devices/list_archived` | — | `[{configuration, name, friendly_name, comment}]` | List archived devices for the dashboard's archived-devices dialog. |
| `devices/delete_archived` | `{configuration}` | — | Permanently delete an archived YAML and its sidecars. The companion to `unarchive` for "I really don't want this back". |
| `devices/get_config` | `{configuration}` | `string` | Read device YAML config |
| `devices/update_config` | `{configuration, content, allow_wipe?}` | — | Write device YAML config; `allow_wipe` confirms clearing secrets.yaml |
| `devices/add_component` | `{configuration, component_id, fields?, sub_entities?, yaml?}` | `AddComponentResponse` | Add component to device config. Optional `yaml` is the editor's unsaved draft — merge into it and return the result **without** persisting (the editor saves it, like `automations/upsert`); omit to merge the on-disk YAML and persist. |
| `devices/import` | `{name, project_name?, package_import_url?, ...}` | `dict` | Import/adopt discovered device |
| `devices/ignore` | `{name, ignore?}` | — | Toggle device visibility |
| `devices/validate` | `{configuration}` | Streaming | Validate YAML config |
| `devices/logs` | `{configuration, port?: "OTA" \| serial, no_states?: bool}` | Streaming | Stream live device logs. `port` defaults to `"OTA"` (empty string is treated the same) — without a default, `esphome logs` falls into an interactive port-choice prompt when multiple targets are visible and the stdin-less subprocess crashes with `EOFError`. When `port` resolves to `"OTA"` the dashboard forwards its mDNS / DNS cache as `--mdns-address-cache` / `--dns-address-cache` so the CLI doesn't redo resolution the dashboard already has (legacy-dashboard parity with `build_cache_arguments`). |
| `devices/subscribe_reachability` | `{device_name}` | Streaming (`reachability_state`) | Drawer-only per-device reachability stream. Each `reachability_state` event carries the per-signal freshness snapshot (`mdns_last_seen_seconds_ago`, `mdns_ttl_remaining_seconds`, `mdns_ptr_ttl_seconds`, `mdns_txt_records`, `ping_last_seen_seconds_ago`, `ping_rtt_ms`, `mqtt_last_seen_seconds_ago`, plus `state` / `active_source` / `ip`). `mdns_ptr_ttl_seconds` is the PTR record's full announced lifetime (mDNS default ~4500s, `null` when no PTR is cached); the drawer's offline countdown is that lifetime measured from `mdns_last_seen_seconds_ago`, so it stays in lockstep with "last seen". Pair with `devices/stop_stream`. |

`Device.runtime_state`: nested object grouping the monitor-observed fields (`state`, `active_source`, `ip_addresses`, `deployed_version`, `deployed_config_hash`, `queued_update`, `api_encryption_active`) — populated after startup by the mDNS / MQTT / ping monitors; the sidecar-persisted ones (`deployed_version`, `deployed_config_hash`, `queued_update`, `api_encryption_active`) are also seeded from disk on cold load, so they can survive a restart before the first announce.
`Device.runtime_state.state`: `DeviceState` — `unknown`, `online`, or `offline` (discovered via mDNS + ping).
`Device.has_pending_changes`: `true` = config changed since last compile, `false` = up to date, `null` = never compiled.
`Device.pending_changes_via_hash`: `true` when `has_pending_changes` came from the mDNS-sourced config-hash compare (vs the local mtime fallback). The frontend gates only this case on a live mDNS, so a local YAML edit still cues "install" when mDNS is dark.
`Device.update_available`: `true` = device was compiled with a different ESPHome version than the server.
`Device.runtime_state.active_source`: `ReachabilitySource` — channel currently driving online state (`mdns` > `mqtt` > `ping`); `unknown` until a source claims it (also the transient default after a restart). The frontend gates the mDNS-sourced out-of-sync / update indicators on `active_source == "mdns"`, since `deployed_version` / `deployed_config_hash` come only from the mDNS broadcast.

### Firmware

> Models: [`FirmwareJob`](../esphome_device_builder/models/firmware.py), [`JobStatus`](../esphome_device_builder/models/firmware.py), [`JobType`](../esphome_device_builder/models/firmware.py)
>
> Controller: [`FirmwareController`](../esphome_device_builder/controllers/firmware.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `firmware/compile` | `{configuration}` | `FirmwareJob` | Queue compile job |
| `firmware/upload` | `{configuration, port?: "", bootloader?: bool}` | `FirmwareJob` | Queue upload of existing binary. `port` defaults to `""` (no `--device` arg — CLI auto-detects). Also accepts `"OTA"`, a serial path (`/dev/ttyUSB0`, `COM3`), or an explicit IP / hostname for "install to a specific address" — the address-cache shortcut is bypassed when a target is named directly. `bootloader=true` flashes the bootloader image instead of the app (`esphome upload --bootloader`); requires an OTA target (`"OTA"` / IP / hostname) and a YAML whose `ota: platform: esphome` sets `allow_partition_access: true` (surfaced as `Device.ota_partition_access`). |
| `firmware/install` | `{configuration, port?: "OTA" \| serial \| ip \| hostname, force_local?: bool, bootloader?: bool}` | `FirmwareJob` (the COMPILE job) | If `port` is `"OTA"` and the device is OFFLINE, queues a single deferred `COMPILE` job (the update is armed to flash automatically the next time the device wakes up). Otherwise, queues an install as a **two-job chain**: a COMPILE job + a dependent `UPLOAD` job (`FirmwareJob.depends_on` = the compile's `job_id`). Returns the COMPILE job; the UPLOAD renders as queued and starts only after the compile succeeds, on the **upload lane** so it doesn't block the next device's compile. A cancelled/failed compile cascades to cancel the held upload (a cancelled build never flashes). The OFFLINE check re-applies when the compile finishes: a device that went OFFLINE during the build converts the chain to the deferred shape (held upload cancelled, update armed for the next wake) instead of flashing a dead address, and an OTA app upload that *fails* while its device is OFFLINE arms the queued update the same way. An install submitted while the device is still in the UNKNOWN startup window nudges an immediate reachability probe (mDNS resolve + ICMP sweep wake) so the state has settled by the time those re-checks run. `follow_job`'s terminal result carries `queued_update_armed: true` only when the finished job actually armed a queued update — distinct from the job row's raw `is_deferred_install` flag, which stays true on a *failed* deferred compile that armed nothing. `force_local=true` bypasses the scheduler (compile runs LOCAL). Remote installs use the same chain — the remote compile materialises artifacts locally, then the local upload lane flashes. `bootloader=true` makes the chain's UPLOAD flash the bootloader image instead of the app (`esphome upload --bootloader`); OTA targets only, never deferred — an OFFLINE device is rejected with `INVALID_ARGS`. Power loss / reset mid-update can soft-brick the device (serial reflash recovers it) — the frontend surfaces this warning on the option. |
| `firmware/clean` | `{configuration}` | `FirmwareJob` | Queue build clean for one device. **Cancels any in-flight build (compile/upload/install) for that configuration first** — a clean is the user asking for a fresh build, and the two lanes mean the upload could otherwise read artifacts the clean is wiping. The cancelled jobs fire `JOB_CANCELLED`. |
| `firmware/clear_queued_update` | `{configuration}` | — | Clears a staged offline update for a device that hasn't woken up yet. |
| `firmware/reset_build_env` | — | `FirmwareJob` | Queue full reset of `.esphome/` build dirs and PIO cache. **Cancels every in-flight job on both lanes first** (the wipe trashes the whole tree, which a concurrent compile or upload would race). |
| `firmware/compile_bulk` | `{configurations: string[]}` | `[FirmwareJob]` | Queue multiple compiles |
| `firmware/install_bulk` | `{configurations: string[], port?: "OTA" \| serial \| ip \| hostname}` | `[FirmwareJob]` | Queue multiple installs. `port` defaults to `"OTA"` and is shared across every queued job — almost always callers want that default rather than a single explicit target across the fleet. Same `port` validation as `firmware/install`, and the same OFFLINE deferral: an OFFLINE device with an `"OTA"` target queues a single deferred `COMPILE` job that flashes on the device's next wake, instead of a chain whose upload would fail. |
| `firmware/get_jobs` | `{status?, configuration?}` | `[FirmwareJob]` | List jobs with filters |
| `firmware/get_job` | `{job_id}` | `FirmwareJob` | Get job with full output |
| `firmware/follow_job` | `{job_id}` | Streaming | Historical output + live stream for one job |
| `firmware/follow_jobs` | `{snapshot?: true}` | Streaming | All jobs' lifecycle + output + progress |
| `firmware/get_binaries` | `{configuration}` | `[{title, file, type?, description?}]` | List downloadable build artifacts present on disk |
| `firmware/download_token` | `{configuration, file}` | `{token}` | Mint a single-use token for the HTTP download of one artifact |
| `firmware/cancel` | `{job_id}` | — | Cancel queued or running job |
| `firmware/clear` | `{status?}` | — | Remove finished jobs |

**`firmware/get_binaries`**: returns only artifacts that exist in the build directory (an empty list means "compile first"), each `{title, file, type?, description?}`. `description` is optional subtext from the platform's `get_download_types`. `type` is a stable tag (`factory` / `ota` / `bin` / `uf2` / `elf`) the frontend maps to a localized label, falling back to `title` when absent or unrecognized. A `firmware.elf` entry (debug symbols for the ESP stack trace decoder) is appended when present; `get_download_types` itself never lists it.

**Downloading a `file`** goes over **HTTP**, not the WebSocket: call `firmware/download_token` to mint a token, then `GET /api/firmware/download?token=<token>`, which streams the raw file with `Content-Disposition: attachment`. A 14 MB `firmware.elf` as a single base64 WS message exceeds a proxy's WebSocket `max_msg_size` (HA ingress, nginx); HTTP has no such cap, skips the base64 overhead, and a plain `<a href>` navigation streams it straight to disk (no in-browser buffering — works on mobile). This matches the legacy ESPHome dashboard, which served downloads over HTTP.

The token is the route's authorization (so a navigation needs no `Authorization` header): it is minted only over the authenticated WebSocket, is unguessable, expires within ~60 s, is single-use, and is bound to one `(configuration, file)` — so the query string can't repoint it at another artifact. `configuration` and `file` are traversal-validated server-side and the served filename is sanitized for the header. Every download path (the save-to-disk picker and the in-browser Web Serial flash) uses this route, so there is no WebSocket download command.

**Job queue**: two concurrent single-worker lanes — a **compile lane** (CPU: compile, clean, reset) and an **upload lane** (network: upload, rename's flash) — so a slow upload doesn't block the next device's compile. One job runs at a time *per lane*. `firmware/install` is a `COMPILE` + dependent `UPLOAD` chain across the two; `devices/rename` is the same shape with a `RENAME` tail (flash the old address, swap the YAMLs, revert on failure). Jobs persist across server restarts. Output buffered in `FirmwareJob.output` — clients can reconnect via `firmware/follow_job`.

**One active job per device**: queuing a new job for a device cancels any existing queued or running job with the same `configuration` first. The cancelled job fires `JOB_CANCELLED` as usual, then the new job fires `JOB_QUEUED` — frontends following lifecycle events stay consistent with the "show the latest result" UX. `firmware/clean` for a device follows the same rule (it cancels that device's in-flight build before cleaning). `firmware/reset_build_env` is global (empty `configuration`): rather than being exempt, it cancels **every** in-flight job on both lanes before wiping.

**History retention**: terminal `compile`/`upload`/`install` jobs are kept in a global pool capped at 50, deduplicated to one entry per `(configuration, job type)` (newest wins) — so an install's `compile` and its `upload` both survive rather than the upload evicting the compile. Terminal `clean`/`reset_build_env` jobs sit in a separate pool capped at 5 so they don't crowd device history. Active (queued/running) jobs are exempt from pruning. Each retained job's `output` is trimmed to the last 2000 lines on terminal transition; a synthetic first line `... [output trimmed: N earlier line(s) elided]` indicates how many lines were dropped. `firmware/clear` still wipes terminal jobs on demand.

**`firmware/reset_build_env`**: cancels every in-flight job on both lanes (the wipe would otherwise race a concurrent compile or upload), then wipes `.esphome/build/`, `.esphome/external_components/`, and `.esphome/platformio_cache/` so the next compile re-fetches external components and re-downloads PlatformIO toolchains. Returns a `FirmwareJob` with empty `configuration` and `job_type: "reset_build_env"`. Streams progress through the same `JOB_OUTPUT` event as compile jobs. Mid-run cancellation is honoured between the three target directories, not during a single removal.

**Cancel semantics**:
- Queued jobs flip to `cancelled` immediately.
- Running jobs receive SIGTERM, with SIGKILL escalation after a 3 s grace period. The job's status becomes `cancelled` (not `failed`) and `JOB_CANCELLED` fires.

**Progress**: `FirmwareJob.progress` is an `int | null` 0–100 latched from the highest percentage seen in `[ 17%] Compiling …` (PlatformIO) or `Writing at 0x… (45 %)` (esptool) lines. `null` means the tooling hasn't emitted a percentage yet — most early compile output is opaque. The value is monotonically non-decreasing *within a phase*; at known phase seams (REMOTE install's compile → upload boundary) the runner explicitly resets to 0 and fires `job_progress{progress: 0}` so the next phase's percents aren't silently clamped against the previous phase's peak. Subscribers should render the bar from the latest event rather than asserting non-decreasing progress.

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
| `boards/get_boards` | `{query?, platform?, variant?, mcu?, tag?, offset?, limit?}` | `PagedBoardsResponse` (slim) | Search/list slim board index entries (`mcu` narrows rp2040 to one chip series) |
| `boards/get_board` | `{board_id}` | `BoardCatalogEntry` | Get one board's full body (hardware, pins, featured / default components). The response carries a derived `requires_wifi` flag (see network-providers note below): the wizard makes Wi-Fi mandatory for Wi-Fi-only boards and skips the step for boards with their own network. |
| `boards/get_compatible_boards` | `{board_id}` | `PagedBoardsResponse` (slim) | Boards interchangeable with `board_id` (same PlatformIO target), the whole set in one page; includes the board itself, empty when unknown. Powers the device editor's "change board" picker |

The board catalog is split into three artefacts under `definitions/`: the slim `boards.index.json` (`BoardCatalogIndex` per board — id, name, esphome, tags, images, urls, sort flags), per-board `board_bodies/<id>.json` (hardware, pins, featured_components, featured_bundles, default_components — lazy-loaded via the controller's `LazyBodyStore`), and the aggregated `featured_components.index.json` (read once at startup by the components controller so the featured-component registry build never touches per-board body files). `boards/get_boards` ships slim entries; the detail view fetches the full body on demand via `boards/get_board`.

Board catalog dataclasses (`BoardCatalogIndex`, `BoardCatalogEntry`, `BoardHardware`, `BoardEsphomeConfig`, `BoardPin`, `FeaturedComponent`, `FeaturedBundle`, `DefaultComponent`, and the shared `FieldPreset`) use mashumaro's `omit_default = True` + `omit_none = True` Config, so any field whose runtime value equals the declared default or `None` is **absent from the wire payload** rather than being sent as `null` / `false` / `[]` / `""`. `from_dict` re-defaults missing fields from the same declared defaults, so the strip is lossless. The frontend hydrates at the API client (`hydrateBoard` in `src/util/board-hydrate.ts`, defensive `?? defaults` per field) so consumer code reads the populated shape without per-call `?? []` defaulting. The same `hydrateBoard` runs on both the slim list response from `boards/get_boards` and the full body response from `boards/get_board`; missing body fields in the slim case fall back to `[]` / `null`, which is what the wizard / picker code already tolerates. The shape descriptions below list every field; assume any field with a clear default-empty form (`null`, `false`, `[]`, `""`) is wire-omitted in that state.

`BoardCatalogEntry` carries three recommendation / default lists used by the Add Component dialog and the new-device wizard:

- `featured_components: list[FeaturedComponent]` — components recommended for this board, surfaced in the catalog API as `featured.<board_id>.<local_id>` under category `featured`. Each entry can override the catalog `name`/`description` and pre-fill any subset of the underlying component's `config_entries` via a `fields` map keyed by `ConfigEntry.key`. Three preset modes per field:
  - **default**: a primitive value the frontend pre-fills; user can change it.
  - **locked**: `{value, locked: true}` — frontend disables the input and `devices/add_component` rejects deviating user values.
  - **suggestions**: `{suggestions: [...]}` — frontend renders a picker, user must pick from the list.
  - Each entry also carries `multi_conf: bool`, mirroring the underlying component's repeatability. Unlike the default-empty fields above it **defaults `True`**, so under `omit_default` it is wire-omitted when *true* and only single-instance entries serialise `multi_conf: false` — **absent means multi-conf**. The frontend reads this to drop an already-configured single-instance recommendation (e.g. an onboard `ethernet`) and collapse the Recommended category / skip auto-selecting it.
  - Each entry also carries `locked_pins: dict[str, int]`, mapping each *locked* PIN field key to its canonical GPIO (e.g. `{"scl": 0, "sda": 1}`). Computed at sync time from the underlying component's `type: pin` `config_entries`, so the pin keys are authoritative (no key-name guessing on the frontend). Wire-omitted when empty (components with no locked pins). The frontend reads it to hide a recommended card whose fixed pins are already occupied by an existing same-domain instance (e.g. a board's featured i2c bus when a generic `i2c` on the same `scl`/`sda` was already added).
- `featured_bundles: list[FeaturedBundle]` — `{id, name, description?, component_ids?}` groups of featured components (e.g. "Status LED" = `output.gpio` + `light.binary`). The frontend triggers sequential `devices/add_component` calls for each `component_id` when the user adds a bundle. `description` and `component_ids` are wire-omitted when empty (see the catalog-shape note above).
- `default_components: list[DefaultComponent]` — components installed automatically in every new device created from this board, no clicks needed. Each entry is `{id, fields}` where `id` resolves either to a local `featured_components.id` (picks up that entry's full presets) or to a bare catalog `component_id`, and `fields` carries plain `key: value` overrides that layer on top of any featured presets (inline wins). The manifest YAML accepts a string shorthand (`- accessory_power`) for the no-overrides case; the loader normalises it into the dataclass form. Default components only fire at device creation — the wizard's initial YAML is built by `generate_device_yaml(board, defaults=catalog.resolve_default_components(board))`. Existing devices keep their YAML as-is, and any default block the user deletes won't be re-added on subsequent edits.

**Network providers (onboard ethernet).** A `featured_components` entry whose `component_id` is a network provider — `NETWORK_PROVIDER_COMPONENT_IDS` in `helpers/device_yaml/_generation.py`, currently `{"ethernet"}` — doubles as the board's onboard network. At `devices/create` with no `ssid`, `ComponentCatalog.resolve_network_components(board)` resolves that entry's locked pin presets and appends them to `generate_device_yaml`'s `defaults`. The generator detects a network provider in `defaults` and drops the `wifi:` block in its favour, emitting `api:` / `ota:` only when a network is actually present (Wi-Fi *or* an injected provider), so a board with its own network produces a compilable config without Wi-Fi secrets. The same entry still surfaces normally in the Add Component dialog's `featured` section, so it can also be added by hand on any board. `boards/get_board` derives a `requires_wifi` flag (via `board_requires_wifi` = native Wi-Fi **and** no onboard network provider, the latter computed by `board_provides_network` over the same set): for a Wi-Fi-only board a generated config always needs a network (`api` / `ota` / a board's `web_server` default all depend on one) and the no-network stub wouldn't validate, so the create wizard makes Wi-Fi mandatory (no skip / blank). A board that brings its own network has `requires_wifi = false` and the wizard skips the Wi-Fi step (the generator drops the `wifi:` block in favour of the onboard provider on a no-`ssid` create).

**Wi-Fi radio providers (esp32_hosted co-processor).** A chip with no native radio (ESP32-P4 / H2, `esp32_no_wifi_variants` in the capabilities snapshot) can still be a Wi-Fi board when it carries an ESP-Hosted co-processor: it declares `wifi` in `hardware.connectivity` **and** ships the radio through `default_components` (a featured `esp32_hosted` entry with the SDIO pins locked), so every new device gets the radio automatically. The generator emits the `wifi:` block only when the chip is actually Wi-Fi-capable — natively, or because a component in `WIFI_RADIO_PROVIDER_COMPONENT_IDS` (`helpers/device_yaml/_generation.py`, currently `{"esp32_hosted"}`) arrives in `defaults`; a `wifi` claim without the radio falls back to the no-network TODO stub instead of a `wifi:` block ESPHome rejects ("WiFi requires component esp32_hosted on ESP32P4"). `script/validate_definitions.py` enforces the manifest side: a no-native-wifi variant claiming `wifi` must carry a radio-provider default. `requires_wifi` stays claim-driven. On a dual-network board the ethernet rules above still win a no-`ssid` create; the `esp32_hosted:` block lands either way, so switching a wired device to Wi-Fi is a block swap, not a pin hunt.

### Components

> Controller: [`ComponentCatalog`](../esphome_device_builder/controllers/components.py)
>
> Enums: [`ComponentCategory`](../esphome_device_builder/models/components.py), [`ConfigEntryType`](../esphome_device_builder/models/common.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `components/get_categories` | `{board_id?}` | `[{id, name, count}]` | List categories with counts |
| `components/get_components` | `{query?, category?, exclude_category?, platform?, board_id?, provides?, offset?, limit?}` | `PagedComponentsResponse` | Search/list components |
| `components/get_component_bodies` | `{component_ids, platform?, board_id?}` | `{component_id: ComponentCatalogEntry}` | Hydrate one or many bodies; missing ids omitted |
| `components/get_pin_registry_modes` | _none_ | `{provider_key: [mode_flag, …]}` | Allowed long-form pin `mode` flags per external pin provider; empty when the artefact is missing |

`platform` filters to components compatible with the given target platform; components with an empty `supported_platforms` list are platform-agnostic and always included. `board_id` is a convenience — the boards catalog resolves it to a platform; `platform` wins when both are passed. The platform is also used to materialise each entry's `platform_defaults` into `default_value`.

`category` / `exclude_category` accept either a single category or a list. Use `exclude_category` for the regular catalog selector to hide entries that belong to the dedicated "Add core configuration" dialog.

`provides` filters to components that can be referenced *as* the given interface (e.g. `provides=voltage_sampler` returns the ADC-family sensors), matching `ComponentCatalogIndexEntry.provides`. Powers the Add-component picker for a cross-domain `references_component` field whose providers live under a different top-level block (a `ct_clamp` sensor reference resolves to ADC sensors under `sensor:`, not a `voltage_sampler:` block). A component also provides its *own* domain when its referenceable ids are nested sub-entities rather than the section's own `id` (`sensor.aht10` provides `sensor` via `temperature.id` / `humidity.id`), so a same-domain `references_component` field (`sensor.in_range`'s `id`) can offer multi-entity sub-sensors; single-entity platforms carry no `provides` — their section `id` already resolves through the top-level scan.

`provides_id_paths` is a sibling field on the same index entry, present only when a provided interface's id is *nested* rather than the component's own top-level `id`. It maps an interface namespace to the YAML key-paths the frontend descends to collect candidate ids; `usb_uart` exposes `uart` at `{"uart": [["channels", "id"]]}`, and a component declaring one interface at several nested locations (`sprinkler` switches) lists every path. Absent for own-id providers, whose ids resolve through the section id directly. A same-domain provider may additionally list the root path `["id"]` when the component's own id is itself an entity (`sensor.pulse_counter`), so hybrid platforms keep their root entity offerable alongside the nested ones.

**Pin registry modes.** A long-form pin on an external provider accepts only a subset of `mode` flags: an I2C expander like `pca9554` permits `input` / `output`, a shift register `sn74hc595` only `output`. `get_pin_registry_modes` returns the `{provider_key: [mode_flag, …]}` map (derived from ESPHome's `PIN_SCHEMA_REGISTRY` at sync time, excluding native target platforms which allow every flag) so the visual editor can hide the unsupported flag checkboxes. The key is the provider key that appears in the pin value (`pca9554`). Native pins (no provider key) and a missing artefact both fall back to showing every flag.

**Featured components.** The board catalog's `featured_components` are surfaced through this same API under the synthetic category `featured` and ID prefix `featured.<board_id>.<local_id>`. They are **only** returned when `category` explicitly includes `featured` and `board_id` is supplied — the regular catalog listing never mixes them in. `get_categories` adds a `featured` entry with the board's recommended-count when `board_id` is set. A featured index entry's `name` is derived when the manifest has no explicit override — the preset entity name (`fields.name`, "Relay 1") when present, else the underlying name suffixed with the preset id ("SPI Bus (lcd_spi)") — and its `underlying_category` carries the real catalog category behind the synthetic `featured` (e.g. `bus` for a featured `spi`), so the card can chip its type alongside its recommendation status; `underlying_category` is `null` on regular entries. A featured `ComponentCatalogEntry` carries the board overrides baked into its `config_entries`: `default_value` reflects the preset, and the new `locked: bool` and `suggestions: list[ConfigPrimitive] | None` fields tell the frontend to disable the input or render a picker. `devices/add_component` recognises `featured.*` ids — the wire shape doesn't change, but the backend resolves the underlying component, validates user input against the locked/suggestion constraints, and merges presets before delegating to the regular merge logic.

### Automations

> Controller: [`AutomationsController`](../esphome_device_builder/controllers/automations/controller.py)
>
> Models: [`AutomationTrigger`, `AutomationAction`, `AutomationCondition`, `LightEffect`, `AutomationTree`, `ActionNode`, `ConditionNode`, `ParsedAutomation`, `AutomationLocation`, `YamlDiff`](../esphome_device_builder/models/automations.py)

The automations API is the structured editor's wire surface. The catalog (triggers / actions / conditions / light effects) ships pre-rendered as `definitions/automations.json`, emitted at sync time by `script/sync_components.py`. Parameter schemas come out in the same `ConfigEntry[]` shape the component form already speaks — the editor reuses one form pipeline. Action / condition bodies carry `required_groups` (`[{kind, keys}]`, same shape and kinds as the component catalog's) when the upstream schema wraps them in `cv.has_*_one_key(...)` (e.g. `sensor.in_range` requires at least one of `above` / `below`); group members are never `advanced`.

Parsing and writing live on the backend: the frontend exchanges structured `AutomationTree` blobs through `parse` / `upsert` / `delete`, and the writer returns a `YamlDiff = {fromLine, toLine, replacement}` the device editor splices into the YAML pane through the existing optimistic-update path. The backend does *not* persist the YAML in `upsert` / `delete` — the device editor's config-write debounce handles that.

**Lambda sentinel.** Templatable field values can be either a literal of the field's declared type or `{"_lambda": "<C++ source>"}`. The writer emits the latter as a ruamel `LiteralScalarString` (`|`-style block scalar), the parser inverts.

**Location discriminator.** Every parsed automation carries a tagged-union `AutomationLocation`:

```
{kind: "script",        id: string}
{kind: "interval",      index: int}
{kind: "component_on",  component_id: string, trigger: string, index?: int}
{kind: "component_action", component_id: string, field: string}
{kind: "device_on",     trigger: string}
{kind: "light_effect",  component_id: string, index: int}
{kind: "api_action",    action_name: string}
```

`upsert` / `delete` consume the same shape so the writer knows the exact YAML range to splice. On `component_on`, `index` is omitted for a single-handler mapping (`on_press: {then: [...]}`) and set for one entry of a list-shaped trigger such as `time.on_time` (a YAML list of cron schedules); `index == <entry count>` on `upsert` appends a new entry. `api_action` covers user-defined actions under `api.actions:` — structurally a callable (named, typed `variables:`, `then:` action list, no trigger) so the editor reuses the script pipeline. The deprecated `service:` discriminator is accepted on read; the writer emits `action:`. `component_action` is a `type: trigger` config field on a component instance (cover `open_action` / `close_action` / `stop_action`, hub `opentherm.before_send`, …) — a bare action list keyed on `field`, with no trigger or `then:` wrapper.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `automations/get_triggers` | `{platform?, board_id?}` | `[AutomationTrigger]` | Full trigger catalog. `platform` / `board_id` are reserved for future platform gating and ignored today. |
| `automations/get_actions` | `{platform?, board_id?}` | `[AutomationAction]` | Full action catalog (includes core control-flow: `if`, `while`, `repeat`, `wait_until`, `delay`, `lambda`). |
| `automations/get_conditions` | `{platform?, board_id?}` | `[AutomationCondition]` | Full condition catalog (includes core combinators: `and`, `or`, `all`, `any`, `not`, `xor`, `for`, `lambda`). |
| `automations/get_light_effects` | `{platform?, board_id?}` | `[LightEffect]` | Full light-effects catalog. |
| `automations/get_filters` | `{}` | `[Filter]` | Full sensor / binary_sensor / text_sensor filter catalog. Each entry's `applies_to` lists the component domains the filter is valid on; the REGISTRY_LIST renderer uses it to scope the per-row picker. |
| `automations/get_available` | `{configuration, yaml?}` | `{triggers, actions, conditions, scripts, devices}` | Scoped catalog for a single device. Optional `yaml` scopes off the editor's unsaved draft instead of disk, so a component just added through the wizard exposes its triggers before the global save. `triggers` / `actions` / `conditions` filtered to the components present in the YAML, matched by the catalog's canonical `<domain>.<platform>` form (e.g. an action with `domain == "switch.template"` only surfaces when a switch with `platform: template` is configured); `core` items (control flow, lambda, combinators) and device-level triggers are always included. `scripts` lists declared `script: id`s with their `parameters:` map. `devices` lists every configured component instance with its `id` / `name` for id-picker dropdowns, plus `title` — the component's catalog display name (e.g. `wifi` → "WiFi Component"), shown when `name` is unset so a nameless singleton reads as a human name instead of the raw key. A multi-entity platform component (e.g. `sensor: - platform: aht10`) also lists each configured nested sub-entity (`temperature` / `humidity`) as its own instance keyed on the bare sub-domain (`component_id: "sensor"`) with `parent_id` set to the container — on its declared `id:`, or on the synthetic `<parent_instance_id>_<sub_key>` id when the sub-block has none — and always marks the platform item `is_entity_container: true` (even with no sub-blocks configured) so the frontend offers entity triggers on the sub-entities and never on the container. |
| `automations/parse` | `{configuration, yaml?}` | `[ParsedAutomation]` | Walk the device YAML and return every recognised automation (top-level `script:` / `interval:`, `api.actions:`, device-level `esphome.on_*`, inline component `on_*:`, light `effects:` entries). An unknown action / condition id flags only its own automation (`error` set, empty tree); siblings still parse. A *known* action with no structured form (an oversized LVGL `*.update`) additionally sets `unsupported: true`, so the editor shows the neutral "edit in YAML" hint instead of an error alert. A YAML that won't load at all raises `INVALID_ARGS`. Optional `yaml` parses the unsaved draft instead of disk. |
| `automations/upsert` | `{configuration, automation, location, yaml?}` | `{yaml_diff: YamlDiff}` | Insert or replace one automation at `location`. Returns the splice the frontend applies in place. Optional `yaml` splices into the unsaved draft instead of disk. |
| `automations/delete` | `{configuration, location, yaml?}` | `{yaml_diff: YamlDiff}` | Remove the automation at `location`. Optional `yaml` splices into the unsaved draft instead of disk. |

### Config

> Controller: [`ConfigController`](../esphome_device_builder/controllers/config/controller.py)
>
> Models: [`UserPreferences`](../esphome_device_builder/models/preferences.py)

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `config/version` | — | `{server_version, esphome_version}` | Get versions |
| `config/serial_ports` | — | `[{port, desc, vid, pid, hint}]` | List serial ports. `vid`/`pid` are the USB ids (`null` when unknown); `hint` is `esp` (Espressif native USB), `bridge` (common USB-UART bridge chip), or `null` |
| `config/get_preferences` | — | `UserPreferences` | Get user preferences |
| `config/set_preferences` | `{theme?, dashboard_view?, experience_level?, remote_compute_only?, version_history_enabled?, device_editor_layout?, secrets_editor_layout?, ...}` | `UserPreferences` | Update preferences (partial). `experience_level` is `beginner` / `expert` (or `null` until chosen); `remote_compute_only` marks an install as a remote build node. `version_history_enabled` (default `true`) gates the git auto-commit of config edits; setting it `false` stops new commits and skips repo creation, leaving any existing repo intact. `device_editor_layout` is `visual` / `yaml` / `both` and `secrets_editor_layout` is `visual` / `yaml` (the secrets editor has no split view); they persist which editor panes the user keeps open. |
| `config/get_secrets` | — | `[string]` | List secret key names |
| `config/set_secret` | `{key, value, overwrite?}` | `{created}` | Atomically set one secret in secrets.yaml under a write lock; `overwrite=false` is create-if-absent |
| `config/set_wifi_credentials` | `{ssid, password?}` | `{}` | Set `wifi_ssid` / `wifi_password` in `secrets.yaml` via a line-based rewrite that preserves standalone and inline trailing comments and other secrets. Backs the kebab "Set up Wi-Fi" action; the create wizard's own Wi-Fi entry is persisted by `devices/create`. Validates (shared with that path) against ESPHome's length limits (32 char SSID, 64 char password) plus a control-character check; empty / whitespace-only SSID, oversize values, and control characters (other than `\t`) raise `INVALID_ARGS`. `password` is optional and defaults to the empty string for open networks. |

### Onboarding

> Controller: [`OnboardingController`](../esphome_device_builder/controllers/onboarding.py)
>
> Models: [`OnboardingState`, `OnboardingStep`, `OnboardingStepId`, `OnboardingStepStatus`](../esphome_device_builder/models/onboarding.py)

First-run setup tracking. Each step's `status` is computed from live prefs on every `get_state` call (never persisted), so the frontend's "needs attention" indicators clear the moment the user fixes the underlying state. `completed_version` is the last onboarding-flow version the user has explicitly acknowledged; bumping `ONBOARDING_VERSION` (server-side constant) re-prompts users at lower versions when new steps are added. The step list is environment-aware: `use_case` only on non-HA installs. Wi-Fi credentials are **not** collected here — they're set per-device in the create wizard (persisted by `devices/create`) or via the kebab "Set up Wi-Fi" action (`config/set_wifi_credentials`). A pre-existing install (prior onboarding completed, or device YAML already on disk) is migrated to the `expert` experience at startup so the wizard never auto-pops for it.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `onboarding/get_state` | — | `OnboardingState` | Snapshot of current vs acknowledged version + per-step `pending` / `done` status. Steps: `use_case` (non-HA only — remote-compute choice) and `experience_level` both track whether `experience_level` is set. |
| `onboarding/mark_acknowledged` | — | `OnboardingState` | Record that the user has finished the current onboarding flow (sets `onboarding_completed_version` to `ONBOARDING_VERSION`). Idempotent and monotonic — never downgrades a higher stored value. |

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

### Version History

> Controller: [`VersionHistoryController`](../esphome_device_builder/controllers/version_history/controller.py)

Git-backed history of the config directory. On startup the backend adopts an existing git work tree (covering `/config/esphome` already being a repo, or sitting inside one such as `/config`) or initializes a fresh one. Every dashboard YAML mutation is committed with a descriptive message; edits made outside the dashboard (VS Code, the HA File Editor) are picked up by a debounced, scanner-driven catch-all. Deleting or archiving a config commits the removal so its pre-deletion content stays restorable. The whole feature self-disables when the `git` binary is absent — these commands then return empty lists, and the mutators raise `not_found`.

Commits are pathspec-scoped and never touch the user's git config (commit identity is passed per-invocation), so an automatic commit can't sweep a user's unrelated staged edits into history.

The auto-commit is gated by the `version_history_enabled` preference (default on, an expert-only toggle in the UI). Setting it `false` via `config/set_preferences` stops new commits and creates no repo, but an existing repo is still discovered read-only, so the read commands below keep serving its history — whether it was toggled off at runtime or the dashboard started up already opted out. A fresh install that has never had a repo returns empty until it is re-enabled.

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `version_history/list_versions` | `{configuration}` | `[{sha, short_sha, author, timestamp, message}]` | Commit history for a config, newest first. `[]` when disabled. |
| `version_history/get_version` | `{configuration, sha}` | `{configuration, sha, content}` | The config's YAML content at a commit. `not_found` if the file didn't exist there. |
| `version_history/get_diff` | `{configuration, sha}` | `{configuration, sha, diff}` | Unified diff of the config between `sha` and the working copy. |
| `version_history/list_deleted` | — | `[{configuration}]` | Configs present in history but absent from the working tree (restorable deletions). |
| `version_history/restore` | `{configuration, sha?}` | `{configuration, restored_from, content}` | Restore a config to `sha` (or its latest surviving version when omitted). Recreates a deleted file; the write goes through the normal persist path, so the device row updates via `device_added` / `device_updated` and the restore is itself committed. |

`sha` is validated as plain hex before reaching git. Reads are transient git queries — like `remote_build/list_hosts`, they're `list_*`-style commands rather than `subscribe_events` state.

### Remote Build

> Controller: [`RemoteBuildController`](../esphome_device_builder/controllers/remote_build.py)
>
> Models: [`RemoteBuildSettingsView`](../esphome_device_builder/models/remote_build.py), [`RemoteBuildPeer`](../esphome_device_builder/models/remote_build.py), [`PeerSummary`](../esphome_device_builder/models/remote_build.py), [`IdentityView`](../esphome_device_builder/models/remote_build.py)

Receiver-side surface for the remote-build offload feature (issue #106). Discovers peer dashboards via mDNS (`_esphomebuilder._tcp.local.`) and pairs with offloaders over the peer-link Noise WS (`/remote-build/peer-link`, default port 6055). Cross-subnet pair flows skip the discovery surface entirely — the pair dialog accepts a typed `hostname` / `port` directly and `request_pair` either succeeds or fails; there's no intermediate "save this host so I can pair it later" step. Receiver-side state lives across two files: the master `enabled` toggle in `.device-builder.json` under `_remote_build`, and APPROVED `StoredPeer` rows in their own sibling file `<config_dir>/.receiver_peers.json` (per-file `helpers.storage.Store` with debounced writes — atomic per-domain, no lock contention against unrelated metadata writers). Offloader-side pairings follow the same shape at `<config_dir>/.offloader_pairings.json`.

#### Surface map: which commands run on which side

A single `device-builder` process can be a *receiver* (accepts Noise WS connections from offloaders, lets a human admin pair them) and an *offloader* (initiates Noise WS connections to receivers it has pinned) at the same time. Each WS command targets one role. The frontend surfaces them on different Settings screens — "Build server" (receiver role) vs "Send builds" (offloader role). All commands run over the dashboard's main `/ws` endpoint and inherit whatever auth that endpoint enforces (today: none — the dashboard `/ws` trusts any local connection); none of these commands run over the peer-link Noise WS, which carries only `intent=...` frames between dashboards, never WS commands.

| Command | Side | Notes |
|---|---|---|
| `get_settings` / `set_settings` | receiver | Master toggle for whether this dashboard accepts incoming offloader connections. Off-default; toggling requires a restart. |
| `approve_peer` / `remove_peer` | receiver | Admin manages incoming pairings. The peer list itself is delivered via the `subscribe_events` initial-state push and mutated locally on the frontend from `remote_build_pair_request_received` / `remote_build_pair_status_changed` events — no separate `list_peers` command. |
| (no command) | both | mDNS-discovered peer dashboards reach the frontend the same way: `subscribe_events` initial-state push under `hosts`, plus `remote_build_host_added` / `remote_build_host_removed` events fired from the receiver controller's mDNS browser callbacks. Cross-subnet pair flows bypass discovery — the pair dialog accepts a typed hostname / port and `request_pair` either succeeds or fails. |
| `set_pairing_window` | receiver | Frontend-driven; the Pairing requests screen calls `open=true` on mount + extend ticks, `open=false` on unmount. |
| `get_identity` / `rotate_identity` | receiver | Surfaces / rotates the dashboard's identity for OOB pin verification. |
| `preview_pair` | offloader | Open a brief Noise WS to capture a receiver's pin for OOB display. |
| `request_pair` | offloader | Send `intent=pair_request`. Both PENDING and APPROVED rows live in the controller's unified `_pairings` dict; the per-file `Store` debounce-saves APPROVED rows to `<config_dir>/.offloader_pairings.json` (PENDING is filtered out at serialise time). APPROVED result spawns no listener; PENDING result spawns a `_pair_status_listener` task that flips the row's status on flip. |
| `unpair` | offloader | Drop the row from the unified `_pairings` dict and schedule the debounced save. Cancels the row's listener task if any. Idempotent. Auto-clears any pending `pin_mismatch` / `peer_revoked` alert for the same `(hostname, port)`. |

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `remote_build/get_settings` | — | `RemoteBuildSettingsView` | Read the receiver-side settings (`enabled`, `peers`). |
| `remote_build/set_settings` | `{enabled}` | `RemoteBuildSettingsView` | Persist the master switch. Strict-bool; rejects truthy strings. |
| `remote_build/approve_peer` | `{dashboard_id}` | `RemoteBuildSettingsView` | Promote a `PENDING` row to `APPROVED`. Mutates the RAM-canonical `_approved_peers` dict and schedules a debounced write to `<config_dir>/.receiver_peers.json` via the per-file `Store`. Fires `remote_build_pair_status_changed`. |
| `remote_build/remove_peer` | `{dashboard_id}` | `RemoteBuildSettingsView` | Drop a peer row. PENDING entries live in the controller's `_pending_peers` dict; APPROVED entries live in `_approved_peers` and are debounce-saved to `.receiver_peers.json`. Fires `remote_build_pair_status_changed` with `status="removed"` for either case (the event wakes any in-flight pair_status long-poll, which is needed for the PENDING case to drop the offloader's local state). `not_found` when neither dict has a matching row. |
| `remote_build/set_pairing_window` | `{open}` | — | Open / close the pairing window for the calling WS client. The window narrows when `intent="pair_request"` Noise frames are even accepted; refcounted across clients with auto-close timeout. Fires `remote_build_pairing_window_changed` on transitions. |
| `remote_build/get_identity` | — | `IdentityView` | Read the receiver's stable identity: `{dashboard_id, pin_sha256, server_version, esphome_version, listener_bound}`. `pin_sha256` is the lowercase-hex SHA-256 of the X25519 peer-link public key — the same fingerprint advertised in mDNS TXT and the value peers OOB-verify during pairing. `listener_bound` reports whether the peer-link Noise WS listener is currently serving traffic. Idempotent (no rotation triggered). |
| `remote_build/rotate_identity` | — | `IdentityView` | Mint a fresh X25519 peer-link keypair, replacing whatever's on disk. The `dashboard_id` is preserved across rotations so the receiver-side audit trail stays readable. Every paired peer that pinned the old `pin_sha256` will see a fingerprint mismatch on the next Noise handshake and need to re-pair. The peer-link listener is torn down + rebuilt as a side effect so the new key is in service immediately. |
| `remote_build/preview_pair` | `{hostname, port}` | `{pin_sha256, requires_pairing_key}` | Open a brief Noise XX WS to a receiver, capture the static pubkey, return the lowercase-hex SHA-256 for OOB display. `requires_pairing_key` is true for a key-mode (`--remote-build-only`) receiver, so the pair dialog can require the console key up front; it's static on the server's mode (never reveals whether a bootstrap window is open) and rides only on the preview response, never on a `pair_request` refusal (which stays indistinguishable from a closed window). No state mutated on either side. `unavailable` on transport / handshake failure. |
| `remote_build/request_pair` | `{hostname, port, pin_sha256, receiver_label, offloader_label, pairing_key?}` | `PairingSummary` | Re-handshake (defends against TOCTOU between preview and confirm — the observed static key is checked against `pin_sha256` *before* msg3 is written), send `intent="pair_request"` carrying `{label: offloader_label, dashboard_id}` — plus `pairing_key` when supplied (the `--remote-build-only` bootstrap's console-printed pairing key) — in encrypted msg3. The unified `_pairings` dict holds both PENDING and APPROVED rows; APPROVED rows debounce-save to `<config_dir>/.offloader_pairings.json` via the per-file `Store`, and PENDING rows are filtered out at serialise time so a malicious LAN scanner can't bloat the file. PENDING result spawns a pair-status listener task that flips the row's status in place + schedules a save when the receiver reports the eventual flip; APPROVED result short-circuits the inbox dance. PENDING rows don't survive a controller restart — any in-flight pair attempt has to be re-issued. `precondition_failed` on pin mismatch; `no_pairing_window` when the receiver's window is closed; `unavailable` on transport failure; `internal_error` on an unexpected receiver `intent_response`. |
| `remote_build/unpair` | `{pin_sha256}` | `{removed: bool}` | Pop the row from the unified `_pairings` dict (keyed on the pin) and schedule the debounced save. Idempotent — `removed=false` when no row matched. Cancels the row's pair-status listener task and any long-lived peer-link client. Auto-clears any pending offloader alert (`pin_mismatch` / `peer_revoked`) for the same pin. The receiver-side `StoredPeer` is *not* notified; that's the receiver admin's concern (a future `intent="peer_link"` from this offloader will be rejected because the local row is gone). |
| `remote_build/submit_job` | `{pin_sha256, configuration, target}` | `{job_id, accepted, reason?}` | Offloader-side: bundle *configuration* and dispatch a build to the receiver behind *pin_sha256*. Validates path-traversal via `rel_path`, looks up the live `PeerLinkClient`, spawns `esphome bundle <yaml> -o <tmp.tar.gz>` (subprocess, 60s timeout — see `helpers.config_bundle.build_yaml_bundle`), streams the gzipped tarball as `submit_job_chunk` frames over the open peer-link, and awaits the receiver's `submit_job_ack`. *target* is one of `compile` / `upload`. Live job lifecycle + output flow asynchronously through `offloader_job_state_changed` / `offloader_job_output` events on the global `subscribe_events` stream (no separate subscription channel). `invalid_args` on bad input or non-zero bundle exit (CLI stdout inlined); `not_found` on missing pairing / YAML; `precondition_failed` on PENDING / disconnected peer-link; `unavailable` on ack timeout / session loss mid-flow. |
| `remote_build/cancel_job` | `{pin_sha256, job_id}` | `{sent}` | Offloader-side: cooperative cancel for a previously-submitted remote job. *job_id* is the offloader-local id returned by `remote_build/submit_job`. Fire-and-forget — sends a `cancel_job` frame over the open peer-link and returns `{sent: true}` if the frame made it to the wire. The receiver resolves the offloader-local id back to its `FirmwareJob` via the `JobFanout` correlation cache and routes through `FirmwareController.cancel`, same primitive as a local operator-driven cancel. The next `offloader_job_state_changed` with `status="cancelled"` is the confirmation — no separate ack frame. `invalid_args` on bad pin / empty job_id; `not_found` on missing pairing; `precondition_failed` on PENDING / disconnected peer-link. |
| `remote_build/edit_pairing_endpoint` | `{pin_sha256, hostname, port}` | `PairingSummary` | Offloader-side: user-driven manual rebind of an existing APPROVED pairing onto new (`hostname`, `port`) coords. Fallback for the cross-subnet / no-mDNS cases the mDNS auto-rebind path can't catch. Same trust model as the auto-rebind: a one-shot `peer_link_preview_pair` probe verifies the new endpoint is reachable AND answers with the same pin the row was paired against. Identity-mismatch refuses the edit and leaves the stored pairing untouched (the user's existing trust is keyed on the original pin; substituting a fresh pubkey is what the re-auth wizard exists to gate). Match path mutates `StoredPairing.receiver_hostname` / `.receiver_port` in place, schedules the debounced save, cancels + respawns the `PeerLinkClient` against the new coords, and fires `offloader_pair_endpoint_rebound`. Same probe + commit primitives the auto-rebind path uses. `invalid_args` on bad pin / hostname / port; `not_found` on missing pairing or pairing replaced mid-probe (concurrent unpair / re-pair); `precondition_failed` on non-APPROVED status, no-op edit (new coords match current), missing offloader identity, or pin mismatch at the new endpoint; `unavailable` on probe transport / handshake failure. |
| `remote_build/download_artifacts` | `{pin_sha256, job_id}` | `{job_id, idedata, images, total_bytes}` | Offloader-side: fetch the build's flash-artifact set for a previously-completed remote job. Sends `download_artifacts{job_id}` over the open peer-link, parks on the assembled-bytes future, then unpacks the receiver's gzipped tarball off the event loop. `images` is `[{name, offset, size, data_b64}]` — `firmware.bin` first (offset taken from the `artifacts_start` header — receiver-resolved from `StorageJSON.target_platform` so the offloader doesn't duplicate platform-detection logic), then `idedata.extra.flash_images` in declared order. `idedata` is the parsed manifest with `extra.flash_images[].path` rewritten from absolute receiver-side paths to bare basenames matching the entries in `images`. `total_bytes` is the sum of every image's `size` (frontend progress UI). The downstream install paths (Web Serial / network OTA / download-to-disk) consume this shape directly. `invalid_args` on bad pin / empty job_id / malformed tarball from the receiver; `not_found` on missing pairing or receiver-reported `unknown_job` / `build_dir_missing`; `precondition_failed` on PENDING / disconnected peer-link or receiver-reported `job_not_completed` / `duplicate_download`; `unavailable` on session loss mid-download or receiver-reported `pack_failed`. |
| `remote_build/set_offloader_settings` | `{remote_builds_enabled?, version_match_policy?, include_local_in_pool?}` | `OffloaderRemoteBuildSettingsView` | Offloader-side: flip one or more master settings. `remote_builds_enabled=False` short-circuits `pick_build_path` to LOCAL. `version_match_policy` selects how strictly the scheduler filters peers by ESPHome version — `"any"` (default) skips the gate, `"release"` filters peers whose `YYYY.MM` differs from the offloader, `"exact"` filters any version diff with LOCAL fallback, `"exact_required"` raises `no_compatible_peer` when *any* APPROVED + enabled pairing exists and none are eligible (operator-visible signal — see issue #985). The strictest policy hard-fails on a transient peer-link disconnection just as readily as on a version mismatch, by design: silently routing to LOCAL behind an offline build server has the same surface as silently routing around a version-mismatched one. `include_local_in_pool=True` (advanced, default `False`) adds the local machine to the build pool: at dispatch a compile that would otherwise WAIT because every eligible build server is busy runs on the local compile lane instead, so local and remote compile concurrently. Single builds still prefer an idle remote server (the offload-from-slow-SBC default is unchanged); local only absorbs overflow. Every arg is optional; passing all `None` raises `invalid_args` to keep the surface loud about misuse. Unknown policy values also raise `invalid_args`. Each changed field fires its own event (`OFFLOADER_REMOTE_BUILDS_TOGGLED` / `OFFLOADER_VERSION_MATCH_POLICY_CHANGED` / `OFFLOADER_INCLUDE_LOCAL_CHANGED`) for cross-tab sync and debounce-saves the pairings store. |
| `remote_build/set_pairing_enabled` | `{pin_sha256, enabled}` | `PairingSummary` | Offloader-side: flip a single pairing's "auto-route to this server" switch. `False` skips the row in `pick_build_path` while keeping the peer-link session open and the manual Send-builds dispatch path working. `not_found` on unknown pin; strict-bool on `enabled`. Fires `OFFLOADER_PAIRING_ENABLED_CHANGED`. |

#### Peer-link Noise WS receiver site

A separate aiohttp `web.Application` binds on the dashboard's `--remote-build-port` (default `6055`) and serves `/remote-build/peer-link` — a `Noise_XX_25519_ChaChaPoly_SHA256` WebSocket endpoint. Default-off; binds only when `RemoteBuildSettings.enabled` is true. **Toggling `enabled` requires a dashboard restart for the listener to follow** — `set_settings` persists the new value but doesn't live-bind / unbind.

The Noise XX handshake exchanges static X25519 pubkeys mutually; the offloader pins the receiver's pin (out-of-band verified via `intent="preview"`) and the receiver looks up the offloader's pin against its `peers` list. Post-handshake, a single transport frame carries `{intent_response: ...}`. One-shot intents (`preview` / `pair_request` / `pair_status`) close the WS after that frame; `intent="peer_link"` on a successful auth keeps the WS open for application messages.

A non-OK `intent_response` carries an optional `reason` (`RejectReason`): `no_approved_peer`, `pin_mismatch`, `pending_not_approved`, `bad_dashboard_id`, `bad_intent`. It is additive (absent from older receivers, read as `None`) and disambiguates the otherwise-opaque `rejected`; self-describing responses like `no_pairing_window` carry no reason. For a long-lived `peer_link`, the offloader treats `no_approved_peer` / `pin_mismatch` as terminal: it orphans the client (no 30s reconnect loop) and fires `OFFLOADER_PAIR_PEER_REVOKED` so the UI shows a re-pair CTA; a reason-less or `pending_not_approved` reject stays on the bounded-backoff reconnect path. The receiver logs the dispatch decision (not a premature `ok`) and, on a pin mismatch, logs the stored vs observed offloader pin.

**Fully resetting a pairing** is per-end: offloader `remote_build/unpair` clears the `StoredPairing` (RAM + disk) and cancels the client, receiver `remote_build/remove_peer` clears the `StoredPeer` (RAM `pending` + RAM/disk `approved`). Removing on one side does not clear the other; a clean re-pair after a stuck `peer_link` rejection needs both, then a fresh `request_pair`.

**Peer-link application messages** (post-handshake, ride over the established Noise session as one JSON-encoded transport frame per WS message). The complete set declared by `AppMessageType` in `controllers/remote_build/peer_link.py`:

| `type` | Direction | Payload | Description |
|---|---|---|---|
| `ping` | both | `{nonce}` | Encrypted heartbeat probe. Each side fires every `HEARTBEAT_INTERVAL_SECONDS` and expects the matching `pong` within `HEARTBEAT_DEAD_AFTER_SECONDS`. Three consecutive misses close the session with reason `heartbeat_timeout`. |
| `pong` | both | `{nonce}` | Response to a `ping`. The receiver bumps its `last_pong_at` shared-state field; the heartbeat task watches that timestamp to decide when to call `_on_dead`. |
| `terminate` | both | `{reason}` | Structured close frame. `reason` is one of `superseded` / `server_shutting_down` / `heartbeat_timeout` / `malformed_frame` / `client_stopped`. Sent before the WS close so the peer logs the cause; the matching close-reason flows out via `RECEIVER_PEER_LINK_SESSION_CLOSED` / `OFFLOADER_PEER_LINK_CLOSED`. |
| `queue_status` | receiver → offloader | `{idle, running, queue_depth}` | Receiver pushes a fresh firmware-queue snapshot whenever the queue transitions (`JOB_QUEUED` / `JOB_STARTED` / terminal events). Fan-out covers every paired offloader's open session. The offloader's `PeerLinkClient` receive loop validates the wire shape, then fires `OFFLOADER_QUEUE_STATUS_CHANGED`. |
| `submit_job` | offloader → receiver | `{job_id, configuration_filename, target, total_bundle_bytes, num_chunks, bundle_sha256}` | Header announcing a build before the bundle bytes start. `target` is `compile` / `upload`. The receiver pre-sizes its `BundleAssembler` against `total_bundle_bytes` + `num_chunks` and rejects a mismatched stream cleanly. `bundle_sha256` is the lowercase-hex digest of the full bundle bytes — cheap end-to-end integrity check on top of per-frame Noise AEAD. |
| `submit_job_chunk` | offloader → receiver | `{job_id, chunk_index, data_b64, is_last}` | One slice of the gzipped tarball, base64-encoded inside the JSON envelope so frames stay JSON-shaped (33 % b64 overhead deliberate — keeps the dispatch seam uniform). Chunks must arrive in monotonic order; the assembler rejects out-of-order, duplicate, or post-completion frames with a structured error that triggers `terminate{reason: malformed_frame}`. |
| `submit_job_ack` | receiver → offloader | `{job_id, accepted, reason?}` | Receiver's response after the bundle stream completes and the SHA-256 matches. `accepted=False` carries a structured `reason` (`bundle_hash_mismatch`, `manifest_unsupported`, `queue_full`, etc.); `reason` is omitted on accept. The offloader's submit handler waits with `_SUBMIT_JOB_ACK_TIMEOUT_SECONDS` (60s); ack-missing raises `SubmitJobTimeoutError` (maps to WS `unavailable`). No mid-session retry — the receiver may already have queued the job. |
| `job_state_changed` | receiver → offloader | `{job_id, status, error_message}` | Receiver-pushed lifecycle transitions: `queued` / `running` / `completed` / `failed` / `cancelled`. Fans out from the firmware controller's existing `JOB_*` bus events via `JobFanout` on the receiver, filtered to jobs whose `remote_peer` matches an active peer-link session. The offloader fires `OFFLOADER_JOB_STATE_CHANGED` and maintains the `_offloader_remote_jobs` RAM cache (terminal entries drop on transition). |
| `job_output` | receiver → offloader | `{job_id, stream, line}` | High-rate during an active build (one frame per line of compiler / linker output). `stream` is `stdout` / `stderr`; `line` keeps its trailing terminator (`\n` / `\r` / `\r\n`) — carriage-return-only chunks are esptool / PlatformIO progress overwrites and stripping them would lose the renderer's append-vs-overwrite signal. The offloader fires `OFFLOADER_JOB_OUTPUT` per frame; no cache (live stream only). |
| `cancel_job` | offloader → receiver | `{job_id}` | Cooperative cancel for a previously-submitted job. `job_id` is the offloader-supplied id from the original `submit_job` header — i.e. the value the offloader generated and the receiver stashed as `FirmwareJob.remote_job_id`. Receiver resolves the offloader-side id back to its receiver-local `FirmwareJob` via `JobFanout.resolve_firmware_job_id` (reverse scan over the `_remote_jobs` cache) and routes through `FirmwareController.cancel` — same primitive as a local operator-driven cancel. No ack frame: the resulting `JOB_CANCELLED` bus event fans out a `job_state_changed{status: cancelled}` which the offloader already plumbs through `OFFLOADER_JOB_STATE_CHANGED`. Silent drops at the receiver (debug-logged) on: malformed shape (off-contract peer), unknown correlation (race with terminal transition that already evicted the cache entry), `CommandError` from the firmware queue (already-terminal job — the cancel intent has already been satisfied by the natural exit). |
| `download_artifacts` | offloader → receiver | `{job_id}` | Request the build's flash-artifact set for a previously-completed remote job. `job_id` is the offloader-supplied id from the original `submit_job` header (same id-space as `cancel_job`). Receiver resolves the id back to its `FirmwareJob` (linear scan over `firmware._jobs`; cardinality bounded by the queue's retention), refuses the request with a structured `reason` if the job is unknown / not COMPLETED / has already a download in flight on the same session, otherwise reads `idedata.json` + every flash image from the build dir and streams the bytes back as `artifacts_start` → `artifacts_chunk` → `artifacts_end`. Single-flight per session — a second `download_artifacts` while the first is still streaming gets `duplicate_download`. |
| `artifacts_start` | receiver → offloader | `{job_id, total_bytes, num_chunks, artifacts_sha256, firmware_offset}` | Header announcing the gzipped-tar stream that follows. The offloader pre-sizes its `BundleAssembler` against `total_bytes` + `num_chunks` (capped at `FIRMWARE_MAX_TOTAL_BYTES` = 16 MiB), validates each subsequent chunk against these bounds, and recomputes `artifacts_sha256` after assembly to catch chunk-reordering bugs in our own framing (per-frame Noise AEAD already covers wire confidentiality + authentication). `firmware_offset` is the lowercase-hex flash offset for the `firmware.bin` partition (e.g. `"0x10000"` on ESP32, `"0x0"` on ESP8266 / libretiny / RP2040), resolved on the receiver from `StorageJSON.target_platform` so the offloader doesn't duplicate platform-detection logic. The remaining flash-image offsets ride inside `idedata.json` in the tarball. |
| `artifacts_chunk` | receiver → offloader | `{job_id, chunk_index, data_b64, is_last}` | One slice of the gzipped tarball, base64-encoded inside the JSON envelope (same shape as `submit_job_chunk` — keeps the dispatch seam uniform; `BundleAssembler` is reused with `max_total_bytes=FIRMWARE_MAX_TOTAL_BYTES`). Chunks must arrive in monotonic order; the assembler rejects out-of-order / duplicate / post-completion frames with a structured `DownloadArtifactsError` that resolves the offloader's parked future. |
| `artifacts_end` | receiver → offloader | `{job_id, accepted, reason?}` | Stream terminator. `accepted=true`: assembler finalises (validates count + SHA-256), the parked `download_artifacts()` future resolves to `DownloadArtifactsResult(tarball, firmware_offset)`. `accepted=false`: carries a structured `reason` — `unknown_job`, `job_not_completed`, `duplicate_download`, `build_dir_missing`, `pack_failed` — the offloader's WS layer maps these to `not_found` / `precondition_failed` / `unavailable` `CommandError` codes. (Protocol violations like a malformed `download_artifacts` frame skip the soft-reject path entirely and terminate the session with `MALFORMED_FRAME`.) Tarball layout: `idedata.json` first (the upstream-canonical flash-image manifest, with `extra.flash_images[].path` carrying receiver-absolute paths the offloader rewrites to basenames at unpack time), then `firmware.bin`, then every `extra.flash_images` entry flattened to its basename. |

All application messages flow over the same `Noise_XX_25519_ChaChaPoly_SHA256` transport — the Noise cipher state is single-direction so a per-channel `send_lock` (in `PeerLinkChannel.send_frame`) serialises concurrent encrypts. The dispatch loop on each side branches on the `type` discriminator; unknown types are debug-logged and dropped (forward-compatibility).

The receiver advertises the listener's port over mDNS as a TXT property:

| TXT property | Value | When present |
|---|---|---|
| `server_version` | `"1.2.3"` | always |
| `esphome_version` | `"2026.5.0"` | always |
| `friendly_name` | human machine label (e.g. `"MacBook-Pro"`) | when a friendly name is set (always in practice) |
| `pin_sha256` | lowercase-hex SHA-256 of the X25519 peer-link pubkey | when the peer-link listener is bound |
| `remote_build_port` | stringified int (e.g. `"6055"`) — the port actually bound, which may differ from `--remote-build-port` when a taken port fell forward to the next free one | when the peer-link listener is bound (same condition as `pin_sha256`) |

The service-instance name and SRV target are stable per-install identifiers (`esphome-builder-<dashboard_id[:8]>.local`) derived from the persisted `dashboard_id`, not the OS hostname, so they don't change across reboots; `friendly_name` carries the human machine label for display.

Same-subnet peers read `remote_build_port` from TXT so a `--remote-build-port` override is auto-discovered. Cross-subnet peers type the port into the pair dialog (it's an arg on `request_pair`).

### Utility

| Command | Args | Response | Description |
|---------|------|----------|-------------|
| `ping` | — | `{pong: true}` | Health check |
| `subscribe_events` | — | Streaming | Subscribe to real-time events |

**`subscribe_events` initial state:**

Right after a client subscribes (and before any live events arrive), the server pushes one `initial_state` event carrying a snapshot of state that's accumulated server-side via background activity (mDNS browser, completed pair flows, etc.) so the frontend can paint the first frame without follow-up reads. Shape: `{preferences: UserPreferences, devices?: [...], importable?: [...], pairings?: [PairingSummary], peers?: [PeerSummary], hosts?: [RemoteBuildPeer], offloader_alerts?: [OffloaderAlertSnapshotEntry], peer_queue_status?: [PeerQueueStatusSnapshotEntry], remote_jobs?: [OffloaderRemoteJobSnapshotEntry], remote_builds_enabled?: bool, version_match_policy?: "any" | "release" | "exact" | "exact_required", include_local_in_pool?: bool}`. `preferences` is always present — its `experience_level` and `remote_compute_only` fields gate first-paint UI (which editor surfaces show, whether device-creation entry points are hidden), so they ride the snapshot rather than make the client chase a separate `config/get_preferences`. The rest are present only when the corresponding controller is up; `pairings` carries both PENDING and APPROVED offloader-side rows from the `_pairings` dict, `peers` carries both PENDING (`_pending_peers`) and APPROVED (`_approved_peers`) receiver-side rows, `hosts` carries the receiver controller's mDNS-discovered peer dashboards (`self._peers`, RAM-only — never persisted), `offloader_alerts` carries the offloader-side pair alerts dict (`_offloader_alerts`, RAM-only) so a tab subscribing AFTER a `pin_mismatch` / `peer_revoked` event fired still renders the alert it would have missed on the live stream — the alert only clears via re-pair or unpair, never by an operator-driven dismiss, because the underlying state (broken pairing) doesn't fix itself. `peer_queue_status` carries the most recent `queue_status` snapshot per paired receiver so a late tab paints the per-peer queue depth without waiting for the next event. `remote_jobs` carries every offloader-submitted job that's still in flight (terminal entries drop on the matching `job_state_changed` event) so the UI can render running builds on page load. `remote_builds_enabled`, `version_match_policy` and `include_local_in_pool` carry the current value of the offloader-wide settings so the Settings dialog renders the controls on first paint instead of waiting for the matching `offloader_remote_builds_toggled` / `offloader_version_match_policy_changed` / `offloader_include_local_changed` event to fire. All sync RAM reads, no executor hop, no disk I/O — `preferences` is RAM-canonical behind a `PreferencesStore` (loaded once at startup, mutations debounce a write to its own `.device-builder-preferences.json`, migrated out of the shared sidecar on first run; the same per-file `Store` pattern as the device-metadata and peer-link stores). Undecodable preferences are preserved, never destroyed: a corrupt dedicated file is renamed to `.corrupt` and an undecodable legacy sidecar blob is left in place (not stripped), both logged, before the store falls back to defaults. The `PeerSummary` projection persists `peer_ip` (the source IP observed at pair_request time) on `StoredPeer` so a snapshot-loaded inbox row carries the same IP the live `remote_build_pair_request_received` event would carry; that's what the receiver Settings UI renders alongside the pin as a clone-risk sanity-check. Empty string for legacy on-disk rows from receivers that pre-date the field. Live updates that arrive after the initial state mutate against this seed via the events below.

**`subscribe_events` events:**
- `device_added`, `device_removed`, `device_updated`, `device_state_changed`
- `importable_device_added`, `importable_device_removed`
- `label_created`, `label_updated`, `label_deleted`
- `job_queued`, `job_started`, `job_output`, `job_completed`, `job_failed`
- `remote_build_pair_request_received` — `{dashboard_id, pin_sha256, label, peer_ip, paired_at}` — fires when an offloader's `intent="pair_request"` Noise frame lands a new `PENDING` row inside the receiver's open pairing window. The Settings UI surfaces the row in the inbox with the offending `dashboard_id`, the peer-link `pin_sha256` (X25519 pubkey hash), the offloader's claimed `label`, `peer_ip` for sanity-checking, and `paired_at` (receiver-clock unix timestamp at row creation; matches the value the `subscribe_events` snapshot would show, so a subscriber building the inbox row from the event stream can sort by it directly).
- `remote_build_pair_status_changed` — `{dashboard_id, status}` (`status: "approved" | "removed"`) — fires from three paths: (a) `approve_peer` promoting a `_pending_peers` entry to `_approved_peers` (`status="approved"`), (b) `remove_peer` dropping either a `_pending_peers` entry or an `_approved_peers` entry (`status="removed"`); APPROVED removal also schedules a debounced write to `.receiver_peers.json`, (c) pairing-window-close clearing `_pending_peers` (`status="removed"` per cleared entry). The "removed" event is what wakes any in-flight `intent="pair_status"` long-poll on a paired offloader so its listener task drops the offloader's local state. Subscribers refresh the paired-peers list without polling.
- `remote_build_pairing_window_changed` — `{open, expires_in_seconds}` — fires when the pairing window opens / closes (refcount transitions, auto-close timeout, idle ageing). `expires_in_seconds` is `null` when `open` is `false`; otherwise it's the float remaining lifetime against the latest user-activity extend. Subscribers render the "Pairing window: X seconds remaining" countdown from this value (and tick locally between events).
- `remote_build_identity_rotated` — `{dashboard_id, pin_sha256}` — fires when the operator triggers `remote_build/rotate_identity`. Subscribers refresh their cached pin without polling `get_identity`. Only fires when the on-disk rotation succeeds; the listener rebuild may still fail-soft, in which case the rotater's `IdentityView` response carries `listener_bound=false` while this event reflects only that the persistent key on disk changed.
- `remote_build_host_added` — `{name, hostname, port, source, addresses, server_version, esphome_version, friendly_name}` — fires whenever the receiver controller's mDNS browse callback or the async resolve-success path upserts a row in `self._peers`. Upsert semantics — the frontend keys its discovered-hosts list on `name` (the leftmost service-instance label) and replaces an existing row with the same key. `name` is now an opaque per-install identifier (`esphome-builder-<id>`); `friendly_name` is the human label for display (`""` for older receivers that don't broadcast it). The `subscribe_events` initial-state push carries the full current set under `hosts`, so a fresh tab paints without a round-trip; this event is the live-update channel that keeps the list current as dashboards come online (or refresh their TXT mid-session).
- `remote_build_host_removed` — `{name}` — fires when zeroconf delivers a `Removed` callback (TTL expiry without renewal, or an explicit goodbye). `name` matches the corresponding `remote_build_host_added` event's `name` field, so subscribers drop the row by key.
- `offloader_pair_status_changed` — `{receiver_hostname, receiver_port, status: "approved" | "removed"}` — offloader-side counterpart to `remote_build_pair_status_changed`. Fired by the per-row pair-status listener task (`_await_pair_status_flip` → `_apply_pair_status_result` → `_fire_offloader_pair_status_changed`) when its `intent="pair_status"` round-trip resolves: APPROVED + matching pin → `status="approved"`; APPROVED + drifted pin → `status="removed"` (treat receiver-side identity rotation as peer-revoked); REJECTED → `status="removed"`. Also fired by `remote_build/unpair` when the user removes a row. Keys on `(hostname, port)` because the offloader's `StoredPairing` keys on the receiver coordinates the user dialled, not on a receiver-side identifier the offloader doesn't track. Delivered to clients via the existing global `subscribe_events` stream — no separate subscription channel.
- `offloader_pair_pin_mismatch` — `{receiver_hostname, receiver_port, receiver_label, expected_pin, observed_pin}` — fires alongside `offloader_pair_status_changed status="removed"` when the pair-status listener observes APPROVED + drifted pin (the receiver's static X25519 pubkey hash differs from `StoredPairing.pin_sha256` recorded at pair time). Carries the diagnostic detail the status-changed event doesn't, plus the offloader-side `receiver_label` so the alert can name the row even after the pairings list has dropped it. Frontend's 4b-4 alert plumbing reshape uses the distinct event to surface a "re-pair to confirm the new identity" CTA, separate from the peer-revocation case.
- `offloader_pair_peer_revoked` — `{receiver_hostname, receiver_port, receiver_label}` — fires alongside `offloader_pair_status_changed status="removed"` when the pair-status listener gets `IntentResponse.REJECTED` (admin clicked Reject, pairing window closed, offloader's identity rotated, or row never existed on the receiver). Frontend uses this for the "the receiver removed us; reach out if this was a mistake" alert distinct from a pin-mismatch alert (different operator response — pin-mismatch can be re-paired right away, peer-revoked needs receiver-side admin coordination).
- `offloader_pair_alert_dismissed` — `{receiver_hostname, receiver_port}` — fires when an entry leaves the controller's RAM-only `_offloader_alerts` dict via one of the two resolution paths: a successful `request_pair` against the same `(hostname, port)` (re-pair fixed the broken state), or `unpair` removed the row outright. There is no operator-driven dismiss surface; clicking "OK got it" without acting would just hide a broken pairing the next peer-link session would still fail against, so the only ways out are re-pair or unpair. The event lets other tabs / clients on the global `subscribe_events` stream sync their local alerts list without re-fetching the snapshot.
- `offloader_queue_status_changed` — `{receiver_hostname, receiver_port, pin_sha256, idle, running, queue_depth}` — offloader-side cache update: a paired receiver pushed a fresh `queue_status` snapshot over the peer-link session. Fired from the offloader's `PeerLinkClient` receive loop after parsing the wire frame. The remote-build controller listens, updates its RAM-only `_peer_queue_status` cache (keyed on `pin_sha256`), and the global `subscribe_events` stream re-broadcasts to frontend clients so the per-peer queue-depth indicator renders live without polling. `subscribe_events.initial_state.peer_queue_status` carries the latest cached value per pin so late tabs paint without waiting for the next event.
- `offloader_job_state_changed` — `{receiver_hostname, receiver_port, pin_sha256, job_id, status, error_message}` — offloader-side: a paired receiver pushed a `job_state_changed` frame for a job we submitted. `status` is one of `queued` / `running` / `completed` / `failed` / `cancelled` (mirrors the wire frame literal). The controller mirrors the event into its RAM-only `_offloader_remote_jobs` cache (keyed on offloader-local `job_id`); terminal entries drop on the matching event so the snapshot only ever carries actively-running rows. Seeded into `subscribe_events.initial_state.remote_jobs`. Distinct from the local `job_*` family because remote-driven jobs don't have a corresponding `FirmwareJob` row on the offloader — the receiver owns the queue state and we only see the wire reflection.
- `offloader_job_output` — `{receiver_hostname, receiver_port, pin_sha256, job_id, stream, line}` — offloader-side: a paired receiver pushed a `job_output` frame for a job we submitted. `stream` is `stdout` / `stderr`; `line` preserves its trailing terminator (`\n` / `\r` / `\r\n` — carriage-return-only chunks are esptool / PlatformIO progress overwrites, same contract the receiver-side `job_output` event holds). High-rate path during an active build (one frame per line of compiler / linker output); subscribers should debounce / batch downstream rendering rather than re-render per event. No RAM cache — the offloader's job snapshot tracks lifecycle state only; output bytes belong in the live event stream.
- `offloader_remote_builds_toggled` — `{remote_builds_enabled}` — cross-tab sync for the master "Auto-route installs to remote build" toggle. The scheduler reads the flag off the in-RAM `OffloaderState` snapshot on every install; this event exists so other open tabs reflect the flip without re-fetching settings.
- `offloader_version_match_policy_changed` — `{version_match_policy}` — cross-tab sync for the master version-match policy (`"any"` / `"release"` / `"exact"` / `"exact_required"`; see `remote_build/set_offloader_settings` for the per-value filter + LOCAL-fallback / hard-fail semantics). Same shape as the toggled event above: scheduler reads the field off the snapshot, event keeps other tabs in sync.
- `offloader_include_local_changed` — `{include_local_in_pool}` — cross-tab sync for the "include local in build pool" advanced toggle. The dispatcher reads the flag off the in-RAM `OffloaderState` snapshot every pass; this event exists so other open tabs reflect the flip without re-fetching settings.
- `offloader_pairing_enabled_changed` — `{pin_sha256, enabled}` — cross-tab sync for a single pairing's auto-route enable switch. Subscribers update the matching row's switch without polling.

---

## Legacy REST Endpoints (Deprecated)

For Home Assistant ESPHome integration backward compat only.

| Endpoint | Description |
|----------|-------------|
| `GET /devices` | List devices. `configured` entries keep the flat legacy shape — `runtime_state` is flattened to top-level keys (`deployed_version` etc.), which HA's `esphome-dashboard-api` `ConfiguredDevice` reads |
| `GET /ping` | Online-status map `{<config>.yaml: true\|false\|null}` (third-party widgets, e.g. homepage) |
| `GET /json-config?configuration=...` | Fully-resolved config (substitutions/packages/includes/secrets) as JSON, via `esphome config`; 404 missing, 422 invalid, 503 unavailable, 403 traversal |
| `GET /compile` (WebSocket) | Compile via spawn protocol |
| `GET /upload` (WebSocket) | Upload via spawn protocol |
