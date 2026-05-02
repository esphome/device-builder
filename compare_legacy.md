# Legacy esphome-dashboard vs new device-builder backend

Comparison of the upstream `esphome/dashboard/` package
(released Tornado-based dashboard) and `esphome_device_builder/`
in this repo (the WS rewrite).
Tornado→aiohttp / REST→WS / hand-edited HTML→SPA differences are
intentional and skipped. What follows is the residue: behaviour the
legacy code accreted that the rewrite has yet to fully reabsorb.

## Executive summary

- The new backend has a richer, cleaner state model (mDNS/MQTT/ping
  source priority, per-name index, build-info config-hash plumbing,
  rename via firmware queue) — most state-management improvements are
  legitimate gains over the legacy dashboard.
- A few behaviours from the legacy code didn't make the jump and are
  visible to users today: `delete` doesn't wipe the per-device build
  directory, the WebSocket has no server-side heartbeat or trusted-
  domain origin allowlist, and there's no equivalent to the legacy
  `streamer_mode` toggle — `devices/validate` unconditionally
  redacts `!secret` values to `<removed>` with no opt-in to reveal
  them.
- mDNS coverage in the new dashboard is broader (HTTP service browser,
  TXT-driven config_hash and api_encryption signals) but the legacy
  dashboard's `no_mdns`/HTTP-only-poll path for `web_server`-only
  devices isn't ported — those devices will get pinged but never
  resolve via mDNS even when reachable.
- Firmware/queue handling (process-group cancel, output trim, history
  retention, rename lock, supersede) is a clear improvement; the
  legacy spawn protocol's Windows fall-back via Popen is the only
  porting question — the new dashboard stops pretty bluntly on
  Windows-only edge cases.
- Auth/session handling is far more thorough than legacy
  cookie+Basic — the new backend ships a session store, rate
  limiter, atomic JSON persistence, and Bearer/Basic on the public
  site. The surviving gap: legacy supports HA Supervisor password
  authentication via a `/auth` POST (still used by some HA add-on
  flows for the password-gated public port); the new backend's
  HA add-on path is ingress-only and has no Supervisor-backed
  fallback for the public-port + password combination. (See
  issue #85 — likely a deliberate decline.)

---

## 1. mDNS / device discovery / zeroconf

- **Legacy honours `no_mdns` and uses a polling-on-resolve fallback
  for non-API devices.** `status/mdns.py:96-122` only registers
  `DashboardStatus`-driven ENTRY_STATE pushes for devices with `api`
  loaded; configs whose `loaded_integrations` lack `api` (web_server-
  only, Tasmota-imported) are pushed through `async_resolve_host`
  every refresh so the indicator still flips green even though they
  never advertise on `_esphomelib._tcp.local.`. **New backend
  partial:** `_device_state_monitor.py` only listens on the esphomelib
  service type; non-esphomelib devices fall through to the ICMP ping
  loop only, and there's no equivalent of `entry.no_mdns` to
  short-circuit unreachable devices on networks where mDNS is broken
  (which was a real source of flapping for legacy users).
- **Legacy pre-resolves DNS in the ping sweep at `GROUP_SIZE = 24`
  with `return_exceptions=True` and surfaces `DNS_FAILURE` as its own
  state.** `status/ping.py:80-95`. The new backend collapses that into
  a generic OFFLINE in `_device_state_monitor.py:898-901`. **Accepted
  drop:** the new backend's three-state model (UNKNOWN/ONLINE/OFFLINE)
  is intentionally simpler and the diagnostic value of DNS_FAILURE
  doesn't justify a fourth state. The DNS-failure cache log line
  (`_device_state_monitor.py:_dns_cache.failed_hosts_log`) still
  surfaces typos and `.local` issues for users who go looking.
- **Legacy gates mDNS bootstrap with `MDNS_BOOTSTRAP_TIME = 7.5`
  before the first ping sweep** (`core.py:27`, `core.py:144-148`) so a
  full-fleet startup doesn't ping devices that mDNS is about to flip
  green for free. New backend matches this with
  `_PING_BOOTSTRAP_DELAY = 10` (`_device_state_monitor.py:55-56`).
  **Covered.**
- **Legacy normalizes hostnames identically across paths via
  `esphome.address_cache.normalize_hostname`** (`status/mdns.py:9`).
  New backend reimplements `normalize_hostname` /
  `is_local_hostname` in `helpers/hostname.py`. Functionally
  equivalent — but anything ESPHome upstream changes about the
  normalisation rules (trailing-dot, IDN) won't track unless the
  helper is updated. **Minor.**
- **Race where rename's UNKNOWN reset keys on `old_name` not
  path.** Legacy `EsphomeRenameHandler._proc_on_exit`
  (`web_server.py:466-469`) calls `entries.get(self.old_name)` with
  the old *configuration filename* but `entries.get` is path-keyed —
  a latent legacy bug. New backend handles this through
  `_on_firmware_job_completed → _scanner.scan()` for RENAME jobs
  (`controllers/devices.py:1146-1153`). **New backend wins.**

## 2. OTA / firmware flashing

- **Legacy spawn protocol supports Windows via `subprocess.Popen` +
  reading thread.** `web_server.py:200-316`. New backend's
  `firmware.py` and `_stream_subprocess` rely on
  `asyncio.create_subprocess_exec` everywhere with a dedicated
  taskkill path (`_terminate_subtree_windows`, `firmware.py:235-278`)
  for cancel; **the streaming-read path does not have a Windows
  pipe-buffering mitigation** equivalent to the legacy
  reading-thread+queue. ESPHome / PlatformIO output on Windows
  buffers in 4 KB chunks unless explicitly flushed, so live progress
  may stutter. **Partial.**
- **Legacy address-cache argument builder is identical in shape**
  (`web_server.py:340-404` vs `controllers/devices.py:1465-1501`).
  Both pass `--mdns-address-cache` / `--dns-address-cache` before the
  subcommand. New backend additionally falls back to `device.ip`
  when zeroconf cache misses — a small improvement. **Covered+.**
- **Legacy `EsphomePortCommandWebSocket` only attaches cache args
  when `port == "OTA"` AND `api` is in `loaded_integrations`.**
  `web_server.py:421-429`. New backend matches this in
  `firmware.py:1200-1206` and `controllers/devices.py:154-169`.
  **Covered.**
- **Legacy's `EsphomeUpdateAllHandler` shells out to
  `update-all`** (`web_server.py:533-535`). New backend has no
  equivalent — no batch update-all command. The frontend's "update
  every device" button has to fan out N firmware/install jobs by
  hand. Not a behaviour bug, but it serializes through the queue
  while `update-all` was self-batching. **Missed (minor).**
- **Legacy `EsphomeCleanAllHandler` accepts `clean_build_dir`
  flag** (`web_server.py:509-514`) so the user can wipe just
  PlatformIO caches without nuking per-device builds. New backend has
  `firmware/reset_build_env` (`firmware.py:1031-1084`) which is more
  thorough but offers no in-between mode. **Refactored.**
- **Legacy uses `--show-secrets` only when `streamer_mode` is
  off** (`web_server.py:498-499`). New `devices/validate`
  unconditionally streams `esphome --dashboard config <path>` with
  no `--show-secrets`, which means secrets are *always* redacted
  with the trailing `<removed>`. This is the safer default but it
  also means the `streamer_mode=off` reveal-on-demand mode is gone
  — a regression for solo users who want to see resolved secrets in
  their own validate output. **Partial.**

## 3. File watching / scanning / StorageJSON

- **Legacy scanner stats `ext_storage_path(file.name)` first,
  falling back to the YAML.** `entries.py:281-298`. New backend
  `_device_scanner.py:247-256` keys cache solely on the YAML stat —
  if a `--only-generate` run rewrites StorageJSON without touching
  the YAML, the scanner won't notice. The new backend masks this
  via `_scanner.reload(configuration)` from
  `_persist_expected_config_hash → _refresh_after_firmware_job`, but
  any *external* StorageJSON change (an out-of-band `esphome compile
  --only-generate`, a backend restart) won't trigger a scan diff.
  **Partial.**
- **Legacy `async_schedule_storage_json_update` runs
  `compile --only-generate` after every YAML save**
  (`entries.py:301-308`, `web_server.py:1316-1318`). New backend has
  `_schedule_storage_regenerate` on `update_config` and on first-
  sight `ADDED` scans (`controllers/devices.py:499-582`); also runs
  on `expected_config_hash` empty + `loaded_integrations` populated
  (`controllers/devices.py:932-946`). **Covered+.**
- **Legacy `MainRequestHandler.MoveTrash → "archive"` rename on
  startup** (`web_server.py:1618-1622`). The legacy backend renamed
  the historical `trash/` directory to `archive/` once on startup
  for users coming from an older release. New backend just `unlink`s
  candidates from `.trash/` and `.archive/` on delete
  (`controllers/devices.py:1399-1401`) but doesn't migrate the
  directory. Migration window is a year-and-a-half old at this
  point — probably moot, but flag it if any HA users still have
  `trash/`. **Likely intentional drop — call it out.**
- **Legacy `ArchiveRequestHandler.post` deletes the device's build
  folder on archive.** `web_server.py:1322-1336` —
  `shutil.rmtree(storage_json.build_path, ignore_errors=True)`. New
  backend's `_delete_all` (`controllers/devices.py:1398-1411`)
  removes the YAML, ext_storage, and metadata, but **NOT** the
  per-device `<config_dir>/.esphome/build/<name>/` tree. Repeatedly
  creating + deleting devices with the same name will leave orphan
  PlatformIO state that the next compile picks up. **Missed —
  high-impact disk leak.**
- **Cache key is `(inode, dev, mtime, size)` in both** —
  legacy `entries.py:283-298`, new `_device_scanner.py:247-256`.
  **Covered.**
- **Legacy entries are read-only across threads.**
  `entries.py:312-314` "This class is thread-safe and read-only." New
  backend's `Device` model is mutated in place from multiple
  callbacks (`_on_state_change`, `_on_ip_change`, etc.) — fine for
  a single-loop async system but means a future executor-pool
  consumer would see torn writes. **Stylistic; flag if multi-threaded
  consumers ever appear.**
- **YAML name vs filename:** legacy `entry.name`
  (`entries.py:410-414`) prefers `storage.name`, falls back to
  filename. New backend `load_device_from_storage`
  (`helpers/device_yaml.py:319-475`) prefers parsed YAML, then
  StorageJSON, then filename — and *validates* the YAML name against
  `[a-z0-9-]+` to reject a leaked friendly_name / package id from
  becoming the catalog key. **New backend wins.**

## 4. Authentication / sessions / CSRF / rate limiting

- **Legacy authenticates against HA Supervisor.**
  `web_server.py:1364-1392` — `LoginHandler.post_ha_addon_login`
  POSTs `username/password` to `http://supervisor/auth` with
  `X-Supervisor-Token`. New backend's `auth.py` has no Supervisor
  flow; the HA add-on path runs trusted-only via the ingress site
  in `device_builder.py:365-378`. If a user opts into the
  password-gated public site as an HA add-on, the legacy supervisor
  hand-off is gone. **Mostly intentional — confirm with the HA add-
  on's expected wiring; flag if `ha_addon` + non-ingress is meant
  to keep working.**
- **Legacy CSRF via Tornado XSRF cookie** (`web_server.py:1148`,
  `web_server.py:1563`). New backend doesn't have a parallel
  protection because the WS auth is in-band and REST is restricted to
  Bearer/Basic with no cookies — the cross-origin attack vector
  Tornado was protecting against is gone. **Intentional drop.**
- **Origin allowlist via `ESPHOME_TRUSTED_DOMAINS`.**
  `web_server.py:170-183`. New backend rejects cross-origin connect
  attempts unconditionally on `using_password` (`api/ws.py:191-194`)
  — there is no env var for "I've put the dashboard behind a reverse
  proxy on a different host". A user behind nginx-proxy-manager who
  sets `using_password` will be forced to disable auth, terminate at
  the proxy, or lose dashboard access entirely. **Missed.**
- **Rate limiter, sliding-window session store, atomic JSON
  persistence:** all *new* — legacy has none of these. **New backend
  wins clearly.**

## 5. Logging / event streams / WS push

- **Legacy sets `set_nodelay(True)` on every WS open**
  (`web_server.py:209`, `web_server.py:634`) to keep subprocess
  output flushing in real-time without the 200-500ms TCP nagling
  delay. **aiohttp's `WebSocketResponse` exposes the equivalent
  through `request.transport.set_write_buffer_limits` or socket
  options, but the new dashboard does not configure either.** Live
  log/compile output may sit in the kernel buffer for a few hundred
  ms longer than necessary. **Missed.**
- **Legacy sets `websocket_ping_interval=30.0`** (`web_server.py:
  1561`). aiohttp's WSResponse default heartbeat is `None` — the new
  backend's `WebSocketResponse()` constructor in `api/ws.py:196` does
  not set `heartbeat=`. Long-idle connections behind NAT/load
  balancers may silently drop without either side noticing.
  **Missed.**
- **Legacy `_safe_send_message` swallows
  `WebSocketClosedError`** (`web_server.py:736-739`). New backend
  swallows `ConnectionResetError` only (`api/ws.py:80-83`). aiohttp
  has its own `ConnectionResetError` raised by `send_json` on a
  closed socket — but `WSServerHandshakeError` /
  `ClientConnectionError` from the underlying transport can still
  surface during teardown. Probably fine, but worth a probe.
  **Minor.**
- **Logs / validate streams** — legacy spawn protocol used `\n`
  *or* `\r` regex split via Tornado's `read_until_regex`; new
  backend's `firmware.py:898-924` does the same byte-wise scan in
  Python, which preserves esptool's `\r`-based progress lines. New
  backend's `_stream_subprocess` (`controllers/devices.py:1413-
  1462`) splits on `\n` only — non-firmware logs will not show
  esptool-style progress overwrites. **Inconsistent within the new
  dashboard; firmware path is right, logs path matches legacy
  upstream behaviour but loses the niceness firmware path adds.**

## 6. Configuration validation / wizard / device adoption

- **Wizard** — legacy has `WizardRequestHandler`
  (`web_server.py:795-879`) supporting `type=basic|upload|empty` with
  per-type validation, file_content base64 decode, `secrets.token_*`
  generation, and 422 / 409 / 500 status codes for each failure
  mode. New backend's `devices/create` (`controllers/devices.py:197-
  303`) handles `file_content` and board-templated configs, with
  encryption keys generated via `device_yaml.generate_device_yaml`
  (`helpers/device_yaml.py:108`). The legacy `type=upload` path
  (paste a complete YAML) is supported via `file_content`; the
  legacy `type=empty` is supported by passing neither
  `file_content` nor `board_id`. **Covered+.**
- **Adoption / package_import_url** — legacy `ImportRequestHandler`
  (`web_server.py:882-934`) calls `import_config(...)` with
  `network=imported_device.network` falling back to
  `const.CONF_WIFI` (the *factory firmware's* network type). New
  backend's `devices/import` (`controllers/devices.py:642-729`)
  *always* passes `const.CONF_WIFI` — if a user adopts a
  factory-imported Ethernet device, they'll get a Wi-Fi-templated
  config and have to fix it themselves. **Missed bug.**
- **`mDNS-name vs YAML-name` mismatch on adopt** — legacy didn't
  handle this gracefully (it keyed on the YAML name, which is also
  the picked filename). New backend's `devices/import` walks the
  importable cache by `package_import_url` to find the *original*
  mDNS-broadcast name, applies state under the new YAML name but
  probes via the original name (`controllers/devices.py:691-728`).
  **New backend wins.**
- **Wizard legacy returns 409 on collision** with content-type set
  (`web_server.py:856-863`). New backend raises `FileExistsError`
  which the WS dispatcher routes to a generic
  `INTERNAL_ERROR`. `devices/import` raises
  `CommandError(INVALID_ARGS)` (`controllers/devices.py:669-675`)
  but `devices/create` only `raise FileExistsError(msg)`
  (`controllers/devices.py:231-232`) which the WS layer catches as
  generic `INTERNAL_ERROR`. **Inconsistent — fix the create path
  to raise `CommandError(INVALID_ARGS)` like import.**

## 7. MQTT / API connections

- **MQTT discovery** — legacy uses a single thread with one shared
  client driven by `ESPHOME_DASHBOARD_USE_MQTT` env var
  (`status/mqtt.py:17-78`), `mqtt.config_from_env()`. New backend has
  `DeviceMqttCoordinator` (`_device_mqtt_coordinator.py`) which
  parses each device's YAML, resolves `!secret`, groups by broker,
  and runs one `DeviceMqttMonitor` per unique broker. **New backend
  wins clearly** — supports per-device MQTT brokers, no env var
  contortions.
- **Legacy MQTT subscribes to a fixed topic** (`esphome/discover/#`,
  `status/mqtt.py:32`). New backend retains the same convention via
  `DeviceMqttMonitor`. **Covered.**
- **API encryption upgrade flow** — legacy doesn't track API
  encryption status at all. New backend reads the mDNS
  `api_encryption` TXT and surfaces it via
  `apply_api_encryption` (`_device_state_monitor.py:331-351`).
  **New backend wins.**

## 8. Error handling / startup / shutdown

- **Legacy installs a custom event-loop policy** with
  PidfdChildWatcher / ThreadedChildWatcher fallback
  (`dashboard.py:42-88`). Required for sane subprocess reaping on
  Python 3.12 before the upstream defaults caught up. New backend
  relies on the default policy — **fine on 3.12+ but the
  configurable executor max_workers and the `loop.time = monotonic`
  micro-optimisation aren't ported.** The new backend does
  provision its own `ThreadPoolExecutor` (`device_builder.py:77-
  79`, max 64). **Refactored — confirm no perf regression on a
  large fleet.**
- **Legacy `start_dashboard` resolves
  `EsphomeStorageJSON.cookie_secret`** (`dashboard.py:115-127`) so
  XSRF cookies survive restarts. New backend has its own `Session`
  store (`helpers/auth.py:68-188`) — equivalent purpose, cleaner
  shape. **Refactored.**
- **Legacy unix-socket binding via `bind_unix_socket(socket,
  mode=0o666)`** (`web_server.py:1639-1641`). New backend only
  binds TCP. HA add-on uses TCP+ingress-port now, so this is moot,
  but `--socket` accepters in front of the HA stack will break.
  **Likely intentional drop.**
- **Legacy `--open-ui` flag** (`dashboard.py:143-146`). New backend
  has no equivalent — minor DX regression. **Missed (cosmetic).**
- **Background task tracking**: both maintain a strong-ref set
  (`core.py:175-178` vs `device_builder.py:295-301`). Both cancel
  on stop and `gather(return_exceptions=True)`. **Covered.**

## 9. Concurrency primitives

- **Legacy uses `asyncio.run_coroutine_threadsafe(..., self._loop).
  result()`** to bridge thread-safe mutation back to the event loop
  in `entries.set_state` / `set_state_if_*`
  (`entries.py:124-181`). Required because MQTT runs on a thread.
  New backend's MQTT monitor uses `aiomqtt` natively on the event
  loop (`_device_mqtt_coordinator.py`, `_device_mqtt_monitor.py`),
  so the thread-safety dance is gone. **Refactored.**
- **`MAX_EXECUTOR_WORKERS = 48`** vs new
  `_EXECUTOR_MAX_WORKERS = 64`. Legacy chunked ping/DNS at
  `GROUP_SIZE = MAX_EXECUTOR_WORKERS / 2 = 24`; new backend matches
  with `_PING_BATCH_SIZE = 24` (`_device_state_monitor.py:64`).
  **Covered.**
- **Cancellation discipline** — new backend's `_stream_subprocess`
  re-raises CancelledError when the task is still cancelling
  (`controllers/devices.py:1448-1450`), which is the modern Python
  3.11+ contract. Legacy doesn't bother (Tornado masked it).
  **New backend wins.**
- **Atomic metadata file writes** — new backend uses
  `tempfile.mkstemp + os.replace` under a `threading.Lock`
  (`controllers/config.py:139-183`). Legacy doesn't have an
  equivalent because it never accumulated multi-key state in a
  single sidecar. **New backend wins.**

## 10. CLI / settings / paths / Home Assistant integration

- **Sentinel-file workaround for `CORE.config_path.parent`**
  (`settings.py:55-60`, with the `_DASHBOARD_SENTINEL_FILE`
  comment). New backend reproduces the workaround verbatim
  (`controllers/config.py:36`, `controllers/config.py:96`).
  **Covered.**
- **Legacy `streamer_mode` env var** (`settings.py:80-82`) hides
  serial port descriptions and forces `--show-secrets` off. New
  backend has no equivalent. **Missed.**
- **Legacy `relative_url` via `ESPHOME_DASHBOARD_RELATIVE_URL`**
  (`settings.py:62-64`, used by every Tornado route prefix
  `web_server.py:1565-1604`). New backend mounts everything at root.
  When deployed behind a reverse proxy that wants `/esphome/`-prefix
  routing without rewriting, this won't work. **Missed for
  reverse-proxy users.**
- **Legacy ignores `args.address=None` to fall through to socket
  bind**; new backend's `--host` defaults are simpler. **Refactored.**
- **HA add-on detection via `--ha-addon`** is parallel between
  legacy (`settings.py:45`) and new (`controllers/config.py:70`).
  New backend additionally toggles `create_ingress_site`
  via `DISABLE_HA_AUTHENTICATION`. **Covered+.**

---

## Biggest gaps, ranked by user-visible impact

1. **`devices/delete` doesn't wipe `<config_dir>/.esphome/build/<name>/`.**
   Legacy `web_server.py:1322-1336` does. Repeated create-delete cycles
   leak hundreds of MB. **Fix in `controllers/devices.py:1398-1411`.**
2. **`devices/import` always passes `const.CONF_WIFI`,** ignoring the
   adopted device's actual `network` field (`controllers/devices.py:660-668`).
   Ethernet devices get Wi-Fi templates. Legacy
   `web_server.py:903-908` reads `imported_device.network` correctly.
3. **No WebSocket heartbeat / `ws.heartbeat=` on `WebSocketResponse`.**
   Long-idle connections behind NAT / Cloudflare / nginx can drop
   without notice. Legacy sets `websocket_ping_interval=30.0`
   (`web_server.py:1561`). Set `heartbeat=30.0` in `api/ws.py:196`.
4. **No `ESPHOME_TRUSTED_DOMAINS` equivalent.** Reverse-proxy users
   on a different host with `using_password=True` get a hard 403 on
   `/ws` (`api/ws.py:191-194`). Legacy supports this via
   `web_server.py:170-183`.
5. **`devices/create` raises bare `FileExistsError`** which becomes
   a generic `INTERNAL_ERROR` (`controllers/devices.py:231-232`).
   Mirror the `devices/import` path which raises
   `CommandError(INVALID_ARGS)` so the frontend can show "name
   already exists".
6. **`devices/validate` has no `--show-secrets` toggle.** Legacy's
   `streamer_mode` off (`web_server.py:498-499`) lets solo users see
   resolved secrets. Add a per-call flag or honour a settings
   toggle.
7. **`devices/logs` stream splits on `\n` only.** esptool-style
   progress lines won't render mid-stream the way they do for
   firmware jobs. Use the same byte-buffered `\n`/`\r` split as
   `firmware.py:898-924`.
8. **Non-API mDNS devices never resolve.** Legacy polls
   `_esphomelib._tcp.local.` for them (`status/mdns.py:101-122`);
   new backend's monitor only listens. Consider adding the same
   per-refresh `async_resolve_host` poll for devices that
   `loaded_integrations` lacks `api`.
9. **No HA Supervisor `/auth` POST flow** for the password-gated
    public-site path on HA add-ons. Most users won't hit it
    (ingress is the primary path), but a hardened add-on
    install with `using_password=True` will fail to authenticate
    against Supervisor credentials.

Lower-impact / cosmetic: `--open-ui`, `--socket` unix-socket bind,
trash→archive migration, and `update-all` batch command — all
gone with no parallel; flag for dashboards.md if anyone asks.
