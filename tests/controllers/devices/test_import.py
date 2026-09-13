"""Tests for the ``devices/import`` command path.

The normal adoption writes :func:`generate_adoption_yaml`'s shape
directly; a ``?full_config`` import URL fetches the upstream YAML
and writes it, pinned to the device when it adds a MAC suffix. When
the target YAML already exists the write raises ``FileExistsError``,
re-surfaced as a ``CommandError`` so the dashboard can show a useful
message.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, Mock

import pytest

from esphome_device_builder.controllers.config.metadata import get_device_metadata
from esphome_device_builder.controllers.devices import DevicesController, importable
from esphome_device_builder.controllers.devices.mutations_yaml import (
    _INHERIT_ERROR_MARK,
    PackageWarning,
    ValidationVerdict,
)
from esphome_device_builder.controllers.editor import ValidatorTimeoutError
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.device_yaml import EsphomeConfigUnavailableError
from esphome_device_builder.helpers.yaml import YamlUpsertNotSupportedError
from esphome_device_builder.models import AdoptableDevice, ErrorCode, EventType
from tests.conftest import make_device

from .conftest import (
    ESPHOME_CONFIG_STUB_TARGET,
    VALIDATOR_OUTAGES,
    CaptureDevicesEventsFactory,
    MakeControllerFactory,
    MakeDbFactory,
    RecordingStateMonitor,
)


def _seed_import_state(controller: DevicesController) -> None:
    """Initialise ``import_result`` to an empty dict.

    ``import_device`` iterates ``import_result`` for the cached
    AdoptableDevice — production wires this up in ``__init__``,
    but the bypass-init factory leaves it unset.
    """
    controller.state.import_result = {}


def _full_config_stub(api_tail: str = "") -> AsyncMock:
    """Stub ``fetch_full_config`` returning an esphome header plus *api_tail*."""
    return AsyncMock(return_value=f"esphome:\n  name: kitchen\n{api_tail}")


async def _import_kitchen(ctrl: DevicesController, **overrides: Any) -> dict[str, Any]:
    """Adopt ``kitchen`` from the usual package URL with encryption on."""
    args: dict[str, Any] = {
        "name": "kitchen",
        "project_name": "x",
        "package_import_url": "github://x/y.yaml@main",
        "encryption": "true",
    }
    return await ctrl.import_device(**{**args, **overrides})


def _key_step_raising(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """Make the adoption's key step fail with *exc*."""
    monkeypatch.setattr(importable, "_finalize_adoption_key", AsyncMock(side_effect=exc))


def _deny_unlink_of(monkeypatch: pytest.MonkeyPatch, filename: str) -> None:
    """Make ``Path.unlink`` refuse *filename* only."""
    real_unlink = Path.unlink

    def _unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == filename:
            raise PermissionError("rollback denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)


def _boom(path: Path, content: str) -> None:
    raise OSError("disk full")


async def test_import_device_writes_adoption_yaml_and_returns_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Happy path: write the adoption shape, run a scan, return the configuration name."""
    monkeypatch.setattr(
        ESPHOME_CONFIG_STUB_TARGET,
        AsyncMock(return_value={"esphome": {"name": "kitchen-1a2b3c"}}),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
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
    # Production shape: the resolved package carries no encryption, so a
    # fresh key is minted post-validation.
    assert 'api:\n  encryption:\n    key: "' in content
    # No matching importable cache entry → fall back to wifi (legacy behaviour).
    assert "ssid: !secret wifi_ssid" in content
    # ``import_device`` calls ``scan()`` exactly once on the happy
    # path; pin the full call list so a regression that double-scans
    # (or sneaks in a stray ``reload``) breaks here instead of
    # silently passing the membership check.
    assert ctrl._scanner.calls == [("scan", False)]


async def test_import_device_omits_wifi_for_ethernet_network(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """An ESP32-PoE / Olimex broadcasts ``network=ethernet`` — preserve it.

    Hard-coding ``CONF_WIFI`` produced a YAML with a Wi-Fi template
    that the user had to fix by hand on every Ethernet adoption.
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
        name="olimex-poe-aabbcc",
        project_name="olimex.esp32-poe",
        package_import_url="github://olimex/esp32-poe.yaml",
    )

    assert "wifi" not in (tmp_path / "olimex-poe-aabbcc.yaml").read_text(encoding="utf-8")


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


async def test_import_device_full_config_url_fetches_and_writes_the_upstream_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``?full_config`` import writes the fetched YAML untouched when ``esphome:`` is packaged."""
    upstream = (
        "substitutions:\n  id: '1'\n  name: audio-${id}\n"
        "packages:\n  board: !include boards/rev2_4.yaml\n"
        "logger:\n  level: WARN\n"
    )
    fetch = AsyncMock(return_value=upstream)
    monkeypatch.setattr(importable, "fetch_full_config", fetch)
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    await ctrl.import_device(
        name="audio-33abec",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    fetch.assert_awaited_once_with("github://x/y.yaml@main?full_config")
    assert (tmp_path / "audio-33abec.yaml").read_text(encoding="utf-8") == upstream


async def test_import_device_full_config_fetch_failure_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A fetch that fails surfaces its typed error and leaves no file behind."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        AsyncMock(side_effect=CommandError(ErrorCode.UNAVAILABLE, "Could not fetch")),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x/y.yaml@main?full_config",
        )

    assert excinfo.value.code == ErrorCode.UNAVAILABLE
    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls == []


OTHER_KEY = base64.b64encode(b"o" * 32).decode()
PENDING_KEY = base64.b64encode(b"p" * 32).decode()


async def test_import_device_uses_pending_ha_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Adoption reuses the HA-provisioned key instead of minting a competing one."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main",
        encryption=None,
    )

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # The pending key forces the api block even without the mDNS encryption flag.
    assert f'    key: "{PENDING_KEY}"\n' in content
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_cancelled_during_the_key_step_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A command task cancelled mid key step leaves no half-adopted YAML behind."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    entered = asyncio.Event()

    async def _hang(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(importable, "_finalize_adoption_key", _hang)
    task = asyncio.create_task(_import_kitchen(ctrl))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_rollback_failure_keeps_the_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unlink that fails during the rollback names the stranded file next to the real error."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    _key_step_raising(monkeypatch, RuntimeError("real bug"))
    _deny_unlink_of(monkeypatch, "kitchen.yaml")

    with pytest.raises(CommandError) as excinfo:
        await _import_kitchen(ctrl)

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert "real bug" in excinfo.value.message
    assert "delete it before retrying" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "could not be removed" in caplog.text


def _rollback_dispatch_failing_with(exc: BaseException) -> Any:
    """Build an executor dispatch that fails only when the adoption rollback is dispatched."""
    real = importable.run_in_executor

    async def _dispatch(func: Any, *args: Any) -> Any:
        if func is importable._discard:
            raise exc
        return await real(func, *args)

    return _dispatch


async def test_import_device_rollback_dispatch_failure_keeps_the_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error while awaiting the rollback is logged and the stranded file named in the reply."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    _key_step_raising(monkeypatch, RuntimeError("real bug"))
    monkeypatch.setattr(
        importable, "run_in_executor", _rollback_dispatch_failing_with(RuntimeError("no pool"))
    )

    with pytest.raises(CommandError) as excinfo:
        await _import_kitchen(ctrl)

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert "real bug" in excinfo.value.message
    assert "delete it before retrying" in excinfo.value.message
    assert "did not complete" in caplog.text


async def test_import_device_cancellation_during_the_rollback_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A cancel that lands while the rollback runs is not swallowed into the original error."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    _key_step_raising(monkeypatch, RuntimeError("real bug"))
    monkeypatch.setattr(
        importable, "run_in_executor", _rollback_dispatch_failing_with(asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        await _import_kitchen(ctrl)


async def test_import_device_full_config_splices_pending_ha_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``?full_config`` import replaces the upstream literal key with HA's."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub('api:\n  encryption:\n    key: "OLDKEY=="\n'),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert f'key: "{PENDING_KEY}"' in content
    assert "OLDKEY" not in content
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_full_config_repairs_stale_ota_key_next_to_matching_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An api key already equal to HA's still gets a stale explicit ota key dropped."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub(
            f'api:\n  encryption:\n    key: "{PENDING_KEY}"\n'
            'ota:\n  - platform: esphome\n    encryption:\n      key: "OLDKEY=="\n'
        ),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert content.count(f'key: "{PENDING_KEY}"') == 1
    assert content.endswith("    encryption:\n")
    assert "OLDKEY" not in content


async def test_import_device_full_config_without_literal_key_leaves_yaml_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """No upstream ``api:`` block → YAML stays verbatim, key stays stored, user warned."""
    monkeypatch.setattr(importable, "fetch_full_config", _full_config_stub())
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert PENDING_KEY not in content
    assert "does not declare an api: block" in result["warning"]
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_mints_key_when_package_lacks_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The resolved package has no ``encryption:`` → legacy behaviour, mint a key."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    await _import_kitchen(ctrl)

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    resolve.assert_awaited_once()
    assert 'api:\n  encryption:\n    key: "' in content
    assert ctrl._db.editor.validate_yaml.await_count == 1


@pytest.mark.parametrize(
    "encryption_value",
    [pytest.param(None, id="bare_null"), pytest.param({}, id="empty_mapping")],
)
async def test_import_device_skips_mint_when_package_encrypts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    encryption_value: dict | None,
) -> None:
    """A package-provided ``encryption:`` means an NVS key may exist — never mint."""
    resolve = AsyncMock(return_value={"api": {"encryption": encryption_value}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    await _import_kitchen(ctrl)

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "api:" not in content
    assert "key:" not in content


async def test_import_device_skips_mint_when_package_ota_encryption_is_substituted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A whole OTA ``encryption:`` left as ``${…}`` may hide an own key; nothing is minted."""
    resolve = AsyncMock(
        return_value={
            "esphome": {"name": "kitchen"},
            "ota": [{"platform": "esphome", "encryption": "${ota_encryption}"}],
        }
    )
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    result = await _import_kitchen(ctrl)

    assert "own encryption key" in result["warning"]
    assert "api:" not in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_import_device_skips_mint_when_package_has_own_ota_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A package OTA key would have to match a baked api key, so nothing is minted."""
    resolve = AsyncMock(
        return_value={
            "esphome": {"name": "kitchen"},
            "ota": [{"platform": "esphome", "encryption": {"key": "OWNKEY=="}}],
        }
    )
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    result = await _import_kitchen(ctrl)

    assert "own encryption key" in result["warning"]
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "api:" not in content
    assert "key:" not in content


@pytest.mark.parametrize(
    "mode", [pytest.param("no_cli", id="no_cli"), pytest.param("unavailable", id="unavailable")]
)
async def test_import_device_unresolvable_package_ships_keyless_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    mode: str,
) -> None:
    """An unresolvable package whose unkeyed adoption validated clean never gets a mint."""
    resolve = AsyncMock(side_effect=EsphomeConfigUnavailableError("timed out"))
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    esphome_cmd = [] if mode == "no_cli" else ["esphome"]
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=esphome_cmd)
    _seed_import_state(ctrl)

    result = await _import_kitchen(ctrl)

    assert "could not be resolved" in result["warning"]
    assert "api:" not in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert resolve.await_count == (0 if mode == "no_cli" else 1)


_INHERIT_ERROR = (
    f"'ota' encryption has no key and there is no 'api' {_INHERIT_ERROR_MARK}; set one of them"
)
_BARE_OTA_PACKAGE: dict[str, Any] = {
    "esphome": {"name": "kitchen"},
    "api": {"reboot_timeout": "0s"},
    "ota": [{"platform": "esphome", "encryption": None}],
}
_DEFERRED_BARE_OTA_PACKAGE: dict[str, Any] = {**_BARE_OTA_PACKAGE, "time": "${time_block}"}
_LOADER_MERGES = [
    pytest.param(_BARE_OTA_PACKAGE, id="resolved"),
    pytest.param(_DEFERRED_BARE_OTA_PACKAGE, id="unresolved"),
]


def _package_error_verdict(content: str, *messages: str) -> dict[str, Any]:
    """Build a validator verdict whose errors all root at the packages entry."""
    return {
        "yaml_errors": [],
        "validation_errors": [_package_entry_error(content, m) for m in messages],
    }


def _validator_warning_until_keyed(
    keyed: Callable[[str], dict[str, Any]] | None = None,
    *,
    unkeyed_errors: Sequence[str] = (_INHERIT_ERROR,),
) -> Callable[..., Any]:
    """Build a validator stub: the unkeyed adoption fails in the package, *keyed* decides after."""

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, Any]:
        if "key:" in content and keyed:
            return keyed(content)
        return _package_error_verdict(content, *([] if "key:" in content else unkeyed_errors))

    return _validate


def _push_pending_key(ctrl: DevicesController, content: str) -> dict[str, Any]:
    """Return a clean keyed verdict after handing ``PENDING_KEY`` to the adoption."""
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    return {"yaml_errors": [], "validation_errors": []}


def _push_other_key_once(ctrl: DevicesController) -> tuple[Callable[..., Any], dict[str, Any]]:
    """Build a clean validator stub whose first call pushes ``OTHER_KEY`` through the handoff."""
    handoff: dict[str, Any] = {}

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, Any]:
        if not handoff:
            handoff.update(await ctrl.set_encryption_key(name="kitchen", key=OTHER_KEY))
        return {"yaml_errors": [], "validation_errors": []}

    return _validate, handoff


class _Adoption(NamedTuple):
    result: dict[str, Any]
    content: str
    ctrl: DevicesController
    subprocess: AsyncMock


async def _adopt_kitchen_with_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    *,
    loaded: dict[str, Any] | None,
    keyed: Callable[[DevicesController, str], dict[str, Any]] | None = None,
    unkeyed_errors: Sequence[str] = (_INHERIT_ERROR,),
) -> _Adoption:
    """Adopt ``kitchen`` with encryption on; *loaded* stubs the loader merge, the CLI is down."""
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.resolve.load_device_yaml", lambda path: loaded
    )
    subprocess = AsyncMock(side_effect=EsphomeConfigUnavailableError("invalid"))
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, subprocess)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        side_effect=_validator_warning_until_keyed(
            (lambda content: keyed(ctrl, content)) if keyed else None,
            unkeyed_errors=unkeyed_errors,
        )
    )
    result = await _import_kitchen(ctrl)
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    return _Adoption(result, content, ctrl, subprocess)


@pytest.mark.parametrize("loaded", _LOADER_MERGES)
async def test_import_device_bare_ota_package_mints_and_drops_the_stale_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    loaded: dict[str, Any],
) -> None:
    """A package that fails only for want of the api key gets one, and the warning goes."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path, monkeypatch, make_controller, loaded=loaded
    )

    assert 'api:\n  encryption:\n    key: "' in adoption.content
    assert "warning" not in adoption.result
    validate = adoption.ctrl._db.editor.validate_yaml
    assert validate.await_count == 2
    assert "key:" in validate.await_args.kwargs["content"]
    adoption.subprocess.assert_not_awaited()


@pytest.mark.parametrize("exc", VALIDATOR_OUTAGES)
async def test_import_device_keyed_recheck_outage_keeps_key_and_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    exc: Exception,
) -> None:
    """A resolved package still mints when the re-check can't run; the warning stays."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=_BARE_OTA_PACKAGE,
        keyed=Mock(side_effect=exc),
    )

    assert 'api:\n  encryption:\n    key: "' in adoption.content
    assert _INHERIT_ERROR in adoption.result["warning"]
    assert "could not be resolved" not in adoption.result["warning"]


async def test_import_device_resolved_package_keeps_key_under_an_unrelated_package_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A resolved package with a fault the key can't repair still mints; the keyed warning shows."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=_BARE_OTA_PACKAGE,
        keyed=lambda ctrl, content: {
            "yaml_errors": [],
            "validation_errors": [_package_entry_error(content, "gl-s10.yaml missing")],
        },
    )

    assert 'api:\n  encryption:\n    key: "' in adoption.content
    assert "gl-s10.yaml missing" in adoption.result["warning"]
    assert _INHERIT_ERROR not in adoption.result["warning"]


async def test_import_device_unparsable_adoption_never_mints_tentatively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Without a loader merge to read the guards off, the inherit error alone never mints."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path, monkeypatch, make_controller, loaded=None
    )

    assert "api:" not in adoption.content
    assert "could not be resolved" in adoption.result["warning"]
    assert adoption.ctrl._db.editor.validate_yaml.await_count == 1


async def test_import_device_keyed_recheck_bug_rolls_the_adoption_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A re-check failing for a reason that is not an outage is a bug: it surfaces, nothing kept."""
    with pytest.raises(RuntimeError, match="session gone"):
        await _adopt_kitchen_with_encryption(
            tmp_path,
            monkeypatch,
            make_controller,
            loaded=_BARE_OTA_PACKAGE,
            keyed=Mock(side_effect=RuntimeError("session gone")),
        )

    assert not (tmp_path / "kitchen.yaml").exists()


@pytest.mark.parametrize("loaded", _LOADER_MERGES)
async def test_import_device_keyed_recheck_hard_failure_surfaces_the_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    loaded: dict[str, Any],
) -> None:
    """A keyed YAML esphome refuses outright keeps the unkeyed file and reports esphome's error."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=loaded,
        keyed=lambda ctrl, content: {"yaml_errors": [], "validation_errors": [{"message": "boom"}]},
    )

    assert "api:" not in adoption.content
    assert "boom" in adoption.result["warning"]
    assert "Adopted without a key" in adoption.result["warning"]
    assert "could not be resolved" not in adoption.result["warning"]


@pytest.mark.parametrize(
    "keyed",
    [
        pytest.param(Mock(side_effect=ValidatorTimeoutError("slow")), id="outage"),
        pytest.param(
            lambda ctrl, content: {
                "yaml_errors": [],
                "validation_errors": [_package_entry_error(content, "gl-s10.yaml missing")],
            },
            id="package_confined_error",
        ),
    ],
)
async def test_import_device_unresolvable_package_keyless_when_keyed_check_cannot_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    keyed: Callable[[DevicesController, str], dict[str, Any]],
) -> None:
    """A tentative key lands only on a clean verdict; anything else keeps the file keyless."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path, monkeypatch, make_controller, loaded=_DEFERRED_BARE_OTA_PACKAGE, keyed=keyed
    )

    assert "api:" not in adoption.content
    assert "didn't validate" in adoption.result["warning"]
    assert "could not be resolved" in adoption.result["warning"]


async def test_import_device_unresolvable_package_mints_only_for_the_inherit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A package error that is not the missing api key never mints, even if it clears when keyed."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=_DEFERRED_BARE_OTA_PACKAGE,
        unkeyed_errors=("y.yaml does not exist",),
    )

    assert "api:" not in adoption.content
    assert "could not be resolved" in adoption.result["warning"]
    assert adoption.ctrl._db.editor.validate_yaml.await_count == 1


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        pytest.param({"api": {"encryption": None}}, None, id="package_api_encryption"),
        pytest.param(
            {"ota": [{"platform": "esphome", "encryption": {"key": "OTAKEY"}}]},
            "gives the OTA platform its own encryption key",
            id="package_ota_key",
        ),
    ],
)
async def test_import_device_unresolvable_package_guards_on_the_loader_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    package: dict[str, Any],
    expected: str | None,
) -> None:
    """The package-encryption guard reads the loader's merge when neither route resolves."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded={"packages": {"v": "github://x/y.yaml@main"}, "esphome": {}, **package},
    )

    assert "key:" not in adoption.content
    assert adoption.ctrl._db.editor.validate_yaml.await_count == 1
    assert "could not be resolved" not in adoption.result["warning"]
    assert _INHERIT_ERROR in adoption.result["warning"]
    if expected is not None:
        assert expected in adoption.result["warning"]


@pytest.mark.parametrize("loaded", _LOADER_MERGES)
async def test_import_device_key_pushed_during_the_keyed_check_wins_the_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    loaded: dict[str, Any],
) -> None:
    """A key Home Assistant pushes while the keyed YAML is checked replaces the minted one."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path, monkeypatch, make_controller, loaded=loaded, keyed=_push_pending_key
    )

    assert f'    key: "{PENDING_KEY}"\n' in adoption.content
    assert adoption.content.count("key:") == 1
    assert adoption.ctrl._pending_keys.get("kitchen") is None
    assert "warning" not in adoption.result


async def test_import_device_pending_key_lands_through_a_re_check_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An outage on the pushed key's re-check still writes and consumes it."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("esphome:\n  name: kitchen\n\napi:\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    ctrl._db.editor.validate_yaml = AsyncMock(
        side_effect=_validator_warning_until_keyed(
            Mock(side_effect=ValidatorTimeoutError("slow")), unkeyed_errors=()
        )
    )

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert f'key: "{PENDING_KEY}"' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "warning" not in result
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_second_push_keeps_the_package_exemption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A push replacing a baked key is re-checked with the adoption's own package exemption."""
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, AsyncMock())
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    real_get = ctrl._pending_keys.get
    peeks = 0

    def _push_during_validate(name: str) -> dict[str, str] | None:
        nonlocal peeks
        peeks += 1
        if peeks == 2:
            ctrl._pending_keys.set("kitchen", OTHER_KEY)
        return real_get(name)

    monkeypatch.setattr(ctrl._pending_keys, "get", _push_during_validate)

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, Any]:
        return {
            "yaml_errors": [],
            "validation_errors": [_package_entry_error(content, "gl-s10.yaml missing")],
        }

    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=_validate)

    result = await _import_kitchen(ctrl)

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert f'key: "{OTHER_KEY}"' in content
    assert "gl-s10.yaml missing" in result["warning"]
    assert "not applied" not in result["warning"]
    assert real_get("kitchen") is None


async def test_import_device_pushed_key_the_splice_refuses_keeps_the_minted_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A pushed key the line walker can't place leaves the minted key and the push in place."""
    real_splice = importable._splice_key
    monkeypatch.setattr(
        importable,
        "_splice_key",
        lambda content, key, **kw: (
            importable._SplicedKey(None, "no")
            if key == PENDING_KEY
            else real_splice(content, key, **kw)
        ),
    )

    adoption = await _adopt_kitchen_with_encryption(
        tmp_path, monkeypatch, make_controller, loaded=_BARE_OTA_PACKAGE, keyed=_push_pending_key
    )

    assert 'api:\n  encryption:\n    key: "' in adoption.content
    assert PENDING_KEY not in adoption.content
    assert adoption.ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}
    assert "stays stored" in adoption.result["warning"]


async def test_import_device_mints_when_every_one_of_many_complaints_is_the_inherit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Four inherit errors (one per OTA entry) still read as a package only missing the key."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=_DEFERRED_BARE_OTA_PACKAGE,
        unkeyed_errors=[_INHERIT_ERROR] * 4,
    )

    assert 'api:\n  encryption:\n    key: "' in adoption.content
    assert "warning" not in adoption.result
    assert adoption.ctrl._db.editor.validate_yaml.await_count == 2


async def test_import_device_unresolvable_package_with_a_second_complaint_never_mints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The inherit error beside any other package error is not a package only missing the key."""
    adoption = await _adopt_kitchen_with_encryption(
        tmp_path,
        monkeypatch,
        make_controller,
        loaded=_DEFERRED_BARE_OTA_PACKAGE,
        unkeyed_errors=[_INHERIT_ERROR, "gl-s10.yaml missing"],
    )

    assert "api:" not in adoption.content
    assert "could not be resolved" in adoption.result["warning"]
    assert adoption.ctrl._db.editor.validate_yaml.await_count == 1


@pytest.mark.parametrize(
    "baked", [pytest.param(True, id="baked"), pytest.param(False, id="minted")]
)
@pytest.mark.parametrize(
    "listed", [pytest.param(False, id="unlisted"), pytest.param(True, id="listed")]
)
async def test_import_device_holds_the_name_so_a_push_mid_adoption_is_stored_then_landed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    baked: bool,
    listed: bool,
) -> None:
    """A handoff during the key step stores its key instead of writing; the adoption lands it."""
    monkeypatch.setattr(
        ESPHOME_CONFIG_STUB_TARGET, AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    if baked:
        ctrl._pending_keys.set("kitchen", PENDING_KEY)
    if listed:
        ctrl._scanner._devices_by_name["kitchen"] = [make_device("kitchen")]
    validate, handoff = _push_other_key_once(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=validate)

    result = await _import_kitchen(ctrl)

    assert handoff["result"] == "stored"
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert f'key: "{OTHER_KEY}"' in content
    assert content.count("key:") == 1
    assert PENDING_KEY not in content
    assert ctrl._pending_keys.get("kitchen") is None
    assert "kitchen" not in ctrl.state.adopting
    assert "warning" not in result


async def test_import_device_rejects_a_second_adopt_while_the_first_is_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A duplicate adopt of a name mid-adoption is refused and leaves the first claim held."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    entered = asyncio.Event()

    async def _hang(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(importable, "_finalize_adoption_key", _hang)
    first = asyncio.create_task(_import_kitchen(ctrl))
    await entered.wait()

    with pytest.raises(CommandError) as excinfo:
        await _import_kitchen(ctrl)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "being adopted" in excinfo.value.message
    assert "kitchen" in ctrl.state.adopting
    assert (tmp_path / "kitchen.yaml").exists()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert "kitchen" not in ctrl.state.adopting


async def test_import_device_double_click_lands_one_adoption_and_refuses_the_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Two adopts of one name fired together land exactly one; the other is refused, not raced."""
    monkeypatch.setattr(
        ESPHOME_CONFIG_STUB_TARGET, AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    outcomes = await asyncio.gather(
        _import_kitchen(ctrl), _import_kitchen(ctrl), return_exceptions=True
    )

    landed = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, CommandError)]
    assert [o["configuration"] for o in landed] == ["kitchen.yaml"]
    assert [o.code for o in refused] == [ErrorCode.INVALID_ARGS]
    assert "being adopted" in refused[0].message
    assert 'api:\n  encryption:\n    key: "' in (tmp_path / "kitchen.yaml").read_text("utf-8")
    assert "kitchen" not in ctrl.state.adopting
    assert len(ctrl._scanner.calls) == 1


async def test_import_device_refused_duplicate_leaves_the_first_adoption_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A refused duplicate touches nothing: the first adoption lands its pending key as usual."""
    monkeypatch.setattr(
        ESPHOME_CONFIG_STUB_TARGET, AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    entered, release = asyncio.Event(), asyncio.Event()

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"yaml_errors": [], "validation_errors": []}

    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=_validate)
    first = asyncio.create_task(_import_kitchen(ctrl))
    await entered.wait()

    with pytest.raises(CommandError, match="being adopted"):
        await _import_kitchen(ctrl)

    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}
    assert ctrl._scanner.calls == []
    release.set()
    result = await first

    assert result == {"configuration": "kitchen.yaml"}
    assert f'key: "{PENDING_KEY}"' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert ctrl._pending_keys.get("kitchen") is None
    assert "kitchen" not in ctrl.state.adopting
    assert len(ctrl._scanner.calls) == 1
    with pytest.raises(CommandError, match="already exists"):
        await _import_kitchen(ctrl)


@pytest.mark.parametrize(
    "accepted", [pytest.param(True, id="accepted"), pytest.param(False, id="rejected")]
)
async def test_import_device_push_after_the_mint_declined_lands_only_through_the_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
    accepted: bool,
) -> None:
    """A key pushed after the mint declined is written only if esphome accepts it, and says so."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    async def _resolve(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await ctrl.set_encryption_key(name="kitchen", key=OTHER_KEY)
        return {
            "esphome": {"name": "kitchen"},
            "ota": [{"platform": "esphome", "encryption": {"key": "OWNKEY=="}}],
        }

    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, AsyncMock(side_effect=_resolve))
    rejected = {"yaml_errors": [], "validation_errors": [{"message": "[ota] keys must match"}]}
    ctrl._db.editor.validate_yaml = AsyncMock(
        side_effect=_validator_warning_until_keyed(
            lambda content: {"yaml_errors": [], "validation_errors": []} if accepted else rejected,
            unkeyed_errors=(),
        )
    )

    result = await _import_kitchen(ctrl)

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    if accepted:
        assert f'key: "{OTHER_KEY}"' in content
        assert ctrl._pending_keys.get("kitchen") is None
        assert "warning" not in result
    else:
        assert "key:" not in content
        assert ctrl._pending_keys.get("kitchen") == {"key": OTHER_KEY}
        assert "own encryption key" in result["warning"]
        assert "keys must match" in result["warning"]
        assert "not applied" in result["warning"]


async def test_import_device_same_key_re_pushed_with_a_mac_is_not_a_newer_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Re-pushing the pending key with a MAC mid-adoption changes nothing and warns of nothing."""
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, AsyncMock())
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    handoff: dict[str, Any] = {}

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, Any]:
        if not handoff:
            handoff.update(
                await ctrl.set_encryption_key(
                    name="kitchen", key=PENDING_KEY, mac="AA:BB:CC:DD:EE:FF"
                )
            )
        return {"yaml_errors": [], "validation_errors": []}

    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=_validate)

    result = await _import_kitchen(ctrl)

    assert handoff["result"] == "stored"
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert content.count(f'key: "{PENDING_KEY}"') == 1
    assert ctrl._pending_keys.get("kitchen") is None
    assert "warning" not in result


async def test_import_device_releases_the_name_claim_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The name claim never outlives a failed adoption."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    _key_step_raising(monkeypatch, RuntimeError("real bug"))

    with pytest.raises(RuntimeError, match="real bug"):
        await _import_kitchen(ctrl)

    assert "kitchen" not in ctrl.state.adopting
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_claim_spans_the_create_and_the_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """The name is held before the YAML exists and until the rollback has removed it."""
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, AsyncMock())
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    claimed: list[str] = []
    real_create = importable.atomic_write_exclusive
    real_discard = importable._discard

    def _create(path: Path, data: bytes) -> None:
        claimed.append(f"create:{'kitchen' in ctrl.state.adopting}")
        real_create(path, data)

    def _discard(path: Path) -> None:
        claimed.append(f"discard:{'kitchen' in ctrl.state.adopting}")
        real_discard(path)

    monkeypatch.setattr(importable, "atomic_write_exclusive", _create)
    monkeypatch.setattr(importable, "_discard", _discard)
    monkeypatch.setattr(
        importable, "_finalize_adoption_key", AsyncMock(side_effect=RuntimeError("real bug"))
    )

    with pytest.raises(RuntimeError, match="real bug"):
        await _import_kitchen(ctrl)

    assert claimed == ["create:True", "discard:True"]
    assert "kitchen" not in ctrl.state.adopting


async def test_import_device_existing_file_is_not_rolled_back(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A refused exclusive create leaves the file that was already there untouched."""
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    with pytest.raises(CommandError):
        await ctrl.import_device(name="kitchen", project_name="x", package_import_url="github://x")

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == "esphome:\n  name: kitchen\n"
    assert "kitchen" not in ctrl.state.adopting


async def test_import_device_warns_when_a_push_lands_during_the_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A key pushed while the keyed YAML is being written stays stored and is reported."""
    monkeypatch.setattr(
        ESPHOME_CONFIG_STUB_TARGET, AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    real_write = ctrl._write_yaml_atomic_async

    async def _write(path: Path, content: str) -> None:
        await ctrl.set_encryption_key(name="kitchen", key=OTHER_KEY)
        await real_write(path, content)

    monkeypatch.setattr(ctrl, "_write_yaml_atomic_async", _write)

    result = await _import_kitchen(ctrl)

    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert OTHER_KEY not in content
    assert ctrl._pending_keys.get("kitchen") == {"key": OTHER_KEY}
    assert "while the config was being written" in result["warning"]


async def test_import_device_pending_key_skips_package_resolve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A pending HA key is baked directly; no resolve subprocess runs."""
    resolve = AsyncMock()
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    await _import_kitchen(ctrl)

    resolve.assert_not_awaited()
    assert f'    key: "{PENDING_KEY}"\n' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_import_device_full_config_indirected_key_warns_and_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An upstream ``!secret`` key IS competing; warn and keep the pending key."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("api:\n  encryption:\n    key: !secret api_key\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "supplies its own API encryption key" in result["warning"]
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "!secret api_key" in content
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_keeps_a_key_pushed_while_the_splice_was_in_flight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A push that lands mid-splice is newer than the spliced key and is the one written."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub('api:\n  encryption:\n    key: "OLDKEY=="\n'),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    real_splice = importable._splice_pending_key_validated

    async def splice_then_push(*args: Any, **kwargs: Any) -> Any:
        outcome = await real_splice(*args, **kwargs)
        ctrl._pending_keys.set("kitchen", OTHER_KEY)
        return outcome

    monkeypatch.setattr(importable, "_splice_pending_key_validated", splice_then_push)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "warning" not in result
    assert f'key: "{OTHER_KEY}"' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_full_config_keeps_an_own_ota_key_and_the_pending_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A runtime api key next to the OTA platform's own key refuses the splice; both kept."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub(
            "api:\n  encryption:\nota:\n  - platform: esphome\n"
            "    encryption:\n      key: OWNKEY==\n"
        ),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "own encryption key" in result["warning"]
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "key: OWNKEY==" in content
    assert PENDING_KEY not in content
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_full_config_splice_that_fails_validation_keeps_both(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A spliced import esphome rejects keeps the verbatim file and the pending key."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub(
            'api:\n  encryption:\n    key: "OLDKEY=="\nota: !include common/ota.yaml\n'
        ),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    ok = {"yaml_errors": [], "validation_errors": []}
    bad = {"yaml_errors": [], "validation_errors": [{"message": "keys must match"}]}
    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=[ok, bad])

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "keys must match" in result["warning"]
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "OLDKEY==" in content
    assert PENDING_KEY not in content
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_full_config_inserts_key_under_bare_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A keyless upstream ``encryption:`` gets the pending key inserted, not dropped."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("api:\n  encryption:\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "warning" not in result
    content = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert f'    key: "{PENDING_KEY}"' in content
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_full_config_flow_style_encryption_warns_and_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A flow-style upstream ``encryption:`` can't be edited; warn and keep the key."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("api:\n  encryption: {key: OLDKEY==}\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "warning" in result
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_joins_validation_and_key_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A validation warning must not swallow the key-not-applied warning."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("api:\n  encryption:\n    key: !secret api_key\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    ctrl._validate_rewritten_yaml_or_raise = AsyncMock(  # type: ignore[method-assign]
        return_value=ValidationVerdict(
            PackageWarning("Imported, but the remote package didn't validate.", False)
        )
    )

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "remote package didn't validate" in result["warning"]
    assert "supplies its own API encryption key" in result["warning"]


async def test_import_device_full_config_equal_key_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Upstream already carries the pending key verbatim → no rewrite, entry consumed."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub(f'api:\n  encryption:\n    key: "{PENDING_KEY}"\n'),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "warning" not in result
    assert ctrl._pending_keys.get("kitchen") is None


async def test_import_device_full_config_splice_write_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A failed splice write cleans up the half-imported YAML."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub('api:\n  encryption:\n    key: "OLDKEY=="\n'),
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.write_user_yaml", _boom
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    with pytest.raises(OSError, match="disk full"):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x/y.yaml@main?full_config",
        )

    assert not (tmp_path / "kitchen.yaml").exists()
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_mint_write_failure_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A failed mint write cleans up the half-imported YAML."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.write_user_yaml", _boom
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    with pytest.raises(OSError, match="disk full"):
        await _import_kitchen(ctrl)

    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_mint_round_trip_failure_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A mint whose splice doesn't read back ships keyless with a warning."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.importable.upsert_api_encryption_key",
        lambda content, key: content + "# junk\n",
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    result = await _import_kitchen(ctrl)

    assert "could not be spliced" in result["warning"]
    assert "key:" not in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")


async def test_import_mint_refused_by_own_ota_key_ships_keyless_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A splice the yaml helper refuses ships keyless with its reason, file kept."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)

    def _refuse(content: str, key: str) -> str:
        raise YamlUpsertNotSupportedError(
            "the config gives the OTA platform its own encryption key"
        )

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.importable.upsert_api_encryption_key", _refuse
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)

    result = await _import_kitchen(ctrl)

    assert "own encryption key" in result["warning"]
    assert (tmp_path / "kitchen.yaml").exists()


async def test_import_device_full_config_splice_round_trip_failure_keeps_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A splice whose result doesn't read back warns and keeps the pending key."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        _full_config_stub("api:\n  encryption:\n"),
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.importable.upsert_api_encryption_key",
        lambda content, key: content + "# junk\n",
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x/y.yaml@main?full_config",
    )

    assert "defeated the key splice" in result["warning"]
    assert PENDING_KEY not in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


async def test_import_device_push_during_validate_window_never_mints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A key pushed after the generate-time peek still blocks a competing mint."""
    resolve = AsyncMock(return_value={"esphome": {"name": "kitchen"}})
    monkeypatch.setattr(ESPHOME_CONFIG_STUB_TARGET, resolve)
    ctrl = make_controller(tmp_path, with_state_monitor=True, esphome_cmd=["esphome"])
    _seed_import_state(ctrl)
    real_get = ctrl._pending_keys.get
    peeks: list[str] = []

    def _late_get(name: str) -> dict[str, str] | None:
        peeks.append(name)
        if len(peeks) == 2:  # the generate-time peek missed the in-flight push
            ctrl._pending_keys.set("kitchen", PENDING_KEY)
        return real_get(name)

    monkeypatch.setattr(ctrl._pending_keys, "get", _late_get)

    result = await _import_kitchen(ctrl)

    # No competing mint: the late key wins the finalize re-peek and is
    # spliced into the generated YAML, which gains its api: block.
    resolve.assert_not_awaited()
    assert f'    key: "{PENDING_KEY}"\n' in (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    assert "warning" not in result
    assert real_get("kitchen") is None


async def test_import_device_validation_failure_keeps_pending_key(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A rolled-back import keeps the pending key so a retry still uses it."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._pending_keys.set("kitchen", PENDING_KEY)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "boom"}],
        }
    )

    with pytest.raises(CommandError):
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert ctrl._pending_keys.get("kitchen") == {"key": PENDING_KEY}


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
    """Imported YAML failing schema validation is deleted and the editor errors surface."""
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
    # The file was rolled back, so the copy must not point at an editor.
    assert "editor" not in excinfo.value.message
    # YAML rolled back so a retry doesn't trip ``FileExistsError``.
    assert not (tmp_path / "kitchen.yaml").exists()
    # Scanner must NOT have been notified of the half-imported device.
    assert ctrl._scanner.calls == []


def _package_entry_error(content: str, message: str) -> dict[str, Any]:
    """Build a validation error rooted at the packages entry line."""
    lines = content.splitlines()
    packages_line = next(i for i, line in enumerate(lines) if line.startswith("packages:"))
    return {
        "message": message,
        "range": {
            "document": "<file>",
            "start_line": packages_line + 1,
            "start_col": 2,
            "end_line": packages_line + 3,
            "end_col": 0,
        },
    }


async def test_import_device_keeps_yaml_when_only_the_package_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """Errors rooted inside the ``packages:`` block keep the file and return a warning."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "yaml_errors": [],
            "validation_errors": [
                _package_entry_error(content, "gl-s10.yaml does not exist in repository"),
            ],
        }

    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=_validate)

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result["configuration"] == "kitchen.yaml"
    assert "does not exist in repository" in result["warning"]
    assert (tmp_path / "kitchen.yaml").exists()
    assert ctrl._scanner.calls != []


async def test_import_device_keeps_yaml_when_the_error_roots_in_the_package_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A resolved package whose content fails validation still keeps the file.

    The validator marks such errors with the package cache file's
    document, at whatever line the remote content puts them.
    """
    from esphome.core import CORE  # noqa: PLC0415

    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    package_doc = str(Path(CORE.data_dir) / "packages" / "ab12cd34" / "gl-s10.yaml")
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {
                    "message": "[sensor] required key not provided",
                    "range": {
                        "document": package_doc,
                        "start_line": 41,
                        "start_col": 0,
                        "end_line": 41,
                        "end_col": 10,
                    },
                },
            ],
        }
    )

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert "required key not provided" in result["warning"]
    assert (tmp_path / "kitchen.yaml").exists()


async def test_import_device_refuses_when_the_error_roots_in_secrets_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A document outside the package cache (secrets.yaml) fails closed."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {
                    "message": "[wifi] password too short",
                    "range": {
                        "document": str(tmp_path / "secrets.yaml"),
                        "start_line": 1,
                        "start_col": 0,
                        "end_line": 1,
                        "end_col": 10,
                    },
                },
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
    assert "[wifi] password too short (secrets.yaml)" in excinfo.value.message
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_refuses_when_an_error_roots_outside_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An error outside the ``packages:`` span keeps the conservative refusal."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)

    async def _validate(
        *, configuration: str, content: str, timeout: float | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            "yaml_errors": [],
            "validation_errors": [
                _package_entry_error(content, "gl-s10.yaml does not exist in repository"),
                {
                    "message": "[esphome] invalid key",
                    "range": {"start_line": 0, "start_col": 0, "end_line": 0, "end_col": 0},
                },
            ],
        }

    ctrl._db.editor.validate_yaml = AsyncMock(side_effect=_validate)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_full_config_never_gets_the_package_exemption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``?full_config`` import refuses even for packages-rooted errors."""
    monkeypatch.setattr(
        importable,
        "fetch_full_config",
        AsyncMock(return_value="packages:\n  base: github://acme/base.yaml\nesphome:\n  name: x\n"),
    )
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {
                    "message": "base.yaml does not exist in repository",
                    "range": {
                        "document": "<file>",
                        "start_line": 1,
                        "start_col": 2,
                        "end_line": 2,
                        "end_col": 0,
                    },
                },
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://acme/full.yaml?full_config",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_refuses_when_an_error_has_no_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An error without a range can't be classified; fail closed."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "gl-s10.yaml does not exist in repository"}],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert not (tmp_path / "kitchen.yaml").exists()


async def test_import_device_validation_message_collapses_the_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """An esphome error ending in a period doesn't double up against the tail."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [
                {"message": "gl-s10.yaml does not exist in repository."},
            ],
        }
    )

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert "repository. The import was rolled back" in excinfo.value.message
    assert ".." not in excinfo.value.message


async def test_import_device_names_the_stranded_file_when_the_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_controller: MakeControllerFactory,
) -> None:
    """A rollback the filesystem refuses keeps the validation error and names the leftover."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    ctrl._db.editor.validate_yaml = AsyncMock(
        return_value={
            "yaml_errors": [],
            "validation_errors": [{"message": "[esphome] required key not provided: a platform"}],
        }
    )
    _deny_unlink_of(monkeypatch, "kitchen.yaml")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
    assert "required key not provided" in excinfo.value.message
    assert "delete it before retrying" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, CommandError)


@pytest.mark.parametrize("exc", VALIDATOR_OUTAGES)
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
    assert ctrl._scanner.calls == [("scan", False)]


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


async def test_import_device_clears_metadata_left_under_the_adopted_filename(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Metadata an archived device left under the filename does not bind to the adopted one."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    await ctrl._persist_device_metadata_async(
        "kitchen.yaml",
        board_id="esp32dev",
        board_id_user_set=True,
        labels=["old"],
        expected_config_hash="stale",
    )

    await ctrl.import_device(name="kitchen", project_name="x", package_import_url="github://x")

    assert ctrl._metadata_store.get("kitchen.yaml") == {}
    assert await asyncio.to_thread(get_device_metadata, tmp_path, "kitchen.yaml") == {}


async def test_import_device_retires_its_row_even_when_the_scan_fails(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    capture_devices_events: CaptureDevicesEventsFactory,
) -> None:
    """A failed post-write scan still drops the adopted name's row, and only that row."""
    ctrl = make_controller(tmp_path, with_state_monitor=True)
    _seed_import_state(ctrl)
    for suffix in ("aabbcc", "ddeeff"):
        ctrl.state.import_result[f"apollo-plt-1-{suffix}"] = AdoptableDevice(
            name=f"apollo-plt-1-{suffix}",
            friendly_name="Apollo PLT-1",
            package_import_url="github://apollo/plt-1.yaml",
            project_name="apollo.plt-1",
            project_version="26.3.2.1",
            network="wifi",
            ignored=False,
        )
    captured = capture_devices_events(ctrl, EventType.IMPORTABLE_DEVICE_REMOVED)
    ctrl._scanner.scan = AsyncMock(side_effect=OSError("scan broke"))

    await ctrl.import_device(
        name="apollo-plt-1-ddeeff",
        project_name="apollo.plt-1",
        package_import_url="github://apollo/plt-1.yaml",
    )

    assert list(ctrl.state.import_result) == ["apollo-plt-1-aabbcc"]
    assert [e.data for e in captured] == [{"name": "apollo-plt-1-ddeeff"}]
    assert ctrl._state_monitor.calls == []


async def test_import_device_probe_rides_the_real_scan(
    tmp_path: Path, make_db: MakeDbFactory
) -> None:
    """A real scan over the freshly written YAML emits ADDED, which probes the adopted name."""
    db = make_db(tmp_path)
    db.settings.rel_path = lambda configuration: tmp_path / configuration
    db.editor.validate_yaml = AsyncMock(return_value={"yaml_errors": [], "validation_errors": []})
    db.version_history = None
    ctrl = DevicesController(db)
    ctrl._state_monitor = RecordingStateMonitor()  # type: ignore[assignment]

    await ctrl.import_device(name="kitchen", project_name="x", package_import_url="github://x")

    probes = [c for c in ctrl._state_monitor.calls if c[0].startswith("probe_")]
    assert probes == [("probe_device", "kitchen"), ("probe_device_ping", "kitchen")]
    assert ctrl.get_by_configuration("kitchen.yaml") is not None


async def test_import_device_leaves_probing_to_the_scan(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Adopt touches no monitor state itself; the scan's ADDED handler probes the device."""
    ctrl = make_controller(tmp_path)
    _seed_import_state(ctrl)
    ctrl._state_monitor = RecordingStateMonitor(cached_addresses={"kitchen.local": ["10.0.0.9"]})

    await ctrl.import_device(name="kitchen", project_name="x", package_import_url="github://x")

    assert ctrl._state_monitor.calls == []
    assert ctrl._scanner.calls == [("scan", False)]
