# Fix: upload-phase progress bar frozen after receiver-side compile

## Bug

After a transparent REMOTE install (issue #106 phase 7a-3), the frontend's
firmware-tasks progress bar stays pinned at the compile peak (~95-100%) during
the entire local `esphome upload` phase, never animating with upload progress.
At job completion the bar disappears, so the operator never sees an upload
indicator.

User confirmed this against a 1.4 MB OTA upload that took ~22 seconds: bar
frozen the whole window, only a single `Uploading: [...] 100% Done...` line
visible in the log dialog after completion.

## Root cause

`esphome_device_builder/controllers/firmware/helpers.py:_ingest_output_line`
applies a monotonic clamp to the parsed progress:

```python
progress = _parse_progress(line)
if progress is None or progress <= (job.progress or 0):
    return
job.progress = progress
prog_payload: JobProgressData = {"job_id": job.job_id, "progress": progress}
bus.fire(EventType.JOB_PROGRESS, prog_payload)
```

The receiver-side compile streams PIO / linker lines through the fan-out that
match patterns like `(95%)` and push `job.progress` near 100. When the runner
then enters `_fetch_and_run_local_upload`, the local `esphome upload`
subprocess emits `Uploading: [..] 5% / 10% / 15% / ...` lines via
`ProgressBar.update` → `sys.stderr.write` (with explicit `flush()` after each
write, `\r`-prefixed). Those lines DO reach `_ingest_output_line` (verified by
their presence in `job.output`), but each parsed percent is ≤ 95, fails the
strict-greater check, and silently drops without firing `JOB_PROGRESS`.

Only the final `Uploading: 100% Done...` line passes the clamp, but by then
`JOB_COMPLETED` arrives within milliseconds and the bar disappears.

The frontend's ansi-log renderer collapses `\r`-terminated chunks (deliberate
behaviour for esptool / PIO in-place progress), so the log dialog only shows
the last `Uploading:` frame — that's a display artifact, not data loss. The
intermediate frames are received and appended to `job.output`; only the
`JOB_PROGRESS` fires are missing.

## Fix

Reset `job.progress` at the compile→upload seam in
`esphome_device_builder/controllers/firmware/remote_runner.py`'s
`_fetch_and_run_local_upload`, and fire `JOB_PROGRESS{0}` so the frontend bar
visibly resets at the phase boundary. Subsequent upload percents (5%, 10%, ...)
all advance from 0 and pass the clamp.

### Code change

**File:** `esphome_device_builder/controllers/firmware/remote_runner.py`

**1. Add `JobProgressData` to the models import block** (around line 41-49):

```python
from ...models import (
    EventType,
    FirmwareJob,
    JobProgressData,   # NEW
    JobStatus,
    JobType,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPeerLinkClosedData,
)
```

**2. Insert the reset inside `_fetch_and_run_local_upload`** right after the
`yaml_path` resolution and before the `tempfile.mkdtemp` block. The current
shape is:

```python
    bus = controller.bus
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    # ``tempfile.TemporaryDirectory`` ctor calls
    ...
```

Replace with:

```python
    bus = controller.bus
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    # Reset ``job.progress`` at the compile → upload seam.
    # :func:`helpers._ingest_output_line` monotonically clamps:
    # any line whose parsed percent isn't strictly greater than
    # the running value gets dropped. The receiver-side compile
    # streams linker / PIO ``(N%)`` lines through the fan-out
    # that can push the gauge near 100; if we don't reset here,
    # the local ``esphome upload``'s ``Uploading: [..] 5% / 10%
    # / ...`` lines all fall below the compile's high-water and
    # the progress bar appears frozen at the compile peak
    # throughout the entire flash phase. Fire a 0% event so the
    # frontend bar visibly resets at the phase transition rather
    # than waiting for the first non-clamped upload percent to
    # land.
    job.progress = 0
    bus.fire(EventType.JOB_PROGRESS, JobProgressData(job_id=job.job_id, progress=0))

    # ``tempfile.TemporaryDirectory`` ctor calls
    ...
```

That's the entire fix — two lines of behavioural change, the rest is comment.

## Test

**File:** `tests/controllers/firmware/test_remote_runner.py`

Add a new test in the "UPLOAD / INSTALL — local flash step after receiver
compile" section (just before
`test_remote_install_completes_after_local_upload_succeeds`):

```python
@pytest.mark.asyncio
async def test_remote_install_resets_progress_between_compile_and_upload(
    firmware_controller_factory: FirmwareControllerFactory,
    patch_bundle: AsyncMock,
    patch_extract_firmware: MagicMock,
    tmp_path: Any,
) -> None:
    """The phase transition fires a ``JOB_PROGRESS{0}`` and clears ``job.progress``.

    Pins the compile → upload seam reset:
    :func:`helpers._ingest_output_line` monotonically clamps,
    so a receiver-side compile that pushed the gauge near 100
    via linker / PIO ``(N%)`` lines would suppress every
    ``Uploading: [..] 5% / 10% / ...`` line from the local
    flash subprocess (5 isn't > 95). Without an explicit reset
    at the phase boundary, the progress bar appears frozen at
    the compile peak for the entire upload duration. The
    runner fires a 0% event at the start of
    ``_fetch_and_run_local_upload`` so the frontend visibly
    resets and subsequent upload percents advance the gauge.
    """
    controller = firmware_controller_factory(with_terminate=True)
    captured = _capture_local_events(controller)
    client = _make_client()
    client.download_artifacts = AsyncMock(return_value=_make_packed_artifacts())
    _wire_remote_build(controller, client=client)
    _wire_upload_subprocess(controller, exit_code=0)
    job = _make_remote_install_job()
    # Simulate the receiver-side compile having driven the
    # gauge to 95% via a linker ``(95%)`` line before the
    # phase transition; without the reset the upload's lower
    # percents would all be silently clamped.
    job.progress = 95

    runner = asyncio.create_task(remote_runner.run_remote_job(controller, job))
    await _wait_until_dispatched(client)
    _fire_state(controller, job_id=job.job_id, status="completed")
    await asyncio.wait_for(runner, timeout=5.0)

    # The phase-transition reset fired a 0% event; the
    # captured stream carries it as the first JOB_PROGRESS
    # event from the upload half. (The compile half doesn't
    # appear here because we pre-seeded ``job.progress=95``
    # without going through the bus.)
    assert captured[EventType.JOB_PROGRESS]
    assert captured[EventType.JOB_PROGRESS][0]["progress"] == 0
    assert captured[EventType.JOB_PROGRESS][0]["job_id"] == job.job_id
```

Helpers used (`_make_remote_install_job`, `_wire_upload_subprocess`,
`_wire_remote_build`, `_make_packed_artifacts`, `_capture_local_events`,
`_make_client`, `_wait_until_dispatched`, `_fire_state`,
`patch_extract_firmware`) are already defined in the same file — no new
scaffolding needed.

## Verify

1. New test passes with the fix:
   ```
   .venv/bin/python -m pytest tests/controllers/firmware/test_remote_runner.py::test_remote_install_resets_progress_between_compile_and_upload -v
   ```
2. New test FAILS without the fix (regression guard). Remove the two lines
   from `remote_runner.py` and re-run — the assertion on
   `captured[EventType.JOB_PROGRESS]` should fail because the only
   JOB_PROGRESS event still present would be from `_ingest_output_line`'s
   "Uploading 100% Done..." parse, and that fires AFTER `_fire_state`
   completes the runner. The test will see an empty captured list or only the
   100% entry.
3. No regression in the rest of the runner suite:
   ```
   .venv/bin/python -m pytest tests/controllers/firmware/test_remote_runner.py tests/e2e -q
   ```
4. (Optional, post-merge) end-to-end smoke: run a remote OTA install against
   a real receiver and confirm the firmware-tasks bar resets to 0% at the
   compile→upload transition and animates with `Uploading:` percents through
   to ~100%.

## Out of scope

- The local `esphome run` (LOCAL-source INSTALL) has the same shape — compile
  emits high-percent lines, then OTA emits `Uploading: 5%` which gets
  clamped. Could be addressed by detecting the `Connecting to <addr> port`
  line as the phase marker and resetting there, but that's a heuristic on
  unstructured CLI output. Out of scope here — fix the REMOTE path first
  since it's the path the user is on. Track as a followup.
- Frontend log renderer collapsing intermediate `\r`-terminated frames into
  one visible row is correct behaviour for esptool / PIO in-place progress
  and shouldn't change.

## PR shape

- **Title:** "Reset firmware-job progress between compile and upload on REMOTE installs"
- **Label:** `bugfix`
- **Type:** Bugfix
- **Frontend coordination:** none — the JobProgressData wire shape is
  unchanged, this is a server-side fix-up of when the event fires.

## Branch + commit

```
git fetch origin
git checkout -b reset-progress-between-phases origin/main
# apply the two changes above
git commit -m "Reset firmware-job progress at compile→upload seam on REMOTE installs

helpers._ingest_output_line monotonically clamps parsed progress
percentages, so a receiver-side compile that pushed the gauge near
100 via linker / PIO (N%) lines silently swallows every Uploading:
5% / 10% / ... frame the local esphome upload subprocess emits
during the flash step. Result: the firmware-tasks progress bar
stays frozen at the compile peak for the entire upload duration —
user observed a 22-second OTA where the bar never moved.

Fire JOB_PROGRESS{0} and clear job.progress at the start of
_fetch_and_run_local_upload so the frontend visibly resets at the
phase boundary and subsequent upload percents advance from 0."
git push -u origin reset-progress-between-phases
```
