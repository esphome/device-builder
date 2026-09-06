"""Apply an HA-provisioned API encryption key to a device's YAML."""

from __future__ import annotations

import base64
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.device_yaml import EsphomeConfigUnavailableError, run_esphome_config
from ...helpers.mac_addresses import normalize_mac
from ...helpers.yaml import (
    API_ENCRYPTION_KEY_PATH,
    YamlUpsertNotSupportedError,
    _strip_yaml_quotes,
    component_block_present,
    read_ota_encryption_key,
    read_yaml_scalar,
    rewrite_ota_encryption_key,
    upsert_api_encryption_key,
)
from ...models import ErrorCode
from ..editor import ValidatorUnavailableError
from .mutations_simple import _read_device_yaml_or_raise

if TYPE_CHECKING:
    from ...models import Device
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

_KEY_BYTES = 32


class KeyHandoffResult(StrEnum):
    """Wire ``result`` values for the HA encryption-key handoff."""

    STORED = "stored"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    NOT_WRITABLE = "not_writable"


async def set_encryption_key(
    controller: DevicesController, *, name: str, key: str, mac: str = ""
) -> dict[str, Any]:
    """
    Land an HA-provisioned key: splice into configured YAML(s) or stash for adoption.

    The pushed key reflects what the device actually accepted, so an
    existing literal is overwritten; indirections (``!secret`` /
    ``${…}``) and API-less configurations are refused with a reason.
    """
    _validate_key(key)
    normalized_mac = normalize_mac(mac)
    devices = _match_devices(controller, name, normalized_mac)
    if not devices:
        controller._pending_keys.set(name, key, normalized_mac)
        _LOGGER.info("Stored pending API encryption key for unadopted device %s", name)
        return {"result": KeyHandoffResult.STORED}

    outcomes: set[KeyHandoffResult] = set()
    reason = ""
    for device in devices:
        try:
            outcome, why = await _apply_to_device(controller, device, key)
        except CommandError as err:
            # A failing sibling (validation rejection, vanished file)
            # must not unwind the loop: the key-retention policy and
            # the other devices' outcomes still apply.
            outcome, why = KeyHandoffResult.NOT_WRITABLE, err.message
        outcomes.add(outcome)
        reason = reason or why
    if outcomes & {KeyHandoffResult.UPDATED, KeyHandoffResult.UNCHANGED}:
        # Consume the pending entry only once the key actually landed.
        controller._pending_keys.pop(name)
    else:
        # Nothing accepted the key — keep a copy so a later push, the
        # post-install retry, or a delete-and-readopt can consume it.
        controller._pending_keys.set(name, key, normalized_mac)

    response: dict[str, Any] = {"configurations": [d.configuration for d in devices]}
    if KeyHandoffResult.UPDATED in outcomes:
        response["result"] = KeyHandoffResult.UPDATED
    elif KeyHandoffResult.UNCHANGED in outcomes:
        response["result"] = KeyHandoffResult.UNCHANGED
    else:
        response["result"] = KeyHandoffResult.NOT_WRITABLE
    # A duplicate-name sibling that refused must not vanish behind the
    # aggregate success — its YAML still carries a competing key.
    if KeyHandoffResult.NOT_WRITABLE in outcomes:
        response["reason"] = reason
        _LOGGER.info("HA key handoff for %s refused: %s", name, reason)
    return response


def _match_devices(controller: DevicesController, name: str, mac: str) -> list[Device]:
    """Match by name, disambiguating duplicate-name buckets (and misses) by MAC."""
    devices = controller._scanner.get_by_name(name)
    if mac and len(devices) > 1:
        by_mac = [d for d in devices if d.mac_address == mac]
        if by_mac:
            return by_mac
    if not devices and mac:
        return [d for d in controller.get_devices() if d.mac_address == mac]
    return devices


async def _apply_to_device(
    controller: DevicesController, device: Device, key: str
) -> tuple[KeyHandoffResult, str]:
    """Splice *key* into *device*'s YAML; returns ``(outcome, reason)``."""
    configuration = device.configuration
    content = await _read_device_yaml_or_raise(controller, configuration)

    existing = read_yaml_scalar(content, API_ENCRYPTION_KEY_PATH)
    api_matches = _holds_key(existing, key)
    # An explicit ota key must equal the api key or esphome rejects the
    # config, so it follows the pushed key; a bare ``encryption:`` inherits.
    ota_existing = read_ota_encryption_key(content)
    ota_matches = ota_existing is None or _holds_key(ota_existing, key)
    if api_matches and ota_matches:
        return KeyHandoffResult.UNCHANGED, ""
    if existing is None and not device.api_enabled and not component_block_present(content, "api"):
        # The push itself proves the device's API is up (HA set the key
        # over it), but a package device that has never been compiled is
        # indistinguishable from a config the user stripped api: out of
        # — resolve the config and let ground truth decide.
        has_api = await _resolved_config_has_api(controller, configuration)
        if not has_api:
            reason = (
                "the resolved configuration does not enable the native API"
                if has_api is False
                else "the configuration could not be resolved to confirm the "
                "native API; the key was kept for a later attempt"
            )
            return KeyHandoffResult.NOT_WRITABLE, reason

    new_content, reason = _splice_keys(
        content, key, api_matches=api_matches, ota_matches=ota_matches
    )
    if new_content is None:
        return KeyHandoffResult.NOT_WRITABLE, reason

    reread = read_yaml_scalar(new_content, API_ENCRYPTION_KEY_PATH)
    ota_reread = read_ota_encryption_key(new_content) if ota_existing is not None else key
    if not (_holds_key(reread, key) and _holds_key(ota_reread, key)):
        raise CommandError(
            ErrorCode.INTERNAL_ERROR, "Edited YAML doesn't round-trip through the reader"
        )

    try:
        await controller._validate_rewritten_yaml_or_raise(
            configuration, new_content, action="update encryption key"
        )
    except (TimeoutError, ValidatorUnavailableError):
        reason = (
            "the rewritten configuration could not be validated in time; "
            "the key was kept for a later attempt"
        )
        return KeyHandoffResult.NOT_WRITABLE, reason
    await controller._persist_yaml_mutation(
        configuration, new_content, message=f"Update API encryption key in {configuration}"
    )
    return KeyHandoffResult.UPDATED, ""


def _holds_key(raw: str | None, key: str) -> bool:
    """Whether the raw YAML scalar *raw* is the literal *key*."""
    return raw is not None and _strip_yaml_quotes(raw) == key


def _splice_keys(
    content: str, key: str, *, api_matches: bool, ota_matches: bool
) -> tuple[str | None, str]:
    """Rewrite the api key and any explicit ota key to *key*; ``(None, reason)`` on refusal."""
    try:
        new_content = upsert_api_encryption_key(content, key)
    except YamlUpsertNotSupportedError as exc:
        return None, str(exc)
    if new_content == content and not api_matches:
        return None, "the key is provided via !secret or a substitution"
    if ota_matches:
        return new_content, ""
    rewritten = rewrite_ota_encryption_key(new_content, key)
    if rewritten == new_content:
        return None, "the OTA encryption key is provided via !secret or a substitution"
    return rewritten, ""


async def _resolved_config_has_api(
    controller: DevicesController, configuration: str
) -> bool | None:
    """Whether the fully resolved config carries ``api:``; ``None`` when unresolvable."""
    esphome_cmd = controller.state.esphome_cmd
    if not esphome_cmd:
        return None
    path = controller._db.settings.rel_path(configuration)
    try:
        config = await run_esphome_config(esphome_cmd, path)
    except EsphomeConfigUnavailableError:
        return None
    if config is None:
        return None
    return "api" in config


def _validate_key(key: str) -> None:
    """Require a base64 literal decoding to exactly 32 bytes."""
    try:
        decoded = base64.b64decode(key, validate=True)
    except ValueError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must be base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must decode to exactly 32 bytes")
