"""Tests for the ``devices/create`` command path.

Regression coverage for #81: the three user-correctable failures in
``create_device`` (name collision, empty name, unknown board_id) must
arrive at the WS dispatcher as ``CommandError(INVALID_ARGS, …)`` so
the wizard can show a specific message instead of the generic
``Command failed`` fallback the WS layer emits for any other
exception.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import warnings
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import esphome.config_validation as cv
import pytest
from esphome.core.config import FRIENDLY_NAME_MAX_LEN
from esphome.storage_json import StorageJSON
from ruamel.yaml import YAML

from esphome_device_builder.controllers.config import (
    get_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.controllers.devices.helpers import (
    clean_friendly_name,
    slugify_hostname,
)
from esphome_device_builder.controllers.devices.mutations_yaml import (
    yaml_content_for_create,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import _safe_yaml_scalar
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory, StubBoardLookups

# ESPHome's friendly_name field validator (no slash + byte cap).
_FRIENDLY_NAME_VALIDATOR = cv.All(cv.string_no_slash, cv.ByteLength(max=FRIENDLY_NAME_MAX_LEN))

VALID_FILE_CONTENT = (
    "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n"
    "esp32:\n  variant: esp32\n  board: nodemcu-32s\n"
)


async def test_create_device_translates_file_exists_to_command_error(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Re-creating an existing config raises ``ALREADY_EXISTS`` so the wizard can offer overwrite.

    Without the typed error, the WS dispatcher falls back to a generic
    "Command failed"; the dedicated code lets the frontend route to its
    overwrite-confirm step instead of a dead-end message.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", file_content=VALID_FILE_CONTENT)

    assert excinfo.value.code == ErrorCode.ALREADY_EXISTS
    assert "kitchen.yaml already exists" in excinfo.value.message
    # Nothing should hit the scanner when the pre-flight check fails.
    assert ctrl._scanner.calls == []


async def test_create_device_rejects_empty_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Whitespace-only names produce ``INVALID_ARGS`` instead of a bare ``ValueError``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="   ")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "name is required" in excinfo.value.message
    assert ctrl._scanner.calls == []


async def test_create_device_rejects_unknown_board_id(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An unknown ``board_id`` produces ``INVALID_ARGS`` and names the bad id."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    StubBoardLookups(ctrl).get_board_returns(None)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", board_id="bogus-board")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "bogus-board" in excinfo.value.message
    assert ctrl._scanner.calls == []


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_emits_minimal_stub_when_no_board_or_file_content(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No board / no file_content → minimal valid esp32 stub.

    The wizard's "Empty Configuration — for manually writing or
    pasting" button hits this path: the user wants a starter
    they can fully rewrite. The starter MUST validate so every
    downstream operation (rename, edit_friendly_name, install)
    accepts it; the previous "name-only" stub failed schema
    validation and silently broke those flows. The stub now
    defaults to esp32 + ``board: esp32dev`` with a leading
    "Replace this with your platform" comment so the silent-
    bind concern is at least visible in the file the user is
    about to edit.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    # Catalog returns a board for ``esp32dev`` to model the realistic
    # scenario flagged in review: many curated entries share that
    # PIO board, so a naive lookup would happily pick one.
    pio_lookup = boards.find_by_pio_board_returns("generic-esp32-board")
    variant_lookup = boards.find_by_platform_variant_returns("generic-esp32-board")

    result = await ctrl.create_device(name="kitchen")

    assert result.configuration == "kitchen.yaml"
    yaml_path = tmp_path / "kitchen.yaml"
    content = yaml_path.read_text("utf-8")
    assert "esphome:\n  name: kitchen\n  friendly_name: kitchen\n" in content
    assert "esp32:\n  board: esp32dev\n" in content
    assert "Replace this with your actual platform" in content
    assert "api:\n  encryption:\n    key:" in content
    assert ctrl._scanner.calls == [("scan",)]
    # Stub branch deliberately skips the catalog lookup so an
    # arbitrary entry sharing ``esp32dev`` doesn't get pinned to
    # this device's metadata before the user picks real hardware.
    pio_lookup.assert_not_called()
    variant_lookup.assert_not_called()


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_slugifies_hostname_and_preserves_raw_name_as_friendly(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pins that raw input drives ``friendly_name`` while a slug drives ``name`` and filename."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    result = await ctrl.create_device(name="Lüftung EG Bad")

    assert result.configuration == "luftung-eg-bad.yaml"
    content = (tmp_path / "luftung-eg-bad.yaml").read_text("utf-8")
    assert "esphome:\n  name: luftung-eg-bad\n  friendly_name: Lüftung EG Bad\n" in content
    storage = StorageJSON.load(tmp_path / "storage.json")
    assert storage is not None
    assert storage.name == "luftung-eg-bad"
    assert storage.friendly_name == "Lüftung EG Bad"


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_quotes_friendly_name_with_yaml_metachars(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pins that YAML metacharacters in ``friendly_name`` round-trip through quoted scalars."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    result = await ctrl.create_device(name="Bedroom #2: lamp")

    assert result.configuration == "bedroom-2-lamp.yaml"
    content = (tmp_path / "bedroom-2-lamp.yaml").read_text("utf-8")
    # `#` would otherwise start a comment; `: ` would split into a
    # nested key/value pair. The safe-scalar renderer double-quotes
    # the value so neither happens on round trip.
    assert '  friendly_name: "Bedroom #2: lamp"\n' in content
    storage = StorageJSON.load(tmp_path / "storage.json")
    assert storage is not None
    assert storage.friendly_name == "Bedroom #2: lamp"


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_rejects_name_with_no_hostname_safe_characters(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pins that a name slugifying to empty (only emoji etc.) raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="🚀🚀🚀")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "hostname-safe" in excinfo.value.message
    assert ctrl._scanner.calls == []


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_swaps_reserved_slash_in_friendly_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``/`` in the name becomes ``⁄`` in friendly_name, ESPHome's own swap (#1070)."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    result = await ctrl.create_device(name="Living Room / Bath #2")

    assert result.configuration == "living-room--bath-2.yaml"
    storage = StorageJSON.load(tmp_path / "storage.json")
    assert storage is not None
    # ESPHome reserves ``/`` (deprecated, hard error in 2026.7.0); the
    # backend swaps it for ``⁄`` so the generated YAML validates.
    assert storage.friendly_name == "Living Room ⁄ Bath #2"
    assert "/" not in storage.friendly_name


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_strips_control_chars_from_friendly_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Control / EOL chars become spaces (collapsed) so the YAML scalar stays clean (#1070)."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    # Newline + tab become spaces; bell + NUL are dropped; a literal NUL
    # would otherwise make the generated YAML unparsable.
    result = await ctrl.create_device(name="Bed\nroom\trm\x07\x00")

    assert result.configuration == "bed-room-rm.yaml"
    storage = StorageJSON.load(tmp_path / "storage.json")
    assert storage is not None
    assert storage.friendly_name == "Bed room rm"
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in storage.friendly_name)


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_clamps_friendly_name_to_byte_limit(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An over-long name is clamped to the friendly_name byte cap on a char boundary (#1070)."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    # 70 × ``ü`` = 140 UTF-8 bytes (2 each); friendly_name clamps to 60
    # (120 bytes) on a char boundary, while the hostname is independently
    # clamped to ESPHome's 31-char name cap. Both caps must hold or the
    # create fails ESPHome validation.
    result = await ctrl.create_device(name="ü" * 70)

    storage = StorageJSON.load(tmp_path / "storage.json")
    assert storage is not None
    assert storage.friendly_name == "ü" * 60
    assert len(storage.friendly_name.encode("utf-8")) == 120
    assert storage.name == "u" * 31
    assert result.configuration == f"{'u' * 31}.yaml"


def test_slugify_hostname_clamps_after_trimming_the_cut_dash() -> None:
    """The clamp is applied before the dash-strip so the cut can't leave a trailing dash (#1070)."""
    # 30 'a' + " b" slugs to 'aaaa…(30)-b' (32 chars); the cut at 31
    # lands on the dash. Stripping must happen AFTER the truncation,
    # or the hostname would end in '-'.
    assert slugify_hostname("a" * 30 + " b") == "a" * 30
    # General length clamp + validity.
    long_name = "A Very Long Living Room Climate Sensor Name Here"
    out = slugify_hostname(long_name)
    assert len(out) <= 31
    assert not out.startswith("-") and not out.endswith("-")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("Bedroom #2: lamp", id="hash-and-colon"),
        pytest.param('Lamp "quoted"', id="double-quote"),
        pytest.param("back\\slash", id="backslash"),
        pytest.param("it's a 'test'", id="single-quote"),
        pytest.param(": leading colon", id="leading-colon"),
        pytest.param("- leading dash", id="leading-dash"),
        pytest.param("! & * ? | > % @ ` ~ [ ] { } ,", id="all-indicators"),
        pytest.param("trailing colon:", id="trailing-colon"),
        pytest.param("null", id="reserved-word"),
        pytest.param("Kitchen/Bath", id="slash"),
        pytest.param("emoji 🚀 home", id="emoji"),
        pytest.param("tab\tnl\ncr\r", id="eol-and-tab"),
        pytest.param("\x00\x07\x1b bell", id="c0-control"),
        pytest.param("C1\x85\x9f here", id="c1-control"),
        pytest.param("x" * 200, id="over-byte-cap"),
    ],
)
def test_clean_friendly_name_round_trips_and_validates(raw: str) -> None:
    """Both derived fields stay valid for any raw input (#1070).

    Pins that ``clean_friendly_name`` + ``_safe_yaml_scalar`` leave no
    character class that lands invalid YAML or a schema-invalid
    friendly_name, and that ``slugify_hostname`` always yields a valid,
    length-capped ``esphome.name``.
    """
    cleaned = clean_friendly_name(raw)
    if not cleaned:
        return  # slugs/cleans to empty -> create rejects via "name is required"

    # ESPHome accepts it, with no friendly_name deprecation warning
    # (the slash was already swapped for ``⁄``).
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _FRIENDLY_NAME_VALIDATOR(cleaned) == cleaned

    # And the emitted scalar re-parses to exactly the cleaned value.
    doc = f"esphome:\n  name: x\n  friendly_name: {_safe_yaml_scalar(cleaned)}\n"
    parsed = YAML(typ="safe").load(io.StringIO(doc))
    assert parsed["esphome"]["friendly_name"] == cleaned

    # The hostname from the same input is a valid, length-capped name.
    hostname = slugify_hostname(raw)
    if hostname:
        assert len(hostname) <= 31
        assert cv.valid_name(hostname) == hostname


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_accepts_invalid_file_content_for_user_repair(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """User uploads of schema-invalid YAML still land on disk.

    The "upload YAML" flow exists so a user can bring an existing
    config into the builder and repair it in the editor. Many
    real-world cases are configs from older ESPHome versions
    whose components have since changed schema — refusing the
    upload would strand the user with no way to get the file in
    front of the editor where they can fix it. Pin: even when
    the (mocked) validator returns errors for the uploaded YAML,
    the file still lands on disk and the scanner fires so the
    device shows up in the dashboard for the user to edit.
    Validation runs only on our generators (template / stub
    branches) where an invalid output is *our* regression.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)
    # Mock the validator to return errors. We assert the
    # upload succeeds anyway (validate_yaml should never be
    # called for the user-upload branch).
    validate = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {"message": "[esphome] required key not provided: a platform"},
            ],
        }
    )
    ctrl._db.editor.validate_yaml = validate
    invalid_file_content = "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n"

    result = await ctrl.create_device(name="kitchen", file_content=invalid_file_content)

    assert result.configuration == "kitchen.yaml"
    # File landed verbatim on disk so the user can open it in
    # the editor.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == invalid_file_content
    # Scanner nudged so the device shows up in ``devices/list``.
    assert ctrl._scanner.calls == [("scan",)]
    # Validator must NOT have been called for the upload branch.
    validate.assert_not_called()


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_accepts_old_esphome_version_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A YAML using deprecated upstream syntax still uploads cleanly.

    Concrete regression for the "I upgraded ESPHome and now my
    config doesn't validate" scenario. The YAML below uses the
    pre-2024 ``esp32: { board: ..., framework: { type: arduino } }``
    flat shape and a deprecated ``esphome.platform: ESP32`` key
    that current ESPHome rejects. Even with a validator that
    flags multiple deprecation errors, the upload must succeed
    so the user can open the file in the editor and fix it.
    Without this acceptance the upload path would be useless for
    its primary use case (importing legacy configs to repair).
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)
    legacy_yaml = (
        "esphome:\n"
        "  name: old-device\n"
        "  platform: ESP32\n"  # deprecated key
        "  board: nodemcu-32s\n"  # deprecated location
        "esp32:\n"
        "  framework:\n"
        "    type: arduino\n"
        "    version: 2.0.5\n"
        "wifi:\n"
        "  ssid: !secret wifi_ssid\n"
        "  password: !secret wifi_password\n"
        "  use_address: 192.168.1.50\n"  # legacy field renamed in newer schema
    )
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {"message": "[esphome] 'platform' has been deprecated"},
                {"message": "[esphome] 'board' has been deprecated"},
                {"message": "[esp32.framework] 'version' is no longer supported"},
            ],
        }
    )

    result = await ctrl.create_device(name="old-device", file_content=legacy_yaml)

    assert result.configuration == "old-device.yaml"
    written = (tmp_path / "old-device.yaml").read_text("utf-8")
    assert written == legacy_yaml
    assert ctrl._scanner.calls == [("scan",)]


async def test_create_device_rejects_binary_file_content(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A .tar.gz read as text is refused with ``INVALID_ARGS``, not written."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    # Mirror the frontend's readAsText of a gzip archive.
    bundle_bytes = gzip.compress(b"esphome:\n  name: kitchen\n")
    file_content = bundle_bytes.decode("utf-8", "replace")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", file_content=file_content)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "binary" in excinfo.value.message
    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


async def test_create_device_rejects_file_content_with_nul(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A lone NUL in otherwise-texty content is still refused (unparsable YAML)."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    content = "esphome:\n  name: kitchen\x00\n"

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", file_content=content)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_template_invalid_yaml_surfaces_internal_error(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Generator producing an invalid YAML is *our* bug, not the user's.

    When the wizard's ``board_id`` template emits something that
    doesn't validate, that's a regression in
    ``generate_device_yaml`` — the user can't fix it. Raise
    ``INTERNAL_ERROR`` with a "please report" hint so the
    diagnostic lands in our issue tracker rather than confusing
    the user with a "config doesn't validate" they didn't write.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    # Board returns a valid catalog entry that drives ``generate_device_yaml``.
    board = MagicMock()
    board.id = "esp32-c3"
    board.esphome.platform = "esp32"
    board.esphome.variant = "esp32c3"
    board.esphome.framework = "esp-idf"
    board.esphome.board = ""
    board.hardware.flash_size = "4MB"
    board.hardware.connectivity = []
    board.name = "Generic ESP32-C3"
    board.manufacturer = "Generic"
    # Empty default_components skips the awaitable catalog resolver
    # path — this test only exercises the generator failure mode.
    board.default_components = []
    ctrl._db.boards.get_board = AsyncMock(return_value=board)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esphome] generator regression"}],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", board_id="esp32-c3")

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert "generator regression" in excinfo.value.message
    assert "report" in excinfo.value.message.lower()
    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


async def test_create_device_clears_residual_metadata_from_archived_same_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Create at a previously-archived filename starts with a clean entry.

    Archive preserves identity fields (``board_id``,
    ``friendly_name``, ``comment``) so an unarchive of the same
    YAML restores user-visible state. But a *new* device created
    at the same filename — even via ``file_content`` whose YAML
    didn't carry a recognised board — must NOT inherit those
    archived fields; otherwise the new device is silently bound
    to the old catalog entry and the dashboard renders the new
    YAML's friendly_name as the archived one's. Pin the wipe-on-
    create contract.
    """
    config_dir = tmp_path
    storage_path = tmp_path / "storage.json"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_create.resolve_storage_path",
        lambda _filename: storage_path,
    )

    # Seed a stale entry as if an archived device left it behind:
    # board_id + friendly_name + comment (volatile fields would
    # already have been cleared by ``_archive_clear_device_sidecars``).
    await asyncio.to_thread(
        set_device_metadata,
        config_dir,
        "kitchen.yaml",
        board_id="esp32-archived-board",
        friendly_name="Archived Kitchen",
        comment="Used to live in the kitchen",
    )
    pre = await asyncio.to_thread(get_device_metadata, config_dir, "kitchen.yaml")
    assert pre["board_id"] == "esp32-archived-board"

    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    ctrl._db.settings.config_dir = config_dir
    # Catalog *would* match, but a no-pick create never persists a
    # derived board_id; the scanner recomputes it on resolve.
    boards = StubBoardLookups(ctrl)
    boards.find_by_pio_board_returns("generic-esp32")
    boards.find_by_platform_variant_returns(None)

    await ctrl.create_device(name="kitchen", file_content=VALID_FILE_CONTENT)

    # Stale entry was cleared and nothing was written back, so the
    # entry is absent (not just empty).
    post = await asyncio.to_thread(get_device_metadata, config_dir, "kitchen.yaml")
    assert post == {}


async def test_create_device_write_race_surfaces_already_exists(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A file appearing between the pre-check and the exclusive write maps to ALREADY_EXISTS."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    fake = MagicMock()
    fake.exists.return_value = False  # pre-check passes
    fake.open.side_effect = FileExistsError  # the exclusive write loses the race
    ctrl._db.settings.rel_path = lambda _filename: fake

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", file_content=VALID_FILE_CONTENT)

    assert excinfo.value.code == ErrorCode.ALREADY_EXISTS


async def test_create_device_overwrite_preserves_metadata(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``overwrite=True`` replaces the YAML but keeps labels / comment / board_id."""
    config_dir = tmp_path
    (config_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", "utf-8")
    await asyncio.to_thread(
        set_device_metadata,
        config_dir,
        "kitchen.yaml",
        labels=["kitchen-label"],
        comment="my note",
        board_id="esp32-pick",
        board_id_user_set=True,
    )

    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    ctrl._db.settings.config_dir = config_dir
    new_content = "esphome:\n  name: kitchen\n  friendly_name: New\nesp32:\n  board: nodemcu-32s\n"

    result = await ctrl.create_device(name="kitchen", file_content=new_content, overwrite=True)

    assert result.configuration == "kitchen.yaml"
    assert (config_dir / "kitchen.yaml").read_text("utf-8") == new_content
    post = await asyncio.to_thread(get_device_metadata, config_dir, "kitchen.yaml")
    assert post.get("labels") == ["kitchen-label"]
    assert post.get("comment") == "my note"
    assert post.get("board_id") == "esp32-pick"
    assert ctrl._scanner.calls == [("scan",)]


async def test_create_device_with_board_id_overwrites_archived_board_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An explicit ``board_id`` on create wins over any residual archived value.

    Companion to the stub-flow test above: when create_device is
    called WITH a board_id, the new value must replace the
    archived one rather than be merged or skipped.
    """
    config_dir = tmp_path
    storage_path = tmp_path / "storage.json"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_create.resolve_storage_path",
        lambda _filename: storage_path,
    )

    await asyncio.to_thread(
        set_device_metadata,
        config_dir,
        "kitchen.yaml",
        board_id="esp32-archived-board",
        friendly_name="Archived Kitchen",
    )

    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    ctrl._db.settings.config_dir = config_dir
    # Catalog returns a usable board for the new id.
    new_board = MagicMock()
    new_board.id = "rp2040-new-board"
    new_board.esphome.platform = "rp2040"
    new_board.template = None
    # Skip the awaitable default-components resolver — this test
    # only cares about the metadata-overwrite path.
    new_board.default_components = []
    ctrl._db.boards.get_board = AsyncMock(return_value=new_board)

    await ctrl.create_device(name="kitchen", board_id="rp2040-new-board")

    post = await asyncio.to_thread(get_device_metadata, config_dir, "kitchen.yaml")
    assert post == {"board_id": "rp2040-new-board", "board_id_user_set": True}


@pytest.mark.xdist_group("catalog")
async def test_yaml_content_for_create_threads_default_components_through(
    session_component_catalog: Any,
) -> None:
    """``yaml_content_for_create`` resolves + emits the board's ``default_components``.

    Pins the wire-up: when *board* declares ``default_components``
    and the *catalog* is provided, the resolver runs and each pair
    flows into ``generate_device_yaml`` via the ``defaults`` kwarg.
    A regression that dropped the catalog argument from the call
    site would leave the generated YAML missing the default blocks
    even though the manifest declared them.
    """
    board = await session_component_catalog._db.boards.get_board(board_id="apollo-esk-1")
    assert board is not None
    yaml, source = await yaml_content_for_create(
        name="starter",
        friendly="Starter Kit",
        board=board,
        file_content=None,
        ssid="",
        psk="",
        catalog=session_component_catalog,
    )
    assert source == "template"
    assert "web_server:" in yaml
    assert "switch:" in yaml
    assert "platform: gpio" in yaml
