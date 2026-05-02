"""Tests for streaming-subprocess cancellation via WS ``devices/stop_stream``."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers.devices import DevicesController


class _FakeWS:
    """Minimal WebSocket stand-in capturing sent messages."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_str(self, payload: str) -> None:
        import orjson

        self.sent.append(orjson.loads(payload))

    async def close(self) -> None:
        self.closed = True


def _make_client() -> WebSocketClient:
    return WebSocketClient(_FakeWS(), MagicMock(), authenticated=True)


def _make_controller() -> DevicesController:
    """Return a DevicesController shell that ``_stream_subprocess`` can use."""
    return DevicesController.__new__(DevicesController)


async def test_register_and_cancel_stream_runs_task_to_cancellation() -> None:
    """``cancel_stream`` cancels a registered task; uncancelled stays running."""
    client = _make_client()

    async def long_running() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(long_running())
    client.register_stream("abc", task)

    assert client.cancel_stream("abc") is True
    assert client.cancel_stream("abc") is False  # already cancelled / unregistered
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unregister_stream_is_idempotent() -> None:
    client = _make_client()
    client.register_stream("xyz", asyncio.create_task(asyncio.sleep(0)))
    client.unregister_stream("xyz")
    client.unregister_stream("xyz")  # no error


async def test_stream_subprocess_kills_proc_on_cancel(tmp_path: Any) -> None:
    """Cancellation of the streaming task terminates the subprocess.

    Uses a tiny Python child that writes one line and then sleeps; if our
    cancel path doesn't kill it, the test would hang on ``proc.wait()``.
    """
    ctrl = _make_controller()
    client = _make_client()
    sent_events: list[tuple[str, Any]] = []

    async def capture(_mid: str, event: str, data: Any = None) -> None:
        sent_events.append((event, data))

    # Patch the send_event to record events instead of going over the wire.
    client.send_event = capture  # type: ignore[method-assign]

    script = "import sys, time\nprint('hello', flush=True)\ntime.sleep(60)\n"
    cmd = [sys.executable, "-c", script]

    async def run_stream() -> None:
        await ctrl._stream_subprocess(cmd, client, "stream-1")

    task = asyncio.create_task(run_stream())
    # Wait for the first output line so we know the proc is up.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if any(ev == "output" for ev, _ in sent_events):
            break
    assert any(ev == "output" for ev, _ in sent_events), "child never produced output"

    # Cancel via the public API.
    assert client.cancel_stream("stream-1") is True

    # The task must complete promptly (proc was killed).
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    # No "result" event on cancel — the stream task ends in CANCELLED state
    # and the frontend learns it succeeded via ``devices/stop_stream``'s
    # normal response. We only sent "output" events.
    assert all(ev == "output" for ev, _ in sent_events)


async def test_stream_subprocess_normal_completion_emits_success(tmp_path: Any) -> None:
    r"""A subprocess that exits 0 sends a non-cancelled, success=True result."""
    ctrl = _make_controller()
    client = _make_client()
    sent_events: list[tuple[str, Any]] = []

    async def capture(_mid: str, event: str, data: Any = None) -> None:
        sent_events.append((event, data))

    client.send_event = capture  # type: ignore[method-assign]

    cmd = [sys.executable, "-c", "print('done')"]
    await ctrl._stream_subprocess(cmd, client, "stream-2")

    result_events = [data for ev, data in sent_events if ev == "result"]
    assert result_events == [{"code": 0, "success": True}]


async def test_stream_subprocess_emits_carriage_return_progress_lines() -> None:
    r"""`\r` progress lines reach the client as separate events.

    Pre-fix the consumer used the ``StreamReader`` default
    ``async for``, which only splits on ``\n``. esptool's
    ``Writing at 0x... (5%)\r`` lines piled up in the buffer
    until the next newline arrived — usually only when the
    operation finished — so the user saw a long pause then a wall
    of progress lines. With the shared ``iter_lines_with_progress``
    splitter, each ``\r``-terminated chunk surfaces as its own
    ``output`` event live.
    """
    ctrl = _make_controller()
    client = _make_client()
    sent_events: list[tuple[str, Any]] = []

    async def capture(_mid: str, event: str, data: Any = None) -> None:
        sent_events.append((event, data))

    client.send_event = capture  # type: ignore[method-assign]

    # Subprocess prints three progress chunks separated by ``\r``,
    # then a normal ``\n``-terminated completion line. Without the
    # ``\r`` splitter all four would arrive as one event after the
    # subprocess finished.
    #
    # Write through ``sys.stdout.buffer`` (binary mode) so Windows'
    # text-mode CRLF translation doesn't turn the trailing ``\n``
    # into ``\r\n`` — the helper coalesces CRLF correctly, but
    # binary-mode writes keep the test asserting on exactly the
    # bytes we mean to assert on regardless of platform.
    script = (
        "import sys\n"
        "sys.stdout.buffer.write(b'5%\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'50%\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'100%\\r')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stdout.buffer.write(b'done\\n')\n"
        "sys.stdout.buffer.flush()\n"
    )
    cmd = [sys.executable, "-c", script]
    await ctrl._stream_subprocess(cmd, client, "stream-cr")

    output_lines = [data for ev, data in sent_events if ev == "output"]
    # Terminators are stripped at the device-logs layer (the frontend
    # logs view appends each event as a new line; preserving ``\r``
    # would surface as visible Cs). The point is that all four lines
    # arrive as separate events, not concatenated into one.
    assert output_lines == ["5%", "50%", "100%", "done"]


async def test_cleanup_kills_running_streams() -> None:
    """A pending stream registered on a client is cancelled when the WS cleans up."""
    ctrl = _make_controller()
    client = _make_client()

    async def capture(_mid: str, _event: str, _data: Any = None) -> None: ...

    client.send_event = capture  # type: ignore[method-assign]

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    async def run_stream() -> None:
        await ctrl._stream_subprocess(cmd, client, "stream-3")

    task = client.create_task(run_stream())
    # Also register the stream like the real handler would (via current_task
    # inside _stream_subprocess). Give that a beat to take effect.
    for _ in range(20):
        if "stream-3" in client._stream_tasks:
            break
        await asyncio.sleep(0.05)
    assert "stream-3" in client._stream_tasks

    await client.cleanup()
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Stream-command construction (the WS handlers route into _stream_subprocess)
# ---------------------------------------------------------------------------


def _make_controller_with_settings(esphome_cmd: list[str]) -> DevicesController:
    """Stand up a controller far enough to exercise ``stream_logs`` / ``validate_config``."""
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._esphome_cmd = esphome_cmd
    ctrl._db = MagicMock()
    ctrl._db.settings.rel_path = Path
    return ctrl


async def test_stream_logs_command_includes_dashboard_flag() -> None:
    """``devices/logs`` invokes esphome with ``--dashboard`` before the subcommand.

    Without the flag ESPHome's ``ESPHomeLogFormatter`` lets ``colorama``
    strip the ANSI codes when stdout is piped to us — the dashboard log
    view ends up monochrome. The flag has to land before ``logs``
    because esphome's argparse only accepts it on the top-level parser.
    """
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.stream_logs(
        configuration="kitchen.yaml",
        port="OTA",
        client=MagicMock(),
        message_id="m1",
    )

    assert captured == [["esphome", "--dashboard", "logs", "kitchen.yaml", "--device", "OTA"]]


async def test_stream_logs_command_without_port_omits_device_arg() -> None:
    """No port given → no ``--device`` arg, ``--dashboard`` still present."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.stream_logs(configuration="kitchen.yaml", client=MagicMock(), message_id="m2")

    assert captured == [["esphome", "--dashboard", "logs", "kitchen.yaml"]]


async def test_stream_logs_command_appends_no_states_when_requested() -> None:
    """``no_states=True`` → ``--no-states`` is appended after the device arg."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.stream_logs(
        configuration="kitchen.yaml",
        port="OTA",
        no_states=True,
        client=MagicMock(),
        message_id="m1-ns",
    )

    assert captured == [
        ["esphome", "--dashboard", "logs", "kitchen.yaml", "--device", "OTA", "--no-states"]
    ]


async def test_stream_logs_command_no_states_without_port() -> None:
    """``no_states=True`` and no ``port`` → ``--no-states`` lands without ``--device``."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.stream_logs(
        configuration="kitchen.yaml",
        no_states=True,
        client=MagicMock(),
        message_id="m2-ns",
    )

    assert captured == [["esphome", "--dashboard", "logs", "kitchen.yaml", "--no-states"]]


async def test_stream_logs_command_omits_no_states_by_default() -> None:
    """Default ``no_states=False`` → no ``--no-states`` in argv (regression guard)."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.stream_logs(
        configuration="kitchen.yaml",
        port="OTA",
        client=MagicMock(),
        message_id="m3-ns",
    )

    assert captured == [["esphome", "--dashboard", "logs", "kitchen.yaml", "--device", "OTA"]]


async def test_validate_config_command_includes_dashboard_flag() -> None:
    """``devices/validate`` invokes ``esphome --dashboard config <yaml>``."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.validate_config(configuration="kitchen.yaml", client=MagicMock(), message_id="m3")

    assert captured == [["esphome", "--dashboard", "config", "kitchen.yaml"]]


async def test_validate_config_omits_show_secrets_by_default() -> None:
    """``--show-secrets`` is not appended unless the caller explicitly opts in.

    Resolved secrets are sensitive — the legacy dashboard surfaced
    them in screenshots / live streams when ``streamer_mode`` was
    off. The new dashboard inverts the default so they only appear
    when the user actively asks for them.
    """
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    # show_secrets=False explicitly, mirroring the WS default.
    await ctrl.validate_config(
        configuration="kitchen.yaml",
        show_secrets=False,
        client=MagicMock(),
        message_id="m4",
    )

    assert captured == [["esphome", "--dashboard", "config", "kitchen.yaml"]]


async def test_validate_config_passes_show_secrets_flag_when_enabled() -> None:
    """``show_secrets=True`` appends ``--show-secrets`` to the esphome command."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: list[list[str]] = []

    async def fake_stream(cmd: list[str], _client: Any, _mid: str, **_kwargs: Any) -> None:
        captured.append(cmd)

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.validate_config(
        configuration="kitchen.yaml",
        show_secrets=True,
        client=MagicMock(),
        message_id="m5",
    )

    # ``--show-secrets`` lands at the end of the argv — esphome's
    # subcommand parser accepts it on the ``config`` subparser, so
    # placement after the YAML is correct.
    assert captured == [
        ["esphome", "--dashboard", "config", "kitchen.yaml", "--show-secrets"],
    ]


async def test_validate_config_off_attaches_redactor_transform() -> None:
    r"""``show_secrets=False`` wires the line transform that scrubs concealed runs.

    ``esphome config`` without ``--show-secrets`` doesn't redact —
    it wraps every ``password|key|psk|ssid`` value in the ANSI
    Concealed SGR (``\\x1b[8m...\\x1b[28m``). The escape codes pass
    through to the browser unchanged because ansi-log doesn't honour
    Concealed, so the resolved secret bytes were rendering plain in
    the validate dialog. Hand ``_stream_subprocess`` a callable that
    strips those wrapped runs so the secret never leaves the
    server.
    """
    from esphome_device_builder.controllers.devices import _redact_concealed_secrets

    ctrl = _make_controller_with_settings(["esphome"])
    captured: dict[str, Any] = {}

    async def fake_stream(
        _cmd: list[str], _client: Any, _mid: str, *, line_transform: Any = None
    ) -> None:
        captured["line_transform"] = line_transform

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.validate_config(configuration="kitchen.yaml", client=MagicMock(), message_id="m-off")

    assert captured["line_transform"] is _redact_concealed_secrets


async def test_validate_config_on_passes_no_line_transform() -> None:
    """``show_secrets=True`` ships raw output — no redactor wired."""
    ctrl = _make_controller_with_settings(["esphome"])
    captured: dict[str, Any] = {}

    async def fake_stream(
        _cmd: list[str], _client: Any, _mid: str, *, line_transform: Any = None
    ) -> None:
        captured["line_transform"] = line_transform

    ctrl._stream_subprocess = fake_stream  # type: ignore[method-assign]

    await ctrl.validate_config(
        configuration="kitchen.yaml",
        show_secrets=True,
        client=MagicMock(),
        message_id="m-on",
    )

    assert captured["line_transform"] is None


def test_redact_concealed_secrets_replaces_wrapped_runs() -> None:
    r"""ESPHome's ``\\x1b[8m...\\x1b[28m`` wrapper resolves to ``<removed>``.

    Spec mirrored from ``esphome.__main__.command_config``: every
    ``(password|key|psk|ssid): VALUE`` line gets the value wrapped
    with the Concealed and Reveal SGR codes. Strip the entire
    wrapped run including the escapes — leaving the literal escape
    bytes in the output would still expose them to anyone screen-
    recording the network tab, even if the visible glyphs were
    hidden by a hypothetical conceal-aware renderer.
    """
    from esphome_device_builder.controllers.devices import _redact_concealed_secrets

    raw = "  password: \x1b[8mhunter2\x1b[28m"
    assert _redact_concealed_secrets(raw) == "  password: <removed>"


def test_redact_concealed_secrets_handles_dashboard_literal_escape() -> None:
    r"""``--dashboard`` mode emits literal ``\\033`` not the raw ESC byte.

    ESPHome's ``--dashboard`` flag re-encodes every real ANSI
    escape as the four-character sequence ``\\033`` so the
    dashboard renderer can re-decode them safely. Validate runs
    always pass ``--dashboard``, so the bytes that hit our handler
    are the literal form, not the raw ESC. The first cut of the
    redactor only matched the raw form and the secret bytes were
    leaking through verbatim — the user saw
    ``ssid: \\033[8mrocketiot\\033[28m`` in the dialog instead
    of ``ssid: <removed>``.
    """
    from esphome_device_builder.controllers.devices import _redact_concealed_secrets

    # Build the literal four-character form by hand to avoid Python
    # interpreting ``\033`` as the octal escape for ESC.
    backslash = "\\"
    literal = f"    - ssid: {backslash}033[8mrocketiot{backslash}033[28m"
    assert _redact_concealed_secrets(literal) == "    - ssid: <removed>"


def test_redact_concealed_secrets_handles_multiple_runs_per_line() -> None:
    """Multiple wrapped runs in one line all get redacted (non-greedy)."""
    from esphome_device_builder.controllers.devices import _redact_concealed_secrets

    raw = "ssid: \x1b[8mwifi-name\x1b[28m psk: \x1b[8msuper-secret\x1b[28m"
    assert _redact_concealed_secrets(raw) == "ssid: <removed> psk: <removed>"


def test_redact_concealed_secrets_leaves_unwrapped_lines_alone() -> None:
    r"""No Concealed wrapper → line is forwarded verbatim.

    Validate output is mostly schema dumps and ANSI colour codes
    that aren't conceal — a too-aggressive replacer would garble
    every line. The pattern is anchored on ``\\x1b[8m...\\x1b[28m``
    so unrelated SGR runs (colours, bold, dim) pass through.
    """
    from esphome_device_builder.controllers.devices import _redact_concealed_secrets

    raw = "INFO Reading configuration kitchen.yaml..."
    assert _redact_concealed_secrets(raw) == raw

    coloured = "\x1b[32mINFO\x1b[0m starting up"
    assert _redact_concealed_secrets(coloured) == coloured
