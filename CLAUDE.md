# Notes for Claude

A short orientation file for an LLM working in this repo. Skim before
making changes; keep edits to existing code consistent with what's
described here. Read [README.md](README.md) for the user-facing
intro and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the deep
dive.

## What this project is

Backend for the new ESPHome Device Builder dashboard — a WebSocket
API server that replaces the legacy `esphome dashboard`. Single
multiplexed `/ws` endpoint, persistent firmware-job queue, mDNS +
ping device discovery, schema-driven component catalog, curated
board catalog. Frontend is a separate repo
(`esphome/device-builder-dashboard-frontend`) and ships prebuilt
inside our wheel.

Base functions are in late beta; remote / offload functions are in
early beta. Expect undocumented breaking changes until the project
is marked stable. Targeted to land as an opt-in preview toggle in
the official ESPHome container and Home Assistant add-on.

## Code style

- **Docstrings: terse, default to single-line.** A docstring is
  the function's *contract*, not its narrative. Almost every
  docstring should be one line — `"""Summary."""` — describing
  what the function does and what the caller can pass. Multi-line
  is the exception, not the rule, and is only justified when
  there is genuinely non-obvious caller-visible behaviour that
  the type signature and parameter names don't already convey.

  When a multi-line docstring is needed, put the content on the
  line after `"""`:

  ```python
  def merge_component_yaml(...) -> str:
      """
      Render *component* and merge it into *existing* YAML.

      Platform-style components append under the existing ``<domain>:``.
      """
  ```

  **What does NOT belong in docstrings or comments:**

  * Rationale / motivation / "why we used to do X" — that's the
    PR description and the commit message. Git already remembers.
  * Cross-references to issue numbers ("closes #N", "follow-up
    to #M") — the PR body carries those.
  * Restatement of the function body in prose. If the next line
    of the docstring is just describing what the next line of
    code does, delete the docstring line.
  * Test docstrings retelling the production-side story. A test
    docstring should name what the test pins, in one sentence —
    not re-explain the bug, the fix, or the surrounding flow.
  * "Same shape as X / mirrors Y" framing. A future reader
    doesn't need to learn what *another* function does to read
    this one.

  Default target for new code: three lines between the `"""`
  markers (blank lines count). The example above is at that
  target. Longer is acceptable when the contract genuinely
  needs it — non-obvious priority orders, security-relevant
  fallbacks, an empirical anchor (e.g. issue number proving a
  value matters), or a multi-stage flow whose ordering is
  load-bearing. What's never acceptable is paragraphs that
  restate what the body of the function already says; if a
  reader who's reading the code would only be confused by the
  docstring, drop those parts, don't just clip arbitrarily.

- **Comments**: same bar. Default to writing no comments. Add
  one only when the *why* is non-obvious: a hidden constraint, a
  subtle invariant, a workaround for a specific bug, behaviour
  that would surprise a reader. If removing the comment wouldn't
  confuse a future reader, don't write it.

  **Don't remove existing comments** unless the code they
  describe is gone — the original author left them for a reason.

- **Don't pad commits, docstrings, or comments with cross-
  references** to old codepaths or issue numbers unless there's
  a clear reason a future reader needs that link. ("This used to
  live in X" is rarely useful; the diff already shows that. "See
  #N for context" is what PR bodies are for.)

- **Method order**: public API at the top, private helpers
  (`_underscore_prefixed`) at the bottom. The same applies to
  module-level functions in scripts.

- **Line length**: 100 (ruff). `target-version = "py312"`.

- **Imports**: ruff/isort sorted. `from __future__ import annotations`
  at the top of every module so we can use modern type syntax on
  Python 3.12+.

- **File size: 800-line soft cap.** When a Python module reaches
  ~800 lines, plan a split before adding more. The cap exists
  because: large modules degrade LLM context budget (every edit
  pays the full file size in input tokens), they slow human
  review (a 5000-line controller is hard to hold in working
  memory), and they accumulate cross-concern coupling that's
  invisible until you try to test one piece in isolation.

  The split pattern this codebase uses is: replace
  `controllers/X.py` with a `controllers/X/` package containing
  `controller.py` (the main class + public API) plus per-concern
  submodules (`controllers/X/foo.py` for the foo concern,
  `controllers/X/bar.py` for the bar concern). `__init__.py`
  re-exports the controller class so existing
  `from .controllers.X import XController` callers keep working.
  See `controllers/devices/`, `controllers/firmware/`, and
  `controllers/remote_build/` for the canonical shape.

  **State dataclass convention.** Group mutable *domain*
  state — anything a sibling module reads or writes — into
  a typed `XxxState` dataclass in `controllers/X/_state.py`;
  the controller owns `self.state: XxxState`; siblings reach
  through `controller.state.X` instead of `controller._X`.
  Canonical examples: `OffloaderState`, `DevicesState`,
  `ReceiverState`. Controller-internal handles that no
  sibling touches (`_unsub_job_completed`, `_pairings_store`,
  base infrastructure like `_db` / `_listeners` / `_tasks`)
  stay on the controller even when reassigned across
  `start()` / `stop()`. The cut is cross-module data, not
  reassignment.

  When splitting a controller into a package, design with
  `XxxState` from PR 1 — adding it later forces a
  post-split cleanup arc (#795, #797). When `start()` /
  `stop()` repopulates a `state.X` dict/set that something
  captured a bound method of (`state.X.__contains__`,
  `state.X.add`), use in-place `.clear()` + `.update(...)`
  rather than reassignment, or the captured method will
  point at the original empty container forever (the
  `DevicesController.state.ignored_devices` loader hit this
  pre-refactor; #797 added the regression test).

  Existing modules over 800 lines (audit `wc -l` periodically;
  the worst offender at the time of writing was 5176 lines) are
  grandfathered. The split rule is scoped to invasive changes:

  * **Small bugfix headed for a patch release**: ship the fix,
    skip the split. Blocking a few-line patch on a 5000-line
    refactor would delay releases and force the refactor under
    bugfix pressure (the wrong context for an invasive
    behavior-free move). The cap is a soft cap; readability
    isn't worth a delayed patch.
  * **Invasive change** (new feature, significant refactor,
    structural touch): **split first**. Land the split as a
    behavior-free refactor PR; then layer the actual change on
    top in a follow-up PR. Each PR is reviewable as one
    concern: the refactor diff is a pure move; the feature
    diff is the actual change.

  A drive-by split inside an unrelated bugfix PR is scope creep
  regardless. Opportunistic refactor PRs targeting the largest
  offenders are welcome and should land as their own PRs.

  A PR that touches a module *already over* 800 lines should
  not make it worse. New top-level modules start under the cap.

  **Exception: pytest conftests.** Pytest's conftest is the
  single import surface (`from .conftest import ...`) for
  shared test fixtures and factories, and the cap's three
  motivations apply weakly here: conftest is intentionally a
  grab-bag (the "cross-concern coupling" the cap targets is the
  whole point of the file), edits touch one fixture at a time
  rather than the file as a whole, and the obvious split into
  per-helper modules forces every test file to track which
  helper lives where without buying anything. The 800-line cap
  does not apply to conftest files.

  When hoisting a duplicated test helper, prefer the *narrowest*
  conftest that covers every caller; only widen to
  `tests/conftest.py` when the callers genuinely span the whole
  suite. Create a new subdirectory conftest (and the matching
  test subdirectory, if needed) when a helper is shared by a
  cohesive group of tests but not the rest of the suite. The
  existing subdirectory conftests
  (`tests/controllers/devices/conftest.py`,
  `tests/controllers/firmware/conftest.py`, `tests/e2e/conftest.py`)
  are the canonical shape.

## Commit / PR conventions

- **No `Co-Authored-By: Claude` trailer.** Project preference.
- Imperative-mood subject line ("Add X", not "Added X").
- Every PR needs **exactly one** label from this set so it lands in
  the right release-notes section:
  `breaking-change`, `new-feature`, `enhancement`, `bugfix`,
  `refactor`, `docs`, `maintenance`, `ci`, `dependencies`.
  CI enforces this via `.github/workflows/pr-labels.yaml`. The
  full template is in `.github/PULL_REQUEST_TEMPLATE.md`; the
  `pr-workflow` skill (under `.claude/skills/pr-workflow/`)
  walks through filling it in — branch off `origin/main`, tick
  exactly one Types-of-changes box, pass the body via
  `--body-file` so the template's backticks aren't shell-escaped.
- Pre-commit runs ruff (lint + format), codespell, yaml/json/python
  checks. Failures auto-fix where possible, then the commit needs to
  be re-staged.

## Workflow conventions

- All GitHub Actions are SHA-pinned with the version as a trailing
  comment (`uses: actions/checkout@<sha>  # v4`) so dependabot can
  bump them while preserving traceability. Org policy.
- Release flow lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#ci--release-pipeline).
- **CI runs the test matrix on Windows too.** PowerShell is the
  default shell on `windows-latest` and **does not accept
  bash-style `\` line continuations** — multi-line `run:` steps
  break with "Missing expression after unary operator '--'". Keep
  cross-platform CI commands on a single line, or use PowerShell-
  compatible backtick continuations.

## Comparison with the legacy esphome dashboard

The legacy Tornado-based dashboard (`esphome/dashboard/` in the
upstream `esphome` package) is the upstream-canonical reference
for shared concerns (mDNS source dispatch, build-info hashes,
StorageJSON layout, `CORE` lifecycle, `address_cache` semantics).
Functional parity with it is essentially achieved as of 2026-05;
the one intentional decline is the HA Supervisor `/auth` POST
flow (the new backend's HA add-on path is ingress-only by
design — see issue #85 and the inline comment at
`device_builder.py:419`). Before declaring a new feature
complete, check the open issue list filtered to "legacy parity"
in case anything has resurfaced.

## Lessons learned about the legacy comparison itself

- The legacy code is the **upstream-canonical** behaviour for
  shared concerns. When we diverge, divergence should be an
  intentional design choice, not an oversight — flag the choice
  in code comments so future readers know it's deliberate.
- The new dashboard's *cleaner* shape can hide gaps that the
  legacy mess was actually solving: Wi-Fi vs Ethernet adoption,
  WS heartbeat, reverse-proxy origin allowlist, per-device build-
  dir cleanup on delete. Audit additions against legacy behaviour
  before assuming the simpler version is sufficient.

## Architecture conventions worth knowing

- **WS-first API.** Real-time updates are the default — clients
  `subscribe_events` once and get pushes. REST is only kept for HA
  backward compat in `api/legacy.py`.
- **Stateful lists ship through subscribe_events, not a `list_*`
  WS command.** Any per-session list whose contents mutate over
  the lifetime of a connected client (devices, importable
  devices, offloader pairings, receiver peers, …) must reach the
  frontend via the snapshot-plus-events pattern below. The
  pattern that *doesn't* belong on new code is "client calls
  `list_X` on mount, then re-calls it after every mutation to
  refresh." That shape — call it list-then-poll — has three
  recurring failure modes, all of which we've hit:
  * **Read-vs-write races.** A snapshot read concurrent with a
    write returns whichever side won the lock, which may
    disagree with what the next event delivers a moment later;
    the frontend's local state ping-pongs between the two until
    the user reloads. Receiver-side `remote_build/list_peers`
    had this exact shape (#514) — `load_remote_build_settings`
    on every read raced `_modify_settings` writes against the
    metadata sidecar.
  * **Cross-tab desync.** A second tab mutating state never
    reaches the first tab unless the first tab re-polls.
    Subscribers on the same dashboard see different worlds
    until one of them reloads.
  * **Round-trip overhead.** Every mutation pays a follow-up
    list-fetch the events were already going to deliver. On a
    cold tab the first paint is gated on the round-trip.

  Do this instead:
  1. Hold the list as a **RAM-canonical dict** on the
     controller, keyed on whatever the wire commands key on
     (`dashboard_id` for receiver peers, `(hostname, port)` for
     offloader pairings, the YAML filename stem for devices).
     Mutations update RAM immediately and schedule a debounced
     disk write through a per-file `helpers.storage.Store`
     (mirror `RemoteBuildController._approved_peers` /
     `_peers_store` for the canonical shape).
     `_to_view`-style projections and any post-mutation
     response read straight off the dict; no executor hop, no
     disk read, no race window. RAM is loaded from the Store at
     `controller.start()`; disk is just persistence.
  2. Seed the **first paint** through `subscribe_events`'s
     `initial_state` push. The seed point is the `_send_initial`
     inner async helper inside `DeviceBuilder._cmd_subscribe_events`
     — passed as the `send_initial=` callback to
     `helpers.event_bus.stream_events`. Add a sync `*_snapshot()`
     method on the controller that returns the projection (e.g.
     `pairings_snapshot()`, `peers_snapshot()`) and stitch its
     output into `initial["<key>"] = [s.to_dict() for s in
     controller.<key>_snapshot()]` inside that helper. The
     snapshot reads must be sync — the subscribe handler runs in
     the WS dispatch hot path and an executor hop on every
     connect is the kind of thing that slows down dashboard
     cold-load on large fleets.
  3. Fire **per-mutation bus events** with TypedDict payloads
     carrying every field a subscriber needs to construct the
     row from the event alone — no follow-up snapshot read
     allowed. If a subscriber would otherwise look at the
     timestamp / pin / label that the snapshot would have had,
     the event payload carries it (see
     `RemoteBuildPairRequestReceivedData`'s `paired_at`, added
     in #514 for exactly this reason). Frontend mutates its
     local list directly from the event; the dashboard never
     gets a "refetch the list now" command.
  4. Emit one event per state transition with deterministic
     listener-attach-then-snapshot ordering — `subscribe_events`
     attaches the bus listener *before* awaiting
     `_send_initial`, so events fired during the snapshot await
     are buffered behind the initial_state and arrive in
     order. Frontend subscribers can rely on
     "initial_state first, then live updates" without any
     reordering logic.

  Don't add `list_*` WS commands for new state surfaces. The
  acceptable carve-outs that already exist are
  `remote_build/list_hosts` (transient mDNS browse output that's
  not stateful — no per-row events make sense) and
  `devices/list_archived` (cold archive directory listing,
  read-once on a dedicated screen). `labels/list` is the
  middle-ground holdover — it's snapshot-fetch-then-events
  rather than full subscribe-driven; new code should land
  through `initial_state` rather than copying that shape.
- **Event payloads use TypedDict, not dataclass.** Mirrors
  Home Assistant core's `Event[_DataT]` /
  `EventStateChangedData` / `EventStateReportedData` pattern.
  Each event-specific shape gets a `TypedDict` declaration next
  to the controller that fires it (e.g.
  `RemoteBuildPairRequestReceivedData` in `models/remote_build.py`,
  `JobLifecycleData` / `JobOutputData` / `JobProgressData` in
  `models/firmware.py`, `DeviceEventData` /
  `DeviceStateChangedData` / `DeviceReachabilityData` in
  `models/devices.py`). Fire sites use the TypedDict-call syntax
  so mypy validates the construction:

  ```python
  bus.fire(EventType.X, SomeEventData(field=value))
  ```

  `Event` and `EventBus.fire` are generic on `DataT` so a typed
  payload flows through without a `cast()` and without a
  `Mapping[str, Any]` widening. Subscribers narrow at the
  callback signature:

  ```python
  def _on_x(event: Event[SomeEventData]) -> None:
      value = event.data["field"]  # typed
  bus.add_listener(EventType.X, _on_x)
  ```

  `add_listener` is intentionally non-generic — listeners share
  a type-erased `Callable[[Event[Any]], None]` bucket and
  ``Any`` bridges the variance gap. The trade vs ~42
  `Literal[EventType.X]`-keyed overloads at end-state: the type
  system enforces the *correct* pairing (subscriber typed for
  the matching event) but doesn't reject the *wrong* pairing
  (subscriber typed for a different event). Mismatches live in
  code review.

  `tests/test_event_payload_contracts.py` pins each TypedDict
  against its emitter at runtime + walks `models.*` to assert
  every `*Data(TypedDict)` is covered, so a new TypedDict can't
  silently skip the wire-shape contract check.

  See `docs/ARCHITECTURE.md` "Event bus → Typing event payloads"
  for the full rationale. New events ship with a TypedDict from
  day one and a row in `_PAYLOAD_FACTORIES`.
- **Persistent firmware queue.** One job runs at a time; queue +
  output buffers survive restarts. See `controllers/firmware.py`.
- **Component catalog is generated**, not hand-edited. Source is
  ESPHome's pre-built schema bundle (https://schema.esphome.io)
  plus narrow live `esphome` introspection for things the schema
  doesn't carry (`multi_conf`, `platform_defaults`,
  `supported_platforms`, type refinement, `unit_of_measurement`
  options). Component-level descriptions and titles fall back to
  the docs MDX repo when the schema's index is sparse. The whole
  thing is in `script/sync_components.py`.
- **Catalog id format**: `<domain>.<stem>` (e.g. `sensor.dht`,
  `output.gpio`). The schema's natural format is the reverse —
  `<stem>.<domain>` (e.g. `dht.sensor`). `_split_qualified_key`
  flips it.
- **Board catalog** (`definitions/boards/<id>/manifest.yaml`) is
  hand-curated YAML. ~80 popular boards plus generic fallbacks per
  platform. `script/validate_definitions.py` lints the manifests.
- **Frontend handoff** for the catalog is documented inline in
  models (`ConfigEntry`, `ComponentCatalogEntry`). New
  `ConfigEntryType` values need a frontend update — coordinate.
- **Deployment modes change the on-disk paths — never hardcode
  them.** Three deployment shapes ship today, and `CORE.data_dir`
  resolves differently in each. Every storage / build-info /
  firmware-binary read MUST go through `ext_storage_path` (or
  `CORE.data_dir` directly) rather than reconstructing
  `<config_dir>/.esphome/...`, or the read silently misses the
  file in the addon and the user sees the bug as
  empty-Local-hash + Pending-install on every device:

  | Mode | `CORE.data_dir` | StorageJSON | Build tree |
  |---|---|---|---|
  | Default (`pip install esphome-device-builder`, dev checkout) | `<config_dir>/.esphome` | `<config_dir>/.esphome/storage/<file>.json` | `<config_dir>/.esphome/build/<name>/` |
  | Home Assistant addon (`is_ha_addon()` true) | `/data` | `/data/storage/<file>.json` | `/data/build/<name>/` |
  | `ESPHOME_DATA_DIR` env override | `$ESPHOME_DATA_DIR` | `$ESPHOME_DATA_DIR/storage/<file>.json` | `$ESPHOME_DATA_DIR/build/<name>/` |

  The HA-addon shape is the dominant one in production —
  device-builder ships as the opt-in preview toggle in the
  official ESPHome HA addon, with the YAML configs at
  `/config/esphome/` (Home Assistant's `/config` mount) and
  every ESPHome-managed artefact at `/data/` (the addon's
  per-instance persistent volume). The split exists so the addon
  can wipe `/data/build/` for upgrades without touching user
  YAML, and so two addon instances on the same host get
  independent data dirs while sharing the user-visible config
  tree. `CORE.config_path` is set to a sentinel YAML inside
  `config_dir` on dashboard startup
  (`controllers/config.py:_DASHBOARD_SENTINEL_FILE`); helpers
  that want the storage layout MUST resolve through that
  initialised CORE rather than reconstructing paths from a
  `Path` argument. The "everything goes through
  `ext_storage_path`" audit covers the in-tree consumers
  (`controllers/firmware/`, `controllers/devices/`,
  `helpers/config_hash`, `helpers/build_size`,
  `helpers/device_yaml`); when adding a new caller, mirror that
  pattern. Tests need `CORE.config_path` set to a tmp-path
  sentinel — the autouse fixtures in
  `tests/controllers/devices/conftest.py` and
  `tests/test_config_hash.py` show the shape.
- **`config_hash` source of truth is `build_info.json`.** ESPHome
  writes `<storage.build_path>/build_info.json` after every
  successful compile *and* every `--only-generate` (the relevant
  `write_cpp(config)` call runs before the `args.only_generate`
  exit branch). The dashboard reads it back via
  `helpers.config_hash.read_build_info_hash` rather than
  recomputing — see "Things that have bitten us" for why.
  `_resolve_device_metadata` reads `build_info.json` first and
  only falls back to the `.device-builder.json` sidecar when the
  build directory has been wiped.
- **`api_encryption` mDNS TXT is a tri-state, not a boolean.**
  Truthy value (e.g. `Noise_NNpsk0_25519_ChaChaPoly_SHA256`) →
  encryption confirmed live. Empty string → TXT seen, key absent
  → device confirmed plaintext. `None` → no broadcast yet,
  unknown. The frontend's `getEncryptionState` and the backend's
  `apply_api_encryption` both lean on the empty-string-means-
  plaintext distinction; a nullable boolean would lose it.
- **Optimistic post-flash sync** in
  `DevicesController._sync_deployed_hash_after_flash` pre-pins
  `deployed_config_hash = expected_config_hash` after a successful
  UPLOAD/INSTALL by routing through
  `DeviceStateMonitor.apply_config_hash`. The dot clears
  immediately instead of waiting on the rebooted device's mDNS
  announce. If the OTA actually failed silently, the next real
  announce pushes the truth back through the same callback.
- **Two mDNS paths with different OFFLINE semantics.** The
  monitor has two distinct mDNS data sources, and they trust
  the protocol differently:

  - **Browser callback** (`_on_service_state_change`) —
    passively subscribed to `_esphomelib._tcp.local.`.
    Trust mDNS in **both directions**: the
    `AsyncServiceBrowser` delivers a `Removed` event when a
    cached record's TTL expires without renewal, which is the
    canonical "device gone" signal. ONLINE → mdns, OFFLINE →
    mdns, no ICMP needed.

  - **One-off active resolve** (`_resolve_non_api_mdns_targets`,
    used for non-API devices that don't broadcast on
    `_esphomelib._tcp.local.`). Trust mDNS for **ONLINE only**.
    A hit claims ONLINE under `mdns` (priority 3, locks out
    ICMP) — once mDNS has answered, repeat-pinging is just
    redundant noise we want to avoid on broadcast-capable
    fleets. A miss is **deliberately silent**: a single active
    query that didn't reply in time conflates "device gone",
    "device slow", and "transient packet loss" — there's no
    subscription delivering TTL-expiry events here, so we can't
    tell them apart. Wait for the ICMP sweep that follows in
    the same loop to decide OFFLINE.

  Don't add an OFFLINE branch to the active-resolve path
  without re-reading this. The asymmetry isn't an oversight —
  it's the only way to get aggressive ONLINE detection without
  flipping the indicator red on every quiet device or dropped
  reply.
- **The `Device` is the source of truth, not the monitor.**
  `DeviceStateMonitor.apply_*` (state, ip, version, config_hash,
  api_encryption) all dedupe by comparing the broadcast value
  against every matching `Device`'s current field — never against
  a separate monitor-side cache dict. A monitor cache can drift
  out of sync with the device when the scanner rebuilds a `Device`
  with `previous=None` (atomic-save churn, REMOVED+re-ADDED
  paths) and the cache then short-circuits the next legitimate
  broadcast, leaving the device with empty fields forever. This
  is the lesson from PR #75; future apply-* style methods need
  to follow the same shape.
- **Scanner keeps a name-keyed index alongside the path-keyed
  one.** `DeviceScanner._devices_by_name: dict[str, list[Device]]`
  is maintained in lockstep with `_devices` via `_set_device` /
  `_pop_device` / `_unindex_name`. Buckets are sorted by
  `configuration` filename so `bucket[0]` consumers and the apply
  / dedupe path see a deterministic "first match" — set-derived
  iteration order would otherwise let the dedupe flip-flop across
  scans for duplicate-named YAMLs. Lookup via
  `scanner.get_by_name(name)` returns a fresh list snapshot
  (mirrors the `devices` property), so callers can iterate freely
  without poisoning the index.
- **mDNS-source dedupe must look at every matching device, not
  just `bucket[0]`.** Two YAMLs sharing an `esphome.name` (a
  config plus a `foo (1).yaml` copy, `dashboard_import` siblings)
  share a single mDNS broadcast. If `apply()` checks only the
  first match's state, a sibling rebuilt with state=UNKNOWN never
  catches up. `apply()` and `_any_matching_device_differs` both
  use `all(...)` / `any(...)` over the whole bucket; the
  per-device callbacks fan out the actual mutation.

## Design principles

- **Never generate invalid configs; fix the source, not the
  consumer.** When a downstream code path encounters an invalid
  YAML — `esphome config` exits non-zero, schema validation
  rejects, compile fails — the right response is to fix the
  *generator* (wizard, `dashboard_import`, `clone`,
  `create_device`, anything that emits a YAML) so it always
  produces something the next step can validate cleanly. Don't
  reach for a fallback in the consumer that "tries to make it
  work anyway." The rename path tried that — a file-level
  rewrite when `esphome config` failed — and silently desynced
  on-disk state from the device's running firmware: the YAML
  got renamed while the device on the network kept its old
  hostname forever, with no error to the user. The same shape
  bit `edit_friendly_name` (PR #390 added pre-write validation
  there) and the rename fallback was deleted entirely (PR #402,
  with an upstream companion at esphome/esphome#16296).

  When you're tempted to add a defensive branch for "what if
  the input is broken?", first audit *what generated this YAML*
  and whether the generator can be hardened so the broken case
  doesn't reach you. If the generator legitimately can't
  guarantee validity (e.g. a user hand-edits between create and
  rename), surface a typed `CommandError(INVALID_ARGS, …)` with
  the actual validation errors — refuse the operation cleanly —
  rather than a silent best-effort fallback. Pair the
  consumer-side error with a generator-side test that runs the
  output through `editor.validate_yaml` (or the equivalent
  schema check) so the same shape can't reappear.

  **Important exception — user-supplied content is *not* a
  generator.** Don't apply this principle to YAMLs the user is
  bringing into the dashboard via the wizard's "Upload YAML"
  flow, drag-and-drop, paste-into-editor, or any other
  user-typed entry point. The whole point of those entry points
  is to land an existing config in the builder *so the user can
  repair it in the editor*. The most common real-world case is
  a YAML from an older ESPHome version whose components have
  since changed schema (deprecated `esphome.platform` /
  `esphome.board`, renamed fields like `wifi.use_address`,
  components whose schema tightened across releases); refusing
  the write strands the user with no way to get the file into
  the editor in the first place. Validate *our* outputs
  (`generate_device_yaml`, `generate_minimal_stub_yaml`,
  `dashboard_import.import_config`, clone's leaf rewrites) but
  pass user-supplied content through unchanged. PR #412
  reverses #405's overzealous validation on `create_device`'s
  `file_content` branch and pins the legacy-config
  acceptance contract; if you ever feel the urge to add a
  `_validate_*_or_raise` to a path whose content originated
  from outside the dashboard, stop and check this exception
  first. The next compile / install will surface real schema
  errors with line numbers — that's what the user wants when
  they're repairing an old config, not a "config doesn't
  validate" up-front refusal.

## Things that have bitten us before

When changing the sync script or catalog handling, watch for these:

- **Don't swap `sys.executable` for a sibling `python`.** It silently
  jumps to a different interpreter (e.g. system Python without
  `esphome` installed) and produces "No module named esphome" at
  compile time. `_find_esphome_cmd` uses `sys.executable` directly.
- **`extends:` references need a deep merge.** The schema uses
  partial overrides — `dht.sensor.humidity.config_vars.device_class`
  only carries `{"default": "humidity"}` and inherits the rest from
  `_SENSOR_SCHEMA`. A flat `{**extended, **local}` drops the inherited
  enum values. `_convert_config_vars` does per-field deep merge.
- **`id_type` ≠ `use_id_type`.** `id_type` describes the type of
  id this field *creates* (the component's own id). `use_id_type`
  marks an actual cross-reference (`i2c_id`, `output_id`, etc.).
  Don't pull `references_component` from `id_type` — that turned
  every `output.gpio.id` into a "select existing gpio" dropdown.
- **Schema's `type: "schema"` can be an extends-only wrapper.** If
  the inner has only `extends` and no `config_vars`, collapse to the
  underlying primitive (often a time_period reference). Don't blindly
  emit `type=nested`.
- **Custom validators lose type info upstream.** `api.encryption`
  emerges from the schema as `{key: Optional, docs: ...}` because
  ESPHome validates it with a custom function. Use
  `_FIELD_OVERRIDES` for these — keep the override list small and
  targeted.
- **MDX field-description backfill is top-level only.** The
  `## Configuration variables` bullet list in MDX is flat; recursing
  into nested entries leaks descriptions across levels (e.g.
  `esphome.name` -> `esphome.areas[].name`).
- **`CORE.config_hash` is post-codegen, not post-`read_config`.**
  Each component's `to_code` runs after validation and can mutate
  the config (id-pinning, default backfill, normalisation), and
  the build reads `CORE.config_hash` from `writer.get_build_info`
  *after* `generate_cpp_contents` has run. A naive subprocess that
  loads the YAML, calls `read_config`, and reads the property
  produces a value that disagrees with the firmware's broadcast.
  Verified empirically against `acfloatmonitor32.yaml`: pre-codegen
  `f3e21d5a`, post-codegen `5a94a12d` — the latter is what the
  firmware bakes in. Read `build_info.json` instead of trying to
  reproduce the codegen pipeline in-process.
- **`compute_has_pending_changes` checks the hash before the
  mtime.** When both `expected_config_hash` and
  `deployed_config_hash` are known, the hash comparison is the
  authoritative answer — equal hashes mean the running firmware is
  built from the same logical config the YAML resolves to today,
  even when the YAML's mtime is newer (whitespace edits,
  `--only-generate` rewriting `StorageJSON`, comment changes). The
  mtime check stays as the fallback for pre-#16145 firmware that
  doesn't broadcast a hash.
- **`ext_storage_path` requires `CORE.config_path` to be set.** It's
  a thin wrapper around `CORE.data_dir`, which crashes
  (`AttributeError: 'NoneType' object has no attribute 'is_dir'`)
  when CORE hasn't been initialised. Fine in production (the
  dashboard sets `CORE.config_path` on startup); tests get the
  prerequisite via the autouse `_core_config_path_in_tmp` fixture
  in `tests/conftest.py`, which pins `CORE.config_path` to a
  per-test sentinel under `tmp_path`. New helpers that read
  storage / build_info / firmware-bin paths MUST resolve through
  `ext_storage_path` (or `CORE.data_dir` directly) — never
  reconstruct `<yaml_dir>/.esphome/...` from a `Path` argument,
  even if it works locally. The default-mode shortcut is invisibly
  wrong on the HA addon (`/data` is the data dir, not
  `<config_dir>/.esphome`) and silently returns `None` for every
  device, surfacing as empty-Local-hash + Pending-install on the
  encryption indicator. See "Deployment modes change the on-disk
  paths — never hardcode them" in *Architecture conventions* for
  the full path table.
- **Atomic-save editors (vscode-on-macOS et al.) can briefly
  remove the YAML mid-save.** The scanner sees the file
  disappear, fires `REMOVED`, then re-`ADDED` on the next sweep
  with `previous=None` — so any monitor-derived state on the
  Device (`deployed_config_hash`, `api_encryption_active`,
  `ip`, `deployed_version`, `state`) gets reset to its default.
  Don't let dedupe layers cache values keyed only on the device
  *name*; the rebuilt Device starts fresh and the cache will
  silently mask the next legitimate broadcast. (See "The
  `Device` is the source of truth, not the monitor" above for
  the resolution.)
- **Test callbacks that drive dedupe must mirror production's
  state mutation.** The monitor's apply-* methods short-circuit
  when every matching device's field already equals the broadcast
  value — and in production, the controller's `_on_*_change`
  callback writes that value back onto the `Device`. A bare
  `MagicMock` callback in tests doesn't, so a "second call
  short-circuits" assertion will (correctly) fail unless the
  mock has a `side_effect` that flips the device's field. See
  the `_flip_state` / `_flip` helpers in `tests/test_mdns_*.py`
  for the pattern.
- **In-place file writes need `esphome.helpers.write_file`,
  not `Path.write_text`.** Both `Path.write_text` and a plain
  `open(path, "w")` truncate the destination *before* writing
  the new bytes — a crash or exception between the truncate and
  the flush leaves the user with an empty or half-written file.
  For YAML configs that's unrecoverable; the device's config is
  gone. `esphome.helpers.write_file` is the canonical helper:
  stages the new bytes in a `NamedTemporaryFile` in the
  *destination* directory, then `shutil.move`s into place. The
  resulting move is atomic only when it can resolve to a same-FS
  `os.rename` / `os.replace`; cross-filesystem it degrades to
  copy+delete which is *not* atomic. Staging the tempfile in the
  destination directory keeps it same-FS and the move atomic.
  `write_file` also handles `fchmod` to 0o644 by default and
  wraps `OSError` as `EsphomeError`. Use it for any in-place
  rewrite of user-editable YAML / settings (`edit_friendly_name`
  is the canonical example). Don't fall back to
  `tempfile.mkstemp` without the `dir=` argument — it lands on
  `/tmp`, which is a separate filesystem from `/config` in the
  HA addon, and the cross-FS `shutil.move` silently loses
  atomicity. Don't hand-roll a temp+rename dance either; the
  helper already does it correctly.

  *New* files are a different shape — `clone_device` opens via
  `open(path, "x")` (exclusive-create), which is already atomic
  by virtue of failing if the target exists. Only in-place
  *edits* of an existing file need `write_file`. Build artefacts
  (StorageJSON sidecars, `.device-builder.json` metadata) where a
  partial write is recoverable on next compile / scan can stay
  on direct-write paths — the criterion is "would losing this
  file lose user-authored content."
- **CodSpeed parametrize values need explicit
  `pytest.param(value, id="<short>")` IDs when the value isn't
  already a short primitive.** pytest's auto-generated test ID
  concatenates the parameter values verbatim, so a multi-KB
  YAML body / bytes payload / anything whose `repr()` runs more
  than a few dozen characters bakes into the test name. The
  benchmark run itself passes and the data uploads fine, but
  the server-side "CodSpeed Performance Analysis" check fails
  with "Unable to generate the performance report" / "internal
  error while processing the run's data"; the report ingest
  hits a length limit on the test identifier and silently
  drops the whole run. Follow the existing pattern in
  `tests/benchmarks/test_log_streaming.py` and
  `tests/benchmarks/test_peer_link_noise_xx.py`:
  `pytest.param(_NEWLINE_PAYLOAD, 1000, id="newline_1k")`,
  `pytest.param(1024, id="1KiB")`. Bare ints / short slugs whose
  autogen ID is already terse
  (`parametrize("fleet_size", [50, 200])` -> `[50]` / `[200]`)
  are fine as-is; the trap is parametrize values that carry
  large content.

## Useful entry points

| Path | What |
|---|---|
| `esphome_device_builder/device_builder.py` | Singleton owning controllers + event bus |
| `esphome_device_builder/controllers/*.py` | One file per API surface (components, boards, labels, ...). Larger surfaces (devices, firmware, remote_build) are packages — same shape, with a `controller.py` for the main class plus per-concern submodules and an `__init__.py` re-exporting the controller. |
| `esphome_device_builder/models/*.py` | Data classes (mashumaro) — pure shape, no logic |
| `esphome_device_builder/api/ws.py` | WebSocket dispatch |
| `esphome_device_builder/definitions/components.json` | Generated; do not hand-edit |
| `esphome_device_builder/definitions/boards/<id>/manifest.yaml` | Curated; hand-edited |
| `script/sync_components.py` | Regenerates the component catalog |
| `script/check_catalog.py` | Smoke test for popular components |
| `script/validate_definitions.py` | Lint board manifests |
| `docs/ARCHITECTURE.md` | Full architecture + deployment + CI overview |
| `docs/API.md` | Every WS command + payload shape + event |

## Things not to do

- **Don't hand-edit `components.json`.** Regenerate via
  `script/sync_components.py`. CI runs the sync nightly and opens a
  PR with diff summary + smoke-test verification — that's the
  intended update path.
- **Don't auto-merge catalog PRs.** Schema regressions and sync-
  script bugs both surface as PR diffs; a human gate catches them
  before they ship. The diff summary in the PR body is designed for
  fast review.
- **Don't add `Co-Authored-By: Claude` to commits** in this repo.
- **Don't bump the `esphome` dependency casually.** Dependabot
  ignores it for a reason — bumping needs a coordinated catalog
  re-sync against the matching schema version. Do it as a deliberate
  step at release time.
- **Don't reorder existing public methods** without a reason. The
  controllers' API surface is the de-facto public interface for the
  frontend.
