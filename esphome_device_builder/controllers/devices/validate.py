"""``devices/validate`` WS command body."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...models import ErrorCode
from .helpers import _redact_concealed_secrets

if TYPE_CHECKING:
    from collections.abc import Callable

    from .controller import DevicesController

# Each ``esphome config`` child imports ``esphome.components`` (~70 MiB RSS).
# A stream holds its permit until the client drains it, so this pool is
# separate from the bounded ``_config_semaphore`` in ``helpers/device_yaml``.
_MAX_CONCURRENT_VALIDATES = 3
_QUEUE_TIMEOUT = 30.0
_validate_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_VALIDATES)

_LOGGER = logging.getLogger(__name__)


async def validate_config(
    controller: DevicesController,
    *,
    configuration: str,
    show_secrets: bool,
    client: Any,
    message_id: str,
) -> None:
    """
    Validate a device YAML config; streams output per-connection.

    ``show_secrets`` passes ``--show-secrets`` to ``esphome
    config`` when True so resolved ``!secret`` values appear
    in the output; when False, ANSI-conceal-wrapped secret
    runs are stripped from each line before it leaves the
    WS handler.
    """
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*controller.state.esphome_cmd, "--dashboard", "config", config_path]
    line_transform: Callable[[str], str] | None = None
    if show_secrets:
        cmd.append("--show-secrets")
    else:
        # ``esphome config`` without ``--show-secrets`` doesn't
        # redact; it wraps each ``password|key|psk|ssid`` value
        # in the ANSI conceal SGR (8/28). Browsers don't honour
        # the escape, so the resolved secret bytes were leaking
        # plain into the validate dialog. Strip the wrapped runs
        # before the line leaves the WS handler.
        line_transform = _redact_concealed_secrets
    if _validate_semaphore.locked():
        _LOGGER.debug("Validate of %s is queued behind the running pool", configuration)
    try:
        async with asyncio.timeout(_QUEUE_TIMEOUT):
            await _validate_semaphore.acquire()
    except TimeoutError as err:
        _LOGGER.warning(
            "Validate of %s refused: the pool stayed full for %ss", configuration, _QUEUE_TIMEOUT
        )
        msg = "Too many validations are running; retry shortly"
        raise CommandError(ErrorCode.UNAVAILABLE, msg) from err
    try:
        await controller._stream_subprocess(cmd, client, message_id, line_transform=line_transform)
    finally:
        _validate_semaphore.release()
