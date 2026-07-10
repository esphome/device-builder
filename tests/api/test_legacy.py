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
- ``GET /json-config`` — fully-resolved config as JSON. Like
  upstream's ``JsonConfigRequestHandler``, it spawns ``esphome
  config --show-secrets`` (via ``run_esphome_config``) so
  substitutions / packages / includes / secrets all resolve —
  the data HA's encryption-key detection needs. Returns 404 on
  a missing file, 422 on an invalid config, 403 on a traversal
  (our hardening over upstream's 404).
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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from pytest_aiohttp.plugin import AiohttpClient

from esphome_device_builder.api import legacy
from esphome_device_builder.api.legacy import create_legacy_routes
from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    Device,
    DeviceRuntimeState,
    DeviceState,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _make_app(
    tmp_path: Path,
    *,
    devices: object | None = None,
    firmware: object | None = None,
    bus: EventBus | None = None,
) -> web.Application:
    """Build an aiohttp app wired to just enough DeviceBuilder shape.

    ``devices`` is the ``DevicesController`` instance the
    ``/devices`` route reads through. Tests build it via
    :func:`_make_devices_mock`, which is spec'd against the real
    ``DevicesController`` class so a method rename (or a
    misspelt caller — the failure mode that motivated #376)
    surfaces as the same ``AttributeError`` in CI that production
    raised. Tests that don't hit ``/devices`` can leave it
    ``None``.

    ``firmware`` + ``bus`` wire up the WS spawn handlers (see
    :class:`_FakeFirmwareController`). Tests that don't open
    ``/compile`` or ``/upload`` can leave them ``None``.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    db_attrs: dict[str, Any] = {"settings": settings}
    if devices is not None:
        db_attrs["devices"] = devices
    if firmware is not None:
        db_attrs["firmware"] = firmware
    if bus is not None:
        db_attrs["bus"] = bus

    app = web.Application()
    app["device_builder"] = type("DB", (), db_attrs)()
    app.add_routes(create_legacy_routes())
    return app


# ---------------------------------------------------------------------------
# /devices
# ---------------------------------------------------------------------------


def _make_devices_mock(
    *,
    configured: list[Any] | None = None,
    importable: dict[str, Any] | None = None,
    ignored: set[str] | None = None,
) -> MagicMock:
    """Build a ``DevicesController`` mock spec'd against the real class.

    ``MagicMock(spec=DevicesController)`` makes attribute *access*
    on the mock honour the spec class — reading a name that
    isn't on ``DevicesController`` raises ``AttributeError``.
    That's the end-to-end-shape guarantee #376 was missing: the
    previous hand-rolled stub exposed an ``_request_scan``
    method that didn't exist on the real ``DevicesController``
    (renamed to ``poll`` in the controller-split refactor), so
    the legacy route's broken call site passed CI but crashed
    in production. Speccing forces the test fakes to track the
    real method surface — any future caller invoking a method
    that isn't on ``DevicesController`` raises here exactly the
    same way it raises in the running app.

    Caveat: ``spec=`` constrains read-side access to names on
    the spec class, but does NOT block setting attributes on
    the mock — including names that aren't on the class at all
    (instance-only attributes set in ``__init__``, for example,
    aren't visible to ``spec`` introspection). That's why the
    setattrs below for ``import_result`` / ``ignored_devices``
    succeed: ``spec=`` doesn't enforce instance-attribute
    correctness, just method-name access. ``spec_set=`` would
    block setattrs but would also reject these instance attrs
    because they live on the controller instance, not the
    class. The valuable guarantee here is the method-name
    access check; the data attributes are mocked freely.

    ``poll`` is set to an explicit ``AsyncMock()`` so individual
    tests can ``assert_awaited_once`` against it without
    depending on whether the spec'd-mock auto-detects async
    methods on its own.
    """
    devices = MagicMock(spec=DevicesController)
    devices.poll = AsyncMock()
    devices.get_devices = MagicMock(return_value=list(configured or []))
    # ``spec=DevicesController`` doesn't auto-include attributes
    # set in ``__init__``; ``state`` is one of those, so attach a
    # bare ``MagicMock`` and populate the dict/set fields the
    # legacy ``/devices`` route reads off it.
    devices.state = MagicMock()
    devices.state.import_result = importable or {}
    devices.state.ignored_devices = ignored or set()
    return devices


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
    devices = _make_devices_mock(
        configured=[
            _StubDevice({"name": "kitchen", "configuration": "kitchen.yaml", "runtime_state": {}})
        ],
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


async def test_devices_matches_ha_client_contract(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``configured`` entries carry every key HA's client library declares.

    Pins the ``esphome-dashboard-api`` ``ConfiguredDevice`` TypedDict
    surface (except ``path``, a known pre-existing absence) so a model
    reshape can't silently break the HA integration again.
    """
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    devices = _make_devices_mock(configured=[device])
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    resp = await client.get("/devices")
    payload = await resp.json()

    [entry] = payload["configured"]
    ha_client_keys = {
        "address",
        "comment",
        "configuration",
        "current_version",
        "deployed_version",
        "loaded_integrations",
        "name",
        "target_platform",
        "web_port",
    }
    assert ha_client_keys <= entry.keys()


async def test_devices_serializes_runtime_state_flat(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """``configured`` entries carry the runtime fields flat, never nested.

    HA's ``esphome-dashboard-api`` ``ConfiguredDevice`` TypedDict (and
    HA core diagnostics) read ``deployed_version`` at the top level, so
    the legacy wire shape must stay flat even though ``Device`` nests
    these under ``runtime_state``.
    """
    device = Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        address="kitchen.local",
        current_version="2026.6.0",
        runtime_state=DeviceRuntimeState(
            state=DeviceState.ONLINE,
            deployed_version="2026.5.0",
            deployed_config_hash="deadbeef",
            ip_addresses=["192.168.1.5"],
        ),
    )
    devices = _make_devices_mock(configured=[device])
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    body = await (await client.get("/devices")).json()

    (entry,) = body["configured"]
    assert "runtime_state" not in entry
    assert entry["deployed_version"] == "2026.5.0"
    assert entry["deployed_config_hash"] == "deadbeef"
    assert entry["state"] == "online"
    assert entry["active_source"] == "unknown"
    assert entry["ip_addresses"] == ["192.168.1.5"]
    assert entry["queued_update"] is False
    assert entry["api_encryption_active"] is None
    assert entry["current_version"] == "2026.6.0"


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
    devices = _make_devices_mock(
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


async def test_devices_filters_already_configured_importable_entries(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """An importable entry whose name is already configured is dropped.

    A stale ``import_result`` row (YAML dropped on disk out-of-band,
    or the scan-vs-callback race) must not surface a device that the
    ``configured`` list already carries — otherwise HA offers to
    import an already-configured device. Mirrors the WS surface's
    ``not in configured_names`` guard.
    """
    devices = _make_devices_mock(
        configured=[
            _StubDevice({"name": "kitchen", "configuration": "kitchen.yaml", "runtime_state": {}})
        ],
        importable={
            "kitchen": _StubDevice({"name": "kitchen"}),
            "garage": _StubDevice({"name": "garage"}),
        },
    )
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    body = await (await client.get("/devices")).json()

    importable_names = [d["name"] for d in body["importable"]]
    assert importable_names == ["garage"]


async def test_devices_returns_empty_lists_on_cold_start(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Cold-start (no configured, no importable) → ``{configured: [], importable: []}``.

    First-run UX: ``/devices`` must surface a clean empty state
    rather than 404 or 500. The upstream contract is "always 200
    with the two keys present".
    """
    devices = _make_devices_mock()
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    body = await (await client.get("/devices")).json()

    assert body == {"configured": [], "importable": []}


async def test_devices_triggers_scan_on_request(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Each ``GET /devices`` call awakens a fresh file scan.

    The route's first action is ``await devices_ctrl.poll()`` —
    without this, a freshly-added YAML on disk wouldn't show up
    until the next background scan tick (up to 5 s on the
    file-poll cadence). HA's sync-after-edit pattern relies on
    this.

    Asserts via ``poll.assert_awaited_once`` so a regression that
    silently swaps the call site to a method that *also* exists
    on ``DevicesController`` (rather than crashing outright)
    still fails — the issue isn't just "doesn't crash", it's
    "actually triggers the scan path".
    """
    devices = _make_devices_mock()
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    await client.get("/devices")

    devices.poll.assert_awaited_once_with()


async def test_devices_route_call_chain_matches_real_controller(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """The legacy /devices route only calls methods that exist on ``DevicesController``.

    Regression test for #376: a controller-split refactor renamed
    ``_request_scan`` to ``poll`` but missed the legacy route's
    caller, so production crashed with ``AttributeError`` while
    tests (which used a duck-typed stub matching the broken
    name) stayed green. ``MagicMock(spec=DevicesController)``
    enforces the real surface — calling any non-existent method
    on the mock raises ``AttributeError`` exactly as the running
    backend does.

    Driven through the real aiohttp app + middleware so the call
    chain is exercised end-to-end (route → handler → controller
    method lookup), not just the handler body.
    """
    devices = _make_devices_mock(
        configured=[_StubDevice({"name": "kitchen", "runtime_state": {}})],
    )
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    resp = await client.get("/devices")

    # Status 200 means no AttributeError surfaced from the
    # controller method lookup. (A spec'd-mock failure on a
    # missing method becomes a 500 with the AttributeError in
    # the response body — both 4xx/5xx would fail this.)
    assert resp.status == 200


# ---------------------------------------------------------------------------
# /ping
# ---------------------------------------------------------------------------


class _StubStateDevice:
    """Minimal ``Device`` stand-in exposing ``configuration`` + ``runtime_state.state``."""

    def __init__(self, configuration: str, state: DeviceState) -> None:
        self.configuration = configuration
        self.runtime_state = DeviceRuntimeState(state=state)


async def test_ping_returns_tristate_online_map(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """Tri-state ``/ping`` map: ONLINE->true, OFFLINE->false, UNKNOWN->null, keyed by filename."""
    devices = _make_devices_mock(
        configured=[
            _StubStateDevice("kitchen.yaml", DeviceState.ONLINE),
            _StubStateDevice("garage.yaml", DeviceState.OFFLINE),
            _StubStateDevice("attic.yaml", DeviceState.UNKNOWN),
        ],
    )
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    resp = await client.get("/ping")

    assert resp.status == 200
    assert await resp.json() == {
        "kitchen.yaml": True,
        "garage.yaml": False,
        "attic.yaml": None,
    }


async def test_ping_triggers_poll(tmp_path: Path, aiohttp_client: AiohttpClient) -> None:
    """``/ping`` refreshes the scanner before reading, like ``/devices``."""
    devices = _make_devices_mock()
    client = await aiohttp_client(_make_app(tmp_path, devices=devices))

    resp = await client.get("/ping")

    assert resp.status == 200
    assert await resp.json() == {}
    devices.poll.assert_awaited_once()


# ---------------------------------------------------------------------------
# /json-config
# ---------------------------------------------------------------------------


def _make_json_config_app(
    tmp_path: Path, *, esphome_cmd: list[str] | None = None
) -> web.Application:
    """``_make_app`` plus a devices stub carrying ``state.esphome_cmd``.

    ``legacy_json_config`` reads ``db.devices.state.esphome_cmd`` to spawn
    ``esphome config``; the resolution itself is patched per-test via
    ``legacy.run_esphome_config``.
    """
    devices = MagicMock()
    devices.state.esphome_cmd = ["esphome"] if esphome_cmd is None else esphome_cmd
    return _make_app(tmp_path, devices=devices)


async def test_json_config_returns_resolved_config(
    tmp_path: Path, aiohttp_client: AiohttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: ``esphome config`` output → JSON-serialised dict response.

    HA reads device metadata + the resolved ``api.encryption.key`` off this
    endpoint; the resolved config is what carries it (substitutions expanded,
    packages merged).
    """
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    resolved = {"esphome": {"name": "kitchen", "platform": "ESP32"}, "sensor": [{"delta": 0.1}]}
    monkeypatch.setattr(legacy, "run_esphome_config", AsyncMock(return_value=resolved))
    client = await aiohttp_client(_make_json_config_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "kitchen.yaml"})

    assert resp.status == 200
    body = await resp.json()
    assert body["esphome"]["name"] == "kitchen"
    assert body["esphome"]["platform"] == "ESP32"
    assert body["sensor"][0]["delta"] == 0.1


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


async def test_json_config_returns_422_on_invalid_config(
    tmp_path: Path, aiohttp_client: AiohttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``esphome config`` failure (``None``) surfaces as a generic 422.

    The body is generic; an invalid config's error can carry resolved
    secrets, so the helper's detail never reaches the response.
    """
    (tmp_path / "broken.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    monkeypatch.setattr(legacy, "run_esphome_config", AsyncMock(return_value=None))
    client = await aiohttp_client(_make_json_config_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "broken.yaml"})

    assert resp.status == 422
    body = await resp.json()
    # Pin the generic body: a future refactor that echoes esphome's stderr
    # (which carries resolved secrets under --show-secrets) must fail here.
    assert body == {"error": "Configuration is invalid"}


async def test_json_config_returns_503_on_infra_fault(
    tmp_path: Path, aiohttp_client: AiohttpClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An infra fault (spawn fail / timeout) is a retryable 503, not a 422.

    HA must keep retrying a config that would resolve once esphome is reachable,
    rather than treating a transient backend fault as a permanent config error.
    """
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    monkeypatch.setattr(
        legacy,
        "run_esphome_config",
        AsyncMock(side_effect=legacy.EsphomeConfigUnavailableError("timed out")),
    )
    client = await aiohttp_client(_make_json_config_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "kitchen.yaml"})

    assert resp.status == 503


async def test_json_config_returns_404_on_missing_file(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A missing YAML file surfaces as 404 before any subprocess spawn."""
    client = await aiohttp_client(_make_json_config_app(tmp_path))

    resp = await client.get("/json-config", params={"configuration": "ghost.yaml"})

    assert resp.status == 404


async def test_json_config_returns_500_when_esphome_unavailable(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """No resolved ``esphome`` binary (empty ``esphome_cmd``) → 500."""
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    client = await aiohttp_client(_make_json_config_app(tmp_path, esphome_cmd=[]))

    resp = await client.get("/json-config", params={"configuration": "kitchen.yaml"})

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
# Since #394 the handler routes through the ``FirmwareController``
# job queue (so HA-triggered builds show up in the "Firmware tasks"
# panel) instead of spawning a subprocess directly. These tests
# inject a fake firmware controller that records the submitted job
# and a real ``EventBus`` the test drives by firing
# ``JOB_OUTPUT`` / ``JOB_*`` lifecycle events. That exercises the
# legacy handler's translation between the bus event shape and
# the upstream WS frame shape end-to-end without depending on the
# ``FirmwareController`` itself running an actual build.


class _FakeFirmwareController:
    """Minimal stand-in for ``FirmwareController.compile`` / ``.upload``.

    Records the kwargs each method was called with and returns a
    ``FirmwareJob`` the test can then drive by configuring
    *plan*: a list of ``("line", "<text>")`` and one final
    ``("exit", <code>, <status>)`` tuple that the fake will fire
    on the bus *after* the legacy handler has subscribed. Firing
    from inside the post-yield deferral (rather than from the
    test body) is what avoids the race where the test driver's
    events arrive before the handler attaches its listener — see
    ``_schedule_plan`` for the timing detail.

    Optionally raises ``CommandError`` to simulate the boundary-
    validation rejection path.

    *Not* spec'd against the real ``FirmwareController`` (unlike
    ``_make_devices_mock`` for the devices controller). The reason
    is that ``FirmwareController.compile`` / ``.upload`` are
    decorated with ``@api_command`` and live on a class with a
    deep import surface (``esphome.components.esp32``,
    ``esphome.storage_json``, …) that's painful to drag into the
    legacy unit-test file. The trade-off is that a rename of
    ``compile`` → ``compile_job`` (etc.) wouldn't surface here —
    if such a rename ever lands, the integration-style end-to-end
    test in ``test_spawn_ws_round_trip_through_real_event_bus``
    catches it the same way #376's regression test catches the
    devices-controller surface.
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        job: FirmwareJob,
        plan: list[tuple] | None = None,
        raise_on_submit: bool = False,
    ) -> None:
        self._bus = bus
        self._job = job
        self._plan = plan or []
        self._raise_on_submit = raise_on_submit
        self.compile_calls: list[dict[str, Any]] = []
        self.upload_calls: list[dict[str, Any]] = []
        self.fire_task: asyncio.Task | None = None

    async def compile(self, *, configuration: str, **kwargs: Any) -> FirmwareJob:
        self.compile_calls.append({"configuration": configuration, **kwargs})
        if self._raise_on_submit:
            raise CommandError(ErrorCode.INVALID_ARGS, "rejected")
        self._schedule_plan()
        return self._job

    async def upload(self, *, configuration: str, port: str = "", **kwargs: Any) -> FirmwareJob:
        self.upload_calls.append({"configuration": configuration, "port": port, **kwargs})
        if self._raise_on_submit:
            raise CommandError(ErrorCode.INVALID_ARGS, "rejected")
        self._schedule_plan()
        return self._job

    def _schedule_plan(self) -> None:
        """Schedule ``_fire_plan`` to run after the handler subscribes.

        The handler's flow after ``compile()`` returns is sync:
        snapshot ``job.output``, then route through
        ``stream_events``, which attaches the bus listener and
        awaits ``send_initial`` (the snapshot replay) before
        starting the live drain. The first ``await`` inside
        ``send_initial`` yields back to the loop, at which point
        the deferred task scheduled here runs and fires events
        into the now-attached listener.

        Without the deferral the events fire before the listener
        is attached and are dropped — exactly the behaviour the
        production firmware controller's "subscribe before
        snapshot" comment is designed to prevent in
        ``follow_job``.

        Attaches a ``done_callback`` that re-raises any
        ``_fire_plan`` exception by calling ``task.result()``.
        Without the callback, an exception inside the deferred
        task lands in pytest-asyncio's unhandled-task warning
        channel rather than failing the test loudly — making
        debugging the next person who breaks this miserable.
        """
        if not self._plan:
            return
        loop = asyncio.get_running_loop()
        self.fire_task = loop.create_task(self._fire_plan())
        # ``task.result()`` re-raises any exception the task
        # produced; the bare access surfaces it through the
        # done-callback path so pytest sees a real failure.
        self.fire_task.add_done_callback(lambda t: t.result())

    async def _fire_plan(self) -> None:
        # One ``sleep(0)`` is enough: ``compile()`` returns
        # synchronously to the handler, which then runs through
        # ``stream_events``'s sync setup (subscribe + initial-
        # frame push) before reaching its first ``await`` on
        # ``queue.get()``. That await is the yield this task is
        # waiting on.
        await asyncio.sleep(0)
        for entry in self._plan:
            kind = entry[0]
            if kind == "line":
                self._bus.fire(
                    EventType.JOB_OUTPUT,
                    {"job_id": self._job.job_id, "line": entry[1]},
                )
                continue
            # ("exit", code_or_None, status)
            _, exit_code, status = entry
            self._job.status = status
            self._job.exit_code = exit_code
            event_type = {
                JobStatus.COMPLETED: EventType.JOB_COMPLETED,
                JobStatus.FAILED: EventType.JOB_FAILED,
                JobStatus.CANCELLED: EventType.JOB_CANCELLED,
            }[status]
            self._bus.fire(event_type, {"job": self._job})


def _make_job(
    *,
    job_type: JobType = JobType.COMPILE,
    output: list[str] | None = None,
    status: JobStatus = JobStatus.RUNNING,
    exit_code: int | None = None,
) -> FirmwareJob:
    """Build a ``FirmwareJob`` for tests."""
    return FirmwareJob(
        job_id="test-job-id",
        configuration="kitchen.yaml",
        job_type=job_type,
        status=status,
        output=output if output is not None else [],
        exit_code=exit_code,
    )


def _plan(
    *,
    lines: list[str] | None = None,
    exit_code: int | None = 0,
    status: JobStatus = JobStatus.COMPLETED,
) -> list[tuple]:
    """Build a ``_FakeFirmwareController.plan`` payload.

    Convenience over a verbose tuple list — most tests want
    "fire these lines, then exit with this code/status".
    """
    out: list[tuple] = [("line", line) for line in (lines or [])]
    out.append(("exit", exit_code, status))
    return out


async def test_compile_ws_streams_lines_and_exit_frames(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """``/compile`` emits ``{event: line}`` per output chunk + ``{event: exit, code}``.

    Pinning the upstream frame shape verbatim: the tornado
    dashboard sends ``{"event": "line", "data": <utf-8 string>}``
    per stdout chunk and ``{"event": "exit", "code": <int>}``
    on subprocess exit. HA's ``esphome-dashboard-api`` reads
    those exact keys, regardless of whether the build runs as a
    direct subprocess (legacy) or as a queued firmware job (#394).
    """
    bus = EventBus()
    job = _make_job(job_type=JobType.COMPILE)
    firmware = _FakeFirmwareController(
        bus=bus,
        job=job,
        plan=_plan(lines=["compile output line 1\n", "compile output line 2\n"]),
    )
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

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
) -> None:
    """Non-zero firmware exit → ``{event: exit, code: N}``.

    HA renders the compile result based on this code (0 = green
    check, anything else = red X). Pin that the code is passed
    through verbatim, not coerced or normalised.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(
        bus=bus,
        job=job,
        plan=_plan(exit_code=42, status=JobStatus.FAILED),
    )
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 42}


async def test_compile_ws_submits_compile_job_with_configuration(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """``/compile`` submits a ``compile`` firmware job with the YAML name.

    Replaces the upstream "command shape" test now that the WS
    routes through the firmware queue instead of spawning a
    subprocess. The shape we care about pinning is "the legacy
    route reaches the same job-submission API the new dashboard
    uses, with the configuration travelling verbatim and no
    spurious port" — that's what makes HA-triggered builds appear
    in the "Firmware tasks" panel (#394).
    """
    bus = EventBus()
    job = _make_job(job_type=JobType.COMPILE)
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan())
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml", "port": "ignored"})
        await ws.receive_json()

    assert firmware.compile_calls == [{"configuration": "kitchen.yaml"}]
    # Compile path doesn't accept a port — the field on the
    # spawn message is silently dropped (matches the previous
    # subprocess shape that never appended ``--device`` for
    # ``compile``).
    assert firmware.upload_calls == []


async def test_upload_ws_submits_upload_job_with_port(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """``/upload`` with ``port`` → upload job carries the port verbatim.

    Pins the legacy → ``firmware.upload`` leg of the port-pass-
    through chain. The next leg — ``firmware.upload`` storing the
    port on the job and ``_build_command`` translating it into
    ``["--device", port]`` for the esphome CLI — is pinned by
    ``tests/controllers/firmware/test_address_cache.py``
    (search for ``--device``). The two together protect every
    place a serial / OTA / IP target could get dropped between
    HA's spawn message and the actual ``esphome upload`` invocation.
    """
    bus = EventBus()
    job = _make_job(job_type=JobType.UPLOAD)
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan())
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/upload") as ws:
        await ws.send_json(
            {"type": "spawn", "configuration": "kitchen.yaml", "port": "/dev/ttyUSB0"}
        )
        await ws.receive_json()

    assert firmware.upload_calls == [{"configuration": "kitchen.yaml", "port": "/dev/ttyUSB0"}]


async def test_upload_ws_submits_upload_job_with_empty_port_when_omitted(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """``/upload`` without ``port`` → upload job submitted with ``port=""``.

    Deviation from upstream: their dashboard requires ``port``
    in the message. Our impl plumbs an empty string through; the
    firmware controller forwards it to esphome which then
    auto-detects. HA's library always sends a port, so the
    practical contract isn't observable from the integration —
    but pinning our actual shape protects against a refactor
    that flips this branch silently.
    """
    bus = EventBus()
    job = _make_job(job_type=JobType.UPLOAD)
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan())
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/upload") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        await ws.receive_json()

    assert firmware.upload_calls == [{"configuration": "kitchen.yaml", "port": ""}]


async def test_compile_ws_replays_buffered_output_then_streams_live(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """Lines already in ``job.output`` at submit time are replayed first.

    Because the firmware queue is shared, ``compile()`` may
    return a job that already has buffered output (e.g. a
    superseded job that landed mid-build). Pin the contract that
    the legacy WS replays the snapshot before draining live
    events, so HA's library sees a contiguous output stream
    rather than a gap before its own subscription window.
    """
    bus = EventBus()
    job = _make_job(output=["pre-existing line 1\n", "pre-existing line 2\n"])
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan(lines=["live line\n"]))
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        # Two replayed history lines, one live, one exit.
        first = await ws.receive_json()
        second = await ws.receive_json()
        third = await ws.receive_json()
        fourth = await ws.receive_json()

    assert first == {"event": "line", "data": "pre-existing line 1\n"}
    assert second == {"event": "line", "data": "pre-existing line 2\n"}
    assert third == {"event": "line", "data": "live line\n"}
    assert fourth == {"event": "exit", "code": 0}


async def test_compile_ws_emits_exit_immediately_when_job_already_terminal(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """A job that resolved as terminal before subscribe → snapshot + exit.

    Edge case: ``compile()`` can resolve a job that's already in
    a terminal state (most commonly when a duplicate-submit
    supersede lands the previous job in CANCELLED before the
    new job is created and the queue surfaces the cached
    terminal). The legacy handler should drain the buffered
    output and send the exit frame from the job snapshot rather
    than parking on a queue that will never receive the live
    terminal event.
    """
    bus = EventBus()
    job = _make_job(output=["final line\n"], status=JobStatus.COMPLETED, exit_code=0)
    firmware = _FakeFirmwareController(bus=bus, job=job)
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        first = await ws.receive_json()
        second = await ws.receive_json()

    assert first == {"event": "line", "data": "final line\n"}
    assert second == {"event": "exit", "code": 0}


async def test_compile_ws_coerces_null_exit_code_on_cancellation(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """A cancelled job (no exit code) emits ``{event: exit, code: 1}``.

    Cancellation is the supersede path's "previous build was
    abandoned" signal; the underlying subprocess never ran to
    completion so ``exit_code`` is ``None``. The legacy frame
    requires an integer code (HA's library decodes it as a
    number), so coerce the absent code to ``1`` — same shape
    HA would have seen from the subprocess being killed.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(
        bus=bus,
        job=job,
        plan=_plan(exit_code=None, status=JobStatus.CANCELLED),
    )
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "kitchen.yaml"})
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 1}


# ---------------------------------------------------------------------------
# /compile and /upload — input-shape edge cases
# ---------------------------------------------------------------------------


async def test_spawn_ws_emits_exit_frame_on_traversal(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """A boundary-rejected submission emits ``{event: exit, code: 1}``.

    The legacy spawn protocol's only signalling channel is the
    exit frame, so a controlled rejection masquerades as
    "subprocess exited with code 1". Without this, the
    ``CommandError`` from the firmware controller's
    ``_validate_configuration_boundary`` would bubble through
    aiohttp and tear the WebSocket down — HA's library would
    surface a connection drop instead of a clean reject. The
    fake firmware controller raises ``CommandError(INVALID_ARGS)``
    from ``compile`` to simulate the real boundary rejection.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(bus=bus, job=job, raise_on_submit=True)
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
        await ws.send_json({"type": "spawn", "configuration": "../etc/passwd"})
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 1}


async def test_spawn_ws_skips_non_json_frame(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
) -> None:
    """Non-JSON text frames are ignored; the next valid spawn still works.

    Defensive against a buggy / pre-handshake client that
    accidentally sends non-JSON. The handler logs and
    continues iterating; the next valid spawn still gets
    processed. Pin so a refactor that closes the WS on the
    first decode error breaks loudly here.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan(lines=["a\n", "b\n"]))
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

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
) -> None:
    """Messages with ``type != "spawn"`` are skipped, not handled.

    The legacy protocol defines only ``spawn`` for the
    compile/upload routes. Upstream defines other types
    (``stdin``) for interactive logs but compile/upload don't
    consume them. Our impl silently drops anything that isn't
    ``spawn``; pin so a follow-up that adds support doesn't
    accidentally break the silent-skip behaviour.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=_plan(lines=["a\n", "b\n"]))
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

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

    Wires up firmware + bus so ``_handle_spawn`` (which reads
    them from the ``DeviceBuilder``) has the controllers
    available — the receive loop's binary-frame branch should
    bail before hitting ``_handle_spawn`` either way, so the
    asserts below confirm no submission happened.
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=None)
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))
    async with client.ws_connect("/compile") as ws:
        await ws.send_bytes(b"not-text")
    # No exit frame, no error — the handler returns the empty WS
    # response on the break and the close handshake completes.
    # Also pins that the binary frame did NOT submit a job (the
    # message loop should bail before reaching the spawn handler).
    assert firmware.compile_calls == []
    assert firmware.upload_calls == []


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


async def test_stream_helper_ignores_terminal_events_for_other_jobs() -> None:
    """A ``JOB_COMPLETED`` for a different job is filtered out, not sent.

    The handler subscribes to lifecycle events for *all* jobs (the
    bus broadcast is per-event-type, not per-job) and filters
    inside the listener by ``job_id``. A misroute here — sending
    another job's terminal frame as our exit — would tell HA the
    build finished when ours hadn't even started, and the
    dashboard's "Firmware tasks" panel would lose the running
    job's WS follower as a side effect.

    Drives the helper directly with a stub WS that records sends.
    Fires a ``JOB_COMPLETED`` for an unrelated job, then a
    ``JOB_OUTPUT`` + ``JOB_COMPLETED`` for ours; only the second
    pair should appear on the wire.
    """
    bus = EventBus()
    job = _make_job()
    other_job = _make_job()
    other_job.job_id = "other-job-id"
    other_job.status = JobStatus.COMPLETED
    other_job.exit_code = 0

    sent: list[dict[str, Any]] = []

    class _RecordingWS:
        async def send_json(self, payload: dict[str, Any], **_kwargs: Any) -> None:
            sent.append(payload)

    async def _fire() -> None:
        await asyncio.sleep(0)
        # Unrelated job's terminal event — must be ignored.
        bus.fire(EventType.JOB_COMPLETED, {"job": other_job})
        # Our job's line + terminal — these must come through.
        bus.fire(EventType.JOB_OUTPUT, {"job_id": job.job_id, "line": "ours\n"})
        job.status = JobStatus.COMPLETED
        job.exit_code = 0
        bus.fire(EventType.JOB_COMPLETED, {"job": job})

    fire_task = asyncio.create_task(_fire())
    try:
        await legacy._stream_job_to_legacy_ws(_RecordingWS(), bus, job)  # type: ignore[arg-type]
    finally:
        await fire_task

    assert sent == [
        {"event": "line", "data": "ours\n"},
        {"event": "exit", "code": 0},
    ]


async def test_legacy_ws_writer_forwards_frame_dict_verbatim() -> None:
    """``_LegacyWSWriter`` writes its ``payload`` arg unchanged to the WS.

    Pins the adapter's contract independently of the rest of the
    streaming pipeline. The integration tests cover this via
    composition, but a regression that started post-processing
    the dict (re-keying, dropping fields, encoding the wrong way)
    would surface in many tests with confusing failures rather
    than one focused message. Direct unit test: build a writer
    around a stub WS, feed it a frame dict, assert the dict
    arrives unchanged.

    Also pins that ``_message_id`` and ``_name`` really are
    unused — passing arbitrary values for either must not affect
    the wire output. A future refactor that started reading them
    would break this test loudly.
    """
    sent: list[dict[str, Any]] = []

    class _RecordingWS:
        async def send_json(self, payload: dict[str, Any], **_kwargs: Any) -> None:
            sent.append(payload)

    writer = legacy._LegacyWSWriter(_RecordingWS())  # type: ignore[arg-type]

    frame = {"event": "line", "data": "hello\n"}
    await writer.send_event("ignored-message-id", "ignored-name", frame)
    await writer.send_event("", "", {"event": "exit", "code": 7})

    assert sent == [
        {"event": "line", "data": "hello\n"},
        {"event": "exit", "code": 7},
    ]


async def test_stream_helper_releases_listener_when_send_raises() -> None:
    """``_stream_job_to_legacy_ws`` removes the listener on a send failure.

    Pins the cleanup contract for the disconnect path. In
    production, a client that closes mid-stream causes the next
    ``ws.send_json`` to raise (``ConnectionResetError`` or
    similar), and ``with bus.listening(...)`` must run the
    listener-removal callback exactly once on the way out.
    Without this, a dead listener would stay attached on the
    bus until the dashboard restarted — fine in the short term,
    a leak over a long-lived process's lifetime.

    Drives the helper directly with a stub WS rather than going
    through the aiohttp close handshake (which carries a 10-second
    server-side timeout that makes the end-to-end version of this
    test slow). The cleanup path is purely a Python ``with``-exit
    so the unit-level assertion catches the same regression a
    full-stack test would.
    """
    bus = EventBus()
    job = _make_job()

    class _BrokenWS:
        async def send_json(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionResetError("client gone")

    # Confirm the bus starts with no listeners — a stale fixture
    # would make the post-call assertion meaningless.
    assert bus._listeners == {}

    # Pre-stage a JOB_OUTPUT event so ``stream_events``'s drain
    # wakes from ``queue.get()`` immediately, attempts the
    # (failing) send through the adapter, and exits the
    # ``bus.listening`` ``with`` block.
    async def _fire_after_subscribe() -> None:
        await asyncio.sleep(0)
        bus.fire(EventType.JOB_OUTPUT, {"job_id": job.job_id, "line": "x\n"})

    fire_task = asyncio.create_task(_fire_after_subscribe())
    try:
        with pytest.raises(ConnectionResetError):
            await legacy._stream_job_to_legacy_ws(_BrokenWS(), bus, job)  # type: ignore[arg-type]
    finally:
        await fire_task

    # All listener slots must be empty: the helper subscribes to
    # JOB_OUTPUT plus the three terminal events, and a partial
    # cleanup (e.g. exit on JOB_OUTPUT before JOB_COMPLETED is
    # added) would still leave a leaking entry behind.
    for event_type in (
        EventType.JOB_OUTPUT,
        EventType.JOB_COMPLETED,
        EventType.JOB_FAILED,
        EventType.JOB_CANCELLED,
    ):
        assert bus._listeners.get(event_type, set()) == set()


@pytest.mark.parametrize(
    ("payload", "description"),
    [
        ({"type": "spawn", "configuration": None}, "configuration: null"),
        ({"type": "spawn", "configuration": 123}, "configuration: int"),
        (
            {"type": "spawn", "configuration": {"nested": "object"}},
            "configuration: object",
        ),
        (
            {"type": "spawn", "configuration": "kitchen.yaml", "port": None},
            "port: null (upload route)",
        ),
        (
            {"type": "spawn", "configuration": "kitchen.yaml", "port": 42},
            "port: int (upload route)",
        ),
    ],
)
async def test_spawn_ws_emits_exit_frame_on_non_string_fields(
    tmp_path: Path,
    aiohttp_client: AiohttpClient,
    payload: dict[str, Any],
    description: str,
) -> None:
    """Non-string ``configuration`` / ``port`` → ``{event: exit, code: 1}``.

    ``data.get("configuration", "")`` only uses the default when
    the key is *absent*; an explicit ``"configuration": null`` (or
    a non-string like ``123`` / an object) lands here as a
    non-``str``. Forwarding that to the firmware controller's
    path-validation helpers would crash with ``TypeError`` /
    ``AttributeError`` and surface to HA as an opaque connection
    drop. The handler rejects up front via the protocol's only
    signalling channel — same shape HA already handles for the
    boundary-rejection case.

    The ``port`` cases use the ``/upload`` route since
    ``/compile`` ignores the port field entirely (its omission /
    type doesn't matter on that path).
    """
    bus = EventBus()
    job = _make_job()
    firmware = _FakeFirmwareController(bus=bus, job=job, plan=None)
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    route = "/upload" if "port" in payload else "/compile"
    async with client.ws_connect(route) as ws:
        await ws.send_json(payload)
        msg = await ws.receive_json()

    assert msg == {"event": "exit", "code": 1}, description
    # The bad input must NOT have submitted a job — the rejection
    # is supposed to happen before the firmware controller is
    # called, so a regression that forwarded the non-string to
    # ``firmware.compile`` / ``.upload`` would surface here.
    assert firmware.compile_calls == []
    assert firmware.upload_calls == []


async def test_spawn_ws_round_trip_through_real_event_bus(
    tmp_path: Path, aiohttp_client: AiohttpClient
) -> None:
    """End-to-end: real ``EventBus`` carries lines + terminal event to the WS.

    The targeted unit tests above mock individual pieces; this one
    drives the same ``EventBus`` the production firmware
    controller fires events on, asserting that the listener
    setup, the snapshot replay, and the live drain all integrate
    correctly. Catches refactor regressions like "subscribe-and-
    snapshot ordering reversed" that wouldn't necessarily show up
    in the targeted tests if the fakes happened to match the
    broken order.
    """
    bus = EventBus()
    job = _make_job(output=["snapshot line\n"])
    firmware = _FakeFirmwareController(
        bus=bus,
        job=job,
        plan=_plan(lines=["live line A\n", "live line B\n"]),
    )
    client = await aiohttp_client(_make_app(tmp_path, firmware=firmware, bus=bus))

    async with client.ws_connect("/compile") as ws:
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

    line_data = "".join(msg["data"] for msg in received if msg.get("event") == "line")
    assert "snapshot line" in line_data
    assert "live line A" in line_data
    assert "live line B" in line_data
    assert received[-1] == {"event": "exit", "code": 0}


# ---------------------------------------------------------------------------
# Frame-shape parity with upstream
# ---------------------------------------------------------------------------


def test_legacy_module_exposes_only_documented_routes() -> None:
    """Pin the legacy route set to the upstream-parity endpoints; no undocumented routes."""
    routes = create_legacy_routes()
    paths = {
        route.path  # type: ignore[union-attr]
        for route in routes
    }
    assert paths == {"/devices", "/ping", "/json-config", "/compile", "/upload"}
