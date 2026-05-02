"""Tests for the ``devices/import`` command path.

Covers two regressions discovered while wiring up the adoption flow:

* ``import_config`` lives at ``esphome.components.dashboard_import``,
  not ``esphome.config_helpers``. The previous import path silently
  became ``None`` and every adoption attempt raised a generic
  RuntimeError before doing anything.
* When the target YAML already exists, ``import_config`` raises
  ``FileExistsError``. We re-surface it as a ``CommandError`` so the
  dashboard can show a useful message instead of the WS layer's
  generic ``Command failed`` fallback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers import devices as devices_module
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _make_controller(tmp_path: Any) -> DevicesController:
    """Stand up a controller without booting the full DeviceBuilder."""
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = MagicMock()
    ctrl._db.settings.rel_path = lambda name: tmp_path / name
    ctrl._scanner = MagicMock()
    ctrl._scanner.scan = AsyncMock()
    # The fast-online seed pulls the cached IP out of the state
    # monitor and forces ONLINE on success; tests use a Mock so they
    # can assert the calls without standing up a real zeroconf.
    ctrl._state_monitor = MagicMock()
    ctrl._state_monitor.get_cached_addresses = MagicMock(return_value=None)
    # ``import_result`` must be a real dict so the iteration in
    # ``import_device`` works; the discovery callback path is
    # exercised separately in ``test_importable_discovery``.
    ctrl.import_result = {}
    return ctrl


def test_import_config_resolves_at_import_time() -> None:
    """Regression guard for the import path move.

    ``import_config`` used to be imported via ``esphome.config_helpers``
    behind a try/except, so a wrong path silently became ``None`` and
    every adoption attempt raised. The current call site imports
    directly from ``esphome.components.dashboard_import``; if that
    module ever moves we want the test suite to fail loudly here, not
    a user's first adoption attempt.
    """
    assert devices_module.import_config is not None
    assert callable(devices_module.import_config)


async def test_import_device_invokes_import_config_and_returns_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: write the YAML, run a scan, return the configuration name."""
    captured: dict[str, Any] = {}

    def fake_import_config(*args: Any, **_kwargs: Any) -> None:
        captured["args"] = args

    monkeypatch.setattr(devices_module, "import_config", fake_import_config)
    ctrl = _make_controller(tmp_path)

    result = await ctrl.import_device(
        name="kitchen-1a2b3c",
        project_name="acme.kitchen",
        package_import_url="github://acme/firmware.yaml@main",
        friendly_name="Kitchen",
        encryption="true",
    )

    assert result == {"configuration": "kitchen-1a2b3c.yaml"}
    # Argument order matters — upstream signature is
    # ``(path, name, friendly_name, project_name, import_url, network, encryption)``.
    args = captured["args"]
    assert args[0] == tmp_path / "kitchen-1a2b3c.yaml"
    assert args[1] == "kitchen-1a2b3c"
    assert args[2] == "Kitchen"
    assert args[3] == "acme.kitchen"
    assert args[4] == "github://acme/firmware.yaml@main"
    # No matching importable cache entry → fall back to wifi (legacy behaviour).
    assert args[5] == "wifi"
    assert args[6] == "true"  # encryption flag forwarded
    ctrl._scanner.scan.assert_awaited_once()


async def test_import_device_passes_ethernet_network_through_to_import_config(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ESP32-PoE / Olimex broadcasts ``network=ethernet`` — preserve it.

    Hard-coding ``CONF_WIFI`` produced a YAML with a Wi-Fi template
    that the user had to fix by hand on every Ethernet adoption.
    Look up the discovered ``AdoptableDevice`` by the
    ``package_import_url`` the dialog passes and forward its
    ``network`` field to ``import_config``.
    """
    from esphome_device_builder.models import AdoptableDevice

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        devices_module, "import_config", lambda *args, **_kw: captured.setdefault("args", args)
    )

    ctrl = _make_controller(tmp_path)
    ctrl.import_result["olimex-poe-aabbcc"] = AdoptableDevice(
        name="olimex-poe-aabbcc",
        friendly_name="Olimex PoE",
        package_import_url="github://olimex/esp32-poe.yaml",
        project_name="olimex.esp32-poe",
        project_version="1.0.0",
        network="ethernet",
        ignored=False,
    )

    await ctrl.import_device(
        # User picked a shorter name in the dialog — discovery key
        # still matches because we look up by URL.
        name="garage",
        project_name="olimex.esp32-poe",
        package_import_url="github://olimex/esp32-poe.yaml",
    )

    assert captured["args"][5] == "ethernet"


async def test_import_device_uses_direct_name_lookup_with_duplicate_products(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple identical products on the LAN don't get the wrong network.

    Factory firmware broadcasts each device with a MAC suffix
    (``apollo-plt-1-983300``, ``apollo-plt-1-aabbcc``), so the
    ``import_result`` key is unique per physical device even when
    several share the same ``package_import_url``. The frontend
    pre-fills the adoption dialog with the discovery row's broadcast
    name, so we look up by ``name`` first — that's unambiguous.

    Pre-fix the lookup walked the dict and returned whichever
    matching ``package_import_url`` row landed first; for two
    Apollo PLT-1s on different networks (one Wi-Fi reflashed for
    Ethernet, one stock) that meant a coin-flip on which network
    the imported YAML got.
    """
    from esphome_device_builder.models import AdoptableDevice

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        devices_module, "import_config", lambda *args, **_kw: captured.setdefault("args", args)
    )

    ctrl = _make_controller(tmp_path)
    # Two Apollo PLT-1s — same firmware, different network types.
    # The import dict's insertion order would otherwise pick whichever
    # arrived first; the direct-name lookup ignores order.
    ctrl.import_result["apollo-plt-1-aabbcc"] = AdoptableDevice(
        name="apollo-plt-1-aabbcc",
        friendly_name="Apollo PLT-1 (Wi-Fi)",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="1.0.0",
        network="wifi",
        ignored=False,
    )
    ctrl.import_result["apollo-plt-1-ddeeff"] = AdoptableDevice(
        name="apollo-plt-1-ddeeff",
        friendly_name="Apollo PLT-1 (Ethernet)",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="1.0.0",
        network="ethernet",
        ignored=False,
    )

    # User adopts the second one — frontend passes its broadcast name.
    await ctrl.import_device(
        name="apollo-plt-1-ddeeff",
        project_name="apollo.plt-1",
        package_import_url="github://apollo/plt-1.yaml",
    )

    # Got the Ethernet entry, not whichever came first.
    assert captured["args"][5] == "ethernet"


async def test_import_device_falls_back_to_wifi_for_old_factory_firmware(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older factory firmwares didn't advertise ``network=`` — fall back to wifi.

    The TXT field ``network`` only became part of the dashboard_import
    discovery contract recently. A device whose mDNS broadcast omits
    it (``AdoptableDevice.network == ""``) shouldn't fail adoption —
    Wi-Fi is the historical default and matches what the legacy
    dashboard wrote.
    """
    from esphome_device_builder.models import AdoptableDevice

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        devices_module, "import_config", lambda *args, **_kw: captured.setdefault("args", args)
    )

    ctrl = _make_controller(tmp_path)
    ctrl.import_result["legacy-bulb-001122"] = AdoptableDevice(
        name="legacy-bulb-001122",
        friendly_name="Legacy Bulb",
        package_import_url="github://vendor/old.yaml",
        project_name="vendor.old",
        project_version="0.1.0",
        network="",  # field absent / empty in TXT
        ignored=False,
    )

    await ctrl.import_device(
        name="legacy-bulb",
        project_name="vendor.old",
        package_import_url="github://vendor/old.yaml",
    )

    assert captured["args"][5] == "wifi"


async def test_import_device_translates_file_exists_to_command_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FileExistsError`` becomes a user-facing ``CommandError``.

    The WS layer turns generic exceptions into ``Command failed: …``;
    the dashboard's adopt dialog can't surface that meaningfully. The
    handler catches ``FileExistsError`` and re-raises as a
    ``CommandError`` carrying ``INVALID_ARGS`` and a message that
    names the offending file.
    """

    def raises_file_exists(*_args: Any, **_kwargs: Any) -> None:
        raise FileExistsError

    monkeypatch.setattr(devices_module, "import_config", raises_file_exists)
    ctrl = _make_controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "kitchen.yaml already exists" in excinfo.value.message
    # Scan must NOT run when the YAML write failed — otherwise we'd
    # falsely advertise a successful adoption to subscribers.
    ctrl._scanner.scan.assert_not_called()


async def test_import_device_returns_even_when_post_scan_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan failure after a successful YAML write must not roll back.

    The YAML is on disk; failing the WS command would leave the user
    in a state where retrying produces ``FileExistsError`` despite
    nothing being wrong. Best-effort scan; the periodic poll picks up
    whatever this attempt missed.
    """
    monkeypatch.setattr(devices_module, "import_config", lambda *a, **kw: None)
    ctrl = _make_controller(tmp_path)
    ctrl._scanner.scan = AsyncMock(side_effect=RuntimeError("transient"))

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}


async def test_import_device_seeds_online_state_from_zeroconf_cache(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly-adopted device should land ONLINE without waiting for ping.

    The device was advertising on mDNS milliseconds ago — that's how
    it ended up on the discovery banner — so we already know it's
    reachable. ``import_device`` claims ONLINE via the state monitor
    (``mdns`` priority + ``claim=True`` so a later ping observation
    can't clobber it) and pulls the cached IP out of zeroconf so the
    new card has an address right away.
    """
    from esphome_device_builder.models import DeviceState

    monkeypatch.setattr(devices_module, "import_config", lambda *a, **kw: None)
    ctrl = _make_controller(tmp_path)
    ctrl._state_monitor.get_cached_addresses = MagicMock(return_value=["192.168.1.42"])

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    ctrl._state_monitor.apply.assert_called_once_with(
        "kitchen", DeviceState.ONLINE, "mdns", claim=True
    )
    ctrl._state_monitor.get_cached_addresses.assert_called_once_with("kitchen.local")
    ctrl._state_monitor.apply_ip.assert_called_once_with("kitchen", "192.168.1.42")


async def test_import_device_skips_apply_ip_when_zeroconf_cache_misses(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cached IP → state still flips ONLINE, just no apply_ip call."""
    from esphome_device_builder.models import DeviceState

    monkeypatch.setattr(devices_module, "import_config", lambda *a, **kw: None)
    ctrl = _make_controller(tmp_path)
    ctrl._state_monitor.get_cached_addresses = MagicMock(return_value=None)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    ctrl._state_monitor.apply.assert_called_once_with(
        "kitchen", DeviceState.ONLINE, "mdns", claim=True
    )
    ctrl._state_monitor.apply_ip.assert_not_called()


async def test_import_device_drops_matching_import_result_entry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery banner entry disappears the moment adoption finishes.

    Before this fix, the discovered card stuck around until the next
    discovery cycle filtered it out by name. Match the cache entry by
    ``package_import_url`` (which uniquely identifies the firmware)
    so we drop the right entry even when the user typed a different
    YAML name in the dialog.
    """
    from esphome_device_builder.models import AdoptableDevice, EventType

    monkeypatch.setattr(devices_module, "import_config", lambda *a, **kw: None)
    ctrl = _make_controller(tmp_path)
    discovered = AdoptableDevice(
        name="apollo-plt-1-983300",
        friendly_name="Apollo PLT-1",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="26.3.2.1",
        network="wifi",
        ignored=False,
    )
    ctrl.import_result["apollo-plt-1-983300"] = discovered

    await ctrl.import_device(
        # User typed a shorter name (without the MAC suffix).
        name="apollo-plt-1",
        project_name="apollo.plt-1",
        package_import_url="github://apollo/plt-1.yaml",
    )

    assert "apollo-plt-1-983300" not in ctrl.import_result
    # Removal is broadcast so subscribed frontends drop the card.
    fired = list(ctrl._db.bus.fire.mock_calls)
    removed_events = [
        c for c in fired if c.args and c.args[0] == EventType.IMPORTABLE_DEVICE_REMOVED
    ]
    assert removed_events, "expected IMPORTABLE_DEVICE_REMOVED to fire"
    assert removed_events[0].args[1] == {"name": "apollo-plt-1-983300"}
