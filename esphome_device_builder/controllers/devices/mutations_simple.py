"""Smaller mutation WS commands: update / set_labels / rename / edit_friendly_name."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    configuration_filename,
    parse_esphome_meta,
    resolved_device_name,
)
from ...helpers.hostname import default_mdns_address
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import (
    YamlUpsertNotSupportedError,
    rewrite_rename_content,
    upsert_yaml_leaf_under_top_block,
)
from ...models import Device, ErrorCode, UpdateDeviceResponse
from ..config import set_device_labels
from ..firmware.rename_flow import RENAME_REMEDY
from . import archive
from .firmware_sync import migrate_metadata_then_scan
from .helpers import raise_device_name_exists, raise_device_not_found
from .mutations_create import save_device_storage

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def update_device(
    controller: DevicesController,
    *,
    configuration: str,
    friendly_name: str | None,
    comment: str | None,
    board_id: str | None,
) -> UpdateDeviceResponse:
    """Update device metadata (sidecar JSON, not the YAML file).

    Keyed by ``configuration`` (the ``.yaml`` filename) like every other
    device mutation; a device's ESPHome ``name`` can differ from its
    filename stem, so keying on name writes the wrong sidecar.
    """
    # Flag board_id as user-set only when it differs from the displayed
    # board, so a client echoing the shown (maybe auto-derived) value
    # while editing name/comment can't pin a derived id. A deliberate
    # pick equal to the current derivation is intentionally left
    # unflagged; it re-derives to the same id, so nothing is lost.
    user_set: bool | None = None
    if board_id:
        current = controller.get_by_configuration(configuration)
        displayed = current.board_id if current is not None else ""
        if board_id != displayed:
            user_set = True
    await controller._persist_device_metadata_async(
        configuration,
        board_id=board_id,
        board_id_user_set=user_set,
        friendly_name=friendly_name,
        comment=comment,
    )
    # Re-scan the one file so the in-memory device + its resolved board_id
    # pick up the sidecar write and a DEVICE_UPDATED event fires for clients.
    await controller._scanner.reload(configuration)
    meta = await controller._shared_sidecar.get(configuration)
    name = configuration.removesuffix(".yaml")
    return UpdateDeviceResponse(
        name=name,
        friendly_name=meta.get("friendly_name", name),
        comment=meta.get("comment"),
        board_id=meta.get("board_id"),
    )


async def set_labels(
    controller: DevicesController,
    *,
    configuration: str,
    label_ids: list[str],
) -> Device:
    """
    Replace this device's label assignments.

    ``label_ids`` is the new full list (no diff semantics; ``[]``
    clears every assignment). Unknown IDs raise ``INVALID_ARGS``;
    the catalog check runs inside the same metadata transaction
    as the write so a concurrent ``labels/delete`` cascade can't
    leave a dangling reference.
    """
    # ``rel_path`` raises CommandError(INVALID_ARGS) on path
    # traversal; reuses the existing single chokepoint.
    controller._db.settings.rel_path(configuration)
    if not isinstance(label_ids, list):
        raise CommandError(ErrorCode.INVALID_ARGS, "label_ids must be a list of label id strings")

    # Verify the device exists before writing the sidecar; a
    # configuration that passes ``rel_path`` but isn't tracked
    # by the scanner (typo, deleted YAML) would otherwise leave
    # an orphaned ``.device-builder.json`` entry pinning labels
    # to a non-existent device.
    device = controller.get_by_configuration(configuration)
    if device is None:
        raise_device_not_found(configuration)

    config_dir = controller._db.settings.config_dir

    def _persist() -> None:
        try:
            set_device_labels(config_dir, configuration, label_ids)
        except FileNotFoundError as err:
            # YAML vanished mid-write (racing ``devices/delete``) — surface NOT_FOUND.
            raise_device_not_found(configuration, from_exc=err)
        except (TypeError, ValueError) as err:
            # ``set_device_labels`` raises ``TypeError`` for non-string
            # items and ``ValueError`` for unknown label ids; both
            # surface as ``INVALID_ARGS`` to the WS caller.
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err

    await asyncio.to_thread(_persist)
    await controller._scanner.reload(configuration)

    # Re-fetch from the scanner; reload replaces the Device in
    # the index, so the reference held above is stale.
    refreshed = controller.get_by_configuration(configuration)
    if refreshed is None:
        raise_device_not_found(configuration)
    return refreshed


def _row_configuration(row: Any) -> str | None:
    """Extract ``row["configuration"]`` as a string, else ``None``.

    Single source for the malformed-row tolerance used by both the
    bulk runner's ``get_configuration`` extractor (which needs a
    string fallback for the result row) and the action's pre-call
    validation (which needs to raise ``INVALID_ARGS``).
    """
    if not isinstance(row, dict):
        return None
    cfg = row.get("configuration")
    return cfg if isinstance(cfg, str) else None


async def set_labels_bulk(
    controller: DevicesController,
    *,
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Assign labels across multiple devices.

    Each entry in ``updates`` is ``{configuration: str, label_ids:
    list[str]}``. Returns one ``{configuration, success, error?}``
    per entry in input order; duplicates produce duplicate rows
    (last-write-wins on disk); malformed rows fail per-row without
    blocking the rest. Malformed rows whose configuration can't be
    extracted at all return ``{configuration: "", ...}``.
    """

    async def _apply(row: Any) -> None:
        # ``row`` is typed ``Any`` because the bulk runner calls
        # this for every input entry — including the non-dict
        # rows that ``test_set_labels_bulk_malformed_row_isolates_failure``
        # exercises. ``_row_configuration`` is the validation
        # boundary; it returns None for both non-dict rows and
        # dicts whose ``configuration`` isn't a string.
        configuration = _row_configuration(row)
        if configuration is None:
            raise CommandError(ErrorCode.INVALID_ARGS, "configuration must be a string")
        label_ids = row.get("label_ids")
        if not isinstance(label_ids, list):
            raise CommandError(ErrorCode.INVALID_ARGS, "label_ids must be a list")
        await set_labels(controller, configuration=configuration, label_ids=label_ids)

    return await archive.run_bulk_per_row(
        controller, updates, _apply, lambda r: _row_configuration(r) or ""
    )


async def rename_device(
    controller: DevicesController,
    *,
    configuration: str,
    new_name: str,
    config_only: bool = False,
) -> dict[str, Any]:
    """
    Rename a device configuration.

    Default path queues a rename chain on the firmware queue — a COMPILE
    of the renamed YAML (remote-eligible) plus a dependent flash of the
    old device address that swaps the files on success; only succeeds
    against a reachable device. ``config_only`` rewrites the YAML +
    ``esphome.name`` and renames the file without compiling or flashing,
    for an offline device. An in-place rename (target filename equals the
    device's own file) is always config-only since the OTA chain needs a
    new filename to compile against.
    """
    new_filename = configuration_filename(new_name)
    old_path = controller._db.settings.rel_path(configuration)
    new_path = controller._db.settings.rel_path(new_filename)

    content = await _read_device_yaml_or_raise(controller, configuration)

    # Reject same-name renames up-front. Compare against the device's real
    # ``esphome.name``, not the filename stem: an uploaded config keeps its
    # raw name (``test_1``) while the file is slugified (``test-1.yaml``), so
    # a stem compare wrongly rejects the legitimate ``test_1`` -> ``test-1``
    # rename and wrongly accepts a real no-op whose filename differs from its
    # name.
    if new_name == resolved_device_name(content, configuration):
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "new_name must differ from the current device name",
        )

    # When the slugified target filename is the device's own file, the rename
    # changes ``esphome.name`` without moving the file — the OTA chain needs
    # a distinct new filename to compile against, so route it in place.
    # Compare lexically-normalized paths so a configuration with redundant
    # segments (``./x.yaml``) still reads as the same file, without the
    # blocking filesystem access ``Path.resolve()`` would do in this async
    # path.
    in_place = os.path.normpath(old_path) == os.path.normpath(new_path)

    # Single rewrite + refusal point: offline, in-place, and the OTA chain
    # all retarget the name the same way.
    new_content = rewrite_rename_content(content, new_name, remedy=RENAME_REMEDY)

    # An in-place rename can't go through the OTA chain (same filename), so
    # it rewrites the name with no flash even when the caller wanted the OTA
    # path. The firmware (which broadcasts the raw ``esphome.name``) keeps its
    # old hostname until the next install; the resulting expected/deployed hash
    # mismatch surfaces as a pending-changes indicator, same as the offline
    # ``config_only`` rename, so the divergence is visible rather than silent.
    if config_only or in_place:
        # Reject if another file owns the target; the chain path's own
        # collision check (with its active-retry exemption) lives in
        # ``firmware.rename_chain``.
        if not in_place and await run_in_executor(new_path.exists):
            raise_device_name_exists(new_filename)
        return await _config_only_rename(
            controller,
            configuration=configuration,
            new_name=new_name,
            new_content=new_content,
            in_place=in_place,
        )

    firmware = controller._db.firmware
    if firmware is None:
        msg = "Firmware controller is unavailable"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
    head, tail = await firmware.rename_chain(
        configuration=configuration, new_name=new_name, content=content, new_content=new_content
    )
    return {"configuration": new_filename, "job": head.to_dict(), "tail_job": tail.to_dict()}


async def _read_device_yaml_or_raise(controller: DevicesController, configuration: str) -> str:
    """
    Return *configuration*'s YAML text, raising INVALID_ARGS if it's gone.

    A single ``read_text`` with no preceding ``exists()`` check, so a file
    deleted mid-call surfaces as the typed "device gone" error instead of
    leaking ``FileNotFoundError`` as INTERNAL_ERROR.
    """
    path = controller._db.settings.rel_path(configuration)

    def _read() -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    content = await run_in_executor(_read)
    if content is None:
        raise CommandError(ErrorCode.INVALID_ARGS, f"Device {configuration} not found")
    return content


async def _config_only_rename(
    controller: DevicesController,
    *,
    configuration: str,
    new_name: str,
    new_content: str,
    in_place: bool,
) -> dict[str, Any]:
    """
    Land the rewritten YAML with no compile or OTA.

    Validates *new_content* before touching disk, writes the new file
    atomically, removes the old, and migrates the StorageJSON + sidecar
    metadata. Returns ``job: None`` (nothing is queued). When *in_place*
    the target filename is the device's own file: the rewrite lands on it
    and the old-file / old-sidecar removals are skipped so the just-written
    file isn't deleted.
    """
    new_filename = configuration_filename(new_name)
    old_path = controller._db.settings.rel_path(configuration)
    new_path = controller._db.settings.rel_path(new_filename)

    # Validate before any disk change so a bad rewrite never lands on disk.
    await controller._validate_rewritten_yaml_or_raise(new_filename, new_content, action="rename")

    await controller._write_yaml_atomic_async(new_path, new_content)
    if not in_place:
        await run_in_executor(lambda: old_path.unlink(missing_ok=True))
    # The YAML is already renamed; storage migration is best-effort (logs on
    # failure) and the shared metadata-migrate-then-scan always rescans.
    await run_in_executor(_migrate_storage_json, configuration, new_filename, new_name)
    await migrate_metadata_then_scan(controller, configuration, new_filename)
    return {"configuration": new_filename, "job": None}


def _migrate_storage_json(old_configuration: str, new_filename: str, new_name: str) -> None:
    """Move the StorageJSON sidecar to the new filename, retargeting the name.

    Best-effort (errors are logged, not raised): the YAML rename has already
    landed, so a sidecar failure must not fail the rename. The trade-off is
    that on failure the rename still returns success while the device shows as
    never-built until its next compile regenerates the sidecar, and the old
    sidecar is left orphaned. Recoverable, so it's logged rather than surfaced
    to the client. ``friendly_name`` / ``address`` are only retargeted when
    they still carry the old-name defaults.
    """
    try:
        old_storage_path = resolve_storage_path(old_configuration)
        new_storage_path = resolve_storage_path(new_filename)
        storage = StorageJSON.load(old_storage_path)
        if storage is None:
            return
        old_name = storage.name
        storage.name = new_name
        if storage.friendly_name == old_name:
            storage.friendly_name = new_name
        if storage.address == default_mdns_address(old_name):
            storage.address = default_mdns_address(new_name)
        save_device_storage(new_filename, storage)
        # An in-place rename saves the sidecar back to the same path; unlinking
        # the "old" path would delete the file we just wrote.
        if old_storage_path != new_storage_path:
            old_storage_path.unlink(missing_ok=True)
    except OSError:
        # Filesystem hiccup only; a logic bug here should surface, not hide.
        _LOGGER.exception("Could not migrate StorageJSON for %s", new_filename)


async def edit_friendly_name(
    controller: DevicesController,
    *,
    configuration: str,
    new_friendly_name: str,
) -> dict[str, str | bool]:
    """
    Rewrite ``esphome.friendly_name:`` in the device YAML.

    YAML is the source of truth: a sidecar-only update would
    let the dashboard label drift from what the running firmware
    broadcasts (every reboot would announce the YAML's value via
    mDNS, the next compile bakes it in, dashboard and device
    disagree). Doesn't touch firmware; the frontend composes the
    follow-up install separately.

    Returns ``{"configuration": ..., "rewritten": bool}``;
    ``rewritten`` is False on a no-op rewrite so callers can
    skip a redundant install.

    Insertion behaviour: an existing leaf is rewritten in place
    (substitution-aware); an existing ``esphome:`` block without
    ``friendly_name:`` gets the leaf inserted; a YAML with no
    ``esphome:`` block gets one prepended carrying just
    ``friendly_name:`` (ESPHome's package merge gives the local
    leaf precedence over the included one).

    ``esphome.name`` is intentionally not synthesised: a
    text-level check can't see ``name:`` supplied by ``packages:``
    / ``!include`` / substitutions, and a synthesised slug here
    would silently override the package-supplied hostname.
    """
    new_friendly_name = new_friendly_name.strip()
    if not new_friendly_name:
        raise CommandError(ErrorCode.INVALID_ARGS, "new_friendly_name is required")

    content = await _read_device_yaml_or_raise(controller, configuration)

    try:
        new_content = upsert_yaml_leaf_under_top_block(
            content, "esphome", "friendly_name", new_friendly_name
        )
    except YamlUpsertNotSupportedError as exc:
        # Flow-style ``esphome: { ... }`` or a tagged value
        # (``esphome: !include ...``); the line-based walker
        # can't safely insert into either shape.
        raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

    # Round-trip check: parse the rewritten YAML through the
    # same reader the scanner uses. Defends against the
    # line-based upsert producing a YAML shape that serializes
    # fine but the reader misinterprets; a real bug shipped
    # once where wizard-emitted column-0 ``# Board:`` /
    # ``# Definition:`` comments ended up between an inserted
    # ``name:`` and ``friendly_name:``, the reader hit
    # ``# Board:`` at column 0, treated it as a fresh top-level
    # key, dropped the ``esphome:`` context, and silently lost
    # ``friendly_name`` on every load.
    _, parsed_friendly, _, _ = parse_esphome_meta(new_content)
    if parsed_friendly != new_friendly_name:
        raise CommandError(
            ErrorCode.INTERNAL_ERROR,
            "Edited YAML doesn't round-trip through the reader — "
            "the line-based upsert produced a shape the parser "
            "misinterprets. This is a dashboard bug; please file "
            "an issue with a redacted snippet of just the "
            "esphome: / substitutions: blocks (strip Wi-Fi "
            "credentials, API keys, and static IPs) so we can "
            "extend the rewriter's coverage.",
        )
    if new_content == content:
        # Idempotent: same value submitted (or the leaf already
        # was that value). Skip the write and signal no install
        # is needed; skip the validation pass too since the file
        # isn't changing.
        return {"configuration": configuration, "rewritten": False}

    await controller._validate_rewritten_yaml_or_raise(
        configuration, new_content, action="update friendly name"
    )
    await controller._persist_yaml_mutation(
        configuration, new_content, message=f"Update friendly name in {configuration}"
    )
    return {"configuration": configuration, "rewritten": True}
