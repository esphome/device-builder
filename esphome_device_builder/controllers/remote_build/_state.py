"""
Mutable domain state for :class:`OffloaderController`.

Grouping the controller's mutable state into a typed
:class:`OffloaderState` dataclass keeps the sibling-module
helpers (``discovery``, ``rebind``, ``peer_link_lifecycle``,
``pair_status``, ``submit_job_commands``, ``pair_commands``,
``settings_commands``, ``bus_handlers``) honest: they reach
through ``controller.state.X`` rather than a long tail of
``controller._X`` private attrs.

What lives here vs on the controller:

* **Here**: every attr that mutates after ``__init__``
  (domain dicts/sets, ``remote_builds_enabled``, identity /
  resolver / browser refs that ``start()`` populates and
  ``stop()`` clears).
* **On the controller**: ``_db``, ``_listeners``, ``_tasks``,
  ``_shutdown_callbacks`` (base infrastructure),
  ``_pairings_store`` (constructed once, never reassigned),
  bound-method delegates, ``@api_command`` WS methods,
  snapshot methods.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from zeroconf.asyncio import AsyncServiceBrowser

from ...helpers.peer_link_resolver import PeerLinkDNSResolver
from ...models import (
    OffloaderAlertSnapshotEntry,
    OffloaderRemoteJobSnapshotEntry,
    PeerQueueStatusSnapshotEntry,
    RemoteBuildPeer,
    StoredPairing,
)
from ._models import PeerLinkClientHandle


@dataclass
class OffloaderState:
    """Mutable state for :class:`OffloaderController`."""

    # Pairing + peer-link domain state.
    pairings: dict[str, StoredPairing] = field(default_factory=dict)
    peers: dict[str, RemoteBuildPeer] = field(default_factory=dict)
    peer_link_clients: dict[str, PeerLinkClientHandle] = field(default_factory=dict)
    pair_status_listeners: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    open_peer_links: set[str] = field(default_factory=set)
    offloader_alerts: dict[str, OffloaderAlertSnapshotEntry] = field(default_factory=dict)
    peer_queue_status: dict[str, PeerQueueStatusSnapshotEntry] = field(default_factory=dict)
    offloader_remote_jobs: dict[str, OffloaderRemoteJobSnapshotEntry] = field(default_factory=dict)
    rebind_probe_until: dict[str, float] = field(default_factory=dict)
    remote_builds_enabled: bool = True

    # Identity / discovery refs reassigned during start() / discovery.
    # ``offloader_peer_link_priv`` and ``offloader_dashboard_id`` are
    # cached in :meth:`OffloaderController.start`; WS-command handlers
    # re-read identities from disk via
    # :meth:`OffloaderController._load_offloader_identities_async` to
    # pick up rotations without invalidating the cache.
    offloader_peer_link_priv: bytes | None = None
    offloader_dashboard_id: str | None = None
    peer_link_resolver: PeerLinkDNSResolver | None = None
    own_instance_name: str | None = None
    browser: AsyncServiceBrowser | None = None
