"""Unit tests for ``helpers/device_yaml.py``.

Focused on the parsers consumed by the devices controller, where
hand-rolled text scanning makes regression risk meaningful.
"""

from __future__ import annotations

from esphome_device_builder.helpers.device_yaml import (
    compute_has_pending_changes,
    parse_esphome_meta,
)


def test_parse_meta_plain_values() -> None:
    """No substitutions block: literal values are returned as-is."""
    yaml_content = """
esphome:
  name: my-device
  friendly_name: My Device
  comment: A useful little box
"""
    assert parse_esphome_meta(yaml_content) == ("my-device", "My Device", "A useful little box")


def test_parse_meta_missing_keys_return_none() -> None:
    """Absent fields return ``None`` so callers can fall back to storage."""
    yaml_content = """
esphome:
  name: my-device
"""
    assert parse_esphome_meta(yaml_content) == ("my-device", None, None)


def test_parse_meta_resolves_dollar_substitution() -> None:
    """``$friendly_name`` resolves against the ``substitutions:`` block."""
    yaml_content = """
substitutions:
  friendly_name: "Living Room Lamp"
esphome:
  name: living-room-lamp
  friendly_name: $friendly_name
"""
    _, friendly_name, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Living Room Lamp"


def test_parse_meta_resolves_brace_substitution() -> None:
    """``${friendly_name}`` brace syntax also resolves."""
    yaml_content = """
substitutions:
  friendly_name: Kitchen
esphome:
  name: kitchen
  friendly_name: ${friendly_name}
"""
    _, friendly_name, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Kitchen"


def test_parse_meta_resolves_substitution_inside_string() -> None:
    """References that are part of a larger string are interpolated in place."""
    yaml_content = """
substitutions:
  room: Bedroom
esphome:
  friendly_name: "${room} Lamp"
"""
    _, friendly_name, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Bedroom Lamp"


def test_parse_meta_substitutions_block_after_esphome() -> None:
    """Block order in the file does not matter (single pass + post-resolve)."""
    yaml_content = """
esphome:
  friendly_name: $friendly_name
substitutions:
  friendly_name: "Office"
"""
    _, friendly_name, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Office"


def test_parse_meta_unknown_reference_left_untouched() -> None:
    """Unknown substitution names stay as the raw ``$token`` in the output."""
    yaml_content = """
substitutions:
  device_name: foo
esphome:
  friendly_name: $missing
"""
    _, friendly_name, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "$missing"


# ----------------------------------------------------------------------
# compute_has_pending_changes
# ----------------------------------------------------------------------


def test_pending_when_no_binary_yet() -> None:
    """Never-compiled device → always pending, regardless of mtime/hash inputs."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=None,
            expected_config_hash="abc",
            deployed_config_hash="abc",
        )
        is True
    )


def test_pending_when_yaml_edited_after_compile_and_hashes_unknown() -> None:
    """YAML newer than binary with no hash signal → pending via mtime fallback.

    Pre-#16145 firmware path: the device doesn't broadcast a config
    hash, so we have nothing to compare against and the mtime
    "YAML edited since the last compile" check is the only signal
    we have.
    """
    assert (
        compute_has_pending_changes(
            yaml_mtime=200.0,
            bin_mtime=100.0,
            expected_config_hash="",
            deployed_config_hash="",
        )
        is True
    )


def test_in_sync_when_hashes_match_even_if_yaml_edited() -> None:
    """Matching hashes win over newer YAML mtime.

    Real-world case from the field (Apollo R_PRO-1): the user edits
    the YAML in a way that doesn't change the resolved config —
    whitespace, comment changes, ``--only-generate`` rewriting
    ``StorageJSON`` and bumping the YAML stat — and the
    firmware-canonical hashes still match. The device is genuinely
    in sync; the previous mtime-first ordering reported "Modified"
    in the drawer even with hashes equal, which the user reasonably
    flagged as wrong.
    """
    assert (
        compute_has_pending_changes(
            yaml_mtime=200.0,
            bin_mtime=100.0,
            expected_config_hash="039818dc",
            deployed_config_hash="039818dc",
        )
        is False
    )


def test_pending_when_hashes_diverge_even_if_yaml_unchanged() -> None:
    """Diverging hashes win over an unchanged YAML mtime.

    Mirror image of the case above: ``--only-generate`` updated
    ``expected_config_hash`` after a YAML edit but the device still
    runs the old firmware, so deployed != expected. Hashes are
    authoritative, the mtime side is irrelevant.
    """
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=200.0,
            expected_config_hash="aaaa1111",
            deployed_config_hash="bbbb2222",
        )
        is True
    )


def test_in_sync_when_hashes_match_and_yaml_unchanged() -> None:
    """Both hashes known, YAML unchanged since compile → not pending."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=200.0,
            expected_config_hash="abc",
            deployed_config_hash="abc",
        )
        is False
    )


def test_pending_when_hashes_diverge() -> None:
    """Hashes known and differ → pending (compiled but device runs older firmware)."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=200.0,
            expected_config_hash="abc",
            deployed_config_hash="def",
        )
        is True
    )


def test_in_sync_when_hashes_unknown_and_yaml_unchanged() -> None:
    """Pre-#16145 firmware path: no hashes, YAML <= binary → not pending."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=200.0,
            expected_config_hash="",
            deployed_config_hash="",
        )
        is False
    )


def test_in_sync_when_only_one_hash_known() -> None:
    """Half-known hash isn't usable — fall through to the mtime answer."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=200.0,
            expected_config_hash="abc",
            deployed_config_hash="",
        )
        is False
    )
