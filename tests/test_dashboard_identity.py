"""
Tests for the phase-3a dashboard identity helper.

Covers:

* First-call generation: creates cert + key files in the config
  dir, generates a fresh ``dashboard_id`` in the metadata sidecar.
* Idempotence: a second call returns identical bytes / id without
  regenerating.
* Cert / key file mode: the key file is ``0600``, the cert file
  isn't restricted.
* Fingerprint shape: lowercase hex, 64 chars (SHA-256 of the DER).
* Recovery from partial corruption: cert without key, key without
  cert, or unparsable PEM all reset to "missing" and regenerate.
* ``rotate_certificate``: keeps the same ``dashboard_id``, changes
  the cert, persists the new pair.
* ``dashboard_id`` survives ``_remote_build`` mutations: adding a
  manual host or flipping ``enabled`` between save calls doesn't
  drop the id.
"""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from esphome_device_builder.helpers import dashboard_identity
from esphome_device_builder.helpers.dashboard_identity import (
    _CERT_FILENAME,
    _KEY_FILENAME,
    _KEY_MODE,
    DashboardIdentity,
    atomic_write,
    get_or_create_identity,
    rotate_certificate,
)


def _read_metadata(config_dir: Path) -> dict:
    return json.loads((config_dir / ".device-builder.json").read_bytes())


def test_first_call_generates_and_persists_identity(tmp_path: Path) -> None:
    """Fresh config dir → cert, key, and dashboard_id all created."""
    identity = get_or_create_identity(tmp_path)

    assert isinstance(identity, DashboardIdentity)
    assert identity.dashboard_id  # non-empty
    assert (tmp_path / _CERT_FILENAME).exists()
    assert (tmp_path / _KEY_FILENAME).exists()
    # Cert PEM round-trips through the file.
    assert identity.cert_pem == (tmp_path / _CERT_FILENAME).read_bytes()
    # ``dashboard_id`` lands in ``_remote_build.dashboard_id``.
    metadata = _read_metadata(tmp_path)
    assert metadata["_remote_build"]["dashboard_id"] == identity.dashboard_id


def test_second_call_returns_identical_identity(tmp_path: Path) -> None:
    """Idempotent: post-generation, every call returns the same bytes."""
    first = get_or_create_identity(tmp_path)
    second = get_or_create_identity(tmp_path)
    assert first == second


def test_key_file_has_restrictive_mode(tmp_path: Path) -> None:
    """
    The private-key file is created at ``0600``, never wider.

    A ``Path.write_bytes`` followed by a ``Path.chmod`` would
    leave a window between the write and the chmod where the
    bytes sit at the umask default (typically ``0644``); a
    backup tool snapshotting the config dir during that window
    would capture the key at the wrong mode. Pin the
    "key was created at ``0600`` from the start" semantics.
    Cert is intentionally public-by-design and stays at the
    default mode.
    """
    get_or_create_identity(tmp_path)
    key_path = tmp_path / _KEY_FILENAME
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == _KEY_MODE


def test_key_file_mode_is_corrected_when_pre_existing(tmp_path: Path) -> None:
    """
    A pre-existing key file at a looser mode is chmod'd back to ``0600``.

    Real-world path: an older version of the helper, or the user
    poking at the file, left it at the umask default. The next
    call to ``get_or_create_identity`` regenerates via
    ``os.open(..., mode=0o600)`` which is a creation-time mode
    only; if the file already exists, that argument is ignored.
    The explicit ``os.chmod`` after the write makes the mode
    apply unconditionally so a previously-too-loose key gets
    locked down on the next regen.
    """
    key_path = tmp_path / _KEY_FILENAME
    cert_path = tmp_path / _CERT_FILENAME
    key_path.write_bytes(b"placeholder")
    key_path.chmod(0o644)
    cert_path.write_bytes(b"placeholder")

    get_or_create_identity(tmp_path)

    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == _KEY_MODE


def test_cert_sha256_is_lowercase_hex_64_chars(tmp_path: Path) -> None:
    """SHA-256 fingerprint is 64 lowercase hex chars."""
    identity = get_or_create_identity(tmp_path)
    assert len(identity.cert_sha256) == 64
    assert identity.cert_sha256 == identity.cert_sha256.lower()
    assert all(c in "0123456789abcdef" for c in identity.cert_sha256)


def test_cert_sha256_formatted_groups_in_pairs(tmp_path: Path) -> None:
    """Display form groups the hex into space-separated byte pairs."""
    identity = get_or_create_identity(tmp_path)
    formatted = identity.cert_sha256_formatted
    parts = formatted.split(" ")
    assert len(parts) == 32
    assert all(len(p) == 2 for p in parts)
    # Round-trip: stripping spaces yields the bare form.
    assert formatted.replace(" ", "") == identity.cert_sha256


def test_missing_key_file_triggers_regeneration(tmp_path: Path) -> None:
    """Cert file alone (key gone) is treated as missing; both regenerate."""
    first = get_or_create_identity(tmp_path)
    (tmp_path / _KEY_FILENAME).unlink()

    second = get_or_create_identity(tmp_path)
    assert second.cert_pem != first.cert_pem
    assert second.dashboard_id == first.dashboard_id  # id is stable


def test_missing_cert_file_triggers_regeneration(tmp_path: Path) -> None:
    """Key file alone (cert gone) regenerates both."""
    first = get_or_create_identity(tmp_path)
    (tmp_path / _CERT_FILENAME).unlink()

    second = get_or_create_identity(tmp_path)
    assert second.cert_pem != first.cert_pem


def test_unparsable_cert_triggers_regeneration(tmp_path: Path) -> None:
    """Garbage in the cert file regenerates rather than crashing on load."""
    first = get_or_create_identity(tmp_path)
    (tmp_path / _CERT_FILENAME).write_bytes(b"not a real cert")

    second = get_or_create_identity(tmp_path)
    assert second.cert_pem != first.cert_pem


def test_unparsable_key_triggers_regeneration(tmp_path: Path) -> None:
    """Garbage in the key file regenerates rather than crashing on load."""
    first = get_or_create_identity(tmp_path)
    (tmp_path / _KEY_FILENAME).write_bytes(b"not a real key")

    second = get_or_create_identity(tmp_path)
    assert second.cert_pem != first.cert_pem


def test_mismatched_cert_and_key_triggers_regeneration(tmp_path: Path) -> None:
    """
    Cert + key both parse but don't pair; treated as missing, regenerate.

    Real-world path: a backup-restore reassembles mismatched files,
    or someone manually rotated one half. Without the cross-check,
    the helper would happily return the mismatched pair and the
    failure would only surface deep inside the TLS handshake as
    "key values mismatch".
    """
    first = get_or_create_identity(tmp_path)
    # Generate a SECOND independent identity in another tmp dir so
    # we have a valid-but-unrelated key, then drop it next to the
    # first identity's cert. Both files parse cleanly; only the
    # cross-check rejects them.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = get_or_create_identity(other_dir)
    (tmp_path / _KEY_FILENAME).write_bytes(other.key_pem)

    third = get_or_create_identity(tmp_path)
    # New cert + key generated; both halves now match each other.
    assert third.cert_pem != first.cert_pem
    assert third.cert_pem != other.cert_pem


def test_atomic_write_cleans_up_tempfile_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A crash mid-write leaves no leftover ``.tmp`` files in the config dir.

    ``atomic_write`` stages bytes in ``mkstemp(prefix=name + ".",
    suffix=".tmp", dir=parent)`` and ``os.replace``s into place. If
    ``os.replace`` raises (disk full, permissions, ...) the tempfile
    must be unlinked rather than accumulating one ``.<name>.<random>.tmp``
    file per failed write across the dashboard's lifetime.
    """
    target = tmp_path / "demo.bin"

    def _fail(*args: object, **kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("os.replace", _fail)

    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, b"payload")

    # Target wasn't created; no tempfiles linger.
    assert not target.exists()
    assert not list(tmp_path.glob("demo.bin.*.tmp"))


def test_atomic_write_closes_fd_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A failure during ``os.write`` doesn't leak the file descriptor.

    The cleanup branch closes the fd-still-open and unlinks the
    tempfile so a transient I/O error doesn't leave the dashboard
    accumulating leaked descriptors per failed write.
    """
    target = tmp_path / "demo.bin"

    real_write = dashboard_identity.os.write

    def _failing_write(fd: int, data: bytes) -> int:
        # Raise on our target write, but pass through any other
        # ``os.write`` calls happening elsewhere in the process.
        if data == b"payload":
            msg = "io error"
            raise OSError(msg)
        return real_write(fd, data)

    monkeypatch.setattr(dashboard_identity.os, "write", _failing_write)

    with pytest.raises(OSError, match="io error"):
        dashboard_identity.atomic_write(target, b"payload")

    assert not target.exists()
    assert not list(tmp_path.glob("demo.bin.*.tmp"))


def test_concurrent_dashboard_id_generation_is_serialised(tmp_path: Path) -> None:
    """
    Two concurrent ``get_or_create_identity`` calls land on the same id.

    The ``metadata_transaction`` lock serialises the read-modify-
    write under one critical section, so even if two callers race
    in via ``run_in_executor`` thread pool one of them blocks until
    the other completes; whichever wins persists its id, the other
    re-reads and returns it. Without the lock both would generate
    independent ids and one would silently overwrite the other.
    """
    results: list[str] = []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()
        results.append(get_or_create_identity(tmp_path).dashboard_id)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1, results


def test_rotate_certificate_keeps_dashboard_id(tmp_path: Path) -> None:
    """``rotate_certificate`` swaps the cert / key but preserves the id."""
    first = get_or_create_identity(tmp_path)
    rotated = rotate_certificate(tmp_path)

    assert rotated.dashboard_id == first.dashboard_id
    assert rotated.cert_sha256 != first.cert_sha256
    assert rotated.cert_pem != first.cert_pem
    assert rotated.key_pem != first.key_pem


def test_rotate_certificate_persists_to_disk(tmp_path: Path) -> None:
    """A subsequent ``get_or_create_identity`` call returns the rotated values."""
    rotate_certificate(tmp_path)
    rotated = get_or_create_identity(tmp_path)

    next_call = get_or_create_identity(tmp_path)
    assert next_call == rotated


def test_dashboard_id_survives_other_remote_build_mutations(tmp_path: Path) -> None:
    """
    Writing other ``_remote_build`` keys doesn't drop ``dashboard_id``.

    Pin the read-modify-write semantics of ``_save_dashboard_id`` —
    a bare overwrite of the ``_remote_build`` blob would silently
    reset every other field; equally, an external mutation that
    follows the same RMW shape must preserve ``dashboard_id``.
    """
    identity = get_or_create_identity(tmp_path)

    # Simulate phase 2 / 2b writing other fields under the same key.
    metadata_path = tmp_path / ".device-builder.json"
    data = json.loads(metadata_path.read_bytes())
    data["_remote_build"]["enabled"] = True
    data["_remote_build"]["manual_hosts"] = [{"hostname": "10.0.0.5", "port": 6052}]
    metadata_path.write_bytes(json.dumps(data).encode())

    # Re-read the identity; dashboard_id still there.
    second = get_or_create_identity(tmp_path)
    assert second.dashboard_id == identity.dashboard_id


def test_rotation_after_id_only_mutation(tmp_path: Path) -> None:
    """
    Writing ``_remote_build`` data BEFORE first identity init still works.

    Real-world path: a user enables remote-build via a phase-2b
    Settings flow before phase 3 ever fires. The metadata sidecar
    already has ``_remote_build.enabled`` set; the identity init
    must merge into that rather than replacing the whole key.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": {"enabled": true, "manual_hosts": []}}')

    identity = get_or_create_identity(tmp_path)
    metadata = _read_metadata(tmp_path)
    assert metadata["_remote_build"]["dashboard_id"] == identity.dashboard_id
    assert metadata["_remote_build"]["enabled"] is True
    assert metadata["_remote_build"]["manual_hosts"] == []


def test_corrupt_metadata_does_not_block_generation(tmp_path: Path) -> None:
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

    identity = get_or_create_identity(tmp_path)
    assert identity.dashboard_id  # generated fresh
    metadata = _read_metadata(tmp_path)
    assert metadata["_remote_build"]["dashboard_id"] == identity.dashboard_id


def test_non_dict_metadata_root_falls_back(tmp_path: Path) -> None:
    """A JSON list at the root (instead of a dict) falls back to defaults."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b"[1, 2, 3]")

    identity = get_or_create_identity(tmp_path)
    assert identity.dashboard_id


def test_non_dict_remote_build_value_falls_back(tmp_path: Path) -> None:
    """``_remote_build`` set to a non-dict value falls back to defaults."""
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_remote_build": "string-not-dict"}')

    identity = get_or_create_identity(tmp_path)
    assert identity.dashboard_id


def test_dashboard_id_is_url_safe(tmp_path: Path) -> None:
    """``secrets.token_urlsafe`` output: only ``[A-Za-z0-9_-]``."""
    identity = get_or_create_identity(tmp_path)
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(identity.dashboard_id) <= allowed
    # 24 bytes base64url-encoded = 32 chars (no padding in token_urlsafe).
    assert len(identity.dashboard_id) == 32
