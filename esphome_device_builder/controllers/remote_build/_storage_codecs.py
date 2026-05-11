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

from ...helpers.json import dumps as json_dumps
from ...helpers.json import loads as json_loads
from ...models import OffloaderRemoteBuildSettings, ReceiverPeers

_LOGGER = logging.getLogger(__name__)

# Sibling of ``.device-builder.json`` in ``config_dir`` rather
# than a sub-key of it: per-domain atomicity, no lock contention
# against unrelated writers, and matches HA's per-file ``Store``
# shape. Leading dot keeps the files out of normal directory
# listings on the user's editor pane.
OFFLOADER_PAIRINGS_FILE = ".offloader_pairings.json"
RECEIVER_PEERS_FILE = ".receiver_peers.json"


def encode_pairings(value: OffloaderRemoteBuildSettings) -> bytes:
    """Serialise the offloader-side pairings shape for the store."""
    return json_dumps(value.to_dict())


def decode_pairings(raw: bytes) -> OffloaderRemoteBuildSettings:
    """Decode the offloader-side pairings shape from the store.

    Defaults on a malformed blob rather than crashing dashboard
    startup. The ``Store`` lets decoder errors propagate so a
    consumer can pick the recovery posture; here we want
    "soft-recover to empty" because a corrupt pairings file
    means every offloader has to re-pair (annoying) but isn't
    fatal, whereas crashing the dashboard would lock the user
    out entirely.
    """
    try:
        return OffloaderRemoteBuildSettings.from_dict(json_loads(raw))
    except Exception:
        _LOGGER.exception("Corrupt offloader pairings file; resetting to empty")
        return OffloaderRemoteBuildSettings()


def encode_peers(value: ReceiverPeers) -> bytes:
    """Serialise the receiver-side peers shape for the store."""
    return json_dumps(value.to_dict())


def decode_peers(raw: bytes) -> ReceiverPeers:
    """Decode the receiver-side peers shape from the store.

    Soft-recover to empty on malformed blobs, mirror of
    :func:`decode_pairings`. A corrupt peers file means every
    paired offloader has to re-pair — annoying, not fatal — so
    crashing dashboard startup is the wrong recovery posture.
    """
    try:
        return ReceiverPeers.from_dict(json_loads(raw))
    except Exception:
        _LOGGER.exception("Corrupt receiver peers file; resetting to empty")
        return ReceiverPeers()
