"""
Benchmarks for the peer-link Noise XX session.

Profiles the pure-Python wrapper (:class:`PeerLinkNoiseSession`)
around ``noiseprotocol`` so a CodSpeed flamegraph can attribute
CPU time to user code rather than collapsing into runtime
internals. Three slices of the same transaction so a regression
shows up against the matching slice:

* **construction** — ``initiator()`` / ``responder()``; how much
  the per-session setup costs.
* **handshake** — the 3 XX messages, no payload.
* **transport** — one ``encrypt`` + one ``decrypt`` round-trip
  post-handshake at a small frame (~1 KiB) and a large frame
  (near Noise's 65535-byte ciphertext limit).
* **full transaction** — construction + handshake + 1 KiB
  encrypt/decrypt. This is the headline number — what a single
  Noise XX exchange costs end-to-end.

All sync, in-process. No sockets, no asyncio.
"""

from __future__ import annotations

import os

import pytest
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.helpers.peer_link_noise import PeerLinkNoiseSession


def _fresh_keypairs() -> tuple[bytes, bytes]:
    return os.urandom(32), os.urandom(32)


def _completed_sessions(
    init_priv: bytes, resp_priv: bytes
) -> tuple[PeerLinkNoiseSession, PeerLinkNoiseSession]:
    """Drive a fresh XX handshake to completion and return both sides."""
    init = PeerLinkNoiseSession.initiator(init_priv)
    resp = PeerLinkNoiseSession.responder(resp_priv)
    m1 = init.write_handshake_message(b"")
    resp.read_handshake_message(m1)
    m2 = resp.write_handshake_message(b"")
    init.read_handshake_message(m2)
    m3 = init.write_handshake_message(b"")
    resp.read_handshake_message(m3)
    return init, resp


def test_xx_session_construction(benchmark: BenchmarkFixture) -> None:
    """Per-side construction: ``from_name`` + keypair + ``start_handshake``."""
    init_priv, resp_priv = _fresh_keypairs()

    @benchmark
    def run() -> None:
        PeerLinkNoiseSession.initiator(init_priv)
        PeerLinkNoiseSession.responder(resp_priv)


def test_xx_handshake_no_payload(benchmark: BenchmarkFixture) -> None:
    """The 3 XX messages back-to-back; both sides finish ``handshake_finished=True``."""
    init_priv, resp_priv = _fresh_keypairs()

    @benchmark
    def run() -> None:
        init = PeerLinkNoiseSession.initiator(init_priv)
        resp = PeerLinkNoiseSession.responder(resp_priv)
        m1 = init.write_handshake_message(b"")
        resp.read_handshake_message(m1)
        m2 = resp.write_handshake_message(b"")
        init.read_handshake_message(m2)
        m3 = init.write_handshake_message(b"")
        resp.read_handshake_message(m3)


@pytest.mark.parametrize(
    "payload_size",
    [
        pytest.param(1024, id="1KiB"),
        # Noise's max ciphertext is 65535; leave headroom for the
        # 16-byte Poly1305 tag the responder appends.
        pytest.param(65000, id="65KiB"),
    ],
)
def test_xx_transport_encrypt_decrypt(benchmark: BenchmarkFixture, payload_size: int) -> None:
    """One ``encrypt`` + one ``decrypt`` post-handshake at *payload_size* bytes."""
    init_priv, resp_priv = _fresh_keypairs()
    init, resp = _completed_sessions(init_priv, resp_priv)
    plaintext = os.urandom(payload_size)

    @benchmark
    def run() -> None:
        ciphertext = init.encrypt(plaintext)
        recovered = resp.decrypt(ciphertext)
        assert len(recovered) == payload_size


def test_xx_full_transaction(benchmark: BenchmarkFixture) -> None:
    """Construction + handshake + one 1 KiB encrypt/decrypt — single end-to-end XX exchange."""
    init_priv, resp_priv = _fresh_keypairs()
    payload = os.urandom(1024)

    @benchmark
    def run() -> None:
        init = PeerLinkNoiseSession.initiator(init_priv)
        resp = PeerLinkNoiseSession.responder(resp_priv)
        m1 = init.write_handshake_message(b"")
        resp.read_handshake_message(m1)
        m2 = resp.write_handshake_message(b"")
        init.read_handshake_message(m2)
        m3 = init.write_handshake_message(b"")
        resp.read_handshake_message(m3)
        ciphertext = init.encrypt(payload)
        resp.decrypt(ciphertext)
