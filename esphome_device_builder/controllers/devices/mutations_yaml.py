"""Shared YAML helpers for the device-mutation WS commands."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

from ...helpers.api import CommandError
from ...helpers.device_yaml import (
    NETWORK_PROVIDER_COMPONENT_IDS,
    generate_device_yaml,
    generate_minimal_stub_yaml,
)
from ...models import ErrorCode
from ..editor import ValidatorUnavailableError

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
    wifi_secrets_available: bool = True,
    wifi_requested: bool = False,
    catalog: ComponentCatalog | None = None,
) -> tuple[str, CreateYamlSource]:
    """
    Pick the YAML body for ``devices/create`` based on the inputs.

    A board with onboard-network suggested hardware (``ethernet:``) is
    wired by default — the network component is auto-pulled into
    *defaults*, and the generator drops the ``wifi:`` block in its
    favour — unless the user opts that device into Wi-Fi. *wifi_requested*
    is that opt-in for the ``!secret`` path (the caller persisted the
    credentials and cleared *ssid*); a literal *ssid* opts in the same way.
    """
    if file_content:
        return file_content, "user"
    if board:
        defaults = (
            await catalog.resolve_default_components(board)
            if catalog and board.default_components
            else []
        )
        # Auto-pull onboard ethernet only when the board doesn't already
        # provide a network through ``default_components`` — a board listing a
        # provider in both lists would otherwise merge its block twice.
        already_networked = any(
            component.id in NETWORK_PROVIDER_COMPONENT_IDS for component, _ in defaults
        )
        wants_wifi = bool(ssid) or wifi_requested
        if catalog and not wants_wifi and not already_networked and board.featured_components:
            defaults.extend(await catalog.resolve_network_components(board))
        return (
            generate_device_yaml(
                name,
                friendly,
                board,
                ssid,
                psk,
                wifi_secrets_available=wifi_secrets_available,
                defaults=defaults,
            ),
            "template",
        )
    return (
        generate_minimal_stub_yaml(name, friendly, wifi_secrets_available=wifi_secrets_available),
        "stub",
    )


async def validate_rewritten_yaml_or_raise(
    editor: EditorController | None,
    configuration: str,
    content: str,
    *,
    action: str,
    on_failure: ErrorCode = ErrorCode.INVALID_ARGS,
    on_error_cleanup: Callable[[], None] | None = None,
    tolerate_unavailable: bool = False,
    timeout: float | None = None,
) -> None:
    """
    Schema-validate *content* via the editor; raise if invalid.

    No-op when *editor* is None. *on_failure* selects the
    ``ErrorCode`` raised: ``INVALID_ARGS`` for user-fixable
    input, ``INTERNAL_ERROR`` for broken YAML from our own
    generators. *on_error_cleanup* runs in a finally on any
    non-success path so callers that wrote the YAML before
    validating can roll back.

    *tolerate_unavailable* treats validator unavailability (timeout /
    subprocess failure) as success: file kept, no cleanup; genuine
    YAML/schema errors still raise. *timeout* overrides the validator's
    round-trip budget.
    """
    if editor is None:
        return
    succeeded = False
    try:
        try:
            result = await editor.validate_yaml(
                configuration=configuration, content=content, timeout=timeout
            )
        except TimeoutError:
            if not tolerate_unavailable:
                raise
            # Expected on adopt: the cold ``github://`` fetch outran the budget.
            _LOGGER.info(
                "Validation of %s for %s timed out; keeping file, deferring to compile/install",
                configuration,
                action,
            )
            succeeded = True
            return
        except (ValidatorUnavailableError, BrokenPipeError):
            if not tolerate_unavailable:
                raise
            # Subprocess down (a generic RuntimeError still propagates); WARNING
            # since an always-down validator is operationally significant.
            _LOGGER.warning(
                "Validator subprocess unavailable during %s of %s; keeping file unvalidated",
                action,
                configuration,
            )
            succeeded = True
            return
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
