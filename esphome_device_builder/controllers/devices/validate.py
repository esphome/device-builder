"""``devices/validate`` WS command body."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .helpers import _redact_concealed_secrets

if TYPE_CHECKING:
    from collections.abc import Callable

    from .controller import DevicesController


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
    await controller._stream_subprocess(cmd, client, message_id, line_transform=line_transform)
