"""
Mutable domain state for :class:`DevicesController`.

Grouping the controller's mutable state into a typed
:class:`DevicesState` dataclass keeps the sibling-module
helpers (``storage_regen``, ``scan_change``, ``validate``,
``logs``, ``importable``) honest: they reach through
``controller.state.X`` rather than ``controller._X`` /
``controller.X`` private attrs.

What lives here vs on the controller:

* **Here**: every attr that mutates after ``__init__``
  (``esphome_cmd`` is rewritten in ``start()``; the discovery
  / regen dicts and sets mutate as devices are observed and
  YAMLs are regenerated; ``import_result`` and
  ``ignored_devices`` mutate as discovery events arrive).
* **On the controller**: ``_db``, base infrastructure,
  service refs constructed in ``__init__`` and never
  reassigned (``_scanner``, ``_state_monitor``,
  ``_reachability``, ``_build_size``, ``_mqtt_coordinator``,
  ``_yaml_search_cache``, ``_yaml_search_lock``,
  ``_regenerate_lock``), bound-method delegates,
  ``@api_command`` WS methods. ``_unsub_job_completed`` also
  stays on the controller — only ``start()`` / ``stop()``
  touch it and the sibling modules don't reach in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ...models import AdoptableDevice


@dataclass
class RegenState:
    """
    Bookkeeping for background ``--only-generate`` runs.

    ``pending`` dedupes in-flight schedules; ``retry_timers`` holds the
    armed backoff retries. Failure history (attempt count, stamp) lives
    in the metadata store; ``unreadable_strikes`` counts stat failures
    the stamp can't key (no mtime to record against).
    """

    pending: set[str] = field(default_factory=set)
    retry_timers: dict[str, asyncio.TimerHandle] = field(default_factory=dict)
    unreadable_strikes: dict[str, int] = field(default_factory=dict)

    def arm_retry(self, configuration: str, handle: asyncio.TimerHandle) -> None:
        """Install *handle* as the pending retry, cancelling any prior one."""
        existing = self.retry_timers.pop(configuration, None)
        if existing is not None:
            existing.cancel()
        self.retry_timers[configuration] = handle

    def cancel_retry(self, configuration: str) -> bool:
        """Cancel and drop *configuration*'s retry timer; True iff one was armed."""
        handle = self.retry_timers.pop(configuration, None)
        if handle is None:
            return False
        handle.cancel()
        return True

    def cancel_all_retry_timers(self) -> None:
        """Cancel every pending retry timer."""
        for handle in self.retry_timers.values():
            handle.cancel()
        self.retry_timers.clear()


@dataclass
class DevicesState:
    """Mutable state for :class:`DevicesController`."""

    # ``esphome`` CLI invocation discovered at ``start()`` —
    # ``[sys.executable, "-m", "esphome"]`` or the on-PATH
    # ``esphome`` binary, whichever ``_find_esphome_cmd``
    # picks first.
    esphome_cmd: list[str] = field(default_factory=list)

    # Background ``--only-generate`` bookkeeping; the controller's
    # ``_regenerate_lock`` (kept on the controller) serialises the
    # actual subprocess.
    regen: RegenState = field(default_factory=RegenState)

    # Discovery / import state. Keyed by ``device.name`` so the
    # WebSocket layer and ``devices/ignore`` can address entries
    # without juggling full mDNS service-instance names.
    import_result: dict[str, AdoptableDevice] = field(default_factory=dict)
    ignored_devices: set[str] = field(default_factory=set)
