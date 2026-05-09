"""
Tests for the offloader-side remote-build storage layer (phase 4a-o part 1).

Three layers:

* ``StoredPairing`` field validation in ``__post_init__`` — the
  storage-seam defense against a hand-edited / corrupt sidecar
  smuggling oversize hostnames, malformed pins, or
  wrong-length pubkeys past the WS-command validators.
* ``OffloaderRemoteBuildSettings`` JSON round-trip — a freshly
  saved blob loads back identical, including the raw
  ``static_x25519_pub`` bytes (mashumaro encodes ``bytes`` as
  base64).
* ``offloader_remote_build_settings_transaction`` RMW — atomic
  read-modify-write under the same metadata-transaction lock
  that the receiver-side helpers use, so a concurrent
  offloader pair + receiver token edit don't race.

Tolerance: a wholly malformed ``_offloader_remote_build`` blob
falls back to defaults (empty ``pairings``); the loader doesn't
crash dashboard startup on a corrupt sidecar.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import (
    _METADATA_FILE,
    _OFFLOADER_REMOTE_BUILD_KEY,
    load_offloader_remote_build_settings,
    offloader_remote_build_settings_transaction,
    save_offloader_remote_build_settings,
)
from esphome_device_builder.models import (
    OffloaderRemoteBuildSettings,
    PeerStatus,
    StoredPairing,
)


def _valid_pairing(**overrides: object) -> StoredPairing:
    """Build a passing :class:`StoredPairing` with the given overrides."""
    base: dict[str, object] = {
        "receiver_hostname": "build.local",
        "receiver_port": 6055,
        "pin_sha256": "a" * 64,
        "static_x25519_pub": b"\x01" * 32,
        "label": "desktop",
        "paired_at": 1.0,
        "status": PeerStatus.PENDING,
    }
    base.update(overrides)
    return StoredPairing(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# StoredPairing field validation
# ---------------------------------------------------------------------------


def test_stored_pairing_accepts_minimal_valid_row() -> None:
    pairing = _valid_pairing()
    assert pairing.receiver_hostname == "build.local"
    assert pairing.status is PeerStatus.PENDING


def test_stored_pairing_rejects_empty_hostname() -> None:
    with pytest.raises(ValueError, match="receiver_hostname"):
        _valid_pairing(receiver_hostname="")


def test_stored_pairing_rejects_oversize_hostname() -> None:
    with pytest.raises(ValueError, match="receiver_hostname"):
        _valid_pairing(receiver_hostname="x" * 256)


def test_stored_pairing_rejects_whitespace_only_hostname() -> None:
    """``"   "`` passes ``Length(min=1)`` but is unresolvable; reject pre-store."""
    with pytest.raises(ValueError, match="receiver_hostname"):
        _valid_pairing(receiver_hostname="   ")


def test_stored_pairing_rejects_non_string_hostname() -> None:
    with pytest.raises(ValueError, match="receiver_hostname"):
        _valid_pairing(receiver_hostname=123)


def test_stored_pairing_rejects_port_out_of_range() -> None:
    with pytest.raises(ValueError, match="receiver_port"):
        _valid_pairing(receiver_port=0)
    with pytest.raises(ValueError, match="receiver_port"):
        _valid_pairing(receiver_port=70000)


def test_stored_pairing_rejects_bool_port() -> None:
    """``isinstance(True, int)`` is true in Python; explicit bool reject."""
    with pytest.raises(ValueError, match="receiver_port"):
        _valid_pairing(receiver_port=True)


def test_stored_pairing_rejects_wrong_pin_length() -> None:
    with pytest.raises(ValueError, match="pin_sha256"):
        _valid_pairing(pin_sha256="a" * 63)
    with pytest.raises(ValueError, match="pin_sha256"):
        _valid_pairing(pin_sha256="a" * 65)


def test_stored_pairing_rejects_uppercase_hex_pin() -> None:
    """Pin is canonical lowercase-hex; uppercase is the canonicalisation seam."""
    with pytest.raises(ValueError, match="pin_sha256"):
        _valid_pairing(pin_sha256="A" * 64)


def test_stored_pairing_rejects_non_hex_pin() -> None:
    """Right length, wrong alphabet — schema's regex catches it."""
    with pytest.raises(ValueError, match="pin_sha256"):
        _valid_pairing(pin_sha256="z" * 64)


def test_stored_pairing_rejects_wrong_pubkey_length() -> None:
    with pytest.raises(ValueError, match="static_x25519_pub"):
        _valid_pairing(static_x25519_pub=b"\x00" * 31)
    with pytest.raises(ValueError, match="static_x25519_pub"):
        _valid_pairing(static_x25519_pub=b"\x00" * 33)


def test_stored_pairing_rejects_oversize_label() -> None:
    with pytest.raises(ValueError, match="label"):
        _valid_pairing(label="x" * 129)


def test_stored_pairing_rejects_bool_paired_at() -> None:
    """``paired_at=True`` would silently coerce to 1.0 without ``not_bool``."""
    with pytest.raises(ValueError, match="paired_at"):
        _valid_pairing(paired_at=True)
    with pytest.raises(ValueError, match="paired_at"):
        _valid_pairing(paired_at=False)


def test_stored_pairing_accepts_empty_label() -> None:
    """Empty label is fine — the user may legitimately not name the receiver."""
    pairing = _valid_pairing(label="")
    assert pairing.label == ""


# ---------------------------------------------------------------------------
# OffloaderRemoteBuildSettings round-trip
# ---------------------------------------------------------------------------


def test_settings_default_is_empty_pairings() -> None:
    settings = OffloaderRemoteBuildSettings()
    assert settings.pairings == []


def test_settings_round_trip_preserves_pairing_fields(tmp_path: Path) -> None:
    """Save then load — every field round-trips identical.

    Includes the raw ``static_x25519_pub`` bytes (mashumaro
    base64-encodes them on the wire).
    """
    pubkey = bytes(range(32))
    settings = OffloaderRemoteBuildSettings(
        pairings=[
            _valid_pairing(
                receiver_hostname="desk.local",
                receiver_port=6055,
                pin_sha256="b" * 64,
                static_x25519_pub=pubkey,
                label="desk",
                paired_at=42.5,
                status=PeerStatus.APPROVED,
            )
        ]
    )

    save_offloader_remote_build_settings(tmp_path, settings)
    loaded = load_offloader_remote_build_settings(tmp_path)

    [row] = loaded.pairings
    assert row.receiver_hostname == "desk.local"
    assert row.receiver_port == 6055
    assert row.pin_sha256 == "b" * 64
    assert row.static_x25519_pub == pubkey
    assert row.label == "desk"
    assert row.paired_at == 42.5
    assert row.status is PeerStatus.APPROVED


def test_load_returns_defaults_when_metadata_missing(tmp_path: Path) -> None:
    """No sidecar at all → empty pairings, no exception."""
    settings = load_offloader_remote_build_settings(tmp_path)
    assert settings.pairings == []


def test_load_returns_defaults_when_offloader_key_missing(tmp_path: Path) -> None:
    """Sidecar exists but no offloader key → empty pairings."""
    (tmp_path / _METADATA_FILE).write_text(json.dumps({"_remote_build": {"enabled": False}}))
    settings = load_offloader_remote_build_settings(tmp_path)
    assert settings.pairings == []


def test_load_returns_defaults_when_blob_is_not_a_dict(tmp_path: Path) -> None:
    """A scalar / list under the offloader key falls back to defaults."""
    (tmp_path / _METADATA_FILE).write_text(json.dumps({_OFFLOADER_REMOTE_BUILD_KEY: ["nonsense"]}))
    settings = load_offloader_remote_build_settings(tmp_path)
    assert settings.pairings == []


def test_load_returns_defaults_on_malformed_pairing_row(tmp_path: Path) -> None:
    """A bad row trips ``from_dict``; the whole blob falls back to defaults.

    Hand-edited / partial-write sidecars that violate the
    schema (e.g. oversize hostname) shouldn't crash dashboard
    startup. Per-row tolerance (keep the good rows, drop the
    bad one) would mirror the deleted ``_decode_tokens`` helper
    from phase 4a-r2; deferred until the offloader pool
    actually accumulates enough rows for one bad entry to
    matter.
    """
    (tmp_path / _METADATA_FILE).write_text(
        json.dumps(
            {
                _OFFLOADER_REMOTE_BUILD_KEY: {
                    "pairings": [
                        {
                            "receiver_hostname": "x" * 1000,  # oversize
                            "receiver_port": 6055,
                            "pin_sha256": "a" * 64,
                            "static_x25519_pub": "AA==",  # base64 of 1 byte; wrong len
                            "label": "bad",
                            "paired_at": 1.0,
                            "status": "pending",
                        }
                    ]
                }
            }
        )
    )
    settings = load_offloader_remote_build_settings(tmp_path)
    assert settings.pairings == []


def test_load_ignores_unknown_keys_on_legacy_blob(tmp_path: Path) -> None:
    """Older sidecars with extra keys are tolerated by mashumaro defaults."""
    (tmp_path / _METADATA_FILE).write_text(
        json.dumps(
            {
                _OFFLOADER_REMOTE_BUILD_KEY: {
                    "pairings": [],
                    "future_field": "ignore me",
                }
            }
        )
    )
    settings = load_offloader_remote_build_settings(tmp_path)
    assert settings.pairings == []


# ---------------------------------------------------------------------------
# RMW transaction
# ---------------------------------------------------------------------------


def test_transaction_persists_mutations_on_clean_exit(tmp_path: Path) -> None:
    with offloader_remote_build_settings_transaction(tmp_path) as settings:
        settings.pairings.append(_valid_pairing(label="from-txn"))

    loaded = load_offloader_remote_build_settings(tmp_path)
    [row] = loaded.pairings
    assert row.label == "from-txn"


def test_transaction_discards_mutations_on_exception(tmp_path: Path) -> None:
    """An exception inside the block aborts the metadata write."""
    with (
        pytest.raises(RuntimeError, match="boom"),
        offloader_remote_build_settings_transaction(tmp_path) as settings,
    ):
        settings.pairings.append(_valid_pairing(label="will-be-rolled-back"))
        raise RuntimeError("boom")

    loaded = load_offloader_remote_build_settings(tmp_path)
    assert loaded.pairings == []


def test_transaction_observes_existing_state(tmp_path: Path) -> None:
    """RMW reads the current state, not a fresh default."""
    save_offloader_remote_build_settings(
        tmp_path,
        OffloaderRemoteBuildSettings(pairings=[_valid_pairing(label="seeded")]),
    )

    with offloader_remote_build_settings_transaction(tmp_path) as settings:
        assert len(settings.pairings) == 1
        assert settings.pairings[0].label == "seeded"
        settings.pairings.append(_valid_pairing(label="appended"))

    loaded = load_offloader_remote_build_settings(tmp_path)
    assert [p.label for p in loaded.pairings] == ["seeded", "appended"]


def test_transaction_does_not_disturb_receiver_remote_build_key(
    tmp_path: Path,
) -> None:
    """Offloader writes don't disturb the receiver's ``_remote_build`` key.

    The two roles can coexist on the same dashboard; pin the
    isolation so a future "share state across keys" refactor
    has to fail this test on purpose.
    """
    (tmp_path / _METADATA_FILE).write_text(
        json.dumps(
            {
                "_remote_build": {
                    "enabled": True,
                    "manual_hosts": [{"hostname": "peer.local", "port": 6055}],
                }
            }
        )
    )

    with offloader_remote_build_settings_transaction(tmp_path) as settings:
        settings.pairings.append(_valid_pairing(label="offloader-only"))

    raw = json.loads((tmp_path / _METADATA_FILE).read_text())
    assert raw["_remote_build"]["enabled"] is True
    assert raw["_remote_build"]["manual_hosts"] == [{"hostname": "peer.local", "port": 6055}]
    assert raw[_OFFLOADER_REMOTE_BUILD_KEY]["pairings"][0]["label"] == "offloader-only"
