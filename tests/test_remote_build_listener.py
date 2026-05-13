"""
Listener lifecycle tests for the remote-build feature.

Exercises the real :func:`DeviceBuilder._maybe_start_remote_build_site`
and :func:`DeviceBuilder.reload_remote_build_identity` hooks:
default-skip when ``enabled=False``; bind when ``enabled=True``;
fail-soft on bind error; advertise the OS-assigned port for
ephemeral binds; warn on HA-addon mode; rebuild the listener
(now serving the peer-link Noise WS at
``/remote-build/peer-link``) on identity rotation.

Pin-vs-handshake verification is the pairing flow's job
(``test_remote_build_peer_link.py``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from esphome_device_builder.controllers.config import (
    DashboardSettings,
    remote_build_settings_transaction,
)
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.remote_build import (
    OffloaderController,
    ReceiverController,
)
from esphome_device_builder.device_builder import (
    DeviceBuilder,
    _strip_server_header_middleware,
)
from esphome_device_builder.helpers.dashboard_advertise import DashboardAdvertiser
from esphome_device_builder.helpers.dashboard_identity import (
    get_or_create_identity,
    rotate_identity,
)
from esphome_device_builder.helpers.event_bus import EventBus

from .conftest import MakeSettingsFactory
from .conftest import RemoteBuildTestHandles as RemoteBuildController


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_skips_when_explicitly_disabled(
    tmp_path: Path,
) -> None:
    """
    Operator-disabled: ``_maybe_start_remote_build_site`` early-returns when ``enabled=False``.

    Persist ``enabled=False`` via the settings sidecar so the
    operator's explicit-disable choice is what's under test, not
    the model default (which is ``True``). Pins the gate at the
    lifecycle hook, not just at the settings layer — a refactor
    that bound the listener unconditionally (or read the wrong
    field) would fail here.
    """
    loop = asyncio.get_running_loop()

    def _disable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = False

    await loop.run_in_executor(None, _disable)

    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_binds_by_default_on_fresh_install(
    tmp_path: Path,
) -> None:
    """
    Fresh install (no ``_remote_build`` block in metadata) binds by default.

    Default-on for non-HA-addon deployments: the model default
    ``RemoteBuildSettings.enabled = True`` carries through the
    load path so a fresh sidecar with no ``_remote_build`` key
    still ends up with the listener bound. The privilege gate
    is the receiver-side pair-approval dialog, not the bind
    address. Pins the new default so a regression that
    re-introduced ``enabled: bool = False`` would fail here.
    """
    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0  # ephemeral so the bind doesn't collide
    db = DeviceBuilder(settings)
    db.loop = asyncio.get_running_loop()
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path
    db._publish_remote_build_advertise = AsyncMock()

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_binds_when_enabled(tmp_path: Path) -> None:
    """
    Flipping ``enabled=True`` makes the lifecycle hook bind the listener.

    Round-trip: write ``enabled=True`` to the settings sidecar,
    drive ``_maybe_start_remote_build_site`` through the same
    code path the dashboard's startup uses, assert a runner
    landed.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    # Pin the port to ``0`` so the OS picks a free one and the
    # test doesn't collide with a real receiver if 6055 is in use.
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_fails_soft_on_bind_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed bind logs the error and leaves the dashboard running.

    Drive ``_maybe_start_remote_build_site`` through the enabled
    path with a port that fails to bind (port 1, can't bind as
    non-root). The hook MUST NOT raise; the runner must end up
    cleaned up; the dashboard's main flow continues unaffected.
    Pins the fail-soft contract so a misconfiguration in
    Settings (typo'd port, port already in use, cert load
    failure) doesn't take down the whole dashboard.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    # Force the bind to fail by stubbing TCPSite.start to raise.
    real_start = web.TCPSite.start

    async def _failing_start(self: web.TCPSite) -> None:
        raise OSError("address in use (test stub)")

    monkeypatch.setattr(web.TCPSite, "start", _failing_start)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    # Must not raise — the dashboard keeps running on bind failure.
    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None

    # Sanity: with the stub removed, a fresh call would succeed.
    monkeypatch.setattr(web.TCPSite, "start", real_start)


@pytest.mark.asyncio
async def test_strip_server_header_middleware_overrides_to_empty(tmp_path: Path) -> None:
    """
    The Server header is overridden to empty string.

    Setting to empty (not deleting) is what overrides aiohttp's
    connection-level default banner. Pinned at the unit level
    so a refactor that swaps the middleware out gets caught
    here.
    """

    async def _handler(_: web.Request) -> web.StreamResponse:
        return web.Response(status=200, headers={"Server": "Python/3.14 aiohttp/3.13"})

    request = make_mocked_request("GET", "/remote-build/peer-link", client_max_size=0)
    response = await _strip_server_header_middleware(request, _handler)
    assert response.headers["Server"] == ""


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_updates_advertiser_on_success(
    tmp_path: Path,
) -> None:
    """
    Successful bind pushes ``pin_sha256`` + ``remote_build_port`` into the advertiser.

    Pins the post-bind advertiser-update wiring so a refactor that
    accidentally drops the setter calls (or moves them before the
    bind) surfaces here.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    fake_advertiser = MagicMock()
    fake_advertiser.set_pin_sha256 = MagicMock()
    fake_advertiser.set_remote_build_port = MagicMock()
    fake_advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = fake_advertiser

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        # Pin and listener port both made it to the advertiser.
        assert fake_advertiser.set_pin_sha256.called
        assert fake_advertiser.set_remote_build_port.called
        # ``refresh`` was awaited so the TXT change actually
        # leaves the local cache.
        assert fake_advertiser.refresh.called
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_start_registers_advertiser_with_all_txt_keys_in_one_announce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_settings: MakeSettingsFactory,
    _hermetic_lifecycle: None,
) -> None:
    """``DeviceBuilder.start()`` publishes one ServiceInfo carrying all 4 TXT keys."""

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    # ``remote_build_settings_transaction`` does an atomic-write
    # (``tempfile.mkstemp`` → ``os.path.abspath``); hop through
    # the executor so blockbuster doesn't trip on the sync I/O
    # while the test is on the event loop.
    await asyncio.get_running_loop().run_in_executor(None, _enable)

    # Inject a fake zeroconf via the property so the advertise
    # branch actually runs; ``_hermetic_lifecycle`` leaves it at
    # ``None`` (the production "no advertise" path).
    fake_zc = MagicMock()
    fake_zc.async_register_service = AsyncMock()
    fake_zc.async_update_service = AsyncMock()
    fake_zc.async_unregister_service = AsyncMock()
    monkeypatch.setattr(DevicesController, "zeroconf", property(lambda self: fake_zc))

    settings = make_settings(with_core_path=True)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    settings.on_ha_addon = False
    db = DeviceBuilder(settings)
    try:
        await db.start()

        # One register published a ServiceInfo with all 4 keys; no
        # follow-up ``async_update_service`` raced the announce.
        assert fake_zc.async_register_service.await_count == 1
        info = fake_zc.async_register_service.call_args.args[0]
        decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
        assert set(decoded) == {
            "server_version",
            "esphome_version",
            "pin_sha256",
            "remote_build_port",
        }
        assert fake_zc.async_update_service.await_count == 0
    finally:
        await db.stop()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_against_unregistered_advertiser_stages_only(
    tmp_path: Path,
) -> None:
    """A bind before advertiser registration stages pin+port and fires no update."""
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    # Real advertiser, deliberately not registered: ``_info`` and
    # ``_zeroconf`` stay ``None`` so the production ``refresh``
    # short-circuits without touching the wire.
    advertiser = DashboardAdvertiser(
        port=6052,
        server_version="1.2.3",
        esphome_version="2026.5.0",
        dashboard_id="abcd1234",
    )
    db._dashboard_advertiser = advertiser

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        # Pin + port were staged for the next ``build_service_info``.
        assert advertiser._pin_sha256
        assert advertiser._remote_build_port is not None
        # No premature publish: ``_info`` / ``_zeroconf`` still
        # absent, so ``refresh`` was a no-op.
        assert advertiser._info is None
        assert advertiser._zeroconf is None
        # A subsequent ``build_service_info`` carries all 4 TXT keys
        # in one shot; what ``register`` will publish.
        info = advertiser.build_service_info(addresses=["127.0.0.1"])
        decoded = {k.decode(): v.decode() for k, v in info.properties.items()}
        assert set(decoded) == {
            "server_version",
            "esphome_version",
            "pin_sha256",
            "remote_build_port",
        }
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_advertises_actual_port_for_ephemeral(
    tmp_path: Path,
) -> None:
    """
    ``remote_build_port=0`` advertises the OS-assigned port, not literal 0.

    When the operator binds with ``--remote-build-port 0`` (or a
    test pins it to 0 to avoid collisions), the OS picks an
    ephemeral port. Advertising or logging ``0`` would point
    peers at an unreachable port and the operator couldn't
    answer "what port am I on?". Resolve the actual bound port
    from the socket and pass that to the advertiser.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0  # ask the OS for an ephemeral port
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    fake_advertiser = MagicMock()
    fake_advertiser.set_pin_sha256 = MagicMock()
    fake_advertiser.set_remote_build_port = MagicMock()
    fake_advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = fake_advertiser

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        # The advertiser receives the OS-assigned port, never 0.
        assert fake_advertiser.set_remote_build_port.called
        advertised = fake_advertiser.set_remote_build_port.call_args.args[0]
        assert advertised != 0
        assert 1024 <= advertised <= 65535
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_skips_ha_addon_without_persisted_opt_in(
    tmp_path: Path,
) -> None:
    """
    HA addon + no persisted ``_remote_build`` block → skip the bind.

    The addon's docker container doesn't expose port 6055 to the
    LAN by default, and the mDNS advertise is already skipped on
    HA addon, so binding by default would burn the port without
    making the feature reachable. Skip until the operator
    explicitly opts in via the Settings toggle.
    """
    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    settings.on_ha_addon = True  # the branch under test
    db = DeviceBuilder(settings)
    db.loop = asyncio.get_running_loop()
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_binds_ha_addon_after_explicit_opt_in(
    tmp_path: Path,
) -> None:
    """
    HA addon + persisted ``enabled=True`` → bind (operator override).

    Some legacy-dashboard operators historically added the
    receiver port to their addon's ``ports:`` config to expose
    it. Once they flip the toggle in Settings (which persists
    ``_remote_build``), the bind site respects that explicit
    opt-in exactly like every other deployment mode — no
    deployment-mode short-circuit at this point.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    settings.on_ha_addon = True
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path
    db._publish_remote_build_advertise = AsyncMock()

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_maybe_start_remote_build_site_respects_ha_addon_explicit_disable(
    tmp_path: Path,
) -> None:
    """
    HA addon + persisted ``enabled=False`` → skip (operator explicit-off).

    An operator who explicitly disabled the toggle on HA addon
    triggers the second gate (``rb_settings.enabled`` check),
    not the first (HA-addon "no persisted block") -- because
    persisting any value flips the persistence signal to ``True``.
    Pins that the gate composition is correct: HA-addon shortcut
    only suppresses the *fresh-install default-on* path, never
    overrides an explicit operator choice.
    """
    loop = asyncio.get_running_loop()

    def _disable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = False

    await loop.run_in_executor(None, _disable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    settings.on_ha_addon = True
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    await db._maybe_start_remote_build_site()
    assert db._remote_build_runner is None


@pytest.mark.asyncio
async def test_reload_remote_build_identity_no_op_when_listener_unbound(
    tmp_path: Path,
) -> None:
    """
    Rotation when the listener isn't bound: no advertiser touch, no rebuild.

    A user can rotate from the Settings UI even with
    remote-build disabled. The cert + key on disk are already
    updated by the time this method runs; without a listener,
    pushing ``pin_sha256`` to mDNS would contradict the TXT
    contract (pin + port appear iff bound) and point peers at
    a port that isn't serving traffic. Pin the no-op so a
    refactor that adds an unconditional advertiser push gets
    caught here.
    """
    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    advertiser = MagicMock()
    advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = advertiser
    db._remote_build_runner = None

    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_identity, tmp_path)

    listener_bound = await db.reload_remote_build_identity(pin_sha256=identity.pin_sha256)

    advertiser.set_pin_sha256.assert_not_called()
    advertiser.set_remote_build_port.assert_not_called()
    advertiser.refresh.assert_not_awaited()
    assert db._remote_build_runner is None
    # Reload returns ``False`` when there's no listener to rebuild.
    assert listener_bound is False


@pytest.mark.asyncio
async def test_reload_remote_build_identity_rebuilds_listener(tmp_path: Path) -> None:
    """
    Rotation while the listener is bound: tear down + rebuild against the new cert.

    Pins that the live socket picks up the rotated cert without
    a dashboard restart. Done by checking that the runner ID
    changes across the call (a fresh ``AppRunner`` is built).
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    try:
        await db._maybe_start_remote_build_site()
        assert db._remote_build_runner is not None
        first_runner = db._remote_build_runner

        # Rotate the X25519 peer-link key on disk so the
        # rebuild loads the new identity.
        new_identity = await loop.run_in_executor(None, rotate_identity, tmp_path)
        listener_bound = await db.reload_remote_build_identity(
            pin_sha256=new_identity.pin_sha256,
        )

        # Listener was rebuilt — different ``AppRunner`` instance.
        assert db._remote_build_runner is not None
        assert db._remote_build_runner is not first_runner
        # Reload reports the post-rebuild state — listener is up.
        assert listener_bound is True
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_reload_remote_build_identity_clears_advertiser_when_rebuild_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Rebuild failure leaves the advertiser cleared, not stale.

    The TXT contract is "``pin_sha256`` and ``remote_build_port``
    appear iff the listener is currently bound". If rotation
    tears down the runner and the rebuild fails (port now bound
    by something else, cert load throws, …), the advertiser
    must NOT keep advertising the pre-rotation pin + port —
    peers re-browsing would otherwise try to connect to a
    socket that's no longer there. Pin both fields cleared on
    the failure path.
    """
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    advertiser = MagicMock()
    advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = advertiser

    try:
        # Fake the runner-bound state without going through
        # ``_maybe_start_remote_build_site``; the test cares
        # about the teardown + clear + failed-rebuild sequence,
        # not the initial bind.
        old_runner = MagicMock()
        old_runner.cleanup = AsyncMock()
        db._remote_build_runner = old_runner

        # Make the rebuild deterministically fail-soft. Stubbing
        # ``TCPSite.start`` matches the existing fail-soft test
        # in this file.
        async def _failing_start(self: web.TCPSite) -> None:
            raise OSError("address in use (test stub)")

        monkeypatch.setattr(web.TCPSite, "start", _failing_start)

        listener_bound = await db.reload_remote_build_identity(
            pin_sha256="newpin" * 10 + "abcd",  # 64 chars; value irrelevant
        )

        # No listener after failed rebuild.
        assert listener_bound is False
        assert db._remote_build_runner is None
        # Advertiser was cleared during teardown — pin AND port
        # both went to None. _maybe_start_remote_build_site's
        # post-bind push didn't run (rebuild failed before it),
        # so the cleared state is the steady state.
        assert advertiser.set_pin_sha256.call_args_list == [call(None)]
        assert advertiser.set_remote_build_port.call_args_list == [call(None)]
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_reload_remote_build_identity_advertiser_refresh_failure_is_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky mDNS refresh during rotation must not raise out of the helper."""
    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    advertiser = MagicMock()
    advertiser.refresh = AsyncMock(side_effect=RuntimeError("zeroconf wedged"))
    db._dashboard_advertiser = advertiser

    # Listener IS bound — the advertiser-clear-during-teardown
    # path is the one that touches mDNS now. Stub the runner
    # cleanup so the test stays focused on the refresh
    # fail-soft.
    old_runner = MagicMock()
    old_runner.cleanup = AsyncMock()
    db._remote_build_runner = old_runner

    # Force the rebuild to also fail so the test doesn't have
    # to stand up a real listener.
    async def _failing_start(self: web.TCPSite) -> None:
        raise OSError("address in use (test stub)")

    monkeypatch.setattr(web.TCPSite, "start", _failing_start)

    # Must not raise — fail-soft contract on the refresh tick.
    listener_bound = await db.reload_remote_build_identity(pin_sha256="x" * 64)
    # Both fields were cleared (ignoring the flaky refresh).
    advertiser.set_pin_sha256.assert_called_once_with(None)
    advertiser.set_remote_build_port.assert_called_once_with(None)
    assert listener_bound is False


# ---------------------------------------------------------------------------
# Live-toggle: ``ReceiverController.set_settings`` calls
# ``DeviceBuilder.apply_remote_build_enabled`` after persisting, so
# flipping ``enabled`` doesn't require a dashboard restart.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_remote_build_enabled_binds_when_disk_says_true(tmp_path: Path) -> None:
    """Convergence: disk ``enabled=True`` + listener absent → bind."""
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    try:
        bound = await db.apply_remote_build_enabled()
        assert bound is True
        assert db._remote_build_runner is not None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_apply_remote_build_enabled_tears_down_when_disk_says_false(
    tmp_path: Path,
) -> None:
    """Convergence: disk ``enabled=False`` + listener bound → teardown + advertiser clear."""
    loop = asyncio.get_running_loop()

    # Persist an explicit ``enabled=False`` so the test exercises
    # the disabled-on-disk path. With the new default-on model
    # value an empty sidecar would load as ``enabled=True`` and
    # this test would no longer cover its named contract.
    def _disable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = False

    await loop.run_in_executor(None, _disable)

    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    db.loop = loop

    advertiser = MagicMock()
    advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = advertiser

    # Fake a bound runner without standing up a real socket.
    old_runner = MagicMock()
    old_runner.cleanup = AsyncMock()
    db._remote_build_runner = old_runner

    advertiser.unregister = AsyncMock()

    bound = await db.apply_remote_build_enabled()

    assert bound is False
    assert db._remote_build_runner is None
    old_runner.cleanup.assert_awaited_once()
    # mDNS pin + port both cleared so peers re-browsing don't try
    # to connect to a port that's no longer serving traffic.
    advertiser.set_pin_sha256.assert_called_once_with(None)
    advertiser.set_remote_build_port.assert_called_once_with(None)
    # Pin the "TXT update, not unregister" contract — peer caches
    # mustn't see the dashboard service disappear and reappear
    # when we just want to drop pin/port from the TXT.
    advertiser.refresh.assert_awaited_once()
    advertiser.unregister.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_remote_build_enabled_no_op_before_loop_set(tmp_path: Path) -> None:
    """No-op when the dashboard hasn't finished startup yet (``loop is None``)."""
    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    # ``DeviceBuilder.__init__`` doesn't set ``loop``; it's wired
    # by ``start()`` after the event loop is running. A WS command
    # can't legitimately reach this method that early, but the
    # guard keeps a future caller (e.g. a unit test driving the
    # method out of band) from hitting ``run_in_executor`` on
    # ``None``.
    assert db.loop is None
    bound = await db.apply_remote_build_enabled()
    assert bound is False


@pytest.mark.asyncio
async def test_apply_remote_build_enabled_idempotent_when_already_off(tmp_path: Path) -> None:
    """Disk ``enabled=False`` + listener absent → no-op (no advertiser touch)."""
    settings = DashboardSettings(config_dir=tmp_path)
    db = DeviceBuilder(settings)
    db.loop = asyncio.get_running_loop()
    advertiser = MagicMock()
    advertiser.refresh = AsyncMock()
    db._dashboard_advertiser = advertiser
    db._remote_build_runner = None

    bound = await db.apply_remote_build_enabled()

    assert bound is False
    assert db._remote_build_runner is None
    advertiser.set_pin_sha256.assert_not_called()
    advertiser.set_remote_build_port.assert_not_called()
    advertiser.refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_remote_build_enabled_idempotent_when_already_on(tmp_path: Path) -> None:
    """Disk ``enabled=True`` + listener bound → no-op (no rebind, no advertiser churn)."""
    loop = asyncio.get_running_loop()

    def _enable() -> None:
        with remote_build_settings_transaction(tmp_path) as txn:
            txn.enabled = True

    await loop.run_in_executor(None, _enable)

    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = loop
    db.remote_build_receiver = MagicMock()
    db.remote_build_receiver._db.settings.config_dir = tmp_path

    try:
        await db._maybe_start_remote_build_site()
        original_runner = db._remote_build_runner
        assert original_runner is not None

        bound = await db.apply_remote_build_enabled()

        assert bound is True
        # No rebind — the same runner instance is still serving.
        assert db._remote_build_runner is original_runner
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()


@pytest.mark.asyncio
async def test_set_settings_live_rebinds_listener(tmp_path: Path) -> None:
    """End-to-end: ``set_settings(enabled=True)`` flips disk + binds the listener."""
    settings = DashboardSettings(config_dir=tmp_path)
    settings.host = "127.0.0.1"
    settings.remote_build_port = 0
    db = DeviceBuilder(settings)
    db.loop = asyncio.get_running_loop()
    db.bus = EventBus()
    # ``OffloaderController.__init__`` builds a per-file Store
    # under ``config_dir / .offloader_pairings.json``; needs a
    # real Path (tmp_path is fine).
    db.remote_build_receiver = None  # not needed — controller doesn't read it
    controller = RemoteBuildController(
        offloader=OffloaderController(db),
        receiver=ReceiverController(db),
    )
    db.remote_build_receiver = controller.receiver
    # Wire the controller's mDNS-advertiser hook to a no-op.
    db._dashboard_advertiser = None

    try:
        view = await controller.receiver.set_settings(enabled=True)
        assert view.enabled is True
        # Listener bound as a side effect of set_settings.
        assert db._remote_build_runner is not None

        view = await controller.receiver.set_settings(enabled=False)
        assert view.enabled is False
        # Listener torn down as a side effect.
        assert db._remote_build_runner is None
    finally:
        if db._remote_build_runner is not None:
            await db._remote_build_runner.cleanup()
