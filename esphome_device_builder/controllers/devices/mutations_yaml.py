"""Shared YAML helpers for the device-mutation WS commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, NoReturn

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    NETWORK_PROVIDER_COMPONENT_IDS,
    board_provides_network,
    generate_adoption_yaml,
    generate_device_yaml,
    generate_minimal_stub_yaml,
)
from ...helpers.yaml.scan import block_end_index, find_block_header
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
# ``"package"`` -> :func:`generate_adoption_yaml` (a thin
# ``packages: github://...`` reference, the same shape ``devices/import``
# writes; validity depends on a live upstream fetch, so the caller
# validates with the adopt contract — short budget, unavailability
# tolerated — not the strict template one).
CreateYamlSource = Literal["user", "template", "stub", "package"]


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
    if board and board.package_import_url:
        # Remote-package board (bluetooth-proxies import): the device is
        # exactly what ``devices/import`` would have written for it — a thin
        # ``packages:`` reference tracking upstream on every compile — except
        # the wizard supplies a good name up front instead of the factory's
        # mac-suffixed broadcast. The Wi-Fi opt-in doesn't apply: a package
        # with an onboard network pins that network upstream (ESPHome rejects
        # wifi beside ethernet), so credentials only land when needed.
        return (
            generate_adoption_yaml(
                name,
                friendly,
                board.package_name or board.id,
                board.package_import_url,
                network_provided=board_provides_network(board),
                ssid=ssid,
                psk=psk,
                wifi_secrets_available=wifi_secrets_available,
            ),
            "package",
        )
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
    packages_span: tuple[int, int] | None = None,
    failure_tail: str | None = None,
) -> str | None:
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

    *packages_span* (0-indexed line span of the ``packages:`` block):
    when every validation error roots inside it, the file is kept and a
    warning string is returned instead of raising. Returns ``None``
    when *content* validates clean.

    *failure_tail* overrides the ``INVALID_ARGS`` refusal's closing
    sentence.
    """
    if editor is None:
        return None
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
            return None
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
            return None
        errors = [
            *(err.get("message", "") for err in result.get("yaml_errors", [])),
            *(err.get("message", "") for err in result.get("validation_errors", [])),
        ]
        errors = [msg for msg in errors if msg]
        if not errors:
            succeeded = True
            return None
        warning = _packages_confined_warning(result, packages_span, configuration, action)
        if warning is not None:
            succeeded = True
            return warning
        _raise_validation_failure(
            errors, action=action, on_failure=on_failure, failure_tail=failure_tail
        )
    finally:
        if not succeeded and on_error_cleanup is not None:
            # Swallow + log cleanup failures so a permission /
            # FS error during rollback doesn't replace the
            # original validation diagnostic the caller is
            # about to see.
            try:
                await run_in_executor(on_error_cleanup)
            except Exception:
                _LOGGER.exception("on_error_cleanup raised; original error preserved")


def _raise_validation_failure(
    errors: list[str],
    *,
    action: str,
    on_failure: ErrorCode,
    failure_tail: str | None,
) -> NoReturn:
    """Raise the refusal ``CommandError`` for a failed validation."""
    if on_failure is ErrorCode.INTERNAL_ERROR:
        message_tail = (
            ". Please report this with a redacted snippet of just the "
            "esphome: / substitutions: blocks (strip Wi-Fi credentials, "
            "API keys, and static IPs) so the dashboard generator can "
            "be fixed."
        )
    else:
        message_tail = failure_tail or ". Fix the errors in the editor and try again."
    raise CommandError(
        on_failure,
        f"Can't {action} — config doesn't validate: " + _summarise(errors) + message_tail,
    )


def packages_block_span(content: str) -> tuple[int, int] | None:
    """
    0-indexed line span of the top-level ``packages:`` block, or ``None``.

    ``None`` also when the block runs to EOF — an unbounded span would
    classify every trailing error as package-confined.
    """
    lines = content.splitlines(keepends=True)
    start = find_block_header(lines, "packages")
    if start is None:
        return None
    end = block_end_index(lines, start)
    if end >= len(lines):
        return None
    return start, end


def _packages_confined_warning(
    result: dict,
    packages_span: tuple[int, int] | None,
    configuration: str,
    action: str,
) -> str | None:
    """Warning when every validation error is attributable to the packages block, else ``None``."""
    if packages_span is None or result.get("yaml_errors"):
        return None
    entries = result.get("validation_errors", [])
    if not entries or not all(
        _entry_confined_to_packages(entry, packages_span) for entry in entries
    ):
        return None
    _LOGGER.info(
        "Validation of %s for %s failed only inside the packages block; keeping file",
        configuration,
        action,
    )
    body = _summarise([str(entry.get("message", "")) for entry in entries])
    return (
        f"Imported, but the remote package didn't validate: {body}. "
        "Fix the packages entry in the editor; install will surface "
        "the same error until it resolves."
    )


def _summarise(errors: list[str]) -> str:
    """Join up to three non-empty messages with a ``(+N more)`` count, period-trimmed."""
    errors = [msg for msg in errors if msg]
    shown = errors[:3]
    suffix = f" (+{len(errors) - len(shown)} more)" if len(errors) > len(shown) else ""
    # esphome messages often end with their own period; the caller's
    # tail brings the sentence break.
    return ("; ".join(shown) + suffix).removesuffix(".")


def _entry_confined_to_packages(entry: dict, packages_span: tuple[int, int]) -> bool:
    """
    Report whether a validation error is attributable to the packages block.

    The validator marks the edited file ``<file>``; an error in any
    other document (fetched package content, or an ``!include``) is
    treated as confined. Callers gate *packages_span* to generated
    adoption YAML, which carries no ``!include``, so a foreign document
    is package content by construction.
    """
    range_ = entry.get("range")
    if not range_:
        return False
    if range_.get("document", "<file>") != "<file>":
        return True
    start, end = packages_span
    return bool(start <= range_.get("start_line", -1) < end)
