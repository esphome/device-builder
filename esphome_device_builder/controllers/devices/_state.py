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

from dataclasses import dataclass, field

from ...helpers.timer_registry import TimerRegistry
from ...models import AdoptableDevice


@dataclass
class RegenState:
    """
    Bookkeeping for background ``--only-generate`` runs.

    ``pending`` dedupes in-flight schedules, ``failed`` blocks retries
    until the YAML changes, and the retry fields drive the bounded
    escalating retry between a failure and the disk stamp.
    """

    pending: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    retry_timers: TimerRegistry[str] = field(default_factory=TimerRegistry)
    retry_strikes: dict[str, int] = field(default_factory=dict)

    def reset(self, configuration: str) -> None:
        """Drop *configuration*'s failure marker, retry timer, and strikes."""
        self.failed.discard(configuration)
        self.retry_timers.discard(configuration)
        self.retry_strikes.pop(configuration, None)

    def cancel_all_retry_timers(self) -> list[str]:
        """Cancel every pending retry timer, drop the strikes; return the armed keys."""
        armed = self.retry_timers.cancel_all()
        self.retry_strikes.clear()
        return armed


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

    # Post-flash Native-API version re-probe timers, armed and consumed
    # by ``firmware_sync``; ``stop()`` mass-cancels.
    reprobe_timers: TimerRegistry[str] = field(default_factory=TimerRegistry)

    # Discovery / import state. Keyed by ``device.name`` so the
    # WebSocket layer and ``devices/ignore`` can address entries
    # without juggling full mDNS service-instance names.
    import_result: dict[str, AdoptableDevice] = field(default_factory=dict)
    ignored_devices: set[str] = field(default_factory=set)
