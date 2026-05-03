"""Unit tests for ``helpers/device_yaml.py``.

Focused on the parsers consumed by the devices controller, where
hand-rolled text scanning makes regression risk meaningful.
"""

from __future__ import annotations

from pathlib import Path

from esphome_device_builder.helpers.device_yaml import (
    _parse_inline_value,
    compute_has_pending_changes,
    detect_platform_from_yaml,
    parse_esphome_meta,
    parse_platform_from_yaml,
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
    """No binary AND no broadcast data → pending (definitionally unflushed)."""
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=None,
            expected_config_hash="",
            deployed_config_hash="",
        )
        is True
    )


def test_in_sync_when_hashes_match_even_without_local_binary() -> None:
    """Hash match beats missing ``firmware.bin``.

    ``--only-generate`` writes ``build_info.json`` (so
    ``expected_config_hash`` is set) without producing
    ``firmware.bin``; same for a build directory that's been wiped
    by ``clean`` after a flash. If the device is broadcasting the
    same hash via mDNS, the running firmware was built from this
    YAML — that's authoritative, regardless of whether we still
    have the local artefact.
    """
    assert (
        compute_has_pending_changes(
            yaml_mtime=100.0,
            bin_mtime=None,
            expected_config_hash="abc",
            deployed_config_hash="abc",
        )
        is False
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


# ----------------------------------------------------------------------
# parse_esphome_meta — comment branch + edge cases
# ----------------------------------------------------------------------


def test_parse_meta_comment_field() -> None:
    """The ``comment:`` branch of the field-dispatch is exercised.

    Covers the ``else`` arm of the name/friendly_name/comment
    triad — the previous tests only ever hit the first two.
    """
    yaml_content = """
esphome:
  name: my-device
  comment: Hand-built controller
"""
    name, friendly_name, comment = parse_esphome_meta(yaml_content)
    assert name == "my-device"
    assert friendly_name is None
    assert comment == "Hand-built controller"


def test_parse_meta_skips_blank_and_comment_lines_inside_block() -> None:
    """Comment lines and blank lines inside the ``esphome:`` block are skipped.

    Pin the ``stripped.startswith("#") or not stripped`` guard —
    a refactor that dropped it would mis-parse a ``# friendly_name: foo``
    comment as the actual field.
    """
    yaml_content = """
esphome:
  name: my-device

  # friendly_name: this is just a comment, ignore me
  comment: real comment
"""
    name, friendly_name, comment = parse_esphome_meta(yaml_content)
    assert name == "my-device"
    assert friendly_name is None  # comment line wasn't picked up
    assert comment == "real comment"


# ----------------------------------------------------------------------
# parse_platform_from_yaml — pure-text scanner
# ----------------------------------------------------------------------


def test_parse_platform_extracts_board_and_variant() -> None:
    """Board + variant nested under an ``esp32:`` block are picked up."""
    yaml_content = """
esp32:
  board: esp32-c3-devkitm-1
  variant: ESP32C3
"""
    assert parse_platform_from_yaml(yaml_content) == (
        "esp32",
        "esp32-c3-devkitm-1",
        "ESP32C3",
    )


def test_parse_platform_resets_in_platform_on_non_platform_key() -> None:
    """A non-platform top-level key after a platform block stops field capture.

    Pin the ``in_platform = False`` reset — without it, a ``board:``
    nested under ``logger:`` (for example) would erroneously be
    treated as the platform's board.
    """
    yaml_content = """
esp32:
  variant: ESP32C3
logger:
  board: not-really-a-board
"""
    platform, pio_board, variant = parse_platform_from_yaml(yaml_content)
    assert platform == "esp32"
    assert variant == "ESP32C3"
    # ``logger.board`` is ignored because the scanner left the platform.
    assert pio_board == ""


def test_parse_platform_strips_quotes() -> None:
    """Quoted ``board:`` / ``variant:`` values are unwrapped."""
    yaml_content = """
esp8266:
  board: "nodemcuv2"
"""
    assert parse_platform_from_yaml(yaml_content) == ("esp8266", "nodemcuv2", "")


# ----------------------------------------------------------------------
# detect_platform_from_yaml — file I/O wrapper
# ----------------------------------------------------------------------


def test_detect_platform_returns_empty_on_missing_file(tmp_path: Path) -> None:
    """Unreadable file (``OSError``) falls into the ``except`` branch.

    Pin the silent-fallback contract — callers (the device-loader
    address fallback) rely on the empty-string sentinel rather
    than having to wrap every call in their own try/except.
    """
    missing = tmp_path / "no-such-file.yaml"
    assert detect_platform_from_yaml(missing) == ""


def test_detect_platform_reads_real_file(tmp_path: Path) -> None:
    """Round-trip through the file reader picks up the platform key."""
    path = tmp_path / "device.yaml"
    path.write_text("esp32:\n  variant: ESP32S3\n", encoding="utf-8")
    assert detect_platform_from_yaml(path) == "esp32"


# ----------------------------------------------------------------------
# _parse_inline_value — comment + quote stripping
# ----------------------------------------------------------------------


def test_parse_inline_value_strips_trailing_comment() -> None:
    """Bare values drop ``# ...`` trailers; quoted values keep them.

    The ``# in value and not value.startswith('"' / "'")`` guard
    is the key branch — a quoted value containing a literal ``#``
    must survive intact.
    """
    assert _parse_inline_value("my-device  # the device") == "my-device"
    # Quoted values keep an embedded ``#`` literal.
    assert _parse_inline_value('"with #hash"') == "with #hash"


def test_parse_inline_value_strips_matched_quotes() -> None:
    """Outer single or double quotes are stripped; mismatched ones aren't."""
    assert _parse_inline_value('"quoted"') == "quoted"
    assert _parse_inline_value("'quoted'") == "quoted"
    # Mismatched quotes are left alone — picking one off would change
    # the user's literal value.
    assert _parse_inline_value("\"mismatched'") == "\"mismatched'"
