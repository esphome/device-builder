"""Tests for ``DevicesController`` archive / unarchive / list_archived.

Mirrors the legacy dashboard's ``ArchiveRequestHandler`` /
``UnArchiveRequestHandler`` (``esphome/dashboard/web_server.py``):

- Archive moves the YAML to ``<config_dir>/archive/`` and wipes
  the per-device PlatformIO build tree (compile output of an
  archived device is dead weight; the user can recompile after
  unarchive).
- Unarchive moves the YAML back, refusing to clobber an active
  config of the same name.
- list_archived parses each archived YAML's ``esphome:`` block so
  the dashboard's "Show archived devices" toggle can render a
  row + Unarchive / Delete-permanently controls.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.config import (
    _load_metadata,
    _save_metadata,
    clear_volatile_device_metadata,
    get_device_metadata,
    set_device_metadata,
)
from esphome_device_builder.controllers.devices.helpers import _unlink_storage_sidecar
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests._storage_fixtures import write_storage_json

from .conftest import MakeControllerFactory, SeedDeviceFactory


@pytest.fixture(autouse=True)
def _ext_storage_for_archive(redirect_storage_path: None) -> None:
    """Re-export ``redirect_storage_path`` as autouse for the archive tests.

    Every test in this file exercises a code path that reads
    ``ext_storage_path`` (``_archive_single`` /
    ``_list_archived_sync`` / ``_wipe_device_build_dir``).
    Pulling the conftest fixture in via an autouse wrapper means
    new tests in this file inherit the redirect automatically —
    a net-new test that forgot to request it would otherwise
    silently hit the real ``CORE.config_path`` (which is unset in
    the test process and crashes deep in ``ext_storage_path``).
    """


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


async def test_archive_moves_yaml_to_archive_dir(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """The YAML lands in ``<config_dir>/archive/<configuration>``.

    The whole point of archive vs delete: the YAML is reversible.
    Pin the destination shape so a refactor that quietly switches
    to ``.archive/`` (hidden) or moves it under ``.esphome/`` (the
    cache root) shows up as a test failure rather than a user
    finding their old configs in an unfamiliar place.
    """
    controller = make_controller(tmp_path)
    yaml_path, _ = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    original_text = yaml_path.read_text()

    await controller._archive_single("kitchen.yaml")

    assert not yaml_path.exists()
    archived = tmp_path / "archive" / "kitchen.yaml"
    assert archived.exists()
    assert archived.read_text() == original_text


async def test_archive_wipes_build_directory(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """An archived device's compile output is dead weight — wipe it.

    Same shape as ``_delete_single``: read ``StorageJSON.build_path``
    and ``rmtree`` it. Without this the disk savings from archiving
    a no-longer-used device would be ~the YAML's worth (a few KB),
    not the build tree's worth (50-200 MB), and users would still
    complain about disk usage on long-running fleets.
    """
    controller = make_controller(tmp_path)
    _, build_path = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    assert build_path.exists()

    await controller._archive_single("kitchen.yaml")

    assert not build_path.exists()


async def test_archive_wipes_storage_sidecar(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """The StorageJSON sidecar is removed when archiving.

    Per-filename keying means a sidecar that survives archive
    would leak the archived device's address / hash /
    loaded_integrations into a future ``configuration`` of the
    same name. Wipe it on archive so the new device gets a
    clean cache; the scanner re-fills via mDNS once the device
    is back online (only a few seconds of "unknown state").
    """
    controller = make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    storage_path = tmp_path / ".esphome" / "storage" / "kitchen.yaml.json"
    assert storage_path.exists()

    await controller._archive_single("kitchen.yaml")

    assert not storage_path.exists()


async def test_archive_wipes_build_caches(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """Regenerable build caches (idedata, validated YAML) go with the build dir.

    Same rationale as wiping the build tree: a future device that
    recycles the filename mustn't inherit the archived device's
    stale idedata / validated-config cache.
    """
    controller = make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    idedata = tmp_path / ".esphome" / "idedata" / "kitchen.json"
    idedata.parent.mkdir(parents=True, exist_ok=True)
    idedata.write_text("{}", encoding="utf-8")
    validated = tmp_path / ".esphome" / "storage" / "kitchen.yaml.validated.yaml"
    validated.write_text("esphome: {}\n", encoding="utf-8")

    await controller._archive_single("kitchen.yaml")

    assert not idedata.exists()
    assert not validated.exists()


async def test_archive_clears_volatile_metadata_keeps_identity(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """Archive scrubs runtime state but keeps stable identity fields.

    Volatile fields (``ip``, ``expected_config_hash``) describe
    the firmware / network state at archive time and go stale
    immediately — clear them. Identity fields (``board_id``,
    ``friendly_name``, ``comment``) survive so an unarchive of
    the same YAML restores user-visible state unchanged.
    ``board_id`` in particular is the catalog → YAML match key;
    losing it on every archive cycle forced an unnecessary
    re-derive on unarchive.
    """
    controller = make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    # Identity + mac_address live in the shared sidecar.
    # ``metadata_transaction`` → ``tempfile.mkstemp`` trips the
    # blockbuster detector from an async context; push to a thread.
    await asyncio.to_thread(
        set_device_metadata,
        tmp_path,
        "kitchen.yaml",
        board_id="esp32-c3-devkitm-1",
        board_id_user_set=True,
        friendly_name="Kitchen Sensor",
        comment="By the toaster",
        mac_address="94:C9:60:1F:8C:F1",
    )
    # Volatile fields seeded directly into the store.
    controller._metadata_store.update(
        "kitchen.yaml",
        ip="192.168.1.42",
        expected_config_hash="deadbeef",
        regen_failed_mtime=1700000000.5,
        regen_failed_at=1700000005.0,
        build_size_bytes=12345678,
        build_size_dir_mtime=1714900000,
        build_size_info_mtime=1714900050,
    )

    await controller._archive_single("kitchen.yaml")

    # Identity survives in the shared sidecar; mac_address is
    # volatile (YAML may unarchive onto a different physical
    # board) and gets cleared alongside the store fields.
    post_shared = await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml")
    assert post_shared == {
        "board_id": "esp32-c3-devkitm-1",
        "board_id_user_set": True,
        "friendly_name": "Kitchen Sensor",
        "comment": "By the toaster",
    }
    assert controller._metadata_store.get("kitchen.yaml") == {}


async def test_archive_drops_metadata_entry_when_only_volatile_fields(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """An entry with only volatile fields is removed entirely on archive.

    Edge case: if a device has metadata that consists *only* of
    runtime/observed fields (no ``board_id`` / ``friendly_name``
    / ``comment`` ever set), clearing the volatile fields would
    leave an empty dict. Drop the entry entirely so the metadata
    file doesn't accumulate dead keys.
    """
    controller = make_controller(tmp_path)
    # ``write_metadata=False`` so the legacy sidecar has no
    # identity entry — the only metadata is the volatile fields we
    # add to the store below, which is the exact precondition this
    # branch exercises.
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True, write_metadata=False)
    controller._metadata_store.update(
        "kitchen.yaml",
        ip="192.168.1.42",
        expected_config_hash="cafebabe",
    )

    await controller._archive_single("kitchen.yaml")

    # The store's volatile-only entry is gone entirely; the legacy
    # sidecar never had an entry for this device to begin with.
    assert controller._metadata_store.get("kitchen.yaml") == {}
    assert controller._metadata_store.snapshot_all() == {}
    raw = await asyncio.to_thread(_load_metadata, tmp_path)
    assert "kitchen.yaml" not in raw


async def test_archive_succeeds_when_never_compiled(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """A device that was never compiled has no build dir — archive still works.

    First-archive happy path is "user just made a YAML, decided
    they don't need it after all". No StorageJSON, no build tree;
    the move-to-archive must still succeed without raising.
    """
    controller = make_controller(tmp_path)
    yaml_path, _ = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=False)
    # Wipe the StorageJSON sidecar to simulate "never compiled".
    (tmp_path / ".esphome" / "storage" / "kitchen.yaml.json").unlink()

    await controller._archive_single("kitchen.yaml")

    assert not yaml_path.exists()
    assert (tmp_path / "archive" / "kitchen.yaml").exists()


async def test_archive_collision_raises_invalid_args(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """Archiving twice with the same name refuses rather than silently renaming.

    The StorageJSON sidecar and metadata are keyed on the original
    filename and stay there across archive. Renaming the second
    archive copy to ``kitchen (2).yaml`` would orphan the suffixed
    YAML from its sidecar — a later unarchive would surface without
    the cached address / version / loaded_integrations. Refuse the
    operation with ``CommandError(INVALID_ARGS)`` and let the user
    resolve the collision (unarchive or permanently delete the
    existing archive copy first).
    """
    controller = make_controller(tmp_path)

    # First archive lands at the plain name.
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    (tmp_path / "kitchen.yaml").write_text("first version\n", encoding="utf-8")
    await controller._archive_single("kitchen.yaml")
    assert (tmp_path / "archive" / "kitchen.yaml").read_text() == "first version\n"

    # Recreate + archive again — must refuse rather than clobber or rename.
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    (tmp_path / "kitchen.yaml").write_text("second version\n", encoding="utf-8")
    with pytest.raises(CommandError) as exc:
        await controller._archive_single("kitchen.yaml")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    # First archive copy and the active YAML both survive untouched.
    assert (tmp_path / "archive" / "kitchen.yaml").read_text() == "first version\n"
    assert (tmp_path / "kitchen.yaml").read_text() == "second version\n"


async def test_archive_missing_file_raises_file_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An archive of a non-existent configuration raises cleanly.

    Symmetric with ``_delete_single`` — the WS layer surfaces the
    raise as a user-facing error so a stale dashboard reference
    (someone else just deleted the device) doesn't silently
    succeed.
    """
    controller = make_controller(tmp_path)
    with pytest.raises(FileNotFoundError):
        await controller._archive_single("ghost.yaml")


async def test_archive_device_full_flow_calls_scanner(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """End-to-end ``archive_device`` runs the helper and re-scans.

    Covers the public-command success path that helper-level tests
    skip: the wrapper's ``_archive_single`` call on success and the
    follow-up ``self._scanner.scan()`` that triggers the
    ``DEVICE_REMOVED`` event for the dashboard.
    """
    controller = make_controller(tmp_path)
    yaml_path, _ = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)

    await controller.archive_device(configuration="kitchen.yaml")

    assert not yaml_path.exists()
    assert (tmp_path / "archive" / "kitchen.yaml").exists()
    assert controller._scanner.calls == [("scan",)]


async def test_unarchive_device_full_flow_calls_scanner(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """End-to-end ``unarchive_device`` runs the helper and re-scans.

    Same shape as ``test_archive_device_full_flow_calls_scanner`` —
    covers the WS-command tail (``_scanner.scan()`` after success).
    """
    controller = make_controller(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    await controller.unarchive_device(configuration="kitchen.yaml")

    assert not (archive_dir / "kitchen.yaml").exists()
    assert (tmp_path / "kitchen.yaml").exists()
    assert controller._scanner.calls == [("scan",)]


async def test_list_archived_full_flow(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """End-to-end ``list_archived`` returns the parsed rows.

    Covers the WS-command body that runs ``_list_archived_sync``
    in an executor — helper-level tests call the sync version
    directly and miss the executor wrapping.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n",
        encoding="utf-8",
    )

    controller = make_controller(tmp_path)
    rows = await controller.list_archived()

    assert len(rows) == 1
    assert rows[0]["configuration"] == "kitchen.yaml"
    assert rows[0]["friendly_name"] == "Kitchen"


async def test_archive_device_translates_missing_to_command_error(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The WS-layer entry point surfaces ``CommandError(NOT_FOUND)`` to the client.

    The internal ``_archive_single`` raises ``FileNotFoundError`` so
    delete / archive symmetry is preserved at the helper level, but
    the public ``archive_device`` wraps it so a stale dashboard
    reference shows up as a clean ``not_found`` over the wire
    instead of a generic ``internal_error``.
    """
    controller = make_controller(tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.archive_device(configuration="ghost.yaml")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_unarchive_device_translates_missing_to_command_error(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``unarchive_device`` mirrors ``archive_device`` for not-found mapping."""
    controller = make_controller(tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.unarchive_device(configuration="ghost.yaml")
    assert exc.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# unarchive
# ---------------------------------------------------------------------------


async def test_unarchive_moves_yaml_back(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Unarchive is the inverse of archive — YAML returns to config_dir.

    The scanner's next sweep then fires ``DEVICE_ADDED``;
    ``unarchive_device`` calls ``self._scanner.scan()`` itself so
    the dashboard refreshes without a manual reload.
    """
    controller = make_controller(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = archive_dir / "kitchen.yaml"
    archived.write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    await controller._unarchive_single("kitchen.yaml")

    assert not archived.exists()
    assert (tmp_path / "kitchen.yaml").exists()


async def test_unarchive_refuses_to_clobber_active_config(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An active YAML with the same name blocks the unarchive.

    The active YAML may carry the user's recent edits; silently
    overwriting it with the archived copy would surprise them.
    Surface a ``CommandError(INVALID_ARGS)`` so the dialog can
    prompt for an alternate filename or for an explicit overwrite.
    """
    controller = make_controller(tmp_path)
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    archived = archive_dir / "kitchen.yaml"
    archived.write_text("# archived\n", encoding="utf-8")
    active = tmp_path / "kitchen.yaml"
    active.write_text("# active, freshly written\n", encoding="utf-8")

    with pytest.raises(CommandError) as exc:
        await controller._unarchive_single("kitchen.yaml")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    # Archive copy survives untouched so the user's data isn't lost.
    assert archived.read_text() == "# archived\n"
    assert active.read_text() == "# active, freshly written\n"


async def test_unarchive_missing_archive_file_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Unarchiving a name that isn't in the archive raises cleanly."""
    controller = make_controller(tmp_path)
    with pytest.raises(FileNotFoundError):
        await controller._unarchive_single("ghost.yaml")


# ---------------------------------------------------------------------------
# path traversal — defense in depth at the public command boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "configuration",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "subdir/file.yaml",
        "..",
        ".",
        "",
        "/etc/passwd",
        "foo/../bar.yaml",
        "..\\windows\\system32",
        "secrets\\password.yaml",
        "with\x00null.yaml",
    ],
)
async def test_archive_rejects_path_traversal(
    tmp_path: Path, make_controller: MakeControllerFactory, configuration: str
) -> None:
    """All three archive commands reject non-basename ``configuration``.

    The helpers build paths like ``<config_dir>/archive/<configuration>``
    and ``ext_storage_path(configuration)`` (which resolves to
    ``<config_dir>/.esphome/storage/<configuration>.json``). Without
    a top-level filename validator a value containing ``..`` or path
    separators could resolve outside the intended directory — the
    archive flow would unlink / overwrite a file outside the archive
    tree. Reject at the WS boundary so the helpers never see a
    suspect value.
    """
    controller = make_controller(tmp_path)
    for cmd in (
        controller.archive_device,
        controller.unarchive_device,
        controller.delete_archived,
    ):
        with pytest.raises(CommandError) as exc:
            await cmd(configuration=configuration)
        assert exc.value.code == ErrorCode.INVALID_ARGS


# ---------------------------------------------------------------------------
# _unlink_storage_sidecar exception paths
# ---------------------------------------------------------------------------


def test_unlink_storage_sidecar_logs_oserror(tmp_path: Path, monkeypatch: Any, caplog: Any) -> None:
    """OSError from the storage unlink is logged, not raised.

    A permission-error / read-only fs on the StorageJSON sidecar
    shouldn't block the rest of the archive / delete flow.
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    (storage_dir / "kitchen.yaml.json").write_text("{}", encoding="utf-8")

    def _raise_oserror(self: Path, missing_ok: bool = False) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _raise_oserror)

    with caplog.at_level(logging.WARNING):
        _unlink_storage_sidecar("kitchen.yaml")
    assert any("Could not remove storage file" in rec.message for rec in caplog.records)


def test_clear_volatile_device_metadata_drops_corrupt_non_dict_entry(
    tmp_path: Path,
) -> None:
    """A non-dict value at the entry is treated as corrupt and dropped.

    ``set_device_metadata`` later assumes the existing entry is
    a dict and item-assigns into it; leaving a non-dict in place
    would crash that path. Drop the bad value so the next write
    starts from a clean shape.
    """
    # Seed a corrupt entry (string instead of dict) directly via
    # the persistence helper — ``set_device_metadata`` won't let
    # us write a non-dict value through its public surface.
    _save_metadata(tmp_path, {"kitchen.yaml": "not-a-dict"})

    clear_volatile_device_metadata(tmp_path, "kitchen.yaml")

    # ``get_device_metadata`` returns ``{}`` when the value is
    # not a dict — but here the entry should be gone entirely.
    # Read the raw file to distinguish.
    raw = _load_metadata(tmp_path)
    assert "kitchen.yaml" not in raw
    assert get_device_metadata(tmp_path, "kitchen.yaml") == {}


# ---------------------------------------------------------------------------
# delete_archived
# ---------------------------------------------------------------------------


async def test_delete_archived_removes_yaml_and_sidecars(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Permanent delete clears the archived YAML and its sidecars.

    The archive flow leaves StorageJSON / device-metadata behind so
    unarchive can restore the cached state. Once the user explicitly
    says "this is gone for good", those sidecars are dead weight —
    leaving them around would surprise a future create-with-same-
    filename with stale cached state. Mirror ``_delete_single``.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / "kitchen.yaml.json"
    storage_path.write_text("{}", encoding="utf-8")
    validated_path = storage_dir / "kitchen.yaml.validated.yaml"
    validated_path.write_text("esphome: {}\n", encoding="utf-8")

    controller = make_controller(tmp_path)
    await controller._delete_archived_single("kitchen.yaml")

    assert not (archive_dir / "kitchen.yaml").exists()
    assert not storage_path.exists()
    assert not validated_path.exists()


async def test_delete_archived_preserves_active_sidecars(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Same-name active config keeps its sidecars when the archive copy is deleted.

    Defense-in-depth: if an active config has been re-created
    with the same filename since the archive (which shouldn't
    happen because ``_archive_single`` wipes its sidecars on the
    way in, but might if the archive predates this PR or was
    written by the legacy dashboard), the StorageJSON / metadata
    sidecars belong to the *live* device. Permanent-deleting the
    archive copy must not wipe the live device's cached state.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    # Active config with the same filename — and a sidecar that
    # belongs to the *active* device, not the archive copy.
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen-active\n", encoding="utf-8")
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / "kitchen.yaml.json"
    storage_path.write_text('{"name":"active"}', encoding="utf-8")

    controller = make_controller(tmp_path)
    await controller._delete_archived_single("kitchen.yaml")

    assert not (archive_dir / "kitchen.yaml").exists()
    # Active YAML and its sidecars survive untouched.
    assert (tmp_path / "kitchen.yaml").read_text() == ("esphome:\n  name: kitchen-active\n")
    assert storage_path.read_text() == '{"name":"active"}'


async def test_delete_archived_succeeds_without_sidecars(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A bare YAML in the archive (no sidecar) deletes cleanly.

    Same shape as ``_delete_single`` — ``unlink(missing_ok=True)``
    on the StorageJSON path lets a hand-archived YAML or one whose
    sidecar was wiped earlier still go away when the user picks
    Delete-permanently.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    controller = make_controller(tmp_path)
    await controller._delete_archived_single("kitchen.yaml")
    assert not (archive_dir / "kitchen.yaml").exists()


async def test_delete_archived_missing_raises_file_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Permanent delete of a non-existent archive entry raises cleanly."""
    controller = make_controller(tmp_path)
    with pytest.raises(FileNotFoundError):
        await controller._delete_archived_single("ghost.yaml")


async def test_delete_archived_translates_missing_to_command_error(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The WS-layer entry point surfaces ``CommandError(NOT_FOUND)``.

    Mirrors ``archive_device`` / ``unarchive_device`` — the helper
    raises ``FileNotFoundError`` so internal callers can catch by
    type, but the public command translates to a clean WS error.
    """
    controller = make_controller(tmp_path)
    with pytest.raises(CommandError) as exc:
        await controller.delete_archived(configuration="ghost.yaml")
    assert exc.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# list_archived
# ---------------------------------------------------------------------------


def test_list_archived_returns_empty_when_no_archive_dir(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pre-first-archive: no directory, no entries, no error.

    The dashboard pulls this list on every "Show archived"
    toggle; raising on the no-directory case would force the
    frontend to special-case it. Return ``[]`` instead.
    """
    controller = make_controller(tmp_path)
    assert controller._list_archived_sync() == []


def test_list_archived_parses_each_yaml_meta_block(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Each archived YAML's ``esphome:`` block surfaces as a row.

    Mirrors the active list's name / friendly_name / comment shape
    so the frontend can reuse the same row component without
    learning a separate archived-device DTO.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n  friendly_name: Kitchen Sensor\n  comment: by the sink\n",
        encoding="utf-8",
    )
    (archive_dir / "garage.yaml").write_text(
        "esphome:\n  name: garage\n  friendly_name: Garage Door\n",
        encoding="utf-8",
    )

    controller = make_controller(tmp_path)
    rows = controller._list_archived_sync()

    by_config = {r["configuration"]: r for r in rows}
    assert set(by_config) == {"kitchen.yaml", "garage.yaml"}
    assert by_config["kitchen.yaml"]["name"] == "kitchen"
    assert by_config["kitchen.yaml"]["friendly_name"] == "Kitchen Sensor"
    assert by_config["kitchen.yaml"]["comment"] == "by the sink"
    assert by_config["garage.yaml"]["friendly_name"] == "Garage Door"


def test_list_archived_skips_non_yaml_and_hidden(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A stray ``.DS_Store`` / ``.txt`` next to the YAMLs doesn't crash.

    Archive directory is user-managed; some users sync it via Git
    and end up with ``.gitignore`` / ``.DS_Store``. The list
    helper has to ignore non-YAML and hidden files so a single
    stray file doesn't poison the listing.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    (archive_dir / ".DS_Store").write_bytes(b"\x00\x00")
    (archive_dir / "notes.txt").write_text("scratch", encoding="utf-8")
    (archive_dir / ".hidden.yaml").write_text("esphome:\n  name: hidden\n", encoding="utf-8")

    rows = make_controller(tmp_path)._list_archived_sync()
    assert [r["configuration"] for r in rows] == ["kitchen.yaml"]


def test_list_archived_falls_back_to_filename_when_meta_unparseable(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A YAML the meta parser can't read still surfaces as a row.

    Use case: legacy archive contents from a different dashboard
    where the YAML was hand-edited and ``esphome:`` was reorganised.
    The filename is the user's only handle on the file; we'd
    rather show ``kitchen.yaml — kitchen`` than hide it entirely.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    (archive_dir / "kitchen.yaml").write_text("# no esphome: block at all\n", encoding="utf-8")

    rows = make_controller(tmp_path)._list_archived_sync()
    assert len(rows) == 1
    assert rows[0]["configuration"] == "kitchen.yaml"
    assert rows[0]["name"] == "kitchen"
    assert rows[0]["friendly_name"] == "kitchen"


def test_list_archived_falls_back_to_storage_json_when_yaml_meta_sparse(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """When the YAML's ``esphome:`` block is missing fields, fill from StorageJSON.

    Friendly name and comment commonly only live in the StorageJSON
    sidecar (the dashboard's edit-name dialog writes them there
    rather than mutating the YAML). Without this fallback the
    archived listing would regress to bare filenames for those
    devices, hiding the user-visible names they expect.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # YAML carries only `name` — friendly_name + comment live in the sidecar.
    (archive_dir / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n",
        encoding="utf-8",
    )
    # Storage build_path explicitly None — the device has never been
    # compiled, so the archive lister falls back to YAML + sidecar
    # alone for friendly_name / comment.
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        overrides={
            "friendly_name": "Kitchen Sensor",
            "comment": "by the sink",
            "build_path": None,
        },
    )

    rows = make_controller(tmp_path)._list_archived_sync()
    assert len(rows) == 1
    assert rows[0]["configuration"] == "kitchen.yaml"
    assert rows[0]["name"] == "kitchen"
    assert rows[0]["friendly_name"] == "Kitchen Sensor"
    assert rows[0]["comment"] == "by the sink"
