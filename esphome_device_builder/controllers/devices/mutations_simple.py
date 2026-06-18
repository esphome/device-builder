"""Smaller mutation WS commands: update / set_labels / rename / edit_friendly_name."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.device_yaml import configuration_stem, parse_esphome_meta
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import (
    ESPHOME_NAME_PATH,
    YamlUpsertNotSupportedError,
    is_retargetable_name,
    parse_substitution_ref,
    read_yaml_scalar,
    rewrite_name_or_substitution,
    upsert_yaml_leaf_under_top_block,
)
from ...models import Device, ErrorCode, UpdateDeviceResponse
from ..config import set_device_labels
from . import archive
from .firmware_sync import migrate_metadata_then_scan
from .mutations_create import default_mdns_address, save_device_storage

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
        displayed = next(
            (d.board_id for d in controller._scanner.devices if d.configuration == configuration),
            "",
        )
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
    device = next(
        (d for d in controller._scanner.devices if d.configuration == configuration),
        None,
    )
    if device is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found")

    config_dir = controller._db.settings.config_dir

    def _persist() -> None:
        try:
            set_device_labels(config_dir, configuration, label_ids)
        except FileNotFoundError as err:
            # YAML vanished mid-write (racing ``devices/delete``) — surface NOT_FOUND.
            raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found") from err
        except (TypeError, ValueError) as err:
            # ``set_device_labels`` raises ``TypeError`` for non-string
            # items and ``ValueError`` for unknown label ids; both
            # surface as ``INVALID_ARGS`` to the WS caller.
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err

    await asyncio.to_thread(_persist)
    await controller._scanner.reload(configuration)

    # Re-fetch from the scanner; reload replaces the Device in
    # the index, so the reference held above is stale.
    refreshed = next(
        (d for d in controller._scanner.devices if d.configuration == configuration),
        None,
    )
    if refreshed is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found")
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

    Default path delegates to ``esphome rename`` (compile + OTA install,
    via the firmware queue); only succeeds against a reachable device.
    ``config_only`` rewrites the YAML + ``esphome.name`` and renames the
    file without compiling or flashing, for an offline device. An in-place
    rename (target filename equals the device's own file) is always
    config-only since ``esphome rename`` can't keep the same filename.
    """
    new_filename = f"{new_name}.yaml"
    loop = asyncio.get_running_loop()
    old_path = controller._db.settings.rel_path(configuration)
    new_path = controller._db.settings.rel_path(new_filename)

    content = await _read_device_yaml_or_raise(controller, configuration)

    # Reject same-name renames up-front. Compare against the device's real
    # ``esphome.name``, not the filename stem: an uploaded config keeps its
    # raw name (``test_1``) while the file is slugified (``test-1.yaml``), so
    # a stem compare wrongly rejects the legitimate ``test_1`` -> ``test-1``
    # rename and wrongly accepts a real no-op whose filename differs from its
    # name. A nonlocal ``${var}`` (package / !include) stays an unresolved
    # token, so treat that as unknown and fall back to the filename stem
    # rather than comparing against the literal ``${var}``.
    parsed_name = parse_esphome_meta(content).name
    current_name = (
        parsed_name
        if parsed_name and parse_substitution_ref(parsed_name) is None
        else configuration_stem(configuration)
    )
    if new_name == current_name:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "new_name must differ from the current device name",
        )

    # When the slugified target filename is the device's own file, the rename
    # changes ``esphome.name`` without moving the file. ``esphome rename``
    # refuses that (it requires a new filename), so route it in place. Compare
    # lexically-normalized paths so a configuration with redundant segments
    # (``./x.yaml``) still reads as the same file, without the blocking
    # filesystem access ``Path.resolve()`` would do in this async path.
    in_place = os.path.normpath(old_path) == os.path.normpath(new_path)
    # Reject up-front if a *different* file already owns the target filename;
    # ``esphome rename`` doesn't check collisions and would silently overwrite
    # an unrelated device's config and OTA-flash firmware to the wrong device.
    if not in_place and await loop.run_in_executor(None, new_path.exists):
        msg = f"A device named {new_filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    # An in-place rename can't go through ``esphome rename`` (same filename),
    # so it rewrites the name with no flash even when the caller wanted the OTA
    # path. The firmware (which broadcasts the raw ``esphome.name``) keeps its
    # old hostname until the next install; the resulting expected/deployed hash
    # mismatch surfaces as a pending-changes indicator, same as the offline
    # ``config_only`` rename, so the divergence is visible rather than silent.
    if config_only or in_place:
        return await _config_only_rename(
            controller,
            configuration=configuration,
            new_name=new_name,
            content=content,
            in_place=in_place,
        )

    if controller._db.firmware is None:
        msg = "Firmware controller is unavailable"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
    job = await controller._db.firmware.rename(configuration=configuration, new_name=new_name)
    return {"configuration": new_filename, "job": job.to_dict()}


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

    content = await asyncio.get_running_loop().run_in_executor(None, _read)
    if content is None:
        raise CommandError(ErrorCode.INVALID_ARGS, f"Device {configuration} not found")
    return content


async def _config_only_rename(
    controller: DevicesController,
    *,
    configuration: str,
    new_name: str,
    content: str,
    in_place: bool,
) -> dict[str, Any]:
    """
    Rename the YAML + ``esphome.name`` with no compile or OTA.

    Validates the rewritten config before touching disk, writes the new
    file atomically, removes the old, and migrates the StorageJSON +
    sidecar metadata. Returns ``job: None`` (nothing is queued). When
    *in_place* the target filename is the device's own file: the rewrite
    lands on it and the old-file / old-sidecar removals are skipped so the
    just-written file isn't deleted.
    """
    new_filename = f"{new_name}.yaml"
    loop = asyncio.get_running_loop()
    old_path = controller._db.settings.rel_path(configuration)
    new_path = controller._db.settings.rel_path(new_filename)

    # ``esphome.name`` must be retargetable in place: a plain literal (rewrite
    # the leaf) or a pure ``${var}`` ref whose definition lives in this file's
    # ``substitutions:`` block (rewrite the def). A missing leaf, a tag, an
    # embedded substitution (``kitchen_${suffix}``), or a ``${var}`` defined in
    # a package / !include would flatten the indirection to a literal, so refuse
    # those and steer to the OTA rename, which resolves them.
    current = read_yaml_scalar(content, ESPHOME_NAME_PATH)
    var = parse_substitution_ref(current) if current is not None else None
    # A pure ``${var}`` ref whose def isn't in this file would be flattened.
    nonlocal_sub = var is not None and read_yaml_scalar(content, ("substitutions", var)) is None
    if current is None or not is_retargetable_name(current) or nonlocal_sub:
        # The OTA rename resolves packages / includes / embedded substitutions,
        # so steer there for a file-move rename. An in-place rename can't fall
        # back to it (``esphome rename`` won't keep the same filename), so the
        # only fix is editing the name to a plain value.
        remedy = (
            "Edit esphome.name to a plain value and try again."
            if in_place
            else "Bring the device online to rename it."
        )
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "Can't rename: esphome.name isn't a plain literal or a local "
            "${substitution} (it may come from packages, an !include, or an "
            f"embedded substitution). {remedy}",
        )

    new_content = rewrite_name_or_substitution(content, ESPHOME_NAME_PATH, new_name)

    # Validate before any disk change so a bad rewrite never lands on disk.
    await controller._validate_rewritten_yaml_or_raise(new_filename, new_content, action="rename")

    await controller._write_yaml_atomic_async(new_path, new_content)
    if not in_place:
        await loop.run_in_executor(None, lambda: old_path.unlink(missing_ok=True))
    # The YAML is already renamed; storage migration is best-effort (logs on
    # failure) and the shared metadata-migrate-then-scan always rescans.
    await loop.run_in_executor(None, _migrate_storage_json, configuration, new_filename, new_name)
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
