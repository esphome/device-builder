"""Unit tests for ``helpers/device_yaml.py``.

Focused on the parsers consumed by the devices controller, where
hand-rolled text scanning makes regression risk meaningful.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from esphome_device_builder.helpers.device_yaml import (
    _parse_inline_value,
    compute_has_pending_changes,
    configuration_stem,
    detect_platform_from_yaml,
    generate_device_yaml,
    generate_minimal_stub_yaml,
    load_device_from_storage,
    parse_esphome_meta,
    parse_platform_from_yaml,
)
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    BoardHardware,
    Connectivity,
    Esp32Variant,
    Platform,
)
from tests._storage_fixtures import write_storage_json


def _make_esp32_board(
    *,
    variant: Esp32Variant | None = None,
    flash_size: str | None = None,
    framework: str | None = None,
) -> BoardCatalogEntry:
    """Build a minimal ESP32 ``BoardCatalogEntry`` for the YAML generator.

    Defaults reflect the ESP32 generic dev-kit shape; tests pass
    explicit kwargs to drive each ``if`` branch in
    ``generate_device_yaml``'s ESP32-specific block.
    """
    return BoardCatalogEntry(
        id="esp32-test",
        name="ESP32 Test",
        description="",
        manufacturer="Espressif",
        esphome=BoardEsphomeConfig(
            platform=Platform.ESP32,
            board="esp32dev",
            variant=variant,
            framework=framework,
        ),
        hardware=BoardHardware(
            flash_size=flash_size,
            connectivity=[Connectivity.WIFI],
        ),
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("kitchen.yaml", "kitchen"),
        ("kitchen.yml", "kitchen"),
        ("foo.bar.yaml", "foo.bar"),
        ("no-extension", "no-extension"),
        # Stripping is order-tolerant (the helper applies both
        # ``removesuffix`` calls; only one matches per input).
        ("device.yaml.yml", "device.yaml"),
    ],
)
def test_configuration_stem(filename: str, expected: str) -> None:
    """``configuration_stem`` strips ``.yaml`` / ``.yml`` only."""
    assert configuration_stem(filename) == expected


def test_generate_minimal_stub_yaml_has_required_blocks() -> None:
    """Stub YAML carries every block ESPHome's schema requires.

    The wizard's "Empty Configuration" path lands this stub, so a
    fresh device must compile without the user editing anything
    yet. ``esphome.name``, ``esphome.friendly_name``, the
    platform block (``esp32: board: esp32dev``), and a non-empty
    api-encryption key are the load-bearing pieces; the
    "Replace this..." comment carries the silent-bind warning.
    """
    out = generate_minimal_stub_yaml("kitchen", "Kitchen Lamp")
    assert "esphome:\n  name: kitchen\n  friendly_name: Kitchen Lamp\n" in out
    assert "esp32:\n  board: esp32dev\n" in out
    assert "Replace this with your actual platform" in out
    assert 'api:\n  encryption:\n    key: "' in out
    assert "ota:\n  - platform: esphome\n" in out


def test_generate_minimal_stub_yaml_emits_per_device_encryption_key() -> None:
    """API encryption key is freshly generated each call.

    Two stubs created with *the same* name + friendly_name still
    differ — the only thing that varies between calls is the
    32-byte ``secrets.token_bytes`` API key. Comparing the
    extracted key lines directly proves the per-device-key
    contract regardless of whether other YAML output ever
    becomes deterministic.
    """
    a = generate_minimal_stub_yaml("kitchen", "Kitchen Lamp")
    b = generate_minimal_stub_yaml("kitchen", "Kitchen Lamp")
    a_key = next(line for line in a.splitlines() if line.lstrip().startswith("key:"))
    b_key = next(line for line in b.splitlines() if line.lstrip().startswith("key:"))
    assert a_key != b_key


def test_parse_meta_plain_values() -> None:
    """No substitutions block: literal values are returned as-is."""
    yaml_content = """
esphome:
  name: my-device
  friendly_name: My Device
  comment: A useful little box
"""
    assert parse_esphome_meta(yaml_content) == (
        "my-device",
        "My Device",
        "A useful little box",
        None,
    )


def test_parse_meta_missing_keys_return_none() -> None:
    """Absent fields return ``None`` so callers can fall back to storage."""
    yaml_content = """
esphome:
  name: my-device
"""
    assert parse_esphome_meta(yaml_content) == ("my-device", None, None, None)


def test_parse_meta_resolves_dollar_substitution() -> None:
    """``$friendly_name`` resolves against the ``substitutions:`` block."""
    yaml_content = """
substitutions:
  friendly_name: "Living Room Lamp"
esphome:
  name: living-room-lamp
  friendly_name: $friendly_name
"""
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
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
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Kitchen"


def test_parse_meta_resolves_substitution_inside_string() -> None:
    """References that are part of a larger string are interpolated in place."""
    yaml_content = """
substitutions:
  room: Bedroom
esphome:
  friendly_name: "${room} Lamp"
"""
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Bedroom Lamp"


def test_parse_meta_substitutions_block_after_esphome() -> None:
    """Block order in the file does not matter (single pass + post-resolve)."""
    yaml_content = """
esphome:
  friendly_name: $friendly_name
substitutions:
  friendly_name: "Office"
"""
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "Office"


def test_parse_meta_unknown_reference_left_untouched() -> None:
    """Unknown substitution names stay as the raw ``$token`` in the output."""
    yaml_content = """
substitutions:
  device_name: foo
esphome:
  friendly_name: $missing
"""
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
    assert friendly_name == "$missing"


def test_parse_meta_resolves_substitution_in_comment() -> None:
    """Substitutions in ``esphome.comment`` resolve like the other fields."""
    yaml_content = """
substitutions:
  area: Outside
esphome:
  name: well
  comment: "${area} sensor"
"""
    _, _, comment, _ = parse_esphome_meta(yaml_content)
    assert comment == "Outside sensor"


def test_parse_meta_resolves_chained_substitutions() -> None:
    """A substitution whose value references another substitution resolves fully.

    Regression test for substitutions inside ``comment:`` not being
    expanded when the substitution's own value contained a reference
    (e.g. ``comment: "${area}, Well"`` + ``esphome.comment: ${comment}``).
    """
    yaml_content = """
substitutions:
  area: Outside
  comment: "${area}, Well | Irrigation A"
esphome:
  name: well
  comment: ${comment}
"""
    _, _, comment, _ = parse_esphome_meta(yaml_content)
    assert comment == "Outside, Well | Irrigation A"


def test_parse_meta_circular_substitutions_terminate() -> None:
    """Circular substitution references bail out instead of looping forever."""
    yaml_content = """
substitutions:
  a: ${b}
  b: ${a}
esphome:
  name: device
  friendly_name: ${a}
"""
    # Should return without hanging; the exact stuck value is irrelevant
    # — what matters is that the resolver terminates safely.
    _, friendly_name, _, _ = parse_esphome_meta(yaml_content)
    assert friendly_name in {"${a}", "${b}"}


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
    name, friendly_name, comment, _ = parse_esphome_meta(yaml_content)
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
    name, friendly_name, comment, _ = parse_esphome_meta(yaml_content)
    assert name == "my-device"
    assert friendly_name is None  # comment line wasn't picked up
    assert comment == "real comment"


# ----------------------------------------------------------------------
# parse_esphome_meta — area field
# ----------------------------------------------------------------------


def test_parse_meta_area_field() -> None:
    """``esphome.area`` is captured alongside the other meta fields."""
    yaml_content = """
esphome:
  name: kitchen-lamp
  friendly_name: Kitchen Lamp
  area: Kitchen
"""
    name, friendly_name, _, area = parse_esphome_meta(yaml_content)
    assert name == "kitchen-lamp"
    assert friendly_name == "Kitchen Lamp"
    assert area == "Kitchen"


def test_parse_meta_area_absent_returns_none() -> None:
    """Without an ``area:`` line, ``area`` is ``None`` (not empty string)."""
    yaml_content = """
esphome:
  name: my-device
"""
    *_, area = parse_esphome_meta(yaml_content)
    assert area is None


def test_parse_meta_area_resolves_substitution() -> None:
    """Substitutions referenced from ``area:`` resolve like the other fields."""
    yaml_content = """
substitutions:
  room: "Living Room"
esphome:
  name: lamp
  area: ${room}
"""
    *_, area = parse_esphome_meta(yaml_content)
    assert area == "Living Room"


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


# ----------------------------------------------------------------------
# generate_device_yaml — ESP32 platform branch
# ----------------------------------------------------------------------


def test_generate_yaml_emits_esp32_variant_when_set() -> None:
    """ESP32 board with a variant produces ``variant: <id>`` under the platform.

    The variant line drives ESPHome's chip-specific build path
    (ESP32S3 vs ESP32C3 vs base ESP32). A board with ``variant``
    set but no ``flash_size`` / ``framework`` should still emit
    just the variant line — pin the per-field independence so a
    refactor that consolidated the three ``if``s into one block
    can't silently drop a field.
    """
    board = _make_esp32_board(variant=Esp32Variant.ESP32S3)
    yaml = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")

    assert "esp32:\n  variant: esp32s3\n" in yaml
    # No flash_size / framework lines.
    assert "  flash_size:" not in yaml
    assert "  framework:" not in yaml
    # Bare ``board:`` line is the non-ESP32 fallback — must NOT appear here.
    assert "  board:" not in yaml


def test_generate_yaml_emits_esp32_flash_size_when_set() -> None:
    """``hardware.flash_size`` populated → ``flash_size: <value>`` line emitted.

    The flash-size hint lets ESPHome pick the right partition table
    and OTA layout. Boards with non-default flash (4MB / 8MB / 16MB)
    rely on this round-tripping; a regression that dropped the line
    would silently pick the framework's default and break OTA on
    larger-flash boards.
    """
    board = _make_esp32_board(flash_size="8MB")
    yaml = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")

    assert "  flash_size: 8MB\n" in yaml


def test_generate_yaml_emits_esp32_framework_when_set() -> None:
    r"""``framework`` populated → ``framework:`` block with ``type:`` child.

    Pin the two-line emit (``framework:\n    type: esp-idf``) — a
    refactor that flattened it to ``framework: esp-idf`` would
    produce invalid ESPHome YAML, since ``framework`` expects a
    nested mapping.
    """
    board = _make_esp32_board(framework="esp-idf")
    yaml = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")

    assert "  framework:\n    type: esp-idf\n" in yaml


def test_generate_yaml_omits_esp32_branch_fields_when_unset() -> None:
    """All three ESP32 sub-fields ``None`` → only the bare ``esp32:`` line.

    Pin the negative path: without the per-field ``if`` guards a
    refactor could emit ``variant: None`` / ``flash_size: None``
    which ESPHome would reject at validation time.
    """
    board = _make_esp32_board()  # no variant, flash_size, framework
    yaml = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")

    assert "esp32:\n\n" in yaml
    assert "variant:" not in yaml
    assert "flash_size:" not in yaml
    assert "framework:" not in yaml


def test_generate_yaml_emits_all_three_esp32_fields_together() -> None:
    """All three ESP32 sub-fields set → all three lines emit in order.

    Variant first, then flash_size, then framework — the iteration
    order matters because users (and operators reading their
    configs) expect the same shape ESPHome's docs use.
    """
    board = _make_esp32_board(
        variant=Esp32Variant.ESP32S3,
        flash_size="16MB",
        framework="arduino",
    )
    yaml = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")

    # Verify the three lines appear in the documented order.
    variant_idx = yaml.index("  variant:")
    flash_idx = yaml.index("  flash_size:")
    framework_idx = yaml.index("  framework:")
    assert variant_idx < flash_idx < framework_idx


def test_generate_yaml_emits_explicit_wifi_credentials_when_provided() -> None:
    """``ssid`` non-empty → literal credentials; empty ``ssid`` → ``!secret`` refs.

    The non-empty branch is the wizard path (user typed credentials
    in the form); the empty branch matches what the upstream
    ``esphome wizard`` writes by default. Pin both so a refactor
    that always emitted ``!secret`` would silently break the
    "works without secrets.yaml" path.
    """
    board = _make_esp32_board(variant=Esp32Variant.ESP32)

    # Explicit credentials.
    explicit = generate_device_yaml("kitchen", "Kitchen", board, ssid="MyNetwork", psk="hunter2")
    assert "  ssid: MyNetwork\n" in explicit
    assert "  password: hunter2\n" in explicit
    assert "!secret" not in explicit

    # Empty credentials → !secret references.
    secret = generate_device_yaml("kitchen", "Kitchen", board, ssid="", psk="")
    assert "  ssid: !secret wifi_ssid\n" in secret
    assert "  password: !secret wifi_password\n" in secret


# ---------------------------------------------------------------------------
# load_device_from_storage — read-error / firmware bin / target_platform paths
# ---------------------------------------------------------------------------


@pytest.fixture
def _redirect_ext_storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ``ext_storage_path`` at ``tmp_path/.esphome/storage/``.

    The production helper resolves through ``CORE.config_path``,
    which isn't set in isolated tests; the redirect makes
    ``StorageJSON.load(ext_storage_path(filename))`` read the
    sidecar ``write_storage_json`` lays down.
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    def _ext(configuration: str) -> Path:
        return storage_dir / f"{configuration}.json"

    monkeypatch.setattr("esphome_device_builder.helpers.device_yaml.ext_storage_path", _ext)


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_falls_back_to_empty_yaml_on_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError reading the YAML produces an empty content string, not a crash.

    The scanner can race a file rename / unlink; if the YAML
    disappears between ``Path.exists()`` (in the caller) and
    ``read_text()``, the loader must still return a usable
    Device rather than blowing up the whole rebuild. Pin the
    catch so a regression that re-raised the OSError would
    surface here as a hard failure.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    real_read_text = Path.read_text

    def _failing_read(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "kitchen.yaml":
            msg = "permission denied"
            raise OSError(msg)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _failing_read)

    device = load_device_from_storage(yaml_path)

    # Empty-string fallback: parser sees no name/friendly/comment,
    # so the loader leans on StorageJSON for those fields.
    assert device.name == "kitchen"  # from StorageJSON.name (write_storage_json default)
    assert device.configuration == "kitchen.yaml"


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_records_firmware_bin_mtime_when_present(tmp_path: Path) -> None:
    """``bin_mtime`` is populated when the firmware binary actually exists on disk.

    The mtime drives the ``has_pending_changes`` fallback when
    the canonical config-hash comparison can't run (pre-#16145
    firmware). Pin: a sidecar pointing at an existing binary is
    treated as deployed; an absent binary still leaves the
    branch intact via the ``.exists()`` short-circuit.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Lay down a real firmware bin and point StorageJSON at it.
    build_dir = tmp_path / ".esphome" / "build" / "kitchen"
    build_dir.mkdir(parents=True, exist_ok=True)
    firmware_bin = build_dir / "firmware.bin"
    firmware_bin.write_bytes(b"\x00" * 16)
    write_storage_json(tmp_path, "kitchen.yaml", firmware_bin_path=firmware_bin)

    # Pre-existing YAML mtime equal to the bin (both freshly written) +
    # both hashes empty → ``has_pending_changes`` falls back to mtime,
    # and "bin newer than YAML" is False, so the device is in-sync.
    device = load_device_from_storage(yaml_path)

    # The bin mtime path was reached — without it, the loader would
    # treat the device as "never compiled" (bin_mtime=None) and
    # ``has_pending_changes`` would default to True.
    assert device.has_pending_changes is False


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_uses_storage_core_platform_over_yaml(tmp_path: Path) -> None:
    """When StorageJSON carries ``core_platform``, it wins over YAML detection.

    ``core_platform`` is the post-codegen platform key — what
    actually compiled. The YAML's ``esp32:`` / ``esp8266:`` block
    is what the user typed, which can drift from reality if
    ESPHome remapped it during validation. Pin the
    StorageJSON-wins precedence so a regression that
    short-circuited to ``detect_platform_from_yaml`` would
    surface here as the YAML-derived value leaking through.

    Frontend issue #137: the column rendered uppercase
    ``ESP32`` straight from ``StorageJSON.target_platform``
    while uncompiled devices pulled lowercase ``esp32`` from the
    YAML scan. ``core_platform`` is upstream's lowercase platform
    key (added in esphome#9028), always canonical regardless of
    chip variant — so a fleet of mixed compile states now shows
    ``esp32`` end-to-end.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    # YAML says esp32 …
    yaml_path.write_text(
        "esphome:\n  name: kitchen\nesp32:\n  board: esp32-c3-devkitm-1\n",
        encoding="utf-8",
    )
    # … but StorageJSON records rp2040 (post-codegen truth).
    # ``core_platform`` is the lowercase platform key upstream
    # writes alongside the uppercase ``target_platform`` chip
    # variant; override both to keep the on-disk shape consistent.
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={
            "core_platform": "rp2040",
            "esp_platform": "RP2040",
            "target_platform": "RP2040",
        },
    )

    device = load_device_from_storage(yaml_path)

    assert device.target_platform == "rp2040"


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_uses_core_platform_for_esp32_variants(tmp_path: Path) -> None:
    """ESP32 variants land as ``esp32`` (the platform key), not the chip variant.

    ``StorageJSON.target_platform`` is the upstream-canonical
    chip variant (``ESP32S3`` here) — the right level of detail
    for chip-mismatch verification but the wrong level for the
    frontend's PLATFORM column, where the user expects the
    family name (``esp32``) to match the YAML key. The loader
    pulls from ``core_platform`` (lowercase platform key, always
    ``esp32`` for any ESP32 variant) so a heterogeneous ESP32-
    S3/C3 fleet renders consistently against plain ``esp32``
    boards. Variant-level info is still available to chip
    verification, which reads ``StorageJSON.target_platform``
    directly at the firmware-controller call site.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(
        "esphome:\n  name: kitchen\nesp32:\n  variant: esp32s3\n",
        encoding="utf-8",
    )
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={
            "core_platform": "esp32",
            "esp_platform": "ESP32S3",
            "target_platform": "ESP32S3",
        },
    )

    device = load_device_from_storage(yaml_path)

    assert device.target_platform == "esp32"


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_falls_back_to_yaml_when_core_platform_missing(tmp_path: Path) -> None:
    """Pre-2025.6 ``StorageJSON`` (no ``core_platform``) falls back to YAML scan.

    ``core_platform`` was added in esphome#9028 (2025.6+). A
    StorageJSON written by an older esphome carries
    ``target_platform`` (uppercase variant) but no
    ``core_platform``. Rather than lowercase the variant
    (which would surface ``esp32s3`` in the column for ESP32-S3
    boards — re-introducing the inconsistency #137 closed), the
    loader falls back to ``detect_platform_from_yaml``, which
    returns the lowercase platform key from the YAML's top-level
    ``esp32:`` / ``esp8266:`` block. Same end value either way.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(
        "esphome:\n  name: kitchen\nesp32:\n  variant: esp32s3\n",
        encoding="utf-8",
    )
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        # Pre-2025.6 shape: ``core_platform`` absent, only the
        # uppercase variant in ``target_platform``.
        overrides={
            "core_platform": None,
            "esp_platform": "ESP32S3",
            "target_platform": "ESP32S3",
        },
    )

    device = load_device_from_storage(yaml_path)

    assert device.target_platform == "esp32"


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_handles_storage_without_core_platform_attr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``StorageJSON`` from older esphome (< 2025.6) lacks ``core_platform`` entirely.

    pyproject's floor is ``esphome>=2024.1.0`` so the attribute
    can be missing on the loaded object — not just ``None``.
    Direct attribute access would raise ``AttributeError`` and
    blow up the device scan. ``getattr`` with a default keeps
    the loader compatible while we wait for the dep floor to
    move past 2025.6.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(
        "esphome:\n  name: kitchen\nesp32:\n  board: esp32-c3-devkitm-1\n",
        encoding="utf-8",
    )

    class _LegacyStorage:
        # Pre-#9028 ``StorageJSON`` shape — no ``core_platform``
        # attribute at all. Carries the upstream-canonical chip
        # variant uppercase as ``target_platform``.
        name = "kitchen"
        friendly_name = None
        comment = None
        address = ""
        web_port = None
        target_platform = "ESP32C3"
        firmware_bin_path = None
        esphome_version = ""
        loaded_integrations: ClassVar[list[str]] = []

    monkeypatch.setattr(
        "esphome_device_builder.helpers.device_yaml.StorageJSON.load",
        staticmethod(lambda _p: _LegacyStorage()),
    )

    device = load_device_from_storage(yaml_path)

    assert device.target_platform == "esp32"


# ---------------------------------------------------------------------------
# load_device_from_storage — labels threading
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_threads_labels_into_device(tmp_path: Path) -> None:
    """The ``labels`` arg lands on ``Device.labels`` as a list.

    The scanner reads the per-device labels list from the metadata
    sidecar and threads it through here; the loader's job is only
    to copy it onto the freshly-built ``Device``. A regression
    that dropped the assignment would empty every device's labels
    list on every reload, masking a working sidecar write.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    device = load_device_from_storage(yaml_path, labels=("lbl-a", "lbl-b"))

    assert device.labels == ["lbl-a", "lbl-b"]


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_default_labels_is_empty_list(tmp_path: Path) -> None:
    """Omitting ``labels`` produces an empty list (not ``None`` or a tuple).

    The wire shape is ``list[str]``; mashumaro would happily
    serialize a tuple but the frontend expects array semantics.
    Pin: omit the arg → device.labels is exactly ``[]``.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    device = load_device_from_storage(yaml_path)

    assert device.labels == []
    assert isinstance(device.labels, list)


# ---------------------------------------------------------------------------
# load_device_from_storage — monitor-derived field carry-forward
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_carries_api_encryption_active_from_previous(tmp_path: Path) -> None:
    """Reload preserves the mDNS-observed ``api_encryption_active``.

    The mDNS browser fires on Added/Updated, populates
    ``api_encryption_active="Noise_..."`` on the live ``Device``,
    and then sleeps until the next service-record TTL refresh — a
    couple of minutes in the typical fleet. Anything that triggers
    ``scanner.reload`` between announces (a successful flash, an
    ``--only-generate`` run, an unrelated YAML edit on the sibling
    device, an atomic-save remove/re-add cycle) used to wipe the
    field back to ``None`` because the new ``Device`` was built
    from defaults — the user saw a freshly-flashed encrypted
    device flip into the "Pending install" warning despite the
    firmware on the wire still broadcasting encryption.

    Mirrors the existing carry-forward shape for ``state``,
    ``deployed_config_hash``, and ``ip_addresses``: pass
    ``previous`` and ``api_encryption_active`` round-trips
    through.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    previous = load_device_from_storage(yaml_path)
    previous.api_encryption_active = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"

    reloaded = load_device_from_storage(yaml_path, previous=previous)

    assert reloaded.api_encryption_active == "Noise_NNpsk0_25519_ChaChaPoly_SHA256"


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_carries_plaintext_confirmation_from_previous(tmp_path: Path) -> None:
    """The empty-string ``api_encryption_active`` ("confirmed plaintext") also carries.

    The tri-state shape (``"…"`` / ``""`` / ``None``) means
    ``""`` is a *positive* observation — mDNS saw the broadcast,
    the ``api_encryption`` TXT was absent, the device is running
    plaintext. Wiping that to ``None`` on reload would re-enter
    the "encryption unknown" UI state and re-trigger the
    "Pending install" path on devices the dashboard has already
    confirmed as plaintext. Falsy guards in the carry-forward
    would silently re-introduce the bug, so the test pins the
    empty-string case explicitly.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    previous = load_device_from_storage(yaml_path)
    previous.api_encryption_active = ""

    reloaded = load_device_from_storage(yaml_path, previous=previous)

    assert reloaded.api_encryption_active == ""


@pytest.mark.usefixtures("_redirect_ext_storage")
def test_load_device_without_previous_defaults_api_encryption_active_to_none(
    tmp_path: Path,
) -> None:
    """First load (no ``previous``) yields the unknown / not-yet-seen sentinel.

    mDNS hasn't reported and the YAML's ``api_encrypted`` flag
    can't tell us what's actually on the wire — ``None`` is the
    correct "trust the YAML until proven otherwise" state.
    """
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    write_storage_json(tmp_path, "kitchen.yaml")

    device = load_device_from_storage(yaml_path)

    assert device.api_encryption_active is None
