"""Mutable domain state for :class:`ReceiverController`."""

from __future__ import annotations

import asyncio
from collections.abc import Hashable
from dataclasses import dataclass, field

from ...models import StoredPeer
from .artifacts_download import ArtifactsDownloadSender
from .env_provisioner import EnvProvisioner
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

    # Armed by ``--remote-build-only`` first-pair bootstrap: while
    # True (and zero peers are APPROVED), the next ``pair_request``
    # inside the open window presenting ``bootstrap_pairing_key`` is approved
    # without the inbox dance. One-shot — ``record_pair_request``
    # disarms it on use. A wrong/missing key or disallowed source is
    # refused WITHOUT disarming, indistinguishably from a closed
    # window (docs/THREAT_MODEL.md).
    auto_approve_first_pair: bool = False
    # Pairing key printed in the bootstrap banner; armed and cleared
    # with the flag above. ``pair_flow`` fails closed when the flag is
    # armed without a key. Never persisted.
    bootstrap_pairing_key: str | None = None

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

    # Builds + caches per-version esphome venvs so a compile from an
    # offloader on a different esphome matches its version. Constructed
    # in :meth:`ReceiverController.start`; ``None`` before start / after stop.
    env_provisioner: EnvProvisioner | None = None
