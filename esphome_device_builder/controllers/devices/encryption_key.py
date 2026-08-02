"""Apply an HA-provisioned API encryption key to a device's YAML."""

from __future__ import annotations

import base64
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.mac_addresses import normalize_mac
from ...helpers.yaml import (
    API_ENCRYPTION_KEY_PATH,
    YamlUpsertNotSupportedError,
    _strip_yaml_quotes,
    component_block_present,
    read_yaml_scalar,
    upsert_api_encryption_key,
)
from ...models import ErrorCode
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
        outcome, why = await _apply_to_device(controller, device, key)
        outcomes.add(outcome)
        reason = reason or why
    # Consume the stale pending entry only once the key actually landed
    # (or already matches) — an all-refused push must not destroy the
    # only stored copy.
    if outcomes & {KeyHandoffResult.UPDATED, KeyHandoffResult.UNCHANGED}:
        controller._pending_keys.pop(name)

    response: dict[str, Any] = {"configurations": [d.configuration for d in devices]}
    if KeyHandoffResult.UPDATED in outcomes:
        response["result"] = KeyHandoffResult.UPDATED
    elif KeyHandoffResult.UNCHANGED in outcomes:
        response["result"] = KeyHandoffResult.UNCHANGED
    else:
        response["result"] = KeyHandoffResult.NOT_WRITABLE
        response["reason"] = reason
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
    if existing is not None and _strip_yaml_quotes(existing) == key:
        return KeyHandoffResult.UNCHANGED, ""
    if existing is None and not device.api_enabled and not component_block_present(content, "api"):
        return KeyHandoffResult.NOT_WRITABLE, "the configuration does not enable the native API"

    try:
        new_content = upsert_api_encryption_key(content, key)
    except YamlUpsertNotSupportedError as exc:
        return KeyHandoffResult.NOT_WRITABLE, str(exc)
    if new_content == content:
        return KeyHandoffResult.NOT_WRITABLE, "the key is provided via !secret or a substitution"

    reread = read_yaml_scalar(new_content, API_ENCRYPTION_KEY_PATH)
    if reread is None or _strip_yaml_quotes(reread) != key:
        raise CommandError(
            ErrorCode.INTERNAL_ERROR, "Edited YAML doesn't round-trip through the reader"
        )

    await controller._validate_rewritten_yaml_or_raise(
        configuration, new_content, action="update encryption key"
    )
    await controller._persist_yaml_mutation(
        configuration, new_content, message=f"Update API encryption key in {configuration}"
    )
    return KeyHandoffResult.UPDATED, ""


def _validate_key(key: str) -> None:
    """Require a base64 literal decoding to exactly 32 bytes."""
    try:
        decoded = base64.b64decode(key, validate=True)
    except ValueError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must be base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must decode to exactly 32 bytes")
