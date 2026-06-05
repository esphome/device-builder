# Notes for Claude

A short orientation file for an LLM working in this repo. Skim before
making changes; keep edits consistent with what's described here. Read
[README.md](README.md) for the user-facing intro and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the deep dive.

## What this project is

Backend for the new ESPHome Device Builder dashboard — a WebSocket API
server that replaces the legacy `esphome dashboard`. Single multiplexed
`/ws` endpoint, persistent firmware-job queue, mDNS + ping device
discovery, schema-driven component catalog, curated board catalog.
Frontend is a separate repo (`esphome/device-builder-frontend`) and
ships prebuilt inside our wheel.

**Issues filed here are often frontend bugs.** Users file every
dashboard bug against this backend repo, but the Visual Editor,
component-catalog rendering, and YAML-form UI all live in the
`esphome/device-builder-frontend` SPA. When triaging, check whether the
symptom is UI / editor behaviour before assuming a backend fix, and
trace both sides: the backend can parse and emit correctly while the
frontend's rendering or value-sync drops the data. A frontend fix is a
PR against `esphome/device-builder-frontend` (base `main`) that links
`fixes <this repo's issue URL>` cross-repo. Example: issue #1005 (dotted
`logger.logs` map keys lost in the Visual Editor) was a frontend
`data-field-key` round-trip bug, not a backend one.

Base functions are in late beta; remote / offload functions are in early
beta. Expect undocumented breaking changes until stable. Targeted to
land as an opt-in preview toggle in the official ESPHome container and
Home Assistant add-on.

## Code style

- **Docstrings: terse, default to single-line.** A docstring is the
  function's *contract*, not its narrative. Almost every docstring
  should be one line — `"""Summary."""`. Multi-line is the exception,
  justified only by genuinely non-obvious caller-visible behaviour the
  signature and parameter names don't convey. When multi-line is needed,
  put content on the line after `"""`:

  ```python
  def merge_component_yaml(...) -> str:
      """
      Render *component* and merge it into *existing* YAML.

      Platform-style components append under the existing ``<domain>:``.
      """
  ```

  Default target for new code: three lines between the `"""` markers.
  Longer is acceptable when the contract genuinely needs it (non-obvious
  priority orders, security-relevant fallbacks, an empirical anchor like
  an issue number proving a value matters, load-bearing ordering).

  **Never put in docstrings or comments:** rationale / motivation /
  "why we used to do X" (that's the PR/commit); issue-number cross-refs
  ("closes #N"); prose restatement of the function body; test docstrings
  retelling the production story (name what the test pins, in one
  sentence); "same shape as X / mirrors Y" framing.

- **Comments**: same bar. Default to none. Add one only when the *why*
  is non-obvious: a hidden constraint, subtle invariant, bug workaround,
  surprising behaviour. **Don't remove existing comments** unless the
  code they describe is gone.

- **Don't pad commits, docstrings, or comments with cross-references**
  to old codepaths or issue numbers unless a future reader needs the
  link.

- **Method order**: public API at the top, private helpers
  (`_underscore_prefixed`) at the bottom. Same for module-level
  functions in scripts.

- **Line length**: 100 (ruff). `target-version = "py312"`.

- **Imports**: ruff/isort sorted. `from __future__ import annotations`
  at the top of every module.

- **File size: 800-line soft cap.** When a Python module reaches ~800
  lines, plan a split before adding more. The cap exists because large
  modules degrade LLM context budget, slow human review, and accumulate
  invisible cross-concern coupling.

  The split pattern: replace `controllers/X.py` with a `controllers/X/`
  package containing `controller.py` (main class + public API) plus
  per-concern submodules (`controllers/X/foo.py`). `__init__.py`
  re-exports the controller class so existing
  `from .controllers.X import XController` callers keep working. See
  `controllers/devices/`, `controllers/firmware/`,
  `controllers/remote_build/` for the canonical shape.

  **State dataclass convention.** Group mutable *domain* state —
  anything a sibling module reads or writes — into a typed `XxxState`
  dataclass in `controllers/X/_state.py`; the controller owns
  `self.state: XxxState`; siblings reach through `controller.state.X`.
  Canonical: `OffloaderState`, `DevicesState`, `ReceiverState`.
  Controller-internal handles no sibling touches (`_unsub_job_completed`,
  `_pairings_store`, base infra like `_db` / `_listeners` / `_tasks`)
  stay on the controller even when reassigned across `start()` /
  `stop()`. The cut is cross-module data, not reassignment. Design with
  `XxxState` from PR 1 of a split — adding it later forces a cleanup arc
  (#795, #797). When `start()` / `stop()` repopulates a `state.X`
  dict/set something captured a bound method of (`.add`, `.__contains__`),
  use in-place `.clear()` + `.update(...)` rather than reassignment, or
  the captured method points at the original empty container forever
  (the `DevicesController.state.ignored_devices` loader hit this; #797
  added the regression test).

  Existing over-800 modules (worst was 5176 lines) are grandfathered.
  The split rule is scoped to invasive changes:

  * **Small bugfix for a patch release**: ship the fix, skip the split.
    The cap is soft; readability isn't worth a delayed patch.
  * **Invasive change** (new feature, significant refactor, structural
    touch): **split first** as a behavior-free refactor PR, then layer
    the change on top in a follow-up PR — each reviewable as one concern.

  A drive-by split inside an unrelated bugfix is scope creep.
  Opportunistic refactor PRs targeting the largest offenders are welcome
  as their own PRs. A PR touching a module *already over* 800 lines
  should not make it worse. New top-level modules start under the cap.

  **Exception: pytest conftests.** Conftest is the single import surface
  for shared fixtures/factories; it's intentionally a grab-bag, edits
  touch one fixture at a time, and splitting per-helper buys nothing. The
  800-line cap does not apply. When hoisting a duplicated helper, prefer
  the *narrowest* conftest covering every caller; only widen to
  `tests/conftest.py` when callers span the whole suite. Canonical
  subdirectory conftests: `tests/controllers/devices/conftest.py`,
  `tests/controllers/firmware/conftest.py`, `tests/e2e/conftest.py`.

## Commit / PR conventions

- **No `Co-Authored-By: Claude` trailer.** Project preference.
- Imperative-mood subject line ("Add X", not "Added X").
- Every PR needs **exactly one** label from: `breaking-change`,
  `new-feature`, `enhancement`, `bugfix`, `refactor`, `docs`,
  `maintenance`, `ci`, `dependencies`. CI enforces this via
  `.github/workflows/pr-labels.yaml`. Template is in
  `.github/PULL_REQUEST_TEMPLATE.md`; the `pr-workflow` skill walks
  through it — branch off `origin/main`, tick exactly one
  Types-of-changes box, pass the body via `--body-file` so the
  template's backticks aren't shell-escaped.
- Pre-commit runs ruff (lint + format), codespell, yaml/json/python
  checks. Failures auto-fix where possible, then re-stage.

## Workflow conventions

- All GitHub Actions are SHA-pinned with the version as a trailing
  comment (`uses: actions/checkout@<sha>  # v4`) so dependabot can bump
  while preserving traceability. Org policy.
- Release flow lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#ci--release-pipeline).
- **CI runs the test matrix on Windows too.** PowerShell is the default
  shell on `windows-latest` and **does not accept bash-style `\` line
  continuations** ("Missing expression after unary operator '--'"). Keep
  cross-platform CI commands on a single line, or use PowerShell-
  compatible backtick continuations.

## Empirical browser testing

When a change needs the live dashboard (UI behaviour, WS round-trip
shape, end-to-end flows), pair this backend with its companion frontend
dev server. Tests verify correctness; the browser verifies what the user
sees.

1. Backend on `:6052` in dev mode:
   `.venv/bin/python -m esphome_device_builder --dev configs`. `--dev`
   serves `index.html` with `Cache-Control: no-cache` so a rebuilt
   bundle isn't masked by a stale SPA shell.
2. Frontend dev server in a checkout of `esphome/device-builder-frontend`
   (default port 5173): `npm run dev`. It proxies `/ws` to
   `http://localhost:6052`, so the backend above answers. HMR is on. If
   5173 is taken, pass `PORT=5174 npm run dev` — configure the *frontend*
   port; the backend stays on 6052 and the proxy still works.

**Run both in the background** (`Bash` with `run_in_background: true`
under Claude Code). **Wait for both before telling the user the URL**:
poll `lsof -iTCP:6052 -sTCP:LISTEN` and
`lsof -iTCP:<frontend-port> -sTCP:LISTEN`; once both LISTEN, point at
`http://localhost:<frontend-port>/`. **Cleanup**:
`lsof -iTCP:6052 -iTCP:<frontend-port> -sTCP:LISTEN -t | xargs kill`.

**Alternative: production-shape testing.** `npm run build` in the
frontend checkout writes to the `esphome_device_builder_frontend`
site-packages dir the backend reads via `_get_frontend_dir`; no proxy,
browse to `http://localhost:6052/`. Use dev-server for fast iteration,
built path to verify the bundle the user actually receives.

## Comparison with the legacy esphome dashboard

The legacy Tornado dashboard (`esphome/dashboard/` upstream) is the
upstream-canonical reference for shared concerns (mDNS source dispatch,
build-info hashes, StorageJSON layout, `CORE` lifecycle, `address_cache`
semantics). Functional parity is essentially achieved as of 2026-05; the
one intentional decline is the HA Supervisor `/auth` POST flow (our HA
add-on path is ingress-only by design — issue #85, inline comment at
`device_builder.py:419`). Before declaring a new feature complete, check
the open issue list filtered to "legacy parity".

**Lessons about the comparison itself:** the legacy code is
upstream-canonical for shared concerns — when we diverge, that should be
an intentional choice flagged in code comments, not an oversight. The
new dashboard's *cleaner* shape can hide gaps the legacy mess was
solving (Wi-Fi vs Ethernet adoption, WS heartbeat, reverse-proxy origin
allowlist, per-device build-dir cleanup on delete); audit additions
against legacy behaviour before assuming the simpler version suffices.

## Architecture conventions worth knowing

- **WS-first API.** Real-time updates are the default — clients
  `subscribe_events` once and get pushes. REST is only kept for HA
  backward compat in `api/legacy.py`.
- **Stateful lists ship through subscribe_events, not a `list_*` WS
  command.** Any per-session list whose contents mutate over a connected
  client's lifetime (devices, importable devices, offloader pairings,
  receiver peers, …) must reach the frontend via the snapshot-plus-events
  pattern. The shape that *doesn't* belong on new code is list-then-poll
  ("client calls `list_X` on mount, re-calls after every mutation"),
  which has three recurring failure modes we've hit:
  * **Read-vs-write races** — a snapshot read concurrent with a write
    returns whichever side won the lock, then disagrees with the next
    event; frontend state ping-pongs until reload. `remote_build/list_peers`
    had this (#514).
  * **Cross-tab desync** — a second tab's mutation never reaches the
    first unless it re-polls.
  * **Round-trip overhead** — every mutation pays a follow-up list-fetch
    the events were already going to deliver.

  Do this instead:
  1. Hold the list as a **RAM-canonical dict** on the controller, keyed
     on what the wire commands key on (`dashboard_id` for receiver peers,
     `(hostname, port)` for offloader pairings, the YAML filename stem
     for devices). Mutations update RAM immediately and schedule a
     debounced disk write through a per-file `helpers.storage.Store`
     (mirror `RemoteBuildController._approved_peers` / `_peers_store`).
     Projections and post-mutation responses read straight off the dict.
     RAM loads from the Store at `controller.start()`; disk is just
     persistence.
  2. Seed the **first paint** through `subscribe_events`'s
     `initial_state` push — the `_send_initial` inner helper in
     `DeviceBuilder._cmd_subscribe_events`, passed as `send_initial=` to
     `helpers.event_bus.stream_events`. Add a sync `*_snapshot()` method
     on the controller (e.g. `pairings_snapshot()`, `peers_snapshot()`)
     and stitch `initial["<key>"] = [s.to_dict() for s in
     controller.<key>_snapshot()]`. Snapshot reads MUST be sync — the
     subscribe handler runs in the WS dispatch hot path; an executor hop
     on every connect slows cold-load on large fleets.
  3. Fire **per-mutation bus events** with TypedDict payloads carrying
     every field a subscriber needs to build the row from the event
     alone — no follow-up snapshot read (see
     `RemoteBuildPairRequestReceivedData`'s `paired_at`, #514). Frontend
     mutates its local list from the event.
  4. Emit one event per state transition. `subscribe_events` attaches the
     bus listener *before* awaiting `_send_initial`, so events fired
     during the snapshot await buffer behind initial_state and arrive in
     order; subscribers can rely on "initial_state first, then live
     updates".

  Don't add `list_*` commands for new state surfaces. Existing carve-outs:
  `remote_build/list_hosts` (transient mDNS browse, not stateful) and
  `devices/list_archived` (cold archive listing, read-once). `labels/list`
  is the snapshot-fetch-then-events holdover — new code lands through
  `initial_state`, not by copying it.
- **Event payloads use TypedDict, not dataclass.** Mirrors HA core's
  `Event[_DataT]` / `EventStateChangedData` pattern. Each event-specific
  shape gets a `TypedDict` next to the controller that fires it (e.g.
  `RemoteBuildPairRequestReceivedData` in `models/remote_build.py`,
  `JobLifecycleData` / `JobOutputData` / `JobProgressData` in
  `models/firmware.py`, `DeviceEventData` / `DeviceStateChangedData` /
  `DeviceReachabilityData` in `models/devices.py`). Fire with TypedDict-
  call syntax so mypy validates: `bus.fire(EventType.X,
  SomeEventData(field=value))`. `Event` and `EventBus.fire` are generic
  on `DataT`, so a typed payload flows without `cast()`. Subscribers
  narrow at the callback signature: `def _on_x(event:
  Event[SomeEventData])`. `add_listener` is intentionally non-generic
  (type-erased bucket, `Any` bridges variance) — it enforces the correct
  pairing but doesn't reject the wrong one; mismatches live in review.
  `tests/test_event_payload_contracts.py` pins each TypedDict against its
  emitter and walks `models.*` to assert coverage. New events ship with a
  TypedDict from day one and a row in `_PAYLOAD_FACTORIES`. Full
  rationale in `docs/ARCHITECTURE.md` "Event bus → Typing event payloads".
- **Persistent firmware queue — two concurrent lanes.** A CPU/compile lane
  and a network/upload lane each run one job at a time, but run
  concurrently, so a slow upload doesn't block the next compile (#3702).
  `FirmwareState.compile_lane` / `upload_lane` (`controllers/firmware/
  _state.py`); `lane_for(job)` routes UPLOAD to the upload lane, everything
  else to compile. `firmware/install` enqueues a COMPILE job + a dependent
  local UPLOAD job (`FirmwareJob.depends_on`); the upload is held off its
  lane until the compile succeeds (`lifecycle.release_dependents`), and a
  cancelled/failed compile cascades to cancel the held upload (so a
  cancelled build never flashes). Remote install uses the same chain — a
  remote compile materialises artifacts locally, then the local upload lane
  flashes. Queue + output buffers survive restarts. See
  `controllers/firmware/`.
- **Component catalog is generated**, not hand-edited. Source is
  ESPHome's pre-built schema bundle (https://schema.esphome.io) plus
  narrow live `esphome` introspection for what the schema doesn't carry
  (`multi_conf`, `platform_defaults`, `supported_platforms`, type
  refinement, `unit_of_measurement` options). Component descriptions/
  titles fall back to the docs MDX repo when the schema index is sparse.
  All in `script/sync_components.py`.
- **Catalog id format**: `<domain>.<stem>` (e.g. `sensor.dht`). The
  schema's natural format is the reverse — `<stem>.<domain>`;
  `_split_qualified_key` flips it.
- **Board catalog** (`definitions/boards/<id>/manifest.yaml`) is
  hand-curated YAML. ~80 popular boards plus generic fallbacks per
  platform. `script/validate_definitions.py` lints the manifests.
- **Frontend handoff** for the catalog is documented inline in models
  (`ConfigEntry`, `ComponentCatalogEntry`). New `ConfigEntryType` values
  need a frontend update — coordinate.
- **Deployment modes change the on-disk paths — never hardcode them.**
  Three shapes ship, and `CORE.data_dir` resolves differently in each.
  Every storage / build-info / firmware-binary read MUST go through
  `ext_storage_path` (or `CORE.data_dir`) rather than reconstructing
  `<config_dir>/.esphome/...`, or the read silently misses the file in
  the addon (user sees empty-Local-hash + Pending-install on every
  device):

  | Mode | `CORE.data_dir` | StorageJSON | Build tree |
  |---|---|---|---|
  | Default (pip install, dev checkout) | `<config_dir>/.esphome` | `<config_dir>/.esphome/storage/<file>.json` | `<config_dir>/.esphome/build/<name>/` |
  | HA addon (`is_ha_addon()` true) | `/data` | `/data/storage/<file>.json` | `/data/build/<name>/` |
  | `ESPHOME_DATA_DIR` env override | `$ESPHOME_DATA_DIR` | `$ESPHOME_DATA_DIR/storage/<file>.json` | `$ESPHOME_DATA_DIR/build/<name>/` |

  The HA-addon shape is dominant in production: YAML configs at
  `/config/esphome/` (HA's `/config` mount), every ESPHome artefact at
  `/data/` (the addon's per-instance volume). The split lets the addon
  wipe `/data/build/` for upgrades without touching user YAML, and gives
  two addon instances independent data dirs while sharing the config
  tree. `CORE.config_path` is set to a sentinel YAML inside `config_dir`
  on startup (`controllers/config/settings.py:_DASHBOARD_SENTINEL_FILE`); helpers
  wanting the storage layout MUST resolve through that initialised CORE,
  not reconstruct from a `Path` argument. The audit covers
  `controllers/firmware/`, `controllers/devices/`, `helpers/config_hash`,
  `helpers/build_size`, `helpers/device_yaml`; new callers mirror it.
  Tests need `CORE.config_path` set to a tmp-path sentinel — see autouse
  fixtures in `tests/controllers/devices/conftest.py` and
  `tests/test_config_hash.py`.

  Native Windows auto-applies the override row: `__main__` wraps startup
  in `helpers/windows_build_paths` to relocate `CORE.data_dir` to
  `C:\esphb\<id8>` (clears `MAX_PATH` + spaces), so don't assume
  default-mode `<config>/.esphome` paths on Windows.
- **`config_hash` source of truth is `build_info.json`.** ESPHome writes
  `<storage.build_path>/build_info.json` after every successful compile
  *and* every `--only-generate` (the `write_cpp(config)` call runs before
  the `args.only_generate` exit). Read it back via
  `helpers.config_hash.read_build_info_hash`; don't recompute (see
  "Things that have bitten us"). `_resolve_device_metadata` reads
  `build_info.json` first, falling back to the `.device-builder.json`
  sidecar only when the build dir is wiped.
- **`api_encryption` mDNS TXT is a tri-state, not a boolean.** Truthy
  (e.g. `Noise_NNpsk0_25519_ChaChaPoly_SHA256`) → encryption confirmed.
  Empty string → TXT seen, key absent → confirmed plaintext. `None` → no
  broadcast yet, unknown. Frontend's `getEncryptionState` and backend's
  `apply_api_encryption` both lean on the empty-string-means-plaintext
  distinction; a nullable boolean would lose it.
- **Optimistic post-flash sync** in
  `DevicesController._sync_deployed_hash_after_flash` pre-pins
  `deployed_config_hash = expected_config_hash` after a successful
  UPLOAD/INSTALL via `DeviceStateMonitor.apply_config_hash`, so the dot
  clears immediately instead of waiting on the rebooted device's mDNS
  announce. If the OTA silently failed, the next real announce pushes the
  truth back through the same callback.
- **Two mDNS paths with different OFFLINE semantics:**
  - **Browser callback** (`_on_service_state_change`) — passively
    subscribed to `_esphomelib._tcp.local.`. Trust mDNS **both
    directions**: `AsyncServiceBrowser` delivers a `Removed` event on
    TTL expiry, the canonical "device gone" signal. ONLINE → mdns,
    OFFLINE → mdns, no ICMP.
  - **One-off active resolve** (`_resolve_non_api_mdns_targets`, for
    non-API devices not on `_esphomelib._tcp.local.`). Trust mDNS for
    **ONLINE only** (priority 3, locks out ICMP — once mDNS answers,
    repeat-pinging is redundant noise). A miss is **deliberately
    silent**: a single active query that didn't reply conflates "gone",
    "slow", and "packet loss", and there's no subscription delivering
    TTL-expiry here. Wait for the ICMP sweep in the same loop to decide
    OFFLINE.

  Don't add an OFFLINE branch to the active-resolve path without
  re-reading this. The asymmetry is the only way to get aggressive ONLINE
  detection without flipping the indicator red on every quiet device.
- **The `Device` is the source of truth, not the monitor.**
  `DeviceStateMonitor.apply_*` (state, ip, version, config_hash,
  api_encryption) dedupe by comparing the broadcast against every
  matching `Device`'s current field — never a separate monitor-side
  cache. A cache drifts when the scanner rebuilds a `Device` with
  `previous=None` (atomic-save churn, REMOVED+re-ADDED) and then
  short-circuits the next legitimate broadcast, leaving empty fields
  forever. Lesson from PR #75; future apply-* methods follow this shape.
- **Scanner keeps a name-keyed index alongside the path-keyed one.**
  `DeviceScanner._devices_by_name: dict[str, list[Device]]` is maintained
  in lockstep with `_devices` via `_set_device` / `_pop_device` /
  `_unindex_name`. Buckets are sorted by `configuration` filename so
  `bucket[0]` consumers and the dedupe path see a deterministic "first
  match". `scanner.get_by_name(name)` returns a fresh list snapshot, so
  callers iterate without poisoning the index.
- **mDNS-source dedupe must look at every matching device, not just
  `bucket[0]`.** Two YAMLs sharing an `esphome.name` (a config plus a
  `foo (1).yaml` copy, `dashboard_import` siblings) share one mDNS
  broadcast. If `apply()` checks only the first match, a sibling rebuilt
  with state=UNKNOWN never catches up. `apply()` and
  `_any_matching_device_differs` use `all(...)` / `any(...)` over the
  whole bucket; per-device callbacks fan out the mutation.

## Design principles

- **Never generate invalid configs; fix the source, not the consumer.**
  When a downstream path hits an invalid YAML (`esphome config` exits
  non-zero, schema rejects, compile fails), fix the *generator* (wizard,
  `dashboard_import`, `clone`, `create_device`) so it always produces
  something the next step validates cleanly. Don't add a consumer
  fallback that "tries to make it work anyway." The rename path tried
  that — a file-level rewrite when `esphome config` failed — and silently
  desynced on-disk state from running firmware (YAML renamed, device kept
  its old hostname, no error). Same shape bit `edit_friendly_name` (PR
  #390 added pre-write validation); the rename fallback was deleted (PR
  #402, upstream companion esphome/esphome#16296). When tempted to add a
  defensive "what if input is broken" branch, audit *what generated this
  YAML* and harden the generator. If the generator legitimately can't
  guarantee validity (user hand-edits between create and rename), surface
  a typed `CommandError(INVALID_ARGS, …)` with the actual errors — refuse
  cleanly, no silent best-effort. Pair the consumer-side error with a
  generator-side test running output through `editor.validate_yaml`.

  **Important exception — user-supplied content is *not* a generator.**
  Don't apply this to YAMLs the user brings in via "Upload YAML",
  drag-and-drop, paste-into-editor, or any user-typed entry point — the
  point is to land an existing config *so the user can repair it in the
  editor*. The common case is a YAML from an older ESPHome version whose
  components changed schema (deprecated `esphome.platform` /
  `esphome.board`, renamed fields like `wifi.use_address`); refusing the
  write strands the user. Validate *our* outputs (`generate_device_yaml`,
  `generate_minimal_stub_yaml`, `dashboard_import.import_config`, clone's
  leaf rewrites) but pass user-supplied content through unchanged. PR
  #412 reverses #405's overzealous validation on `create_device`'s
  `file_content` branch and pins the legacy-config acceptance contract.
  The next compile / install surfaces real schema errors with line
  numbers — that's what the user wants when repairing an old config.

## Things that have bitten us before

When changing the sync script or catalog handling, watch for these:

- **Don't swap `sys.executable` for a sibling `python`.** It silently
  jumps to a different interpreter (e.g. system Python without `esphome`)
  → "No module named esphome" at compile time. `_find_esphome_cmd` uses
  `sys.executable` directly.
- **`extends:` references need a deep merge.** The schema uses partial
  overrides — `dht.sensor.humidity.config_vars.device_class` carries only
  `{"default": "humidity"}` and inherits the rest from `_SENSOR_SCHEMA`. A
  flat `{**extended, **local}` drops inherited enum values.
  `_convert_config_vars` does per-field deep merge.
- **`id_type` ≠ `use_id_type`.** `id_type` is the type of id this field
  *creates*; `use_id_type` marks a cross-reference (`i2c_id`,
  `output_id`). Don't pull `references_component` from `id_type` — that
  turned every `output.gpio.id` into a "select existing gpio" dropdown.
- **Schema's `type: "schema"` can be an extends-only wrapper.** If the
  inner has only `extends` and no `config_vars`, collapse to the
  underlying primitive (often a time_period). Don't blindly emit
  `type=nested`.
- **Custom validators lose type info upstream.** `api.encryption` emerges
  as `{key: Optional, docs: ...}` because ESPHome validates it with a
  custom function. Use `_FIELD_OVERRIDES`; keep the list small.
- **MDX field-description backfill is top-level only.** The `##
  Configuration variables` bullet list is flat; recursing into nested
  entries leaks descriptions across levels (e.g. `esphome.name` →
  `esphome.areas[].name`).
- **`CORE.config_hash` is post-codegen, not post-`read_config`.** Each
  component's `to_code` runs after validation and can mutate the config
  (id-pinning, default backfill, normalisation); the build reads
  `CORE.config_hash` from `writer.get_build_info` *after*
  `generate_cpp_contents`. A subprocess that loads YAML, calls
  `read_config`, and reads the property disagrees with the firmware's
  broadcast. Verified against `acfloatmonitor32.yaml`: pre-codegen
  `f3e21d5a`, post-codegen `5a94a12d` (the latter is baked in). Read
  `build_info.json` instead.
- **`compute_has_pending_changes` checks the hash before the mtime.**
  When both `expected_config_hash` and `deployed_config_hash` are known,
  the hash comparison is authoritative — equal hashes mean the firmware
  is built from the same logical config the YAML resolves to today, even
  when mtime is newer (whitespace edits, `--only-generate` rewrites,
  comments). The mtime check is the fallback for pre-#16145 firmware that
  doesn't broadcast a hash.
- **`ext_storage_path` requires `CORE.config_path` set.** It wraps
  `CORE.data_dir`, which crashes (`AttributeError: 'NoneType' ... is_dir`)
  when CORE isn't initialised. Fine in production; tests get it via the
  autouse `_core_config_path_in_tmp` fixture in `tests/conftest.py`. New
  helpers reading storage / build_info / firmware-bin paths MUST resolve
  through `ext_storage_path` (or `CORE.data_dir`) — never reconstruct
  `<yaml_dir>/.esphome/...` from a `Path`, even if it works locally. The
  default-mode shortcut is invisibly wrong on the HA addon and silently
  returns `None` for every device. See the deployment-modes table above.
- **Atomic-save editors (vscode-on-macOS et al.) can briefly remove the
  YAML mid-save.** The scanner sees it disappear, fires `REMOVED`, then
  re-`ADDED` next sweep with `previous=None` — so monitor-derived Device
  state (`deployed_config_hash`, `api_encryption_active`, `ip`,
  `deployed_version`, `state`) resets to default. Don't let dedupe layers
  cache values keyed only on device *name*; the rebuilt Device starts
  fresh and the cache masks the next legitimate broadcast. (See "The
  `Device` is the source of truth".)
- **Test callbacks that drive dedupe must mirror production's state
  mutation.** The monitor's apply-* methods short-circuit when every
  matching device's field already equals the broadcast — and in
  production the controller's `_on_*_change` callback writes that value
  back. A bare `MagicMock` doesn't, so a "second call short-circuits"
  assertion fails unless the mock has a `side_effect` flipping the
  device's field. See `_flip_state` / `_flip` in `tests/test_mdns_*.py`.
- **In-place file writes need `esphome.helpers.write_file`, not
  `Path.write_text`.** Both `Path.write_text` and `open(path, "w")`
  truncate *before* writing — a crash between truncate and flush leaves
  an empty or half-written file; for a YAML config that's unrecoverable.
  `write_file` stages bytes in a `NamedTemporaryFile` in the
  *destination* dir, then `shutil.move`s into place (atomic only when it
  resolves to a same-FS `os.rename`; staging in the destination dir keeps
  it same-FS). It also `fchmod`s to 0o644 and wraps `OSError` as
  `EsphomeError`. Use it for any in-place rewrite of user-editable YAML /
  settings (`edit_friendly_name` is canonical). Don't use
  `tempfile.mkstemp` without `dir=` — it lands on `/tmp`, a separate FS
  from `/config` in the addon, and the cross-FS move loses atomicity.

  *New* files differ — `clone_device` opens via `open(path, "x")`
  (exclusive-create, already atomic). Only in-place *edits* need
  `write_file`. Build artefacts (StorageJSON sidecars,
  `.device-builder.json`) recoverable on next compile / scan can stay on
  direct writes — the criterion is "would losing this lose user-authored
  content."
- **CodSpeed parametrize values need explicit `pytest.param(value,
  id="<short>")` IDs when the value isn't a short primitive.** pytest's
  auto-ID concatenates values verbatim, so a multi-KB YAML / bytes
  payload bakes into the test name; the benchmark passes and uploads, but
  the server-side "CodSpeed Performance Analysis" check fails ("Unable to
  generate the performance report") because the test-identifier length
  limit silently drops the run. Follow
  `tests/benchmarks/test_log_streaming.py` and
  `tests/benchmarks/test_peer_link_noise_xx.py`:
  `pytest.param(_NEWLINE_PAYLOAD, 1000, id="newline_1k")`,
  `pytest.param(1024, id="1KiB")`. Bare ints / short slugs are fine.

## Useful entry points

| Path | What |
|---|---|
| `esphome_device_builder/device_builder.py` | Singleton owning controllers + event bus |
| `esphome_device_builder/controllers/*.py` | One file per API surface. Larger surfaces (devices, firmware, remote_build) are packages — `controller.py` for the main class, per-concern submodules, `__init__.py` re-exporting the controller. |
| `esphome_device_builder/models/*.py` | Data classes (mashumaro) — pure shape, no logic |
| `esphome_device_builder/api/ws.py` | WebSocket dispatch |
| `esphome_device_builder/definitions/components.index.json` + `components/<id>.json` | Generated; do not hand-edit. Slim index loaded eagerly; per-id bodies hydrate lazily via `ComponentCatalog.get_body`. |
| `esphome_device_builder/definitions/boards.index.json` + `board_bodies/<id>.json` + `featured_components.index.json` | Generated; do not hand-edit. Slim board index + per-id lazy bodies (via `BoardCatalog._body_store`) + aggregated featured-components map (read once by the components controller's registry build). |
| `esphome_device_builder/definitions/boards/<id>/manifest.yaml` | Curated; hand-edited. The body directory is `board_bodies/` (separate from this manifests dir) so the body-swap rmtree can't trample the hand-curated source. |
| `script/sync_boards.py` | Regenerates the split board catalog from the manifests |
| `script/sync_components.py` | Regenerates the component catalog |
| `script/check_catalog.py` | Smoke test for popular components |
| `script/validate_definitions.py` | Lint board manifests |
| `docs/ARCHITECTURE.md` | Full architecture + deployment + CI overview |
| `docs/API.md` | Every WS command + payload shape + event |

## Things not to do

- **Don't hand-edit `components.index.json` / `components/<id>.json`.**
  Regenerate via `script/sync_components.py`. CI runs the sync nightly
  and opens a PR — that's the intended update path.
- **Don't auto-merge catalog PRs.** Schema regressions and sync-script
  bugs both surface as PR diffs; a human gate catches them. The diff
  summary in the PR body is designed for fast review.
- **Don't add `Co-Authored-By: Claude` to commits** in this repo.
- **Don't bump the `esphome` dependency casually.** Dependabot ignores it
  for a reason — bumping needs a coordinated catalog re-sync against the
  matching schema version. Do it deliberately at release time.
- **Don't reorder existing public methods** without a reason. The
  controllers' API surface is the de-facto public interface for the
  frontend.
