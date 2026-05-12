"""
Background ``--only-generate`` regeneration helpers.

After every YAML write (and once per device on startup), the
controller schedules a background ``esphome compile
--only-generate <yaml>`` so the StorageJSON sidecar reflects the
latest config without waiting for a real build. Three guards
keep the spawn rate bounded:

* In-memory ``_regenerate_pending`` dedupes within a session.
* In-memory ``_regenerate_failed`` short-circuits a YAML whose
  last attempt failed; entries are cleared in
  ``_on_scan_change`` when the file's cache key changes (i.e.
  the user actually edited it).
* On-disk ``regen_failed_mtime`` + ``regen_failed_at`` in the
  metadata sidecar carries the same skip across restarts. A
  successful regen clears both atomically alongside the
  ``expected_config_hash`` write.

Three sets + one lock live on the controller (``_regenerate_pending``,
``_regenerate_failed``, ``_regenerate_lock``). The functions here
reach in via the ``controller`` arg rather than holding their own
state — ``_on_scan_change`` (in monitor_callbacks) reads
``_regenerate_failed.discard()`` too, so the state is shared
across modules and stays controller-owned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...helpers.config_hash import read_build_info_hash
from ...helpers.subprocess import create_subprocess_exec
from ..config import get_device_metadata, set_device_metadata

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# How long the persisted "regen failed" stamp is honoured before a
# restart-time check is allowed to re-spawn ``--only-generate`` for
# the same untouched YAML. The in-memory ``_regenerate_failed`` set
# blocks within a session until the user edits the YAML; the TTL
# only applies cross-restart, so a transient external problem
# (git package server flaky, DNS hiccup) eventually recovers
# without forcing the user to touch the file. One hour is short
# enough that "I'll come back to this in a bit and restart" works,
# long enough that a debugger restarting the dashboard 10x in a
# row doesn't churn through 10 spawns on the same broken config.
_REGEN_FAILURE_TTL_SECONDS: float = 3600.0


def schedule(controller: DevicesController, configuration: str) -> None:
    """
    Run ``esphome compile --only-generate <yaml>`` in the background.

    ``--only-generate`` walks ESPHome's full config validation
    pipeline (resolving ``!secret`` / ``!include`` / packages /
    ``dashboard_import``) and writes the resulting StorageJSON
    without doing a real build. That populates ``address``,
    ``loaded_integrations``, ``target_platform``, etc. for devices
    that have never been compiled (the typical "wr2-test was just
    added and shows UNKNOWN forever" path) and refreshes them
    whenever the YAML changes.

    Three guards keep this from running away:
    * ``_regenerate_pending`` skips duplicate schedules for a
      configuration that's already in flight.
    * ``_regenerate_failed`` skips YAMLs whose last attempt
      failed; entries are cleared in ``_on_scan_change`` when the
      file's cache key changes (i.e. the user actually edited it).
    * ``regen_failed_mtime`` + ``regen_failed_at`` in the
      metadata sidecar is the *cross-restart* version of the
      same skip. The previous backend stamped the YAML's
      mtime alongside ``time.time()``; a fresh start that
      finds those two intact and within
      ``_REGEN_FAILURE_TTL_SECONDS`` short-circuits without
      spawning another ``esphome compile`` on the same broken
      config. The check itself runs in an executor so the
      per-device ``stat()`` and metadata read don't stall the
      event loop on a fleet-wide cold start. Two retry
      signals release the guard:

      * The user edits the YAML — its mtime moves past the
        stamp, so the equality check fails naturally.
      * The TTL elapses — covers transient external problems
        (git package server flaky, DNS hiccup) where the
        user shouldn't have to touch the YAML to recover.
    * ``_regenerate_lock`` serialises the subprocess itself so we
      never spawn more than one esphome compile at a time.

    Fire-and-forget: a follow-up ``_scanner.reload(configuration)``
    on success picks up the new storage and re-emits a
    ``DEVICE_UPDATED`` event so the frontend reflects the new
    address / integrations.
    """
    if not controller._esphome_cmd:
        return  # ``start()`` hasn't run yet — skip the regenerate.
    if configuration in controller._regenerate_pending:
        return  # already scheduled, don't queue a duplicate.
    if configuration in controller._regenerate_failed:
        # Last attempt this session failed and the YAML hasn't
        # changed since; rerunning would produce the same error.
        return

    # Mark synchronously so two same-tick ``schedule()`` calls
    # don't both pass the dedupe check above (the coroutine
    # body that used to do the add only runs after the loop
    # yields, leaving a race window where a second call would
    # queue a duplicate task; the lock further down serialises
    # the subprocess but both runs would still pay the
    # failure-stamp read + reload). Discard in ``_run``'s
    # finally below so a single in-flight regen still clears
    # cleanly.
    controller._regenerate_pending.add(configuration)
    controller._db.create_background_task(_run(controller, configuration))


async def _run(controller: DevicesController, configuration: str) -> None:
    try:
        # Cross-restart skip: the previous backend persisted
        # the YAML's mtime + wall-clock when the regen
        # failed. If the file hasn't been touched since
        # *and* the failure stamp is still within the TTL,
        # replay would fail the same way — turn it into a
        # no-op. The check itself batches its disk reads
        # into one executor hop. Routed through the
        # controller's bound delegates so tests that patch
        # any of the four async helpers on the class still
        # intercept.
        if await controller._regen_already_failed_recently_async(configuration):
            controller._regenerate_failed.add(configuration)
            return
        async with controller._regenerate_lock:
            success = await controller._spawn_only_generate(configuration)
        if success:
            # ``--only-generate`` writes build_info.json
            # with the canonical config_hash before
            # exiting, same as a real compile. The single
            # executor hop below reads that hash and
            # writes the sidecar in one transaction, also
            # clearing the regen-failure stamp now that
            # the YAML generates cleanly.
            await controller._finalize_regen_success(configuration)
            await controller._scanner.reload(configuration)
        else:
            controller._regenerate_failed.add(configuration)
            await controller._stamp_regen_failure(configuration)
    finally:
        controller._regenerate_pending.discard(configuration)


async def spawn_only_generate(controller: DevicesController, configuration: str) -> bool:
    """Run ``esphome compile --only-generate`` once. Return True iff exit-0.

    Both failure modes (spawn raised, or the subprocess exited
    non-zero) get logged at debug and produce ``False`` so the
    caller takes the same persist-failure-stamp branch in
    either case. Pulled out of :func:`_run` so the two failure
    paths don't have to duplicate the marker-set + persist
    sequence.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*controller._esphome_cmd, "--dashboard", "compile", "--only-generate", config_path]
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except Exception:
        _LOGGER.debug("Storage regenerate spawn failed for %s", configuration, exc_info=True)
        return False
    if proc.returncode != 0:
        _LOGGER.debug(
            "Storage regenerate for %s exited %s: %s",
            configuration,
            proc.returncode,
            stderr.decode(errors="replace").strip()[:500],
        )
        return False
    return True


async def already_failed_recently_async(controller: DevicesController, configuration: str) -> bool:
    """Return True iff the persisted failure stamp is unchanged-and-fresh.

    Both halves have to hold for the guard to fire:

    * The YAML's current ``stat.st_mtime`` equals the cached
      ``regen_failed_mtime`` — same file as last time (any
      edit moves the mtime forward).
    * Less than ``_REGEN_FAILURE_TTL_SECONDS`` has elapsed
      since the cached ``regen_failed_at`` — covers transient
      external causes (git package server, DNS, ESPHome
      mid-flight) by allowing a re-check after the TTL.

    Disk reads (``Path.stat``, the ``.device-builder.json``
    parse) batch into a single executor job so a cold-start
    fleet sweep neither stalls the event loop nor double-books
    the default thread pool. A negative age (clock skew, NTP
    step, future-dated stamp) clamps to zero; without that
    clamp a bad sidecar value could lock out the regen
    indefinitely.
    """
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    config_path = controller._db.settings.rel_path(configuration)

    def _read() -> tuple[float, dict[str, Any]] | None:
        # One executor hop for both reads — paying for two
        # parallel ``run_in_executor`` jobs would just consume
        # two slots in the shared default thread pool for work
        # that's already serial on disk anyway.
        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            return None
        return mtime, get_device_metadata(config_dir, configuration)

    result = await loop.run_in_executor(None, _read)
    if result is None:
        return False
    current_mtime, md = result
    cached_mtime = md.get("regen_failed_mtime")
    cached_at = md.get("regen_failed_at")
    if not cached_mtime or not cached_at:
        return False
    try:
        mtime_matches = float(cached_mtime) == current_mtime
        age = max(0.0, time.time() - float(cached_at))
    except (TypeError, ValueError):
        return False
    return mtime_matches and age < _REGEN_FAILURE_TTL_SECONDS


async def stamp_failure(controller: DevicesController, configuration: str) -> None:
    """Persist the cross-restart "we already tried, gave up" marker — one executor hop.

    Combines the YAML ``stat()`` and the sidecar write into a
    single closure handed to ``run_in_executor``. The earlier
    standalone-stamp shape took two hops (one to stat, one to
    write); on a fleet-wide cold-start each saved hop is a
    thread-pool slot back to the pool.

    The wall-clock half is sampled inside the closure too, so
    the stamp captures the same instant the file's mtime was
    observed instead of straddling a hop.
    """
    config_dir = controller._db.settings.config_dir
    config_path = controller._db.settings.rel_path(configuration)

    def _stamp() -> None:
        try:
            mtime = config_path.stat().st_mtime
        except OSError:
            return  # file vanished mid-regen; nothing useful to stamp
        set_device_metadata(
            config_dir,
            configuration,
            regen_failed_mtime=mtime,
            regen_failed_at=time.time(),
        )

    await asyncio.get_running_loop().run_in_executor(None, _stamp)


async def finalize_success(controller: DevicesController, configuration: str) -> None:
    """Read the post-only-generate hash and clear the failure stamp — one executor hop.

    Used to be three separate awaits — read ``build_info.json``,
    write the hash, write the cleared regen stamp — totalling
    three executor hops and two sidecar transactions. The
    closure here folds them together: one ``read_build_info_hash``
    call, one ``set_device_metadata`` transaction that writes
    ``expected_config_hash`` and clears
    ``regen_failed_mtime`` / ``regen_failed_at`` atomically.

    See :meth:`DevicesController._persist_expected_config_hash` for the
    rationale on why the hash is read off ``build_info.json`` rather
    than recomputed in-process — a missing / malformed file is
    unexpected on this code path so the warning log lives there.
    """
    config_dir = controller._db.settings.config_dir
    yaml_path = controller._db.settings.rel_path(configuration)

    def _finalize() -> str | None:
        new_hash = read_build_info_hash(yaml_path)
        kwargs: dict[str, Any] = {
            "regen_failed_mtime": 0.0,
            "regen_failed_at": 0.0,
        }
        if new_hash:
            kwargs["expected_config_hash"] = new_hash
        set_device_metadata(config_dir, configuration, **kwargs)
        return new_hash

    new_hash = await asyncio.get_running_loop().run_in_executor(None, _finalize)
    if not new_hash:
        _LOGGER.warning(
            "Could not read config_hash from build_info.json for %s — "
            "the drawer's Local hash may stay stale until the next flash. "
            "If this persists across compiles, check that ESPHome's "
            "build_info.json schema hasn't changed.",
            configuration,
        )
        return
    _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)
