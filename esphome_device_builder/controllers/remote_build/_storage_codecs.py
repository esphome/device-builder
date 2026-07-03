"""
Encode / decode helpers for the remote-build per-file stores.

The controller's two per-file :class:`~helpers.storage.Store`
instances — ``.offloader_pairings.json`` (offloader-side
APPROVED pairings) and ``.receiver_peers.json`` (receiver-side
APPROVED peers) — each thread a pair of sync codec callables
through the store's ``encoder=`` / ``decoder=`` kwargs. The
codecs are tiny but the soft-recover-on-corruption posture on
the decoder side (a corrupt file means every paired peer
re-pairs; a startup crash locks the user out of the dashboard
entirely) is load-bearing enough to live in its own seam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ...helpers.json import dumps as json_dumps
from ...helpers.json import loads as json_loads
from ...models import DashboardModel, OffloaderRemoteBuildSettings, ReceiverPeers

_LOGGER = logging.getLogger(__name__)

# Sibling of ``.device-builder.json`` in ``config_dir`` rather
# than a sub-key of it: per-domain atomicity, no lock contention
# against unrelated writers, and matches HA's per-file ``Store``
# shape. Leading dot keeps the files out of normal directory
# listings on the user's editor pane.
OFFLOADER_PAIRINGS_FILE = ".offloader_pairings.json"
RECEIVER_PEERS_FILE = ".receiver_peers.json"


def _soft_recover_codec[T: DashboardModel](
    model: type[T], label: str
) -> tuple[Callable[[T], bytes], Callable[[bytes], T]]:
    """Build an (encoder, decoder) pair for a model-backed store that soft-recovers.

    The decoder defaults to an empty ``model()`` on a malformed blob rather than
    letting the error propagate: a corrupt *label* file means every paired peer
    has to re-pair (annoying, not fatal), whereas crashing dashboard startup
    would lock the user out entirely.
    """

    def encode(value: T) -> bytes:
        return json_dumps(value.to_dict())

    def decode(raw: bytes) -> T:
        try:
            return model.from_dict(json_loads(raw))
        except Exception:
            _LOGGER.exception("Corrupt %s file; resetting to empty", label)
            return model()

    return encode, decode


encode_pairings, decode_pairings = _soft_recover_codec(
    OffloaderRemoteBuildSettings, "offloader pairings"
)
encode_peers, decode_peers = _soft_recover_codec(ReceiverPeers, "receiver peers")
