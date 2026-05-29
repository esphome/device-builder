"""Shared YAML helpers for the device-mutation WS commands."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from ...helpers.api import CommandError
from ...helpers.device_yaml import generate_device_yaml, generate_minimal_stub_yaml
from ...models import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable

    from ...models import BoardCatalogEntry
    from ..components import ComponentCatalog
    from ..editor import EditorController

_LOGGER = logging.getLogger(__name__)

# Provenance tag for ``yaml_content_for_create``'s return tuple.
# ``"user"`` -> caller-supplied ``file_content`` (validation
# failure surfaces as ``INVALID_ARGS``).
# ``"template"`` -> :func:`generate_device_yaml` against a known
# catalog entry (validation failure -> ``INTERNAL_ERROR``).
# ``"stub"`` -> :func:`generate_minimal_stub_yaml` (no inputs;
# validation failure -> ``INTERNAL_ERROR``; caller skips the YAML-
# driven board-id derivation since the stub's hard-coded
# ``board: esp32dev`` would otherwise pin metadata to whatever
# catalog entry happens to share that PIO board).
CreateYamlSource = Literal["user", "template", "stub"]


async def yaml_content_for_create(
    name: str,
    friendly: str,
    board: BoardCatalogEntry | None,
    file_content: str | None,
    ssid: str,
    psk: str,
    *,
    catalog: ComponentCatalog | None = None,
) -> tuple[str, CreateYamlSource]:
    """Pick the YAML body for ``devices/create`` based on the inputs."""
    if file_content:
        return file_content, "user"
    if board:
        defaults = (
            await catalog.resolve_default_components(board)
            if catalog and board.default_components
            else None
        )
        return (
            generate_device_yaml(name, friendly, board, ssid, psk, defaults=defaults),
            "template",
        )
    return generate_minimal_stub_yaml(name, friendly), "stub"


async def validate_rewritten_yaml_or_raise(
    editor: EditorController | None,
    configuration: str,
    content: str,
    *,
    action: str,
    on_failure: ErrorCode = ErrorCode.INVALID_ARGS,
    on_error_cleanup: Callable[[], None] | None = None,
) -> None:
    """
    Schema-validate *content* via the editor; raise if invalid.

    No-op when *editor* is None. *on_failure* selects the
    ``ErrorCode`` raised: ``INVALID_ARGS`` for user-fixable
    input, ``INTERNAL_ERROR`` for broken YAML from our own
    generators. *on_error_cleanup* runs in a finally on any
    non-success path so callers that wrote the YAML before
    validating can roll back.
    """
    if editor is None:
        return
    succeeded = False
    try:
        result = await editor.validate_yaml(configuration=configuration, content=content)
        errors = [
            *(err.get("message", "") for err in result.get("yaml_errors", [])),
            *(err.get("message", "") for err in result.get("validation_errors", [])),
        ]
        errors = [msg for msg in errors if msg]
        if not errors:
            succeeded = True
            return
        shown = errors[:3]
        suffix = f" (+{len(errors) - len(shown)} more)" if len(errors) > len(shown) else ""
        message_tail = (
            ". Please report this with a redacted snippet of just the "
            "esphome: / substitutions: blocks (strip Wi-Fi credentials, "
            "API keys, and static IPs) so the dashboard generator can "
            "be fixed."
            if on_failure is ErrorCode.INTERNAL_ERROR
            else ". Fix the errors in the editor and try again."
        )
        raise CommandError(
            on_failure,
            f"Can't {action} — config doesn't validate: "
            + "; ".join(shown)
            + suffix
            + message_tail,
        )
    finally:
        if not succeeded and on_error_cleanup is not None:
            # Swallow + log cleanup failures so a permission /
            # FS error during rollback doesn't replace the
            # original validation diagnostic the caller is
            # about to see.
            try:
                await asyncio.get_running_loop().run_in_executor(None, on_error_cleanup)
            except Exception:
                _LOGGER.exception("on_error_cleanup raised; original error preserved")
