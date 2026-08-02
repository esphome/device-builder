"""Apply an HA-provisioned API encryption key to a device's YAML."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.mac_addresses import normalize_mac
from ...helpers.yaml import (
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

_KEY_PATH = ("api", "encryption", "key")
_KEY_BYTES = 32


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
    devices = controller._scanner.get_by_name(name)
    if not devices and normalized_mac:
        devices = [d for d in controller.get_devices() if d.mac_address == normalized_mac]
    if not devices:
        controller._pending_keys.set(name, key, normalized_mac)
        _LOGGER.info("Stored pending API encryption key for unadopted device %s", name)
        return {"result": "stored"}

    updated: list[str] = []
    unchanged: list[str] = []
    reason = ""
    for device in devices:
        outcome, why = await _apply_to_device(controller, device, key)
        if outcome == "updated":
            updated.append(device.configuration)
        elif outcome == "unchanged":
            unchanged.append(device.configuration)
        else:
            reason = reason or why
    # A configured name must not keep a stale pending entry around.
    controller._pending_keys.pop(name)

    response: dict[str, Any] = {"configurations": [d.configuration for d in devices]}
    if updated:
        response["result"] = "updated"
    elif unchanged:
        response["result"] = "unchanged"
    else:
        response["result"] = "not_writable"
        response["reason"] = reason
    return response


async def _apply_to_device(
    controller: DevicesController, device: Device, key: str
) -> tuple[str, str]:
    """Splice *key* into *device*'s YAML; returns ``(outcome, reason)``."""
    configuration = device.configuration
    content = await _read_device_yaml_or_raise(controller, configuration)

    existing = read_yaml_scalar(content, _KEY_PATH)
    if existing is not None and _strip_yaml_quotes(existing) == key:
        return "unchanged", ""
    if (
        existing is None
        and not component_block_present(content, "api")
        and "api" not in device.loaded_integrations
    ):
        return "not_writable", "the configuration does not enable the native API"

    try:
        new_content = upsert_api_encryption_key(content, key)
    except YamlUpsertNotSupportedError as exc:
        return "not_writable", str(exc)
    if new_content == content:
        return "not_writable", "the key is provided via !secret or a substitution"

    reread = read_yaml_scalar(new_content, _KEY_PATH)
    if reread is None or _strip_yaml_quotes(reread) != key:
        raise CommandError(
            ErrorCode.INTERNAL_ERROR,
            "Edited YAML doesn't round-trip through the reader — the "
            "line-based upsert produced a shape the parser misinterprets.",
        )

    await controller._validate_rewritten_yaml_or_raise(
        configuration, new_content, action="update encryption key"
    )
    await controller._persist_yaml_mutation(
        configuration, new_content, message=f"Update API encryption key in {configuration}"
    )
    return "updated", ""


def _validate_key(key: str) -> None:
    """Require a base64 literal decoding to exactly 32 bytes."""
    try:
        decoded = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must be base64") from exc
    if len(decoded) != _KEY_BYTES:
        raise CommandError(ErrorCode.INVALID_ARGS, "key must decode to exactly 32 bytes")
