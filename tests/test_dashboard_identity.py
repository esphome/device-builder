"""Tests for the dashboard identity helper."""

from __future__ import annotations

import json
import stat
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography import x509

from esphome_device_builder.helpers.dashboard_identity import (
    _CERT_FILENAME,
    _CERT_NOT_BEFORE_BACKDATE,
    _KEY_FILENAME,
    _KEY_MODE,
    DashboardIdentity,
    _add_years,
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows doesn't honor POSIX mode bits")
def test_key_file_has_restrictive_mode(tmp_path: Path) -> None:
    """The private-key file lands at ``0600`` from the start, never wider."""
    get_or_create_identity(tmp_path)
    key_path = tmp_path / _KEY_FILENAME
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == _KEY_MODE


def test_pin_sha256_is_lowercase_hex_64_chars(tmp_path: Path) -> None:
    """SHA-256 fingerprint is 64 lowercase hex chars."""
    identity = get_or_create_identity(tmp_path)
    assert len(identity.pin_sha256) == 64
    assert identity.pin_sha256 == identity.pin_sha256.lower()
    assert all(c in "0123456789abcdef" for c in identity.pin_sha256)


def test_pin_sha256_formatted_groups_in_pairs(tmp_path: Path) -> None:
    """Display form groups the hex into space-separated byte pairs."""
    identity = get_or_create_identity(tmp_path)
    formatted = identity.pin_sha256_formatted
    parts = formatted.split(" ")
    assert len(parts) == 32
    assert all(len(p) == 2 for p in parts)
    # Round-trip: stripping spaces yields the bare form.
    assert formatted.replace(" ", "") == identity.pin_sha256


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
    """Cert + key both parse but don't pair; treated as missing, regenerate."""
    first = get_or_create_identity(tmp_path)
    # Drop a valid-but-unrelated key next to the first identity's cert.
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = get_or_create_identity(other_dir)
    (tmp_path / _KEY_FILENAME).write_bytes(other.key_pem)

    third = get_or_create_identity(tmp_path)
    assert third.cert_pem != first.cert_pem
    assert third.cert_pem != other.cert_pem


def test_concurrent_dashboard_id_generation_is_serialised(tmp_path: Path) -> None:
    """Two concurrent ``get_or_create_identity`` calls land on the same id."""
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
    assert rotated.pin_sha256 != first.pin_sha256
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

    # Simulate another phase writing other fields under the same key.
    metadata_path = tmp_path / ".device-builder.json"
    data = json.loads(metadata_path.read_bytes())
    data["_remote_build"]["enabled"] = True
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
    metadata_path.write_bytes(b'{"_remote_build": {"enabled": true}}')

    identity = get_or_create_identity(tmp_path)
    metadata = _read_metadata(tmp_path)
    assert metadata["_remote_build"]["dashboard_id"] == identity.dashboard_id
    assert metadata["_remote_build"]["enabled"] is True


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


def test_add_years_regular_date() -> None:
    """``_add_years`` shifts a non-leap-day date by N years."""
    d = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    assert _add_years(d, 100) == datetime(2126, 5, 8, 12, 0, 0, tzinfo=UTC)


def test_add_years_clamps_feb_29_to_28() -> None:
    """``_add_years`` clamps Feb 29 -> Feb 28 when the target year isn't a leap year."""
    leap = datetime(2000, 2, 29, 12, 0, 0, tzinfo=UTC)
    # 2000 + 100 = 2100, divisible by 100 and not by 400 -> not leap.
    assert _add_years(leap, 100) == datetime(2100, 2, 28, 12, 0, 0, tzinfo=UTC)


def test_cert_not_valid_before_is_backdated(tmp_path: Path) -> None:
    """The cert's ``not_valid_before`` is ~5 minutes before generation."""
    before = datetime.now(UTC)
    identity = get_or_create_identity(tmp_path)
    cert = x509.load_pem_x509_certificate(identity.cert_pem)
    nvb = cert.not_valid_before_utc
    # Cert claims to be valid from before this test started, by at
    # least the configured backdate.
    assert nvb <= before - _CERT_NOT_BEFORE_BACKDATE / 2
