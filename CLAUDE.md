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

Roughly alpha closing on beta. Targeted to land as an opt-in preview
toggle in the official ESPHome container and Home Assistant add-on.

## Code style

- **Docstrings**: consumer-facing. Describe *what the function does
  and what the caller can pass*, not how it's implemented internally.
  Single-line docstrings inline (`"""Summary."""`); for **multi-line
  docstrings put the content on the line after `"""`**, not on the
  same line:

  ```python
  def merge_component_yaml(...) -> str:
      """
      Render *component* and merge it into *existing* YAML.

      For platform-style components the new ``- platform: ...`` list
      item is appended under any existing ``<domain>:`` block.
      """
  ```

  The codebase has both styles in older code; bring new code in
  line with this convention.

- **Comments** clarify code that isn't immediately obvious. Don't
  paraphrase what the code already says. **Don't remove existing
  comments** unless the code they describe is gone — the original
  author left them for a reason.

- **Don't pad commits or comments with cross-references** to old
  codepaths or issue numbers unless there's a clear reason a future
  reader needs that link. ("This used to live in X" is rarely
  useful; the diff already shows that.)

- **Method order**: public API at the top, private helpers
  (`_underscore_prefixed`) at the bottom. The same applies to
  module-level functions in scripts.

- **Line length**: 100 (ruff). `target-version = "py312"`.

- **Imports**: ruff/isort sorted. `from __future__ import annotations`
  at the top of every module so we can use modern type syntax on
  Python 3.12+.

## Commit / PR conventions

- **No `Co-Authored-By: Claude` trailer.** Project preference.
- Imperative-mood subject line ("Add X", not "Added X").
- Every PR needs **exactly one** label from this set so it lands in
  the right release-notes section:
  `breaking-change`, `new-feature`, `enhancement`, `bugfix`,
  `refactor`, `docs`, `maintenance`, `ci`, `dependencies`.
  CI enforces this via `.github/workflows/pr-labels.yaml`.
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

## Useful entry points

| Path | What |
|---|---|
| `esphome_device_builder/device_builder.py` | Singleton owning controllers + event bus |
| `esphome_device_builder/controllers/*.py` | One file per API surface (devices, firmware, components, boards, ...) |
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
