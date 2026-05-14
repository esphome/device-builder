"""Mutable domain state for :class:`FirmwareController`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ...models import FirmwareJob


@dataclass
class FirmwareState:
    """Mutable state for :class:`FirmwareController`."""

    # ``esphome`` CLI invocation discovered at ``start()`` —
    # ``[sys.executable, "-m", "esphome"]`` or the on-PATH
    # ``esphome`` binary, whichever ``_find_esphome_cmd``
    # picks first. ``cli`` reads it to build the subprocess argv.
    esphome_cmd: list[str] = field(default_factory=list)

    # Persistent single-job queue. Producer is ``_enqueue``;
    # consumer is the runner loop. Survives restarts via the
    # on-disk persistence layer.
    queue: asyncio.Queue[FirmwareJob] = field(default_factory=asyncio.Queue)

    # Active + recent jobs keyed by ``job_id``. ``persistence``
    # reads / writes on every state transition; ``clean``,
    # ``follow``, ``jobs``, ``factories``, and ``lifecycle`` read
    # for lookup. Trimmed to history limits by
    # ``persistence._prune_history``.
    jobs: dict[str, FirmwareJob] = field(default_factory=dict)

    # Single-job runner slot — ``None`` when idle. ``runner``
    # reads / writes as the job lifecycle transitions through
    # the queue.
    current_job: FirmwareJob | None = None
    current_process: asyncio.subprocess.Process | None = None

    # Job ids the user asked to cancel; the runner consults this
    # on subprocess exit to mark CANCELLED instead of FAILED.
    cancel_requested: set[str] = field(default_factory=set)

    # Per-job wake event for the remote runner — set by the
    # cancel handler so a remote job waiting on its terminal
    # frame unblocks instantly. The local subprocess path uses
    # SIGTERM instead and doesn't register here.
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)
