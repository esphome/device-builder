"""``devices/create`` WS command body."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NoReturn

from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import parse_platform_from_yaml
from ...helpers.hostname import default_mdns_address
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import write_user_yaml
from ...models import ErrorCode, WizardResponse
from ..editor import IMPORT_VALIDATE_TIMEOUT
from .helpers import (
    _looks_binary,
    clean_friendly_name,
    slugify_hostname,
    write_new_file_exclusive,
)

if TYPE_CHECKING:
    from .controller import DevicesController

# A wifi credential of ``!secret wifi_ssid`` is a caller passing the YAML
# secret *tag* as a literal value; the field takes literals (empty means
# "use !secret refs"), so this would silently land an unquoted-looking
# tag that the generator quotes into a dead string. Match the full tag
# form (``!secret`` + whitespace + a key char) so an odd-but-real password
# like ``!secretsauce`` or a literal ``!secret `` without a key is left alone.
_SECRET_TAG_RE = re.compile(r"^\s*!secret\s+\S")


async def create_device(  # noqa: C901, PLR0912
    controller: DevicesController,
    *,
    name: str,
    friendly_name: str | None = None,
    board_id: str | None,
    ssid: str,
    psk: str,
    file_content: str | None,
    overwrite: bool = False,
) -> WizardResponse:
    """
    Create a new device configuration.

    With *friendly_name*, *name* is the hostname (normalised through
    ``slugify_hostname``); without it, *name* is the raw display label
    and both values derive from it. Three flows decided by which
    arguments are provided: *file_content* writes user-supplied YAML
    as-is; *board_id* generates from the board template; neither emits
    a minimal valid esp32 stub for the wizard's "empty configuration"
    button. Generated flows validate before write
    (``INTERNAL_ERROR`` on regression); the user-upload flow
    deliberately skips validation so an existing config from
    an older ESPHome version (with since-changed schemas) can
    still land in the editor for repair. ``board_id`` is
    persisted (as a deliberate user pick) only when explicitly
    provided; otherwise the scanner derives it from the YAML on
    each resolve against the current catalog. A filename collision
    raises ``ALREADY_EXISTS`` unless *overwrite* is set, which
    replaces the YAML in place and preserves the existing device's
    metadata (labels / comment, and its board_id unless a new
    *board_id* is explicitly provided) and StorageJSON.
    """
    # ``clean_friendly_name`` makes the display label a valid
    # ``esphome.friendly_name:`` (trims, swaps the reserved ``/`` for
    # ``⁄`` as ESPHome itself does, drops control chars, clamps to the
    # byte cap); ``slugify_hostname`` yields the canonical
    # lowercase-dashed hostname clamped to ESPHome's name length cap
    # (mDNS / filename / esphome.name: schema). Centralising both here
    # keeps the frontend out of the sanitisation business and avoids two
    # implementations drifting; a frontend-derived slug passes through
    # ``slugify_hostname`` unchanged.
    friendly = clean_friendly_name(friendly_name) if friendly_name else ""
    if friendly:
        name = slugify_hostname(name)
    else:
        friendly = clean_friendly_name(name)
        if not friendly:
            raise CommandError(ErrorCode.INVALID_ARGS, "name is required")
        name = slugify_hostname(friendly)
    if not name:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"name {friendly!r} has no hostname-safe characters",
        )

    # ssid/psk are literal credentials only for the generated flows; the
    # file_content upload writes user YAML as-is and ignores them. Match
    # yaml_content_for_create's truthiness selection (``if file_content:``)
    # so an empty string falls through to the same template flow this guards.
    if not file_content:
        for field, value in (("ssid", ssid), ("psk", psk)):
            if value and _SECRET_TAG_RE.match(value):
                raise CommandError(
                    ErrorCode.INVALID_ARGS,
                    f"{field} must be a literal value; leave it empty to use "
                    "secrets.yaml (the generated config emits !secret wifi_ssid / wifi_password)",
                )

    filename = f"{name}.yaml"
    config_path = controller._db.settings.rel_path(filename)

    # Fast collision check before the (~hundreds of ms) validator
    # round-trip so a duplicate-name attempt fails on the right
    # diagnostic. ``ALREADY_EXISTS`` (not ``INVALID_ARGS``) so the
    # frontend can offer an overwrite instead of a dead-end error. The
    # write further down is the actual race-safe path; this is a UX
    # optimisation.
    file_existed = await run_in_executor(config_path.exists)
    if file_existed and not overwrite:
        msg = f"Configuration {filename} already exists"
        raise CommandError(ErrorCode.ALREADY_EXISTS, msg)

    # Surface user-correctable failures (unknown board) as typed
    # ``INVALID_ARGS`` so the wizard can show a specific message.
    board = None
    if board_id:
        if controller._db.boards:
            board = await controller._db.boards.get_board(board_id=board_id)
        if board is None:
            msg = f"Unknown board: {board_id}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

    yaml_content, source = await controller._yaml_content_for_create(
        name, friendly, board, file_content, ssid, psk
    )

    # Validate generated YAML before write so a regression in
    # generate_device_yaml / generate_minimal_stub_yaml surfaces
    # as INTERNAL_ERROR rather than landing an unflashable YAML
    # on disk. User uploads are deliberately skipped: the upload
    # flow exists so users can bring an existing (often older)
    # config into the builder and repair it in the editor.
    if source == "user":
        # Schema validation stays off (an older-but-valid config must
        # still land for repair), but a binary blob is a different
        # failure: a .tar.gz read as text can't be opened in the editor
        # at all, so refuse it instead of writing an unparsable .yaml.
        if _looks_binary(yaml_content):
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "This doesn't look like a text YAML configuration; it "
                "contains binary data, for example a .tar.gz archive. "
                "Upload a plain-text .yaml file.",
            )
    elif source == "package":
        # A ``packages: github://...`` YAML only validates through a live
        # upstream fetch, so use the adopt contract: short budget,
        # unavailability tolerated (the compile surfaces a real fetch
        # failure later). Genuine schema errors still mean our generator
        # broke.
        await controller._validate_rewritten_yaml_or_raise(
            filename,
            yaml_content,
            action="create",
            on_failure=ErrorCode.INTERNAL_ERROR,
            tolerate_unavailable=True,
            timeout=IMPORT_VALIDATE_TIMEOUT,
        )
    else:
        await controller._validate_rewritten_yaml_or_raise(
            filename,
            yaml_content,
            action="create",
            on_failure=ErrorCode.INTERNAL_ERROR,
        )

    # Only for _init_storage's platform fallback; board_id is left
    # to the scanner, which derives it on resolve (never persisted here).
    parsed_platform = ""
    if not board_id:
        parsed_platform, _pio_board, _variant = parse_platform_from_yaml(yaml_content)

    def _raise_already_exists(exc: BaseException) -> NoReturn:
        msg = f"Configuration {filename} already exists"
        raise CommandError(ErrorCode.ALREADY_EXISTS, msg) from exc

    overwriting = overwrite and file_existed
    if overwriting:
        # Atomic in-place rewrite (stage + move) so a crash can't leave a
        # half-written config; the user explicitly confirmed the overwrite.
        await run_in_executor(write_user_yaml, config_path, yaml_content)
    else:
        await write_new_file_exclusive(config_path, yaml_content, on_exists=_raise_already_exists)

    # Overwriting an existing device keeps its StorageJSON (build state)
    # and dashboard metadata; only a fresh device gets a new sidecar.
    if not overwriting:
        platform = str(board.esphome.platform) if board else parsed_platform
        await run_in_executor(init_device_storage, filename, name, friendly, platform)
    await controller._register_new_device(
        filename,
        f"{'Overwrite' if overwriting else 'Create'} {filename}",
        board_id=board_id,
        clear_metadata=not overwriting,
    )
    return WizardResponse(configuration=filename)


def save_device_storage(filename: str, storage: StorageJSON) -> None:
    """Persist *storage* to *filename*'s sidecar path, creating the dir."""
    storage_path = resolve_storage_path(filename)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage.save(storage_path)


def init_device_storage(filename: str, name: str, friendly_name: str | None, platform: str) -> None:
    """Write a fresh StorageJSON sidecar for a newly created / imported device."""
    storage = StorageJSON(
        storage_version=1,
        name=name,
        friendly_name=friendly_name,
        comment=None,
        esphome_version=None,
        src_version=None,
        address=default_mdns_address(name),
        web_port=None,
        target_platform=platform,
        build_path=None,
        firmware_bin_path=None,
        loaded_integrations=[],
        loaded_platforms=[],
        no_mdns=False,
    )
    save_device_storage(filename, storage)
