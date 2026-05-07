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

import pytest

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
    assert ctrl._scanner.calls == [("scan",)]


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


async def test_edit_friendly_name_rejects_source_with_no_inline_leaf(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Package-driven friendly_name (no inline leaf) is rejected.

    When the ``esphome:`` block lives in a ``packages:`` /
    ``!include``d file, this YAML has no ``friendly_name:`` leaf
    for the rewriter to touch — silently no-op'ing would leave
    the dashboard label disagreeing with what compiles. Mirror the
    same precondition the clone path uses (and the shared
    ``_rewrite_required_yaml_leaf`` helper raises through).
    """
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    yaml = "packages:\n  base: !include common/base.yaml\nesp32:\n  variant: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml, "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.edit_friendly_name(
            configuration="kitchen.yaml", new_friendly_name="Reading Lamp"
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "esphome.friendly_name" in excinfo.value.message
    # File untouched.
    assert (tmp_path / "kitchen.yaml").read_text("utf-8") == yaml


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
