# Pipeline the firmware queue: concurrent compile + upload lanes

Design note for splitting the firmware job queue so a network-bound
upload runs concurrently with a CPU-bound compile.

## Context

ESPHome org discussion #3702 ("Separate build steps to allow pipelining
of updates"): the device-builder firmware queue processes **one job at a
time**. An `install` is a single fused `esphome run` (compile **and**
upload in one subprocess), so a network-bound upload — e.g. ~9 minutes
over a Thread mesh, CPU idle the whole time — blocks the next device's
compile. There is no reason a CPU-idle upload should gate a compile.

### Current architecture (verified)

- One `asyncio.Queue` + one consumer (`controllers/firmware/runner.py:run_queue`)
  + one `state.current_job` / `state.current_process` slot
  (`controllers/firmware/_state.py`). Strict FIFO serialization.
- `JobType.INSTALL` → `esphome run` (`controllers/firmware/cli.py`),
  fusing compile+upload in one subprocess held in the single slot.
- `JobType.COMPILE` → `esphome compile` and `JobType.UPLOAD` →
  `esphome upload --device <port>` (no recompile) **already exist as
  separate job types / WS commands** (`firmware/compile`,
  `firmware/upload`). The building blocks for "compile then upload" are
  already present.
- `controllers/devices/firmware_sync.py` already keys behavior per type:
  `recompute_hash = type in (COMPILE, INSTALL)`,
  `flashed = type in (UPLOAD, INSTALL)` — so a COMPILE+UPLOAD pair
  reproduces today's INSTALL side effects with no change there.

## Decisions

- **Single PR.**
- **One upload at a time** — a single upload worker runs concurrently
  with the single compile worker. Safe for Thread airtime + serial-port
  exclusivity. A documented seam is left for per-port parallelism later.
- **Two visible jobs** — `firmware/install` (LOCAL) creates a **COMPILE
  job + a dependent UPLOAD job** the frontend sees as two rows, rather
  than one job spanning two phases.

## Design

Two concurrent single-worker **lanes**, each with its own queue +
current-job + current-process slot, plus **job chaining** (`depends_on`):

- **compile lane** (CPU): `COMPILE` (LOCAL = `esphome compile`; REMOTE =
  dispatch to the receiver, stream output, then download + materialise the
  artifacts locally), `CLEAN`, `RESET_BUILD_ENV`, `RENAME`.
- **upload lane** (network): `UPLOAD` — always a LOCAL `esphome upload
  --device <port>`, whether the compile that produced the binary ran
  locally or on a receiver.
- The two lanes run concurrently: while device A's UPLOAD runs on the
  upload lane, device B's COMPILE runs on the compile lane.

### `firmware/install` becomes a chain (local *and* remote)

1. Create a `COMPILE` job (source LOCAL or REMOTE per the build scheduler)
   and a **LOCAL** `UPLOAD` job (`depends_on = compile.job_id`, `port`
   carried), both in `state.jobs`.
2. Supersede prior active jobs for the configuration, excluding **both**
   chain ids.
3. Enqueue COMPILE onto the compile lane; enqueue UPLOAD **held** (fire
   `JOB_QUEUED` so it renders as queued, but it is *not* put on the upload
   lane queue until its prerequisite succeeds).
4. On COMPILE terminal **success**, a lifecycle hook puts the dependent
   UPLOAD on the upload lane; on COMPILE **failure/cancel**, the hook
   cancels the dependent UPLOAD (cascade, `error="prerequisite compile
   did not succeed"`).

This reuses `esphome compile` then `esphome upload` (already implemented),
so no binary is recompiled at upload time, and `firmware_sync` already
does the right per-type hash/flashed bookkeeping.

**Remote builds use the upload lane too — and need no `remote_runner`
rewrite.** Discovery: a remote COMPILE job *already* dispatches to the
receiver and materialises the artifacts into the local build dir
(`remote_runner._finalise_after_receiver_completed` does this for COMPILE
today — it's what backs the frontend's "Download firmware binary" button).
So `firmware/install` builds the *same* chain regardless of source: the
COMPILE job leaves the binary on local disk (compiled locally, or fetched
from the receiver), and the dependent LOCAL `UPLOAD` job flashes it on the
upload lane. The receiver keeps compiling the next device while the local
box uploads the current one — fully utilising the remote compile resource.
The only `remote_runner` touch is threading the lane into its local-flash
subprocess; its existing INSTALL/UPLOAD local-flash branch stays as a
legacy fallback for any persisted fused INSTALL job.

## Concrete changes

### `controllers/firmware/_state.py` — lanes
Introduce a `Lane` dataclass `(queue, current_job, current_process)` and
hold `compile_lane` / `upload_lane` on `FirmwareState`. Keep `queue` /
`current_job` / `current_process` as **backward-compat properties
proxying to `compile_lane`** so the many tests/conftests that read
`state.queue` / `state.current_job` keep working (they refer to the
CPU/everything lane). Document the proxy as transitional. Two small
methods live here as the single source of truth shared by factories /
persistence / lifecycle (DRY): `lane_for(job)` (UPLOAD → upload lane, else
compile lane) and `dependency_satisfied(job)` (no `depends_on`, or the
prerequisite COMPLETED).

### `models/firmware.py`
- `JobType.INSTALL` stays in the enum and `cli.build_command` keeps its
  `INSTALL → run` mapping, but new installs no longer create one
  (`firmware/install` builds a chain). A persisted in-flight INSTALL job
  just re-runs as the fused `esphome run` on the compile lane — a safe
  legacy fallback, so no load-time migration is needed.
- Add `depends_on: str = ""` to `FirmwareJob` (job_id of the prerequisite;
  empty = none). Rides in `JobLifecycleData` (whole-job payload) so the
  frontend can render the dependency; default keeps old persisted blobs
  loadable. `reset()` preserves it.
- Keep `QueueStatus` (NamedTuple) at exactly `(idle, running, queue_depth)`
  — appending fields breaks the `idle, running, queue_depth = ...` unpack
  (a NamedTuple unpacks all its fields). `queue_status_snapshot()` returns
  the **aggregate** (idle = both lanes idle AND both queues empty AND no
  held dependents; running = either lane; queue_depth = sum). Add a
  separate `lane_status(lane)` accessor returning a per-lane `QueueStatus`
  for the remote scheduler's compile-lane read.

### `controllers/firmware/runner.py`
- `run_queue` → `run_lane(controller, lane)`; `controller.start()` spawns
  **two** consumer tasks (compile + upload).
- `execute_job(controller, job, lane)` gains `lane`: writes
  `lane.current_job`, threads `lane` into `_tracked_subprocess` (per-lane
  `current_process`), and the `finally` clears that lane's slots.
  `tracked_subprocess(controller, lane, ...)` saves/restores
  `lane.current_process` (keep the nested save/restore for `_verify_chip`).
- The early-cancel checks (`if job_id in cancel_requested`) and the
  `CancelledError` terminate path pass the lane.

### `controllers/firmware/lifecycle.py`
- `finalize_terminal` / `finalize_cancelled` / `terminate_current_process`
  take (or resolve) the lane; keep the **slot-release-before-`bus.fire`**
  ordering (pinned by `test_queue_status.py`).
- In `finalize_terminal`, after release+fire, run the **dependency hook**:
  success → put each held job whose `depends_on == job.job_id` onto its
  lane; non-success → cancel them. (Lives here so it covers both the
  local subprocess and remote paths.)

### `controllers/firmware/factories.py`
- `enqueue`: route to the lane via a small `_lane_for(job)` helper; if the
  job has an **unmet `depends_on`**, fire `JOB_QUEUED` + persist but do
  **not** put it on a lane queue (held). When the prerequisite later
  completes, the lifecycle hook does the lane `put` (no second
  `JOB_QUEUED`).
- `supersede_active_jobs`: accept `exclude_job_ids: set[str]` (chain-aware)
  so creating the COMPILE+UPLOAD pair doesn't cancel its own sibling.
- New `enqueue_install_chain(...)` (or extend the controller's install
  handler) implementing the 4-step chain above.

### `controllers/firmware/jobs.py` (cancel)
- The RUNNING branch resolves which lane holds the job
  (`compile_lane.current_job` vs `upload_lane.current_job`) and terminates
  **that lane's** process only, so cancelling an upload never SIGTERMs a
  concurrent compile. Cancelling a COMPILE cascades to its held UPLOAD
  (the dependency hook on the COMPILE's CANCELLED terminal).

### `controllers/firmware/controller.py`
- `firmware/install` always builds the COMPILE+UPLOAD chain (local *and*
  remote — `enqueue_install_chain`); no source branch. `firmware/compile`
  / `firmware/upload` unchanged in shape.
- `lane_status(lane)` (per-lane `QueueStatus`) + `queue_status_snapshot()`
  (aggregate across both lanes).
- `start()` spawns two runner tasks (`run_lane` per lane); delegates
  (`_execute_job`, `_tracked_subprocess`, `_terminate_current_process`)
  gain a `lane`.

### `controllers/firmware/persistence.py`
- `load_jobs` re-queues by lane via `_lane_for` + `depends_on`: jobs with
  an **unmet** `depends_on` stay held (the prerequisite's completion hook
  will enqueue them); a held job whose prerequisite is already COMPLETED
  is enqueued to its lane; one whose prerequisite is missing/FAILED is
  cancelled. RUNNING jobs `reset()`→re-queue to their own lane (a
  mid-flash UPLOAD re-flashes; idempotent — does not recompile).
- **Legacy INSTALL:** a persisted active `INSTALL` job re-queues to the
  compile lane and re-runs as the fused `esphome run` (no migration). New
  installs are chains; this only ever applies to a job in flight across
  the upgrade restart.
- A restored dependent (`depends_on` set) whose prerequisite is gone or
  didn't succeed is cancelled; one whose prerequisite is still active stays
  held (`_release_dependents` lands it on completion). Routing happens in a
  second pass so the prerequisite resolves regardless of on-disk order.

### `controllers/firmware/remote_runner.py`
- Minimal: thread the lane into the local-flash subprocess
  (`_run_upload_subprocess` resolves it via `state.lane_for(job)` — remote
  jobs run on the compile lane). No decomposition needed: remote COMPILE
  already materialises locally, so the unified install chain works without
  touching the dispatch/download logic. The existing INSTALL/UPLOAD
  local-flash branch is left intact as a legacy fallback.

### `controllers/remote_build/peer_link_sessions.py`
- The receiver advertises idleness so offloaders route **compiles** to it
  ("receiver only compiles"). Broadcast **compile-lane** idleness as the
  wire `idle` (not the aggregate), so a receiver busy on its upload lane
  still accepts compiles — otherwise it falsely reads busy and offloaders
  silently fall back to LOCAL. Wire shape `{idle, running, queue_depth}`
  is unchanged; only *which lane's* idleness we send changes.

## Scope boundaries (deliberate)

- **One upload at a time.** Per-port/per-device parallel uploads (multiple
  concurrent OTA flashes on WiFi/Ethernet) are a documented future seam on
  the upload-lane consumer, not built now.
- **`JobType.INSTALL` is retired for new jobs** (the `firmware/install`
  command now creates a COMPILE + UPLOAD chain) but stays in the enum +
  `cli` mapping so a persisted in-flight INSTALL re-runs as the fused
  `esphome run` on the compile lane (legacy fallback, no migration).

## Verification

- **Existing tests must stay green** (`tests/controllers/firmware/`):
  `test_queue_status.py` (unpack + slot-release-before-fire ordering),
  `test_install*.py` (INSTALL now emits a COMPILE then an UPLOAD — the
  highest-churn updates), `test_cancel.py` / `test_stop*.py` (lane-aware
  terminate), `test_persistence.py` (re-queue targets the right lane; add
  held-upload resume), `test_supersede.py`, `test_rename_lock.py`,
  `test_remote_runner.py` (remote path unchanged),
  `tests/controllers/firmware/conftest.py`. e2e:
  `tests/e2e/test_install_round_trip.py`, `test_local_compile.py`,
  `test_cancel_job.py`, `test_submit_job*.py`. Scheduler:
  `tests/test_build_scheduler.py`, `tests/test_remote_build_peer_link*.py`.
- **New tests:**
  1. Concurrency: enqueue an UPLOAD for A (slow) + a COMPILE for B; assert
     both lanes' `current_job` are populated simultaneously.
  2. Install chain: `firmware/install` creates a COMPILE + a held UPLOAD;
     UPLOAD starts only after COMPILE succeeds; COMPILE failure cancels the
     UPLOAD.
  3. Cross-lane cancel: cancelling the UPLOAD doesn't touch a concurrent
     compile's process; cancelling the COMPILE cascades to its held UPLOAD.
  4. Cross-lane supersede: re-queuing device X's install cancels both the
     in-flight COMPILE and the held UPLOAD.
  5. Restart: a persisted held UPLOAD with a COMPLETED prerequisite
     enqueues to the upload lane and does not recompile; with an incomplete
     prerequisite stays held.
  6. QueueStatus: per-lane fields + aggregate; positional
     `(idle, running, queue_depth)` unpack still correct; receiver
     advertises compile-lane idle while uploading.
- `docs/API.md` updated (install now yields two jobs; `depends_on` field).
- Pre-commit (ruff/format/codespell); mypy on changed modules.
- Empirical: pair backend `:6052` + frontend dev server, install a device,
  confirm two rows (compile then upload) and that a second device's compile
  starts while the first uploads.

## Risks / coordination

- **Frontend contract change** (coordinate with
  `esphome/device-builder-frontend`): `firmware/install` now produces two
  job rows (a compile and a dependent upload), and two firmware jobs can be
  RUNNING at once. Events are all standard `JOB_*`; the frontend must
  render N concurrent RUNNING rows and the `depends_on`/queued-waiting
  upload. Companion frontend PR linked cross-repo.
- **Riskiest backend bits:** (a) the held-dependent enqueue + completion
  hook race (create both chain jobs *before* enqueueing either, so a fast
  compile can't finish before the dependent exists); (b) cross-lane cancel
  resolving the correct lane's process; (c) the compile-lane-idle wire
  signal for the remote scheduler (getting it wrong reproduces the
  documented frozen-`running` silent-LOCAL-fallback bug).
- **800-line cap:** `controller.py` and `runner.py` grow; extract the
  chain/lane helpers (e.g. a `lanes.py`) to stay under the cap rather than
  fattening `execute_job` (already `# noqa PLR0912/PLR0915/C901`).
