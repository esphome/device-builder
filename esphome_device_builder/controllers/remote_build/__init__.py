"""
Remote-build feature controller package — public surface.

Re-exports :class:`RemoteBuildController` so existing
``from .controllers.remote_build import RemoteBuildController``
imports keep resolving after the package split. Submodules:

- ``controller`` — :class:`RemoteBuildController` itself: pair
  flow, peer-link session registry, queue_status fan-out wiring,
  offloader-side alerts, mDNS host discovery, all WS commands
  under the ``remote_build/`` namespace.
- ``peer_link`` — receiver-side Noise XX handler
  (:func:`make_peer_link_handler`, :class:`PeerLinkSession`,
  :class:`PeerLinkChannel`, the receive loop, heartbeat).
- ``peer_link_client`` — offloader-side initiator
  (:class:`PeerLinkClient`, :func:`drive_initiator_round_trip`,
  :func:`preview_pair` / :func:`request_pair` /
  :func:`await_pair_status`).
- ``submit_job`` — receiver-side ``submit_job`` accept path
  (5c-2a): :class:`SubmitJobReceiver`, bundle assembly →
  ``prepare_bundle_for_compile`` → ``FirmwareJob`` queue.
- ``job_fanout`` — receiver-side fan-out (5c-2b) of firmware
  ``JOB_*`` events to ``job_state_changed`` / ``job_output``
  peer-link frames.

External callers reaching into specific submodules use the
submodule path directly (e.g.
``from .controllers.remote_build.peer_link import PEER_LINK_PATH``);
this ``__init__`` only re-exports the controller class to
match the :mod:`controllers.firmware` shape.
"""

from __future__ import annotations

from .controller import RemoteBuildController

__all__ = ["RemoteBuildController"]
