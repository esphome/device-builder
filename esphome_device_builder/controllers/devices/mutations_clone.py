"""``devices/clone`` WS command body."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.device_yaml import configuration_stem
from ...helpers.yaml import (
    generate_api_encryption_key,
    rewrite_api_encryption_key,
    rewrite_name_or_substitution,
)
from ...models import ErrorCode
from .helpers import _rewrite_required_yaml_leaf, friendly_name_slugify

if TYPE_CHECKING:
    from .controller import DevicesController


async def clone_device(
    controller: DevicesController,
    *,
    configuration: str,
    new_name: str,
    new_friendly_name: str | None,
) -> dict[str, str]:
    """
    Duplicate an existing device YAML under a fresh hostname.

    Designed for the "I bought 10 of the same bulb" workflow:
    keeps the source's components and wiring intact but takes
    a fresh ``esphome.name``, a fresh ``friendly_name``, and a
    freshly-generated ``api.encryption.key`` so two siblings
    don't share encryption material. ``!secret`` /
    ``${substitution}`` indirections for the API key are
    preserved on purpose since the indirection target is
    shared with the source on disk and rewriting the
    indirection name would silently desync the rendered
    config from the actual ``secrets.yaml`` value.
    """
    new_name = new_name.strip()
    if not new_name:
        raise CommandError(ErrorCode.INVALID_ARGS, "new_name is required")
    new_filename = f"{new_name}.yaml"
    # Compare on the *stem* so cloning ``kitchen.yml`` to
    # ``new_name=kitchen`` is rejected even though the filenames
    # differ; both files would still carry the same
    # ``esphome.name`` and collide on mDNS.
    source_stem = configuration_stem(configuration)
    if new_name == source_stem:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "new_name must differ from the source device name",
        )

    loop = asyncio.get_running_loop()
    source_path = controller._db.settings.rel_path(configuration)
    new_path = controller._db.settings.rel_path(new_filename)
    if new_friendly_name is None:
        new_friendly_name = friendly_name_slugify(new_name)
    # Generate the fresh key off-loop so the executor work below
    # is purely I/O.
    new_key = generate_api_encryption_key()

    # Source YAML read on the executor; shared-sidecar read goes
    # through the client's sync path inside the same hop so the
    # identity carry-forward piggy-backs.
    def _gather() -> tuple[str | None, dict[str, Any], bool]:
        if new_path.exists():
            return None, {}, True
        if not source_path.exists():
            return None, {}, False
        content = source_path.read_text(encoding="utf-8")
        meta = controller._shared_sidecar.get_sync(configuration)
        return content, meta, False

    source_content, source_meta, target_existed = await loop.run_in_executor(None, _gather)
    if target_existed:
        msg = f"A device named {new_filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if source_content is None:
        msg = f"Source device {configuration} not found"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    # Validate the source before rewrite work; the leaf rewrites
    # are structure-preserving so a valid source produces a
    # valid clone, and bailing here points the user at the
    # source's actual schema errors instead of burning rewrite
    # work just to re-discover the source was unflashable.
    await controller._validate_rewritten_yaml_or_raise(
        configuration, source_content, action="clone"
    )

    # Rewrite the identity onto whichever line drives the value:
    # direct literal (``esphome.name: kitchen``) or substitution
    # reference (``esphome.name: ${devicename}`` with
    # ``substitutions.devicename: kitchen``). Rewriting the leaf
    # for the substitution case would orphan the variable and
    # break any other consumer of the same name (e.g. a sensor
    # named ``${devicename}_temp``); rewriting the substitution
    # definition retargets every reference atomically.
    # ``_rewrite_required_yaml_leaf`` rejects when the leaf is
    # missing entirely so a package-driven source can't silently
    # produce a duplicate hostname.
    new_content = _rewrite_required_yaml_leaf(source_content, ("esphome", "name"), new_name)
    # ``friendly_name`` is optional on the clone path; the
    # underlying helper is already a no-op when the leaf is
    # missing, so skip the required-leaf wrapper here.
    if new_friendly_name:
        new_content = rewrite_name_or_substitution(
            new_content, ("esphome", "friendly_name"), new_friendly_name
        )
    # No-op when the source uses ``!secret`` / ``${...}`` for
    # the key; those indirections stay shared with the source.
    new_content = rewrite_api_encryption_key(new_content, new_key)

    # Carry forward only the source's ``board_id`` since that's
    # the catalog-key indirection the user picked at wizard
    # time and the scanner can't recover it from the YAML.
    # Friendly name lives in the YAML we just wrote (no need to
    # duplicate to metadata). ``ip`` is intentionally not
    # carried; the clone hasn't booted yet and inheriting the
    # source's address would mis-route ``devices/logs`` until
    # the first mDNS announce. StorageJSON is skipped entirely
    # since it's a build artefact and the next compile writes a
    # real one.
    carry_board_id = source_meta.get("board_id") if source_meta else None

    def _commit() -> None:
        with new_path.open("x", encoding="utf-8") as f:
            f.write(new_content)

    try:
        await loop.run_in_executor(None, _commit)
    except FileExistsError as exc:
        # Race: another caller created the file between our
        # gather pass and the ``open(... "x")``. Surface as the
        # same INVALID_ARGS the preflight produces so the
        # frontend renders a single message.
        msg = f"A device named {new_filename} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
    if carry_board_id:
        await controller._persist_device_metadata_async(new_filename, board_id=carry_board_id)
    # Rescan so the scanner indexes the new YAML and fires the
    # ADDED event WS subscribers expect; ``probe_device`` runs
    # from the scan-change handler so no double-probe here.
    await controller._scanner.scan()
    return {"configuration": new_filename}
