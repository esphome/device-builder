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

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices import importable as devices_module
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import AdoptableDevice, DeviceState, ErrorCode, EventType

from .conftest import (
    CaptureDevicesEventsFactory,
    MakeControllerFactory,
    RecordingStateMonitor,
)


def _seed_import_state(controller: DevicesController) -> None:
    """Initialise ``import_result`` to an empty dict.

    ``import_device`` iterates ``import_result`` for the cached
    AdoptableDevice — production wires this up in ``__init__``,
    but the bypass-init factory leaves it unset.
    """
    controller.state.import_result = {}


def _import_config_stub(
    captured: dict[str, Any] | None = None,
) -> Callable[..., None]:
    """Stub for ``import_config`` that mirrors its on-disk write.

    The real ``import_config`` writes a YAML to ``args[0]``. The
    ``import_device`` post-write validation step then reads it
    back, so a stub that only records call args trips the read
    with ``FileNotFoundError``. This helper writes a minimal
    syntactically-valid YAML (parseable, but deliberately not
    ESPHome-schema-valid — there's no platform block, so the
    fake validator we mock around it is the source of pass/fail
    truth) at the destination path and optionally records the
    call args into *captured* (for tests that assert on what got
    forwarded to upstream).
    """

    def _stub(*args: Any, **_kw: Any) -> None:
        if captured is not None:
            captured.setdefault("args", args)
        args[0].write_text(f"esphome:\n  name: {args[1]}\n", encoding="utf-8")

    return _stub


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: write the YAML, run a scan, return the configuration name."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub(captured))
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

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
    # ``import_device`` calls ``scan()`` exactly once on the happy
    # path; pin the full call list so a regression that double-scans
    # (or sneaks in a stray ``reload``) breaks here instead of
    # silently passing the membership check.
    assert ctrl._scanner.calls == [("scan",)]


async def test_import_device_passes_ethernet_network_through_to_import_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An ESP32-PoE / Olimex broadcasts ``network=ethernet`` — preserve it.

    Hard-coding ``CONF_WIFI`` produced a YAML with a Wi-Fi template
    that the user had to fix by hand on every Ethernet adoption.
    Look up the discovered ``AdoptableDevice`` by the
    ``package_import_url`` the dialog passes and forward its
    ``network`` field to ``import_config``.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub(captured))

    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl.state.import_result["olimex-poe-aabbcc"] = AdoptableDevice(
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
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
    captured: dict[str, Any] = {}
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub(captured))

    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    # Two Apollo PLT-1s — same firmware, different network types.
    # The import dict's insertion order would otherwise pick whichever
    # arrived first; the direct-name lookup ignores order.
    ctrl.state.import_result["apollo-plt-1-aabbcc"] = AdoptableDevice(
        name="apollo-plt-1-aabbcc",
        friendly_name="Apollo PLT-1 (Wi-Fi)",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="1.0.0",
        network="wifi",
        ignored=False,
    )
    ctrl.state.import_result["apollo-plt-1-ddeeff"] = AdoptableDevice(
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Older factory firmwares didn't advertise ``network=`` — fall back to wifi.

    The TXT field ``network`` only became part of the dashboard_import
    discovery contract recently. A device whose mDNS broadcast omits
    it (``AdoptableDevice.network == ""``) shouldn't fail adoption —
    Wi-Fi is the historical default and matches what the legacy
    dashboard wrote.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub(captured))

    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl.state.import_result["legacy-bulb-001122"] = AdoptableDevice(
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
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
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

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
    assert ctrl._scanner.calls == []


async def test_import_device_rejects_when_imported_yaml_does_not_validate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Imported YAML failing schema validation is deleted + raises.

    ``import_config`` produces a wizard-style YAML by construction,
    but a regression upstream — or a project YAML whose
    ``packages:`` reference doesn't resolve cleanly — would
    otherwise leave an unflashable file on disk that every
    downstream operation refuses. After ``import_config`` returns
    we read the file back, validate, and on failure delete it
    and surface the editor errors so the user can fix the source
    project (or pick a different one) and retry without a
    leftover ``FileExistsError`` blocking them.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {"message": "[esphome] required key not provided: a platform"},
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "required key not provided: a platform" in excinfo.value.message
    # YAML rolled back so a retry doesn't trip ``FileExistsError``.
    assert not (tmp_path / "kitchen.yaml").exists()
    # Scanner must NOT have been notified of the half-imported device.
    assert ctrl._scanner.calls == []


async def test_import_device_rolls_back_on_unicode_decode_error_from_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Non-UTF-8 bytes in the freshly-written YAML still trigger rollback.

    ``Path.read_text(encoding='utf-8')`` raises ``UnicodeDecodeError``
    (which is *not* an ``OSError``) when ``import_config`` somehow
    landed bytes that aren't valid UTF-8. Without an explicit
    catch, the rollback would skip and the half-imported file
    would block every retry with ``FileExistsError``.
    """

    def write_garbage(*args: Any, **_kw: Any) -> None:
        # Write a byte that isn't a valid UTF-8 leading byte so
        # ``read_text(encoding='utf-8')`` chokes on it.
        args[0].write_bytes(b"\xff garbage")

    monkeypatch.setattr(devices_module, "import_config", write_garbage)
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    with pytest.raises(UnicodeDecodeError):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


async def test_import_device_preserves_original_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A failing rollback doesn't replace the validation diagnostic.

    If the YAML's permissions changed between write and cleanup
    (``unlink`` raises ``PermissionError``), the user should
    still see the actual validation rejection — not a confusing
    "permission denied" trace from the rollback path. The
    cleanup hook's exception is swallowed and logged; the
    original ``CommandError`` propagates.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esphome] required key not provided: a platform"}],
        }
    )

    # Make ``Path.unlink`` raise on the imported YAML so the
    # cleanup hook's executor call surfaces an exception inside
    # the helper's ``finally``.
    real_unlink = Path.unlink

    def boom_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == "kitchen.yaml":
            raise PermissionError("rollback denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", boom_unlink)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    # Original validation error survives — not a PermissionError
    # from the rollback path.
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "required key not provided: a platform" in excinfo.value.message


async def test_import_device_rolls_back_on_validator_subprocess_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A non-CommandError failure from the validator still rolls the YAML back.

    The validator subprocess can raise ``TimeoutError`` /
    ``RuntimeError`` / ``BrokenPipeError`` (or even an
    ``OSError`` from the post-write read) without going through
    ``CommandError``. Without a broad ``except`` the rollback
    would skip and the half-imported YAML would stick around,
    tripping ``FileExistsError`` on every retry — exactly the
    foot-gun this PR is meant to prevent.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=TimeoutError("subprocess wedged"))

    with pytest.raises(TimeoutError):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    # YAML must be unlinked even though the failure wasn't a CommandError.
    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


async def test_import_device_skips_validation_when_editor_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Editor not yet started → import proceeds without validation.

    Mirrors the boot-window guard the create / clone /
    edit_friendly_name paths already have. If the editor
    subprocess is unavailable, refusing every adoption for the
    lifetime of the process would be worse than landing the
    YAML and letting the next compile surface any schema issues.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor = None

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}
    assert (tmp_path / "kitchen.yaml").exists()


async def test_import_device_returns_even_when_post_scan_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A scan failure after a successful YAML write must not roll back.

    The YAML is on disk; failing the WS command would leave the user
    in a state where retrying produces ``FileExistsError`` despite
    nothing being wrong. Best-effort scan; the periodic poll picks up
    whatever this attempt missed.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._scanner.scan = AsyncMock(side_effect=RuntimeError("transient"))

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}


async def test_import_device_seeds_online_state_from_zeroconf_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A freshly-adopted device should land ONLINE without waiting for ping.

    The device was advertising on mDNS milliseconds ago — that's how
    it ended up on the discovery banner — so we already know it's
    reachable. ``import_device`` claims ONLINE via the state monitor
    (``mdns`` priority + ``claim=True`` so a later ping observation
    can't clobber it) and pulls the cached IP out of zeroconf so the
    new card has an address right away.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path)
    _seed_import_state(ctrl)
    ctrl._state_monitor = RecordingStateMonitor(
        cached_addresses={"kitchen.local": ["192.168.1.42"]}
    )

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    # Full call sequence — includes the post-apply probe_device the
    # previous MagicMock-based assertion silently let through.
    assert ctrl._state_monitor.calls == [
        ("apply", "kitchen", DeviceState.ONLINE, "mdns", True),
        ("get_cached_addresses", "kitchen.local"),
        ("apply_ip_addresses", "kitchen", ["192.168.1.42"]),
        ("probe_device", "kitchen", "kitchen"),
    ]


async def test_import_device_skips_apply_ip_when_zeroconf_cache_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """No cached IP → state still flips ONLINE, just no apply_ip call."""
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path)
    _seed_import_state(ctrl)
    ctrl._state_monitor = RecordingStateMonitor()  # no cached addresses

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert ctrl._state_monitor.calls == [
        ("apply", "kitchen", DeviceState.ONLINE, "mdns", True),
        ("get_cached_addresses", "kitchen.local"),
        ("probe_device", "kitchen", "kitchen"),
    ]


async def test_import_device_drops_matching_import_result_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """The discovery banner entry disappears the moment adoption finishes.

    Before this fix, the discovered card stuck around until the next
    discovery cycle filtered it out by name. Match the cache entry by
    ``package_import_url`` (which uniquely identifies the firmware)
    so we drop the right entry even when the user typed a different
    YAML name in the dialog.
    """
    monkeypatch.setattr(devices_module, "import_config", _import_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    captured = capture_devices_events(ctrl, EventType.IMPORTABLE_DEVICE_REMOVED)
    discovered = AdoptableDevice(
        name="apollo-plt-1-983300",
        friendly_name="Apollo PLT-1",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="26.3.2.1",
        network="wifi",
        ignored=False,
    )
    ctrl.state.import_result["apollo-plt-1-983300"] = discovered

    await ctrl.import_device(
        # User typed a shorter name (without the MAC suffix).
        name="apollo-plt-1",
        project_name="apollo.plt-1",
        package_import_url="github://apollo/plt-1.yaml",
    )

    assert "apollo-plt-1-983300" not in ctrl.state.import_result
    # Removal is broadcast so subscribed frontends drop the card.
    # Pin both count and payload so a future double-fire / regression
    # surfaces here — there's exactly one matching import_result entry,
    # so exactly one event should land on the bus.
    assert [(e.event_type, e.data) for e in captured] == [
        (EventType.IMPORTABLE_DEVICE_REMOVED, {"name": "apollo-plt-1-983300"})
    ]
