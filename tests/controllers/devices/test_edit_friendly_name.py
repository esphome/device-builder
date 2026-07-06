"""Tests for the ``devices/edit_friendly_name`` command path.

The command rewrites ``esphome.friendly_name:`` in the source YAML
in-place (no sidecar drift), reusing the same machinery the clone
path is built on. Frontend drives the install half — this command
just lands the YAML edit and triggers a scan.

What we pin:

- happy path: literal-leaf rewrite + scan, returns ``rewritten=True``
- substitution-driven leaf (``friendly_name: ${friendly_name}``)
  redirects through ``substitutions.<var>``
- YAML-special characters (``Bedroom #2``) get safely double-quoted
- idempotent no-op (same value already on the line) skips the
  write and returns ``rewritten=False``
- user-correctable failures raise ``INVALID_ARGS``: blank input,
  missing source, no inline ``esphome.friendly_name`` leaf
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers._device_scanner import DeviceFileMetadata, DeviceScanner
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

from .conftest import MakeControllerFactory

SOURCE_YAML = """\
esphome:
  name: kitchen
  friendly_name: Kitchen Lamp

esp32:
  variant: ESP32

api:
  encryption:
    key: "AAABBB=="
"""


async def test_edit_friendly_name_rewrites_literal_leaf_and_scans(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: literal ``friendly_name:`` leaf gets rewritten in place.

    Pin the three observable effects in one trace: the YAML on
    disk is updated, ``rewritten=True`` is returned (so the
    frontend knows to follow with an install), and the scanner is
    nudged so the next ``devices/list`` reflects the new label
    without waiting for the periodic poll.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml",
        new_friendly_name="Reading Lamp",
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": True}
    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert "  friendly_name: Reading Lamp\n" in new_yaml
    assert "Kitchen Lamp" not in new_yaml
    # Other leaves untouched.
    assert "  name: kitchen\n" in new_yaml
    assert '    key: "AAABBB=="' in new_yaml
    assert ctrl._scanner.calls == [("request", "kitchen.yaml")]


async def test_edit_friendly_name_schedules_storage_regenerate(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful rewrite schedules a StorageJSON regen."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    scheduled: list[str] = []
    monkeypatch.setattr(ctrl, "_schedule_storage_regenerate", scheduled.append, raising=False)

    await ctrl.edit_friendly_name(
        configuration="kitchen.yaml",
        new_friendly_name="Reading Lamp",
    )

    assert scheduled == ["kitchen.yaml"]


async def test_edit_friendly_name_redirects_through_substitution(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Wizard / dashboard_import shape: rewrite the substitution definition.

    A source with ``friendly_name: ${friendly_name}`` paired with
    ``substitutions.friendly_name: …`` must rewrite the
    substitution rather than the leaf — a leaf rewrite would
    orphan the substitution and break any other consumer
    (e.g. a sensor named ``${friendly_name} Power``). This is
    the same ``rewrite_name_or_substitution`` behaviour the clone
    path relies on; pin it here so a regression in either
    command surfaces immediately.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = (
        "substitutions:\n"
        "  friendly_name: AC Float Monitor\n"
        "esphome:\n"
        "  name: acmon\n"
        "  friendly_name: ${friendly_name}\n"
    )
    (tmp_path / "acmon.yaml").write_text(yaml, "utf-8")

    await ctrl.edit_friendly_name(configuration="acmon.yaml", new_friendly_name="Pump Watcher")

    new_yaml = (tmp_path / "acmon.yaml").read_text("utf-8")
    # Substitution definition flipped, leaf still references the var.
    assert "  friendly_name: Pump Watcher\n" in new_yaml
    assert "  friendly_name: ${friendly_name}\n" in new_yaml
    assert "AC Float Monitor" not in new_yaml


async def test_edit_friendly_name_safely_quotes_yaml_specials(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``Bedroom #2``-style values get double-quoted so they round-trip.

    Plain-scalar ``friendly_name: Bedroom #2`` would silently
    truncate to ``Bedroom`` (everything after `` #`` becomes a
    YAML comment). The shared ``_safe_yaml_scalar`` should kick
    in and emit double-quoted output instead.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    await ctrl.edit_friendly_name(configuration="kitchen.yaml", new_friendly_name="Bedroom #2")

    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert 'friendly_name: "Bedroom #2"\n' in new_yaml


async def test_edit_friendly_name_is_idempotent_when_value_unchanged(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Submitting the same value the leaf already has is a no-op.

    The dialog might fire on every blur even when the user
    didn't actually change anything; the command should not
    rewrite the file or trigger a scan in that case. The
    ``rewritten=False`` return tells the frontend to skip the
    follow-up install too.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    mtime_before = (tmp_path / "kitchen.yaml").stat().st_mtime_ns

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml",
        new_friendly_name="Kitchen Lamp",  # already the value
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": False}
    # File unchanged (mtime stable, contents identical).
    assert (tmp_path / "kitchen.yaml").stat().st_mtime_ns == mtime_before
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == SOURCE_YAML
    # Scanner not nudged for a no-op edit.
    assert ctrl._scanner.calls == []


async def test_edit_friendly_name_rejects_blank_input(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Whitespace-only ``new_friendly_name`` raises ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(configuration="kitchen.yaml", new_friendly_name="   ")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "new_friendly_name is required" in excinfo.value.message


async def test_edit_friendly_name_rejects_missing_source(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A configuration that doesn't exist surfaces as ``INVALID_ARGS``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(configuration="ghost.yaml", new_friendly_name="Reading Lamp")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "ghost.yaml not found" in excinfo.value.message


async def test_edit_friendly_name_handles_race_between_exists_and_read(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File deleted between exists() and read_text() still surfaces as ``INVALID_ARGS``.

    Earlier draft did ``if not exists(): return None; return
    read_text(...)`` — a TOCTOU window between the two calls
    (atomic-save editor mid-save, racing ``devices/delete``, …)
    would leak ``FileNotFoundError`` past us as an untyped
    exception. The WS layer would then surface
    ``INTERNAL_ERROR`` instead of the user-facing
    ``INVALID_ARGS`` the dialog can render. The fix drops the
    ``exists()`` precheck and folds ``FileNotFoundError`` into
    the missing-source branch directly.

    Patches ``Path.read_text`` to raise so the regression
    isolates the race-fold without depending on FS timing.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    real_read = Path.read_text

    def _vanishing_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == "kitchen.yaml":
            raise FileNotFoundError(str(self))
        return real_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _vanishing_read)
    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "kitchen.yaml not found" in excinfo.value.message


async def test_edit_friendly_name_inserts_into_existing_esphome_block(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``esphome:`` exists but no ``friendly_name:`` — insert the line into the block.

    Configs the user hand-edited or imported via dashboard_import
    sometimes lack ``friendly_name:`` entirely. The editor should
    add the line into the existing ``esphome:`` block rather than
    fail the rename. Pin the placement (inside the block, with
    matching indent) and that other esphome children survive.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = "esphome:\n  name: kitchen\n  area: Kitchen\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": True}
    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert "  friendly_name: Reading Lamp\n" in new_yaml
    # Existing children survived.
    assert "  name: kitchen\n" in new_yaml
    assert "  area: Kitchen\n" in new_yaml
    # New leaf landed inside the block, not at column 0.
    assert "esphome:\nfriendly_name:" not in new_yaml


async def test_edit_friendly_name_prepends_esphome_block_when_absent(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Package-driven config — prepend an ``esphome:`` block with just ``friendly_name:``.

    When ``esphome:`` lives in a ``packages:`` / ``!include``d
    file, the local YAML has no block at all. We prepend one with
    just ``friendly_name:`` and *deliberately* leave ``name:``
    alone — a literal-text check can't see ``esphome.name``
    supplied by a package or substitution, so synthesising a slug
    here would silently override the package-supplied hostname
    and break API discovery / OTA / mDNS. ESPHome's package
    merge gives our local ``friendly_name:`` precedence over the
    package's, so the user's intended display-label override
    lands; ``name:`` is left to the package as-is.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = "packages:\n  base: !include common/base.yaml\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": True}
    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    # New ``esphome:`` block at the top with just friendly_name.
    assert new_yaml.startswith("esphome:\n  friendly_name: Reading Lamp\n")
    # No synthesised ``name:`` line that would override the package's.
    assert "  name:" not in new_yaml
    # Pre-existing top-level keys preserved.
    assert "packages:\n  base: !include common/base.yaml\n" in new_yaml
    assert "esp32:\n  variant: ESP32\n" in new_yaml


async def test_edit_friendly_name_inserts_into_block_without_synthesising_name(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    r"""``esphome:`` exists with no ``name:`` — we add ``friendly_name:`` only.

    Edge case where the user has an ``esphome:`` block (e.g. just
    ``esphome:\n  comment: …``) but no ``name:``. We don't
    synthesise ``name:`` for the same reason as the no-block case:
    a package or substitution may already supply it, and a
    literal-text check can't tell. If neither does, ESPHome's
    schema check surfaces "required key not provided" on the
    next compile — actionable and visible, unlike a silently
    overridden hostname.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = "esphome:\n  comment: Adopted device\nesp32:\n  variant: ESP32\n"
    (tmp_path / "device.yaml").write_text(yaml, "utf-8")

    await ctrl.edit_friendly_name(configuration="device.yaml", new_friendly_name="Living Room Lamp")

    new_yaml = (tmp_path / "device.yaml").read_text("utf-8")
    assert "  friendly_name: Living Room Lamp\n" in new_yaml
    # No synthesised ``name:`` line.
    assert "  name:" not in new_yaml
    # Existing comment preserved.
    assert "  comment: Adopted device\n" in new_yaml


async def test_edit_friendly_name_rejects_flow_style_esphome(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``esphome: { … }`` flow-style mapping surfaces as ``INVALID_ARGS``.

    The line-based upsert can't safely insert into a single-line
    flow scalar without re-parsing the whole mapping. Rather than
    silently appending a duplicate ``esphome:`` key, raise so the
    dialog tells the user to convert to block style.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = 'esphome: { name: kitchen, friendly_name: "Kitchen" }\nesp32:\n  variant: ESP32\n'
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "flow-style" in excinfo.value.message or "block style" in excinfo.value.message
    # File untouched.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == yaml


async def test_edit_friendly_name_blocks_when_validation_fails(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Pre-write validation rejects renames whose YAML won't compile.

    The friendly_name only reaches the device through the next
    install — compile bakes the YAML in, OTA flashes the new
    binary, and the running firmware then announces the new name
    via mDNS. If the compile step fails (e.g. unsupported chip
    variant, schema-invalid component config, missing required
    field) the install never happens, the running firmware keeps
    its old name, and the dashboard label stays frozen at the
    last broadcast.

    User-visible symptom from the bug report: H2 device created
    via the wizard whose default config didn't pass validation;
    the rename appeared to succeed in the dialog but the
    dashboard kept showing the filename-stem fallback because
    the install never ran. Pre-write validation refuses the
    rename here so the user sees an actionable "fix the config
    first" error instead of a silently half-finished rename.

    Pin: when the editor's ``validate_yaml`` returns a
    non-empty ``validation_errors`` list the controller raises
    ``INVALID_ARGS`` *and* leaves the YAML on disk untouched.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {"message": "[esp32] Unsupported chip variant: esp32h2"},
                {"message": "[wifi] required key not provided: ssid"},
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Unsupported chip variant: esp32h2" in excinfo.value.message
    assert "required key not provided: ssid" in excinfo.value.message
    # File untouched — rename refused before the write.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == SOURCE_YAML
    # Scanner not nudged for a refused edit.
    assert ctrl._scanner.calls == []


async def test_edit_friendly_name_blocks_validation_failure_on_substitution_rewrite(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Validation runs on the substitution-redirect rewrite shape too.

    When the source uses ``friendly_name: ${friendly_name}`` +
    ``substitutions.friendly_name: …`` the editor rewrites the
    *substitution definition*, not the leaf. Pin that the
    pre-write validation runs against this rewritten shape with
    the same errors-surfaced behaviour, so a regression that
    skipped validation on this branch (e.g. an early-return that
    only fired for the literal-leaf path) would fail loudly.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = (
        "substitutions:\n"
        "  friendly_name: AC Float Monitor\n"
        "esphome:\n"
        "  name: acmon\n"
        "  friendly_name: ${friendly_name}\n"
    )
    (tmp_path / "acmon.yaml").write_text(yaml, "utf-8")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esp32] required key not provided: board"}],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(configuration="acmon.yaml", new_friendly_name="Pump Watcher")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "required key not provided: board" in excinfo.value.message
    # Source unchanged — the rewrite was computed but the write
    # never landed because validation refused.
    assert (tmp_path / "acmon.yaml").read_text("utf-8") == yaml
    assert ctrl._scanner.calls == []


async def test_edit_friendly_name_blocks_validation_failure_on_prepend_path(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Validation runs on the prepend-new-block rewrite shape too.

    When the source has no ``esphome:`` block (package-driven
    config), the editor prepends a fresh block carrying just
    ``friendly_name:``. Pin that pre-write validation runs on the
    prepended YAML — a regression that skipped validation here
    would let the user land an unflashable YAML even though the
    other rewrite shapes refuse the same shape.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = "packages:\n  base: !include common/base.yaml\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esp32] required key not provided: board"}],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "required key not provided: board" in excinfo.value.message
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == yaml
    assert ctrl._scanner.calls == []


async def test_edit_friendly_name_validation_error_action_verb(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Error message uses an action verb that matches the operation.

    The handler is ``edit_friendly_name``, not ``rename`` — a
    leftover ``action="rename"`` would render "Can't rename —
    config doesn't validate", confusing for a user who clicked
    "save friendly name". Pin that the surfaced verb tracks
    the actual operation.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esp32] some error"}],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    # The verb appears in the message; "rename" must not, since
    # this isn't the rename handler.
    assert "friendly name" in excinfo.value.message
    assert "Can't rename" not in excinfo.value.message


async def test_edit_friendly_name_caps_validation_error_list_in_message(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A long validation-error list collapses to "first three + (+N more)".

    The CommandError message lands on a toast in the dialog;
    pasting six full validation errors would overflow it and
    drown out the actionable bit. Three errors plus a tail
    counter is enough for the user to see "this isn't a one-line
    fix" and switch to the editor for the full list.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": f"err-{i}"} for i in range(6)],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    msg = excinfo.value.message
    assert "err-0" in msg
    assert "err-1" in msg
    assert "err-2" in msg
    # Errors past the first three are folded into the counter.
    assert "err-3" not in msg
    assert "(+3 more)" in msg


async def test_edit_friendly_name_skips_validation_when_editor_unavailable(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Editor not yet started → rename proceeds without validation.

    Edge case for the dashboard-boot window where
    ``EditorController.start()`` hasn't run yet (the ``esphome``
    CLI lookup may be in flight, or the binary may not be on
    PATH at all in stripped-down container builds). The
    controller treats ``self._db.editor`` being None as "no
    validator available" and lets the rename through rather
    than rejecting every rename for the lifetime of the
    process. The YAML still gets the round-trip parse-meta
    sanity check; we just skip the deeper schema validation.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    ctrl._db.editor = None

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": True}
    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert "  friendly_name: Reading Lamp\n" in new_yaml


async def test_edit_friendly_name_skips_validation_for_idempotent_rewrite(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """No-op rename short-circuits before the validator subprocess fires.

    The validator round-trip is the expensive part of this
    command (~hundreds of ms on the warm path, multiple seconds
    cold). When the user submits the same friendly_name the
    leaf already has, we shouldn't burn that cost just to
    confirm a state we're not about to change. The idempotent
    branch returns ``rewritten=False`` *before* the validator
    is reached.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")
    validate = AsyncMock(return_value={"yaml_errors": [], "validation_errors": []})
    ctrl._db.editor.validate_yaml = validate

    result = await ctrl.edit_friendly_name(
        configuration="kitchen.yaml",
        new_friendly_name="Kitchen Lamp",  # already the value in SOURCE_YAML
    )

    assert result == {"configuration": "kitchen.yaml", "rewritten": False}
    validate.assert_not_called()


async def test_edit_friendly_name_raises_internal_error_on_round_trip_mismatch(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reader-disagreement with the rewriter surfaces as INTERNAL_ERROR.

    Defends against the column-0-comment class of bug — the
    rewriter produces a YAML the parser misinterprets — by
    parsing the rewritten content back through ``parse_esphome_meta``
    and refusing to write if it doesn't see the new friendly_name.
    Simulate a future regression by patching the parser to return
    None and pin the typed error + redaction-friendly hint.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_simple.parse_esphome_meta",
        lambda _content: (None, None, None, None),
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert "round-trip" in excinfo.value.message
    # Hints user toward redacted reproduction (not the full file).
    assert "redacted" in excinfo.value.message
    # File untouched — refused before write.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == SOURCE_YAML


async def test_edit_friendly_name_routes_through_atomic_write_helper(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source YAML survives a mid-write crash inside the atomic helper.

    The controller writes through ``esphome.helpers.write_file``,
    which stages the new bytes in a sibling tempfile and then
    ``shutil.move`` s into place. ``Path.write_text`` would
    truncate the destination first, so a crash mid-write would
    leave a partial / corrupt YAML. Pin that the controller uses
    the atomic helper by patching ``shutil.move`` to raise during
    the rename — the destination must come back unchanged and no
    tempfile shrapnel can be left behind.

    Patches at ``shutil.move`` rather than ``os.replace`` because
    that's the exact entry point ``esphome.helpers.write_file``
    routes through; a regression that swapped back to a
    non-atomic path would skip ``shutil.move`` entirely and we'd
    catch it as "the patched move was never called and the file
    got modified anyway."
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    (tmp_path / "kitchen.yaml").write_text(SOURCE_YAML, "utf-8")

    boom = RuntimeError("simulated mid-rename crash")
    move_calls: list[tuple[str, str]] = []

    def _exploding_move(src: str, dst: str) -> None:
        move_calls.append((str(src), str(dst)))
        # Mirror the cleanup the real ``shutil.move`` would have
        # done if the rename had succeeded so the regression test
        # observes "no leftover tempfile" via the helper's own
        # finally-clause cleanup, not via the move itself.
        Path(src).unlink(missing_ok=True)
        raise boom

    monkeypatch.setattr(shutil, "move", _exploding_move)
    with pytest.raises(RuntimeError, match="simulated mid-rename"):
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    # Source untouched — atomic-write contract held.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == SOURCE_YAML
    # No leftover tempfile siblings (write_file's finally cleans up).
    leftover = [p.name for p in tmp_path.iterdir() if p.name != "kitchen.yaml"]
    assert leftover == []
    # Pin the helper got invoked — a regression that switched back
    # to ``Path.write_text`` would skip ``shutil.move`` entirely.
    assert len(move_calls) == 1
    _ = boom  # silence the unused-name complaint without a noqa


async def test_edit_friendly_name_preserves_unrelated_lines(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """The encryption key + sensor configs survive the rewrite intact.

    ``rewrite_name_or_substitution`` is path-scoped, but pin it
    here so a future regression that broadens the rewrite (and
    accidentally clobbers ``api.encryption.key`` or random
    ``name:`` lookalikes inside sensor blocks) fails CI.
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = (
        "esphome:\n"
        "  name: kitchen\n"
        "  friendly_name: Kitchen Lamp\n"
        "api:\n"
        "  encryption:\n"
        '    key: "PRESERVE_THIS_KEY=="\n'
        "sensor:\n"
        "  - platform: dht\n"
        "    name: kitchen-temp  # lookalike\n"
    )
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    await ctrl.edit_friendly_name(configuration="kitchen.yaml", new_friendly_name="Reading Lamp")

    new_yaml = (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert "  friendly_name: Reading Lamp\n" in new_yaml
    assert '    key: "PRESERVE_THIS_KEY=="\n' in new_yaml
    assert "    name: kitchen-temp  # lookalike\n" in new_yaml


@pytest.mark.parametrize(
    ("yaml_before", "expected_name", "expected_friendly"),
    [
        # Inline literal — straight rewrite.
        (
            "esphome:\n  name: brandnew\n  friendly_name: brandnew\n\nesp32:\n  variant: ESP32\n",
            "brandnew",
            "The BRAND NEW",
        ),
        # No esphome block at all (package-driven). We don't
        # synthesise ``name:`` here — a slug landing on top of
        # the package's name would silently change device
        # identity. Scanner falls back to the filename stem.
        (
            "packages:\n  base: !include common/base.yaml\nesp32:\n  variant: ESP32\n",
            "brandnew",
            "The BRAND NEW",
        ),
        # esphome block but no name OR friendly_name. Same
        # rationale: don't synthesise; the user can add ``name:``
        # if their config genuinely needs one.
        (
            "esphome:\n  comment: Something\nesp32:\n  variant: ESP32\n",
            "brandnew",
            "The BRAND NEW",
        ),
        # Regression for a real bug: the wizard's
        # ``generate_device_yaml`` emits ``# Board: …`` /
        # ``# Definition: …`` annotations at column 0 above the
        # ``esphome:`` block. The upsert must not pull those
        # column-0 comments *into* the synthesised block —
        # landing them between two indented children produces a
        # YAML where ``parse_esphome_meta`` reads ``# Board:`` as
        # a fresh top-level key, drops the ``esphome:`` context,
        # and silently loses ``friendly_name`` on the next load.
        # User-visible symptom: dashboard keeps showing the old
        # filename-stem fallback instead of the new label.
        (
            "# Board: Generic ESP32-H2 Board (Generic)\n"
            "# Definition: definitions/boards/generic-esp32h2/manifest.yaml\n"
            "\n"
            "esp32:\n  variant: esp32h2\n",
            "brandnew",
            "The BRAND NEW",
        ),
    ],
)
async def test_edit_friendly_name_end_to_end_through_real_scanner(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    yaml_before: str,
    expected_name: str,
    expected_friendly: str,
) -> None:
    """End-to-end: real DeviceScanner sees the new friendly_name after edit.

    The unit tests in this file mostly assert against the YAML on
    disk, but the user-facing chain runs YAML write → scanner
    detects mtime/inode change → ``_load_devices`` rebuilds the
    Device → ``_on_change(UPDATED)`` fires → frontend's
    ``_devices`` reflects the new fields. A regression in any of
    those steps would still pass the disk-content tests but show
    up as "I renamed it but the dashboard still shows the old
    name."

    Pin the full pipeline against a real scanner across the three
    starting-shape variants the controller handles: inline literal
    (rewrite), no-esphome-block (prepend with synthesised name),
    esphome-block-without-name (insert with synthesised name).
    All three should produce a Device with ``friendly_name`` set
    to the user's new value visible via ``scanner.devices`` —
    which is exactly what ``devices/list`` and the ``initial_state``
    snapshot return on a page reload.
    """
    cfg = tmp_path / "brandnew.yaml"
    cfg.write_text(yaml_before, encoding="utf-8")

    metadata = DeviceFileMetadata(board_id="", ip="")
    real_scanner = DeviceScanner(
        tmp_path,
        get_metadata=lambda _cdir, _name: metadata,
        on_change=lambda _kind, _device, _previous: None,
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    ctrl._scanner = real_scanner  # swap the recording fake for a real one

    await real_scanner.scan()
    [seeded] = real_scanner.devices
    # Sanity: pre-edit the device's name falls back to the filename
    # stem when the YAML doesn't carry an esphome.name.
    assert seeded.configuration == "brandnew.yaml"

    real_scanner.start()
    try:
        result = await ctrl.edit_friendly_name(
            configuration="brandnew.yaml", new_friendly_name="The BRAND NEW"
        )
        # ``_persist_yaml_mutation`` queues a background reload on
        # the scanner's worker; ``wait_idle`` parks until the
        # drain (and the reload it contains) actually completes.
        await real_scanner.wait_idle()
    finally:
        await real_scanner.stop()

    assert result == {"configuration": "brandnew.yaml", "rewritten": True}
    [updated] = real_scanner.devices
    assert updated.name == expected_name
    assert updated.friendly_name == expected_friendly
    assert updated.configuration == "brandnew.yaml"
