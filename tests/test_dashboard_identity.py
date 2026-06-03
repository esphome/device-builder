"""
Tests for the dashboard identity helper.

The helper bundles two persistent values: the X25519 peer-link
key's ``pin_sha256`` (which paired offloaders pin against during
the Noise handshake) and the stable ``dashboard_id`` correlation
token in the metadata sidecar's ``_dashboard_identity`` block. The
X25519 key drives both the actual peer-link authentication AND
the displayed fingerprint. Coverage here pins that:

* The compose path returns a struct whose ``pin_sha256`` matches
  the X25519 peer-link helper's output (i.e. no divergence
  between what the UI shows and what offloaders observe).
* ``dashboard_id`` is generated once on first read, persisted
  under ``_dashboard_identity.dashboard_id``, idempotent across
  calls, preserved across rotations, and migrated out of the
  legacy ``_remote_build`` co-tenancy without a re-mint.
* The metadata sidecar's fail-safe paths (missing key, non-dict
  block, corrupt JSON) all land on a fresh dashboard_id without
  crashing.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

from esphome_device_builder.controllers.config.remote_build_settings import (
    has_remote_build_settings_persisted,
    remote_build_settings_transaction,
    save_remote_build_settings,
)
from esphome_device_builder.helpers.dashboard_identity import (
    DASHBOARD_ID_MAX_CHARS,
    DASHBOARD_ID_PATTERN,
    DashboardIdentity,
    _get_or_create_dashboard_id,
    get_or_create_identity,
    rotate_identity,
)
from esphome_device_builder.helpers.peer_link_identity import (
    PeerLinkIdentityStore,
)
from esphome_device_builder.models import RemoteBuildSettings


def _read_metadata(config_dir: Path) -> dict:
    return json.loads((config_dir / ".device-builder.json").read_bytes())


async def test_first_call_generates_and_persists_identity(tmp_path: Path) -> None:
    """Fresh config dir → X25519 key, and dashboard_id all created."""
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))

    assert isinstance(identity, DashboardIdentity)
    assert identity.dashboard_id  # non-empty
    # The X25519 peer-link key file is what actually drives the
    # Noise handshake; verify it landed on disk so a subsequent
    # bind picks it up.
    assert (tmp_path / ".device-builder-peer-link-key.bin").exists()
    # ``dashboard_id`` lands in its own ``_dashboard_identity`` block.
    metadata = _read_metadata(tmp_path)
    assert metadata["_dashboard_identity"]["dashboard_id"] == identity.dashboard_id


async def test_pin_sha256_matches_peer_link_identity(tmp_path: Path) -> None:
    """The displayed ``pin_sha256`` is the X25519 public key's SHA-256.

    Load-bearing contract: the UI fingerprint MUST match what
    paired offloaders observe during the Noise handshake. A
    divergence here was the original bug that motivated this
    helper's rewrite — the UI displayed a dormant Ed25519 cert's
    SPKI hash while peers verified the X25519 peer-link key's
    hash on the wire.
    """
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    peer_link = await PeerLinkIdentityStore(tmp_path).async_load()
    assert identity.pin_sha256 == peer_link.pin_sha256


async def test_second_call_returns_identical_identity(tmp_path: Path) -> None:
    """Idempotent: post-generation, every call returns the same bytes."""
    first = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    second = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert first == second


async def test_pin_sha256_is_lowercase_hex_64_chars(tmp_path: Path) -> None:
    """SHA-256 fingerprint is 64 lowercase hex chars."""
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert len(identity.pin_sha256) == 64
    assert identity.pin_sha256 == identity.pin_sha256.lower()
    assert all(c in "0123456789abcdef" for c in identity.pin_sha256)


async def test_pin_sha256_formatted_groups_in_pairs(tmp_path: Path) -> None:
    """Display form groups the hex into space-separated byte pairs."""
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    formatted = identity.pin_sha256_formatted
    parts = formatted.split(" ")
    assert len(parts) == 32
    assert all(len(p) == 2 for p in parts)
    # Round-trip: stripping spaces yields the bare form.
    assert formatted.replace(" ", "") == identity.pin_sha256


def test_concurrent_dashboard_id_generation_is_serialised(tmp_path: Path) -> None:
    """Concurrent metadata-sidecar callers land on the same id.

    The dashboard_id JSON write is guarded by
    ``metadata_transaction``'s ``_METADATA_LOCK``; this test
    drives the sync helper directly from threads to pin that
    contract independently of the asyncio layer.
    """
    results: list[str] = []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()
        results.append(_get_or_create_dashboard_id(tmp_path))

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1, results


async def test_concurrent_async_get_or_create_returns_one_id(tmp_path: Path) -> None:
    """Concurrent ``get_or_create_identity`` coroutines land on the same id."""
    store = PeerLinkIdentityStore(tmp_path)
    results = await asyncio.gather(*(get_or_create_identity(tmp_path, store) for _ in range(4)))
    assert len({identity.dashboard_id for identity in results}) == 1


async def test_rotate_identity_keeps_dashboard_id(tmp_path: Path) -> None:
    """``rotate_identity`` swaps the X25519 key but preserves the id."""
    first = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    rotated = await rotate_identity(tmp_path, PeerLinkIdentityStore(tmp_path))

    assert rotated.dashboard_id == first.dashboard_id
    assert rotated.pin_sha256 != first.pin_sha256


async def test_rotate_identity_persists_to_disk(tmp_path: Path) -> None:
    """A subsequent ``get_or_create_identity`` call returns the rotated values."""
    rotated = await rotate_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    next_call = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert next_call == rotated


async def test_dashboard_id_survives_other_remote_build_mutations(tmp_path: Path) -> None:
    """Writing the disjoint ``_remote_build`` block doesn't disturb ``dashboard_id``."""
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))

    # Another phase writes the settings block (a separate key now).
    metadata_path = tmp_path / ".device-builder.json"
    data = json.loads(metadata_path.read_bytes())
    data["_remote_build"] = {"enabled": True}
    metadata_path.write_bytes(json.dumps(data).encode())

    second = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert second.dashboard_id == identity.dashboard_id


async def test_remote_build_settings_writes_preserve_foreign_keys(tmp_path: Path) -> None:
    """Both settings writers merge into ``_remote_build``, preserving a foreign key."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(json.dumps({"_remote_build": {"_sentinel": "keep-me"}}).encode())

    # Hop to a thread: the writers do sync metadata I/O that
    # blockbuster (Linux CI) flags when called on the event loop.
    await asyncio.to_thread(
        save_remote_build_settings, tmp_path, RemoteBuildSettings(enabled=False)
    )
    block = _read_metadata(tmp_path)["_remote_build"]
    assert block["_sentinel"] == "keep-me"
    assert block["enabled"] is False

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as settings:
            settings.enabled = True

    await asyncio.to_thread(_enable)
    block = _read_metadata(tmp_path)["_remote_build"]
    assert block["_sentinel"] == "keep-me"
    assert block["enabled"] is True


async def test_legacy_dashboard_id_migrates_out_of_remote_build(tmp_path: Path) -> None:
    """A legacy ``_remote_build.dashboard_id`` migrates to its own block with the same value."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(
        json.dumps({"_remote_build": {"dashboard_id": "legacy-id", "enabled": True}}).encode()
    )

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id == "legacy-id"

    metadata = _read_metadata(tmp_path)
    assert metadata["_dashboard_identity"]["dashboard_id"] == "legacy-id"
    assert "dashboard_id" not in metadata["_remote_build"]
    assert metadata["_remote_build"]["enabled"] is True


async def test_legacy_dashboard_id_swept_even_when_new_block_wins(tmp_path: Path) -> None:
    """A stale legacy id is removed even if ``_dashboard_identity`` already answers."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(
        json.dumps(
            {
                "_dashboard_identity": {"dashboard_id": "current-id"},
                "_remote_build": {"dashboard_id": "stale-id", "enabled": True},
            }
        ).encode()
    )

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id == "current-id"
    block = _read_metadata(tmp_path)["_remote_build"]
    assert "dashboard_id" not in block
    assert block["enabled"] is True


async def test_minting_dashboard_id_does_not_trip_settings_persisted_gate(tmp_path: Path) -> None:
    """Minting the identity doesn't flip ``has_remote_build_settings_persisted``."""
    await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    persisted = await asyncio.to_thread(has_remote_build_settings_persisted, tmp_path)
    assert persisted is False


async def test_legacy_dashboard_id_only_block_removed_on_migration(tmp_path: Path) -> None:
    """A ``_remote_build`` holding only a legacy id is dropped after migration."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(json.dumps({"_remote_build": {"dashboard_id": "legacy-id"}}).encode())

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id == "legacy-id"
    assert "_remote_build" not in _read_metadata(tmp_path)
    persisted = await asyncio.to_thread(has_remote_build_settings_persisted, tmp_path)
    assert persisted is False


async def test_init_after_id_only_mutation_preserves_other_fields(tmp_path: Path) -> None:
    """
    Writing ``_remote_build`` data BEFORE first identity init still works.

    Real-world path: a user flips the Settings toggle before the
    identity helper has ever run. The metadata sidecar already
    has ``_remote_build.enabled`` set; the identity init writes
    its own ``_dashboard_identity`` block and leaves that alone.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": {"enabled": true}}')

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    metadata = _read_metadata(tmp_path)
    assert metadata["_dashboard_identity"]["dashboard_id"] == identity.dashboard_id
    assert metadata["_remote_build"]["enabled"] is True


async def test_corrupt_metadata_does_not_block_generation(tmp_path: Path) -> None:
    """
    Garbage in the metadata sidecar regenerates a fresh ``dashboard_id``.

    The fallback writes a clean replacement; existing per-device
    metadata in the same file would also be lost in this case,
    but the dashboard_id is the load-bearing concern here. The
    metadata-corruption path is so rare in practice that an
    occasional reset is acceptable.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b"{ this isn't json")

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id  # generated fresh
    metadata = _read_metadata(tmp_path)
    assert metadata["_dashboard_identity"]["dashboard_id"] == identity.dashboard_id


async def test_non_dict_metadata_root_falls_back(tmp_path: Path) -> None:
    """A JSON list at the root (instead of a dict) falls back to defaults."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b"[1, 2, 3]")

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id


async def test_non_dict_remote_build_value_falls_back(tmp_path: Path) -> None:
    """``_remote_build`` set to a non-dict value falls back to defaults."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": "string-not-dict"}')

    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert identity.dashboard_id


async def test_dashboard_id_is_url_safe(tmp_path: Path) -> None:
    """``secrets.token_urlsafe`` output: only ``[A-Za-z0-9_-]``."""
    identity = await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert DASHBOARD_ID_PATTERN.fullmatch(identity.dashboard_id)
    # 24 bytes base64url-encoded = 32 chars (no padding in token_urlsafe).
    assert len(identity.dashboard_id) == 32
    assert len(identity.dashboard_id) <= DASHBOARD_ID_MAX_CHARS


def test_dashboard_id_pattern_rejects_control_chars() -> None:
    """The validator rejects spaces, control bytes, unicode, punctuation."""
    assert DASHBOARD_ID_PATTERN.fullmatch("hello world") is None
    assert DASHBOARD_ID_PATTERN.fullmatch("hello\x00world") is None
    assert DASHBOARD_ID_PATTERN.fullmatch("héllo") is None
    assert DASHBOARD_ID_PATTERN.fullmatch("hello.world") is None
    assert DASHBOARD_ID_PATTERN.fullmatch("") is None


async def test_no_legacy_cert_files_created(tmp_path: Path) -> None:
    """
    The pre-pivot Ed25519 cert + key files are no longer produced.

    Pins that a fresh install lands a clean ``config_dir`` without
    the dormant Ed25519 artefacts that used to sit alongside the
    X25519 key. A regression that re-introduced the cert helper
    would put these back; this test catches it.
    """
    await get_or_create_identity(tmp_path, PeerLinkIdentityStore(tmp_path))
    assert not (tmp_path / ".device-builder-cert.pem").exists()
    assert not (tmp_path / ".device-builder-key.pem").exists()
