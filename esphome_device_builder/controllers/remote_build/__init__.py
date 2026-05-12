"""
Remote-build feature — public surface.

The role split: two sibling controllers, one per direction of
the offloader↔receiver protocol, with disjoint state and no
cross-references between them.

- ``offloader`` — :class:`OffloaderController`: outbound side
  (pair flow, peer-link clients, ``submit_job`` /
  ``cancel_job`` / ``download_artifacts``, mDNS host
  discovery, offloader-side alerts + snapshots, master
  ``remote_builds_enabled`` toggle).
- ``receiver`` — :class:`ReceiverController`: inbound side
  (peer-link session registry, ``record_pair_request`` /
  ``approve_peer`` / ``remove_peer``, ``queue_status``
  fan-out, identity ``get`` / ``rotate``, cleanup sweep).
- ``_shared`` — module-level helpers used by both
  (:func:`drain_tasks`).
- ``peer_link`` — receiver-side Noise XX handler
  (:func:`make_peer_link_handler`, :class:`PeerLinkSession`,
  :class:`PeerLinkChannel`).
- ``peer_link_client`` — offloader-side initiator
  (:class:`PeerLinkClient`, :func:`preview_pair` /
  :func:`request_pair` / :func:`await_pair_status`).
- ``submit_job`` — receiver-side ``submit_job`` accept path.
- ``job_fanout`` — receiver-side fan-out of firmware
  ``JOB_*`` events to peer-link frames.

External callers reaching into specific submodules use the
submodule path directly (e.g.
``from .controllers.remote_build.peer_link import PEER_LINK_PATH``);
this ``__init__`` only re-exports the controller classes.
"""

from __future__ import annotations

from .offloader import OffloaderController
from .receiver import ReceiverController

__all__ = ["OffloaderController", "ReceiverController"]
