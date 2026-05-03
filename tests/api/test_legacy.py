"""End-to-end coverage for the legacy HA-compat endpoints.

``api/legacy.py`` exists for backward compatibility with HA's
``esphome-dashboard-api`` library, which still talks to the old
Tornado-based ``esphome dashboard``. These tests pin every
public surface of the four routes against the upstream contract
so ``esphome-dashboard-api`` keeps working unchanged when it
points at our backend.

Routes covered:

- ``GET /devices`` — configured + importable lists, filtered by
  ``ignored_devices``. Upstream:
  ``ListDevicesHandler`` /
  ``build_device_list_response``.
- ``GET /json-config`` — parsed YAML config as JSON. Upstream:
  ``JsonConfigRequestHandler``. **Note**: upstream spawns
  ``esphome config`` for substitution / package resolution
  and returns 422 with stderr on parse failure / 404 on
  missing file. Our impl loads raw YAML directly and surfaces
  parse errors as 500. The deviation is documented in the
  tests below — pin our actual contract so a future refactor
  to match upstream surfaces.
- ``GET /compile`` and ``GET /upload`` — WebSocket spawn
  protocol. Frame shapes match upstream verbatim:
  ``{event: "line", data: <text>}`` per output chunk,
  ``{event: "exit", code: <int>}`` on subprocess exit.
- Validation rejection on traversal — ``{event: "exit", code: 1}``
  for the spawn endpoints (matching the only signalling
  channel HA's library knows), 403 for ``/json-config``.

Drives every test through the real aiohttp app so the route
table, middleware ordering, and JSON serialisation are all
exercised — not just the handler bodies.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import legacy
from esphome_device_builder.api.legacy import create_legacy_routes
from esphome_device_builder.controllers.config import DashboardSettings


def _make_app(
    tmp_path: Path,
    *,
    devices: object | None = None,
) -> web.Application:
    """Build an aiohttp app wired to just enough DeviceBuilder shape.

    ``devices`` is the ``DevicesController``-shaped namespace the
    ``/devices`` route reads through — pass an object exposing
    ``_request_scan`` (async no-op), ``get_devices`` (returns a
    list), ``import_result`` (dict), and ``ignored_devices``
    (set). Tests that don't hit ``/devices`` can leave it
    ``None``.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    db_attrs: dict[str, Any] = {"settings": settings}
    if devices is not None:
        db_attrs["devices"] = devices

    app = web.Application()
    app["device_builder"] = type("DB", (), db_attrs)()
    app.add_routes(create_legacy_routes())
    return app


# ---------------------------------------------------------------------------
# /devices
# ---------------------------------------------------------------------------


class _StubDevicesController:
    """Just enough of ``DevicesController`` for the ``/devices`` route.

    Mirrors the four attributes the route reads:
    ``_request_scan`` (async), ``get_devices``,
    ``import_result``, ``ignored_devices``. Bypasses every other
    side-effect (mDNS, scanner, etc.).
    """

    def __init__(
        self,
        *,
        configured: list[Any] | None = None,
        importable: dict[str, Any] | None = None,
        ignored: set[str] | None = None,
    ) -> None:
        self._configured = configured or []
        self.import_result = importable or {}
        self.ignored_devices = ignored or set()
        self._scan_calls = 0

    async def _request_scan(self) -> None:
        self._scan_calls += 1

    def get_devices(self) -> list[Any]:
        return list(self._configured)


class _StubDevice:
    """Minimal ``Device`` stand-in — only ``to_dict`` is read."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


async def test_devices_returns_configured_and_importable_lists(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Happy path: ``{configured: [...], importable: [...]}`` shape.

    Pin the upstream-compatible response shape — HA's
    ``esphome-dashboard-api`` expects exactly these two keys at
    the top level. A renaming refactor (e.g. ``configured`` →
    ``devices``) would silently break the integration.
    """
    devices = _StubDevicesController(
        configured=[_StubDevice({"name": "kitchen", "configuration": "kitchen.yaml"})],
        importable={
            "garage": _StubDevice(
                {"name": "garage", "package_import_url": "github://owner/repo/garage.yaml"}
            ),
        },
    )
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    resp = await client.get("/devices")

    assert resp.status == 200
    body = await resp.json()
    assert set(body.keys()) == {"configured", "importable"}
    assert body["configured"] == [{"name": "kitchen", "configuration": "kitchen.yaml"}]
    assert body["importable"] == [
        {"name": "garage", "package_import_url": "github://owner/repo/garage.yaml"}
    ]


async def test_devices_filters_ignored_importable_entries(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Importable devices in ``ignored_devices`` are dropped from the response.

    User explicitly said "no thanks" to a discovered importable
    device; surfacing it again on every ``/devices`` poll would
    re-prompt them. Pin the filter so a refactor that drops the
    ``not in ignored_devices`` check resurfaces the regression
    here.
    """
    devices = _StubDevicesController(
        importable={
            "kitchen": _StubDevice({"name": "kitchen"}),
            "garage": _StubDevice({"name": "garage"}),
        },
        ignored={"garage"},
    )
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    body = await (await client.get("/devices")).json()

    importable_names = [d["name"] for d in body["importable"]]
    assert importable_names == ["kitchen"]


async def test_devices_returns_empty_lists_on_cold_start(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Cold-start (no configured, no importable) → ``{configured: [], importable: []}``.

    First-run UX: ``/devices`` must surface a clean empty state
    rather than 404 or 500. The upstream contract is "always 200
    with the two keys present".
    """
    devices = _StubDevicesController()
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    body = await (await client.get("/devices")).json()

    assert body == {"configured": [], "importable": []}


async def test_devices_triggers_scan_on_request(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Each ``GET /devices`` call awakens a fresh file scan.

    The route's first action is ``await
    devices_ctrl._request_scan()`` — without this, a freshly-
    added YAML on disk wouldn't show up until the next
    background scan tick (up to 60s on the file-poll cadence).
    HA's sync-after-edit pattern relies on this.
    """
    devices = _StubDevicesController()
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    await client.get("/devices")

    assert devices._scan_calls == 1


# ---------------------------------------------------------------------------
# /json-config
# ---------------------------------------------------------------------------


async def test_json_config_returns_parsed_yaml(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Happy path: valid YAML → JSON-serialised dict response.

    HA reads device metadata (esphome.name, esphome.platform,
    etc.) off this endpoint to populate the integration UI.
    """
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\n  platform: ESP32\n",
        encoding="utf-8",
    )
    client = await aiohttp_client(_make_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "kitchen.yaml"})

    assert resp.status == 200
    body = await resp.json()
    assert body["esphome"]["name"] == "kitchen"
    assert body["esphome"]["platform"] == "ESP32"


@pytest.mark.parametrize(
    "payload",
    ["../etc/passwd", "../../etc/passwd", "/absolute/path"],
)
async def test_json_config_rejects_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient, payload: str
) -> None:
    """``GET /json-config`` returns 403 on traversal-shaped configuration.

    The boundary check via ``rel_path`` is what gates this.
    Note: upstream returns 404 (file not found) for this case
    because it doesn't have an explicit traversal rejection; our
    explicit 403 is a hardening over upstream that HA's library
    treats the same way it treats 404 (request failed, retry).
    """
    client = await aiohttp_client(_make_app(tmp_path))
    resp = await client.get("/json-config", params={"configuration": payload})
    assert resp.status == 403
    body = await resp.json()
    assert body == {"error": "Forbidden"}


async def test_json_config_returns_500_on_yaml_parse_failure(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Malformed YAML → 500 with the parser's error message in the body.

    Note: upstream returns 422 with the ``esphome config``
    subprocess's stderr (which carries a richer error message
    because it's run through the substitution / package
    resolver). Our impl loads raw YAML directly and surfaces
    the parser exception verbatim — pin the 500 contract so a
    refactor toward the upstream "422 + spawn" shape surfaces
    here.
    """
    (tmp_path / "broken.yaml").write_text(
        "esphome:\n  name: kitchen\n  invalid: [unclosed list\n",
        encoding="utf-8",
    )
    client = await aiohttp_client(_make_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "broken.yaml"})

    assert resp.status == 500
    body = await resp.json()
    assert "error" in body


async def test_json_config_returns_500_on_missing_file(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A missing YAML file surfaces as 500 (not 404).

    Upstream returns 404 in this case; our impl lets the YAML
    loader's ``FileNotFoundError`` bubble through the
    ``except Exception`` and lands as 500. Documented as a
    deviation — HA's library treats both as "request failed,
    retry".
    """
    client = await aiohttp_client(_make_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "ghost.yaml"})

    assert resp.status == 500


# ---------------------------------------------------------------------------
# /compile and /upload — spawn protocol
# ---------------------------------------------------------------------------
#
# Upstream's spawn protocol is the WebSocket message contract HA's
# ``esphome-dashboard-api`` reads:
#
#   client → server: {"type": "spawn", "configuration": "kitchen.yaml", "port": "..."}
#   server → client: {"event": "line", "data": "<utf-8 chunk>"}  (per stdout chunk)
#   server → client: {"event": "exit", "code": <int>}  (when subprocess exits)
#
# These tests use a fake ``create_subprocess_exec`` so the spawned
# command is a controlled in-memory generator rather than a real
# ``esphome`` invocation. That lets us pin the cmd shape, the
# frame ordering, and the exit code passthrough without depending
# on a working ESPHome install.


class _FakeStdout:
    """Async-iterable stdout stream that yields preset bytes."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self._iter = iter(self._lines)

    async def read(self, _n: int = -1) -> bytes:
        try:
            return next(self._iter)
        except StopIteration:
            return b""

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration  # noqa: B904


class _FakeProc:
    """Minimal ``asyncio.subprocess.Process`` stand-in."""

    def __init__(self, lines: list[bytes], exit_code: int) -> None:
        self.stdout = _FakeStdout(lines)
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


@pytest.fixture
def captured_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``create_subprocess_exec`` with a recording fake.

    Tests assert on ``captured["cmd"]`` to verify the command
    shape (this is the upstream-parity check). The default fake
    yields two output lines and exits 0; a test that needs
    different behaviour mutates ``captured["lines"]`` /
    ``captured["exit_code"]`` *before* opening the WS — the
    fake reads them at spawn time, not at fixture-build time.
    """
    captured: dict[str, Any] = {
        "cmd": None,
        "lines": [b"line one\n", b"line two\n"],
        "exit_code": 0,
    }

    async def _fake_create(*args: Any, **_kwargs: Any) -> _FakeProc:
        captured["cmd"] = list(args)
        return _FakeProc(captured["lines"], captured["exit_code"])

    monkeypatch.setattr("esphome_device_builder.api.legacy.create_subprocess_exec", _fake_create)
    return captured


async def test_compile_ws_streams_lines_and_exit_frames(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """``/compile`` emits ``{event: line}`` per output chunk + ``{event: exit, code}``.

    Pinning the upstream frame shape verbatim: the tornado
    dashboard sends ``{"event": "line", "data": <utf-8 string>}``
    per stdout chunk and ``{"event": "exit", "code": <int>}``
    on subprocess exit. HA's ``esphome-dashboard-api`` reads
    those exact keys.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    captured_spawn["lines"] = [b"compile output line 1\n", b"compile output line 2\n"]
    captured_spawn["exit_code"] = 0

    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        first = await ws.receive_json()
        second = await ws.receive_json()
        third = await ws.receive_json()

    assert first == {"event": "line", "data": "compile output line 1\n"}
    assert second == {"event": "line", "data": "compile output line 2\n"}
    assert third == {"event": "exit", "code": 0}


async def test_compile_ws_passes_exit_code_through(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """Non-zero subprocess exit → ``{event: exit, code: N}``.

    HA renders the compile result based on this code (0 = green
    check, anything else = red X). Pin that the code is passed
    through verbatim, not coerced or normalised.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    captured_spawn["lines"] = []
    captured_spawn["exit_code"] = 42

    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 42}


async def test_compile_ws_builds_command_without_device_arg(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """``compile`` doesn't pass ``--device`` (it's not flashing anything).

    Upstream ``EsphomeCompileHandler.build_command`` constructs
    ``[*ESPHOME_COMMAND, "compile", config_file]`` — no
    ``--device``. Pin the same shape so a refactor that adds a
    spurious port flag doesn't break ``esphome compile``.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        await ws.receive_json()  # line
        await ws.receive_json()  # line
        await ws.receive_json()  # exit

    cmd = captured_spawn["cmd"]
    assert "--device" not in cmd
    assert "compile" in cmd
    # Compile target is the resolved YAML path.
    assert any(arg.endswith("kitchen.yaml") for arg in cmd)


async def test_upload_ws_includes_device_arg_when_port_set(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """``upload`` with ``port`` → ``--device <port>`` appended.

    Matches upstream's ``EsphomePortCommandWebSocket.build_device_command``
    which always emits ``["--device", port]``. The port travels
    verbatim — serial path, IP, ``OTA`` literal — esphome's
    ``--device`` resolver handles dispatch.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    async with client.ws_connect("/upload") as ws:
        await ws.send_json(
            {"type": "spawn", "configuration": "kitchen.yaml", "port": "/dev/ttyUSB0"}
        )
        await ws.receive_json()
        await ws.receive_json()
        await ws.receive_json()

    cmd = captured_spawn["cmd"]
    assert "upload" in cmd
    assert "--device" in cmd
    assert cmd[cmd.index("--device") + 1] == "/dev/ttyUSB0"


async def test_upload_ws_omits_device_arg_when_port_empty(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """``upload`` with empty / missing ``port`` → no ``--device`` flag.

    Deviation from upstream: their dashboard requires ``port``
    in the message and unconditionally appends ``--device port``.
    Our impl skips the flag when port is empty, letting esphome
    auto-detect. HA's library always sends a port, so the
    practical contract isn't observable from the integration —
    but pinning our actual shape protects against a refactor
    that flips this branch silently.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    async with client.ws_connect("/upload") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        await ws.receive_json()
        await ws.receive_json()
        await ws.receive_json()

    cmd = captured_spawn["cmd"]
    assert "upload" in cmd
    assert "--device" not in cmd


# ---------------------------------------------------------------------------
# /compile and /upload — input-shape edge cases
# ---------------------------------------------------------------------------


async def test_spawn_ws_emits_exit_frame_on_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A traversal-shaped configuration trips the boundary, emits exit:1.

    The legacy spawn protocol's only signalling channel is the
    exit frame, so a controlled rejection masquerades as
    "subprocess exited with code 1". Without this, the
    ``CommandError`` from ``rel_path`` would bubble through
    aiohttp and tear the WebSocket down — HA's library would
    surface a connection drop instead of a clean reject.
    """
    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "../etc/passwd"})
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 1}


async def test_spawn_ws_skips_non_json_frame(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """Non-JSON text frames are ignored; the next valid spawn still works.

    Defensive against a buggy / pre-handshake client that
    accidentally sends non-JSON. The handler logs and
    continues iterating; the next valid spawn still gets
    processed. Pin so a refactor that closes the WS on the
    first decode error breaks loudly here.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    async with client.ws_connect("/compile") as ws:
        await ws.send_str("not-json-at-all")
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        await ws.receive_json()
        await ws.receive_json()
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 0}


async def test_spawn_ws_skips_non_spawn_message_type(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    captured_spawn: dict[str, Any],
) -> None:
    """Messages with ``type != "spawn"`` are skipped, not handled.

    The legacy protocol defines only ``spawn`` for the
    compile/upload routes. Upstream defines other types
    (``stdin``) for interactive logs but compile/upload don't
    consume them. Our impl silently drops anything that isn't
    ``spawn``; pin so a follow-up that adds support doesn't
    accidentally break the silent-skip behaviour.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "stdin", "data": "ignored"})
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        await ws.receive_json()
        await ws.receive_json()
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 0}


async def test_spawn_ws_breaks_on_non_text_frame(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A binary frame breaks out of the message loop without spawning anything.

    Defensive: a buggy client sending binary frames over a
    text-only protocol shouldn't trigger ``loads`` on a non-text
    payload (which would raise a confusing decode error). The
    handler bails immediately so the next request opens a clean
    connection. Pin the break so a regression that fell through
    to ``loads(msg.data)`` would surface here as a malformed
    error frame instead of a clean break.
    """
    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.send_bytes(b"not-text")
    # No exit frame, no error — the handler returns the empty WS
    # response on the break and the close handshake completes.


async def test_spawn_ws_breaks_on_close(tmp_path: Path, aiohttp_client: AiohttpClient) -> None:
    """Client closing without sending spawn → handler exits cleanly.

    No subprocess spawned, no exit frame, no error. The ``async
    for msg in ws`` loop terminates on the close frame and the
    handler returns the (empty) WS response.
    """
    client = await aiohttp_client(_make_app(tmp_path))
    async with client.ws_connect("/compile") as ws:
        await ws.close()
    # Closing without raising is the assertion.


async def test_spawn_ws_real_subprocess_streams_output(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Real subprocess (no mock) → output streams through, exit code passes.

    True end-to-end: drives the actual ``create_subprocess_exec``
    code path against a real subprocess. Catches a regression
    where a refactor of the streaming loop or the helper itself
    breaks the line iteration. Uses ``sys.executable -c '<one-
    liner>'`` so the test doesn't depend on having ``esphome``
    installed and stays portable across platforms (Windows
    ``cmd`` / POSIX shells differ on quoting; a Python one-liner
    runs identically everywhere).

    Replaces ``_ESPHOME_CMD`` for the duration of the test so
    the spawn shape becomes ``[python, -c, "<script>", ...]``
    instead of ``[python, -m, esphome, ...]``.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    client = await aiohttp_client(_make_app(tmp_path))

    # Replace the esphome command with a Python one-liner that
    # prints two lines and exits 0. Using ``compile`` as the
    # command so the existing route mapping doesn't change.
    original_cmd = legacy._ESPHOME_CMD
    try:
        legacy._ESPHOME_CMD = [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('world'); sys.exit(0)",
        ]
        async with client.ws_connect("/compile") as ws:
            # Argument after the python -c is the subcommand name
            # ('compile') and then the resolved config path —
            # both ignored by the script.
            await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
            received: list[dict[str, Any]] = []
            try:
                async with asyncio.timeout(5.0):
                    while True:
                        msg = await ws.receive_json()
                        received.append(msg)
                        if msg.get("event") == "exit":
                            break
            except TimeoutError:  # pragma: no cover — hangs are bugs
                pytest.fail("never received exit frame")
    finally:
        legacy._ESPHOME_CMD = original_cmd

    line_data = "".join(msg["data"] for msg in received if msg.get("event") == "line")
    assert "hello" in line_data
    assert "world" in line_data
    assert received[-1] == {"event": "exit", "code": 0}


# ---------------------------------------------------------------------------
# Frame-shape parity with upstream
# ---------------------------------------------------------------------------


def test_legacy_module_exposes_only_documented_routes() -> None:
    """The legacy module registers exactly the four routes HA's library knows.

    Upstream registers the same four:
    ``ListDevicesHandler`` (``/devices``),
    ``JsonConfigRequestHandler`` (``/json-config``),
    ``EsphomeCompileHandler`` (``/compile``),
    ``EsphomeUploadHandler`` (``/upload``). Pin the route set so
    a refactor that adds an undocumented route (or drops one of
    the four) shows up here — the legacy module's purpose is
    drift-with-upstream parity, not a place to add new routes.
    """
    routes = create_legacy_routes()
    paths = {
        route.path  # type: ignore[union-attr]
        for route in routes
    }
    assert paths == {"/devices", "/json-config", "/compile", "/upload"}
