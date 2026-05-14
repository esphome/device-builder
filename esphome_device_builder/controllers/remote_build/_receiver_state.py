"""
Mutable domain state for :class:`ReceiverController`.

Receiver-side counterpart to :class:`OffloaderState` in
``_state.py``. Single-file controller today; the State
extraction is upfront so when the eventual sibling-module
split happens, sibling helpers reach through
``controller.state.X`` from PR 1 instead of relitigating the
abstraction post-split.

What lives here vs on the controller:

* **Here**: every attr that mutates after ``__init__``
  (pairing-window dicts + handle, pending / approved peer
  registries, peer-link session table, the rotation
  in-flight flag, the lifecycle service refs that
  ``start()`` constructs and ``stop()`` clears).
* **On the controller**: ``_db``, base infrastructure
  (``_listeners``, ``_tasks``, ``_shutdown_callbacks``),
  ``_peers_store`` (constructed once in ``__init__``,
  never reassigned).
"""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from dataclasses import dataclass, field

from ...models import StoredPeer
from .artifacts_download import ArtifactsDownloadSender
from .job_fanout import JobFanout
from .peer_link import PeerLinkSession
from .submit_job import SubmitJobReceiver


@dataclass
class ReceiverState:
    """Mutable state for :class:`ReceiverController`."""

    # True while ``rotate_identity`` is in flight. Second caller
    # gets ``ALREADY_EXISTS`` rather than queuing — interleaved
    # teardowns can leave no listener at all, and back-to-back
    # rotation is almost always an accidental double-click.
    rotation_in_flight: bool = False

    # Pairing window: gates ``pair_request``, refcounted by WS
    # client so multi-tab admins extend together.
    pairing_window_clients: dict[Hashable, float] = field(default_factory=dict)
    pairing_window_handle: asyncio.TimerHandle | None = None

    # PENDING StoredPeer rows keyed on ``dashboard_id``; never
    # persisted, cleared on window auto-close.
    pending_peers: dict[str, StoredPeer] = field(default_factory=dict)
    # RAM-canonical APPROVED peers keyed on ``dashboard_id``;
    # disk is just persistence.
    approved_peers: dict[str, StoredPeer] = field(default_factory=dict)
    peer_link_sessions: dict[str, PeerLinkSession] = field(default_factory=dict)

    # Receiver-side handlers; constructed in
    # :meth:`ReceiverController.start` once the firmware
    # controller is available.
    submit_job_receiver: SubmitJobReceiver | None = None
    artifacts_download_sender: ArtifactsDownloadSender | None = None
    job_fanout: JobFanout | None = None
