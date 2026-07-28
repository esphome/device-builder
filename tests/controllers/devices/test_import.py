"""Tests for the ``devices/import`` command path.

The normal adoption writes :func:`generate_adoption_yaml`'s shape
directly; only a ``?full_config`` import URL still delegates to
esphome's ``dashboard_import.import_config`` (it downloads and
rewrites the whole upstream YAML). When the target YAML already
exists the write raises ``FileExistsError``, re-surfaced as a
``CommandError`` so the dashboard can show a useful message.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.editor import ValidatorUnavailableError
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import AdoptableDevice, ErrorCode, EventType

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
    """Stub for ``import_config``, reached only via ``?full_config`` URLs.

    The real ``import_config`` writes a YAML to ``args[0]``; the
    post-write validation step reads it back, so the stub writes a
    minimal parseable YAML there and optionally records the call
    args into *captured*.
    """

    def _stub(*args: Any, **_kw: Any) -> None:
        if captured is not None:
            captured.setdefault("args", args)
        args[0].write_text(f"esphome:\n  name: {args[1]}\n", encoding="utf-8")

    return _stub


def test_import_config_resolves_at_import_time() -> None:
    """Regression guard for the upstream import path.

    ``import_config`` lives at ``esphome.components.dashboard_import``;
    if upstream moves it we want CI to fail loudly here, not at a
    user's first adoption attempt. The dashboard lazy-loads the
    module through ``async_import_module``, so this test imports
    it synchronously to verify the contract.
    """
    from esphome.components import dashboard_import  # noqa: PLC0415

    assert callable(dashboard_import.import_config)


async def test_import_device_writes_adoption_yaml_and_returns_path(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: write the adoption shape, run a scan, return the configuration name."""
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
    content = (tmp_path / "kitchen-1a2b3c.yaml").read_text(encoding="utf-8")
    assert "substitutions:" in content
    assert "  name: kitchen-1a2b3c" in content
    assert "  friendly_name: Kitchen" in content
    assert '  acme.kitchen: "github://acme/firmware.yaml@main"' in content
    assert "name_add_mac_suffix: false" in content
    assert "api:" in content  # encryption flag truthy → fresh key
    # No matching importable cache entry → fall back to wifi (legacy behaviour).
    assert "ssid: !secret wifi_ssid" in content
    # ``import_device`` calls ``scan()`` exactly once on the happy
    # path; pin the full call list so a regression that double-scans
    # (or sneaks in a stray ``reload``) breaks here instead of
    # silently passing the membership check.
    assert ctrl._scanner.calls == [("scan",)]


async def test_import_device_omits_wifi_for_ethernet_network(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An ESP32-PoE / Olimex broadcasts ``network=ethernet`` — preserve it.

    Hard-coding ``CONF_WIFI`` produced a YAML with a Wi-Fi template
    that the user had to fix by hand on every Ethernet adoption.
    Look up the discovered ``AdoptableDevice`` by the
    ``package_import_url`` the dialog passes and honour its
    ``network`` field.
    """
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

    assert "wifi" not in (tmp_path / "garage.yaml").read_text(encoding="utf-8")


async def test_import_device_uses_direct_name_lookup_with_duplicate_products(
    tmp_path: Path,
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
    content = (tmp_path / "apollo-plt-1-ddeeff.yaml").read_text(encoding="utf-8")
    assert "wifi" not in content


async def test_import_device_falls_back_to_wifi_for_old_factory_firmware(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Older factory firmwares didn't advertise ``network=`` — fall back to wifi.

    The TXT field ``network`` only became part of the dashboard_import
    discovery contract recently. A device whose mDNS broadcast omits
    it (``AdoptableDevice.network == ""``) shouldn't fail adoption —
    Wi-Fi is the historical default and matches what the legacy
    dashboard wrote.
    """
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

    assert "ssid: !secret wifi_ssid" in (tmp_path / "legacy-bulb.yaml").read_text(encoding="utf-8")


async def test_import_device_without_encryption_omits_api(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """No encryption flag in the broadcast → no ``api:`` block, matching upstream."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main",
        encryption=None,
    )

    assert "api:" not in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_import_device_full_config_url_delegates_to_dashboard_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``?full_config`` import URL still routes through esphome's ``import_config``.

    That variant downloads and rewrites the whole upstream YAML —
    machinery :func:`generate_adoption_yaml` deliberately doesn't
    reimplement.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "esphome.components.dashboard_import.import_config", _import_config_stub(captured)
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert captured["args"][4] == "github://x/y.yaml@main?full_config"


async def test_import_device_translates_file_exists_to_command_error(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``FileExistsError`` becomes a user-facing ``CommandError``.

    The WS layer turns generic exceptions into ``Command failed: …``;
    the dashboard's adopt dialog can't surface that meaningfully. The
    handler catches ``FileExistsError`` and re-raises as a
    ``CommandError`` carrying ``INVALID_ARGS`` and a message that
    names the offending file.
    """
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
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

    monkeypatch.setattr("esphome.components.dashboard_import.import_config", write_garbage)
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    with pytest.raises(UnicodeDecodeError):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x/y.yaml@main?full_config",
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


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("subprocess wedged"),
        ValidatorUnavailableError("closed stdout"),
        BrokenPipeError(),
    ],
)
async def test_import_device_keeps_yaml_when_validator_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    exc: Exception,
) -> None:
    """Adopt tolerates an unavailable validator: file kept, adoption completes, scan runs."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=exc)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}
    assert (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == [("scan",)]


async def test_import_device_propagates_generic_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A generic RuntimeError (a bug, not subprocess loss) propagates and rolls the YAML back."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=RuntimeError("unexpected bug"))

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


async def test_import_device_validates_with_short_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Adopt passes the short import budget so it isn't gated on a cold fetch."""
    from esphome_device_builder.controllers.editor import IMPORT_VALIDATE_TIMEOUT  # noqa: PLC0415

    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    validate = AsyncMock(return_value={"yaml_errors": [], "validation_errors": []})
    ctrl._db.editor.validate_yaml = validate

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert validate.await_args.kwargs["timeout"] == IMPORT_VALIDATE_TIMEOUT


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
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._scanner.scan = AsyncMock(side_effect=RuntimeError("transient"))

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}


async def test_import_device_applies_cached_ip_and_probes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Adopt applies the cached IP and probes; no fabricated state, the real sources decide."""
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

    assert ctrl._state_monitor.calls == [
        ("get_cached_addresses", "kitchen.local"),
        ("apply_ip_addresses", "kitchen", ["192.168.1.42"]),
        ("probe_device", "kitchen", "kitchen"),
        ("probe_device_ping", "kitchen"),
    ]


async def test_import_device_rename_seeds_ping_with_the_factory_broadcast(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A rename-during-adopt applies the factory name's cached IP and wakes ping to arbitrate."""
    ctrl = make_controller(tmp_path)
    _seed_import_state(ctrl)
    ctrl.state.import_result["apollo-plt-1-983300"] = AdoptableDevice(
        name="apollo-plt-1-983300",
        friendly_name="Apollo PLT-1",
        package_import_url="github://apollo/plt-1.yaml",
        project_name="apollo.plt-1",
        project_version="26.3.2.1",
        network="wifi",
        ignored=False,
    )
    ctrl._state_monitor = RecordingStateMonitor(
        cached_addresses={"apollo-plt-1-983300.local": ["192.168.1.77"]}
    )

    await ctrl.import_device(
        name="kitchen",
        project_name="apollo.plt-1",
        package_import_url="github://apollo/plt-1.yaml",
    )

    assert ctrl._state_monitor.calls == [
        ("get_cached_addresses", "apollo-plt-1-983300.local"),
        ("apply_ip_addresses", "kitchen", ["192.168.1.77"]),
        ("probe_device", "kitchen", "apollo-plt-1-983300"),
        ("probe_device_ping", "kitchen"),
    ]


async def test_import_device_skips_apply_ip_when_zeroconf_cache_misses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """No cached IP → probes still run, just no apply_ip call."""
    ctrl = make_controller(tmp_path)
    _seed_import_state(ctrl)
    ctrl._state_monitor = RecordingStateMonitor()  # no cached addresses

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert ctrl._state_monitor.calls == [
        ("get_cached_addresses", "kitchen.local"),
        ("probe_device", "kitchen", "kitchen"),
        ("probe_device_ping", "kitchen"),
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
