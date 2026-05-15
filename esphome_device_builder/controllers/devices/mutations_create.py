"""``devices/create`` WS command body."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.device_yaml import parse_platform_from_yaml
from ...helpers.storage_path import resolve_storage_path
from ...models import ErrorCode, WizardResponse
from .helpers import friendly_name_slugify

if TYPE_CHECKING:
    from .controller import DevicesController


async def create_device(  # noqa: PLR0912, PLR0915
    controller: DevicesController,
    *,
    name: str,
    board_id: str | None,
    ssid: str,
    psk: str,
    file_content: str | None,
) -> WizardResponse:
    """
    Create a new device configuration.

    Three flows decided by which arguments are provided:
    *file_content* writes user-supplied YAML as-is; *board_id*
    generates from the board template; neither emits a minimal
    valid esp32 stub for the wizard's "empty configuration"
    button. Generated flows validate before write
    (``INTERNAL_ERROR`` on regression); the user-upload flow
    deliberately skips validation so an existing config from
    an older ESPHome version (with since-changed schemas) can
    still land in the editor for repair. ``board_id`` is
    derived from the YAML's platform / board / variant fields
    when not explicitly provided, except for the stub branch
    (its hard-coded ``board: esp32dev`` would mis-bind).
    """
    name = name.strip()
    if not name:
        raise CommandError(ErrorCode.INVALID_ARGS, "name is required")

    filename = f"{name}.yaml"
    config_path = controller._db.settings.rel_path(filename)

    # Fast collision check before the (~hundreds of ms) validator
    # round-trip so a duplicate-name attempt fails on the right
    # diagnostic. The ``open(..., "x")`` further down is the
    # actual race-safe write; the check here is a UX optimisation.
    loop_for_check = asyncio.get_running_loop()
    if await loop_for_check.run_in_executor(None, config_path.exists):
        msg = f"Configuration {filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    # Surface user-correctable failures (unknown board) as typed
    # ``INVALID_ARGS`` so the wizard can show a specific message.
    board = None
    if board_id:
        if controller._db.boards:
            board = await controller._db.boards.get_board(board_id=board_id)
        if board is None:
            msg = f"Unknown board: {board_id}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

    friendly = friendly_name_slugify(name)
    yaml_content, source = controller._yaml_content_for_create(
        name, friendly, board, file_content, ssid, psk
    )

    # Validate generated YAML before write so a regression in
    # generate_device_yaml / generate_minimal_stub_yaml surfaces
    # as INTERNAL_ERROR rather than landing an unflashable YAML
    # on disk. User uploads are deliberately skipped: the upload
    # flow exists so users can bring an existing (often older)
    # config into the builder and repair it in the editor.
    if source != "user":
        await controller._validate_rewritten_yaml_or_raise(
            filename,
            yaml_content,
            action="create",
            on_failure=ErrorCode.INTERNAL_ERROR,
        )

    # Derive board_id from YAML when not explicitly provided.
    # Skip the stub branch since ``generate_minimal_stub_yaml``
    # hard-codes ``esp32: board: esp32dev`` and many catalog
    # entries share that PIO board; the lookup would pin the new
    # device to whichever entry the index surfaces first, and the
    # wrong entry would stay bound after the user rewrites the
    # platform block.
    parsed_platform = ""
    if not board_id and controller._db.boards:
        parsed_platform, pio_board, variant = parse_platform_from_yaml(yaml_content)
        if source != "stub":
            matched = None
            if pio_board:
                matched = controller._db.boards.find_by_pio_board(pio_board, variant)
            if matched is None and parsed_platform:
                matched = controller._db.boards.find_by_platform_variant(parsed_platform, variant)
            if matched:
                board = matched
                board_id = matched.id

    loop = asyncio.get_running_loop()

    def _write_exclusive() -> None:
        # Exclusive-create so a concurrent ``devices/create`` (or
        # any other writer) can't slip between a preflight check
        # and the write and silently clobber an in-flight config.
        with config_path.open("x", encoding="utf-8") as f:
            f.write(yaml_content)

    try:
        await loop.run_in_executor(None, _write_exclusive)
    except FileExistsError as exc:
        msg = f"Configuration {filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

    def _init_storage() -> None:
        platform = str(board.esphome.platform) if board else parsed_platform
        storage = StorageJSON(
            storage_version=1,
            name=name,
            friendly_name=friendly,
            comment=None,
            esphome_version=None,
            src_version=None,
            address=f"{name}.local",
            web_port=None,
            target_platform=platform,
            build_path=None,
            firmware_bin_path=None,
            loaded_integrations=[],
            loaded_platforms=[],
            no_mdns=False,
        )
        storage_path = resolve_storage_path(filename)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage.save(storage_path)

    await loop.run_in_executor(None, _init_storage)
    # Archive keeps identity for unarchive; a fresh device at the
    # same filename must start clean or an archived board_id
    # silently mis-binds.
    await controller._delete_device_metadata(filename)
    if board_id:
        await controller._persist_device_metadata_async(filename, board_id=board_id)
    # _scanner.scan fires _on_scan_change(ADDED) for the new
    # YAML and that already runs probe_device; don't double-probe.
    # file_content may carry an esphome.name that differs from
    # the URL name, in which case the scan-change handler probes
    # the YAML's name (the right one) and a second probe here
    # would target the wrong service.
    await controller._scanner.scan()
    return WizardResponse(configuration=filename)
