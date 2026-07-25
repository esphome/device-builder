"""Shared size cap for esphome config bundles.

One number, referenced by every path that materialises a whole config
bundle in memory, so the limits can't drift apart: the peer-link
remote-build receiver (assembles the uploaded bundle) and the UI
``import_bundle`` path (decodes the uploaded bundle). Because they share
this constant, a config small enough to remote-build is always small
enough to import, and vice versa.
"""

from __future__ import annotations

# Hard cap on a compressed esphome config bundle (the gzipped ``.tar.gz``
# ``ConfigBundleCreator`` emits). Most bundles are a few KiB, but
# image/font-heavy include trees (many ``mdi:`` icons resized into embedded
# ``BINARY`` images) have been observed near ~40 MiB in the wild. 128 MiB
# leaves generous headroom while still bounding the RAM a single in-flight
# bundle can pin (peak is ~2x the cap while a copy is held). A peer-link
# receiver enforces this cap, so an offloader only benefits once the build
# server runs a version carrying the larger value.
BUNDLE_MAX_TOTAL_BYTES = 128 * 1024 * 1024
