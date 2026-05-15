"""Smaller mutation WS commands: update / set_labels / rename / edit_friendly_name."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.device_yaml import configuration_stem, parse_esphome_meta
from ...helpers.yaml import YamlUpsertNotSupportedError, upsert_yaml_leaf_under_top_block
from ...models import Device, ErrorCode, UpdateDeviceResponse
from ..config import set_device_labels

if TYPE_CHECKING:
    from .controller import DevicesController


async def update_device(
    controller: DevicesController,
    *,
    name: str,
    friendly_name: str | None,
    comment: str | None,
    board_id: str | None,
) -> UpdateDeviceResponse:
    """Update device metadata (sidecar JSON, not the YAML file)."""
    filename = f"{name}.yaml"
    await controller._persist_device_metadata_async(
        filename,
        board_id=board_id,
        friendly_name=friendly_name,
        comment=comment,
    )
    meta = await controller._shared_sidecar.get(filename)
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


async def rename_device(
    controller: DevicesController,
    *,
    configuration: str,
    new_name: str,
) -> dict[str, Any]:
    """
    Rename a device configuration.

    Thin pass-through to ``esphome rename``: the CLI owns the
    whole atomic flow (YAML edit, revalidation, compile + OTA
    install, rollback on failure). Routed through the firmware
    queue so streaming output shows up alongside other firmware
    tasks. Deliberately no file-level fallback: a fallback would
    silently rename the YAML on disk while the running firmware
    keeps broadcasting the old hostname (dashboard label and
    device state diverge with no error to the user).
    """
    new_filename = f"{new_name}.yaml"

    # Reject same-name renames up-front; a no-op at the YAML
    # level but still queues a real ``esphome rename`` job that
    # re-compiles and OTA-flashes. Compare on the *stem* so
    # cloning ``kitchen.yml`` to ``new_name=kitchen`` is rejected
    # too (the device's mDNS hostname comes from the stem and
    # stays the same either way).
    source_stem = configuration_stem(configuration)
    if new_name == source_stem:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "new_name must differ from the current device name",
        )
    # Reject up-front if the target filename is in use; ``esphome
    # rename`` itself doesn't check collisions and would silently
    # overwrite an unrelated device's config and OTA-flash that
    # firmware to the wrong device. Both ``rel_path`` and
    # ``.exists()`` are blocking; push the pair to the executor.
    loop = asyncio.get_running_loop()
    new_path = controller._db.settings.rel_path(new_filename)
    if await loop.run_in_executor(None, new_path.exists):
        msg = f"A device named {new_filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    if controller._db.firmware is None:
        msg = "Firmware controller is unavailable"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
    job = await controller._db.firmware.rename(configuration=configuration, new_name=new_name)
    return {"configuration": new_filename, "job": job.to_dict()}


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

    loop = asyncio.get_running_loop()
    config_path = controller._db.settings.rel_path(configuration)

    def _read() -> str | None:
        # Single read_text call, no preceding exists() check;
        # a file deleted between the two would leak
        # FileNotFoundError as INTERNAL_ERROR instead of the
        # typed INVALID_ARGS we want for "device gone".
        try:
            return config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    content = await loop.run_in_executor(None, _read)
    if content is None:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"Device {configuration} not found",
        )

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
    await controller._persist_yaml_mutation(configuration, new_content)
    return {"configuration": configuration, "rewritten": True}
