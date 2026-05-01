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
