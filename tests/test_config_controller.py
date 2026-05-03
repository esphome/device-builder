"""Tests for ``controllers/config.py`` — settings + metadata sidecar.

This module fronts the ``.device-builder.json`` metadata file
(per-device board_id / friendly_name / IP / expected_config_hash
plus the user preferences blob) and a small WS surface
(``config/get_preferences`` / ``config/set_preferences`` /
``config/get_secrets`` / ``config/get_info``).

Three coverage targets:

* ``metadata_transaction`` — the atomic RMW context the rest of
  the package uses. Persists via tempfile + ``os.replace`` so
  lock-free readers never observe a torn write; failures inside
  the block discard the pending mutation.
* The partial-update branches of ``set_device_metadata`` —
  empty-string sentinels for ``ip`` (skip) and
  ``expected_config_hash`` (clear) are easy to swap by accident
  during refactor.
* The ``ConfigController`` WS commands. They all use
  ``loop.run_in_executor`` so a future regression that drops the
  executor wrap would stall the dashboard; the suite's
  blockbuster fixture catches that on Linux CI as long as the
  paths are exercised at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from esphome.util import SerialPort

from esphome_device_builder.controllers.config import (
    ConfigController,
    _load_metadata,
    _save_metadata,
    get_device_ip,
    get_device_metadata,
    load_preferences,
    metadata_transaction,
    remove_device_metadata,
    save_preferences,
    set_device_metadata,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from esphome_device_builder.models.preferences import (
    DashboardView,
    Theme,
    UserPreferences,
)

from ._storage_fixtures import write_storage_json
from .conftest import MakeSettingsFactory


def _make_controller(config_dir: Path) -> ConfigController:
    """Bypass __init__ chains; attach a stub DeviceBuilder.settings."""
    controller = ConfigController.__new__(ConfigController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._db.settings.absolute_config_dir = config_dir.resolve()
    controller._db.settings.rel_path = config_dir.joinpath
    return controller


# ---------------------------------------------------------------------------
# metadata_transaction round-trips
# ---------------------------------------------------------------------------


def test_metadata_transaction_persists_changes(tmp_path: Path) -> None:
    """The RMW context writes mutations back to disk on clean exit."""
    with metadata_transaction(tmp_path) as data:
        data["kitchen.yaml"] = {"board_id": "esp32"}

    raw = json.loads((tmp_path / ".device-builder.json").read_bytes())
    assert raw == {"kitchen.yaml": {"board_id": "esp32"}}


def test_metadata_transaction_discards_changes_on_exception(tmp_path: Path) -> None:
    """A raise inside the block drops the pending mutation.

    The atomic-write happens on clean exit; if the block raises,
    we never call ``_save_metadata``. Without this guarantee, a
    half-applied update could land on disk and confuse the next
    reader.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"kitchen.yaml": {"ip": "10.0.0.1"}}')

    with pytest.raises(RuntimeError, match="boom"), metadata_transaction(tmp_path) as data:
        data["kitchen.yaml"]["ip"] = "10.0.0.2"
        raise RuntimeError("boom")

    # Original content survives untouched.
    assert json.loads(metadata_path.read_bytes()) == {"kitchen.yaml": {"ip": "10.0.0.1"}}


def test_load_metadata_returns_empty_when_missing(tmp_path: Path) -> None:
    """No file → empty dict. The most-common state on a fresh install."""
    assert _load_metadata(tmp_path) == {}


def test_load_metadata_returns_empty_on_invalid_json(tmp_path: Path) -> None:
    """A corrupted JSON file falls back to empty rather than raising.

    A user (or a botched migration) leaving truncated JSON on
    disk shouldn't crash the dashboard at startup — every reader
    would suddenly see ``JSONDecodeError`` from a path called
    deep inside the executor.
    """
    (tmp_path / ".device-builder.json").write_bytes(b'{"truncated":')
    assert _load_metadata(tmp_path) == {}


def test_save_metadata_uses_atomic_replace(tmp_path: Path) -> None:
    """Tempfile + ``os.replace`` so concurrent readers never see a torn write.

    Pin the rename behaviour: after ``_save_metadata`` the
    target file holds the new content and the temp file is gone.
    """
    _save_metadata(tmp_path, {"a.yaml": {"board_id": "esp32"}})

    target = tmp_path / ".device-builder.json"
    assert target.exists()
    assert json.loads(target.read_bytes()) == {"a.yaml": {"board_id": "esp32"}}
    # No leftover .tmp files in the dir.
    assert not list(tmp_path.glob(".device-builder.json.*.tmp"))


def test_save_metadata_cleans_up_tmpfile_on_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """If ``os.replace`` fails, the partial tempfile is unlinked.

    Otherwise repeated failures would litter the config dir with
    ``.device-builder.json.<random>.tmp`` files that nothing
    cleans up.
    """

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("rename failed")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="rename failed"):
        _save_metadata(tmp_path, {"a.yaml": {}})

    # Cleanup unlinked the tempfile — directory is back to empty.
    assert not list(tmp_path.glob(".device-builder.json.*.tmp"))


# ---------------------------------------------------------------------------
# set_device_metadata / get_* / remove_device_metadata
# ---------------------------------------------------------------------------


def test_set_device_metadata_partial_update(tmp_path: Path) -> None:
    """Only fields explicitly passed are changed; others survive.

    Each setter argument defaults to ``None`` and the function
    only writes when the caller passes a non-None value.
    Refactor that flips the truthiness check would silently wipe
    every other field on every update.
    """
    set_device_metadata(
        tmp_path,
        "kitchen.yaml",
        board_id="esp32-c3-devkitm-1",
        friendly_name="Kitchen",
        ip="10.0.0.1",
    )
    set_device_metadata(tmp_path, "kitchen.yaml", board_id="esp32-c6")

    entry = get_device_metadata(tmp_path, "kitchen.yaml")
    assert entry["board_id"] == "esp32-c6"
    assert entry["friendly_name"] == "Kitchen"
    assert entry["ip"] == "10.0.0.1"


def test_set_device_metadata_skips_empty_ip(tmp_path: Path) -> None:
    """``ip=""`` is the "leave alone" sentinel, not "clear".

    mDNS clears the in-memory IP whenever a device drops off
    the network, but the persisted cache is still useful — the
    next probe sweep can reuse it. Passing an empty string lets
    the controller blanket-call ``set_device_metadata`` without
    having to branch on whether the device is online.
    """
    set_device_metadata(tmp_path, "kitchen.yaml", ip="10.0.0.1")
    set_device_metadata(tmp_path, "kitchen.yaml", ip="")

    assert get_device_ip(tmp_path, "kitchen.yaml") == "10.0.0.1"


def test_set_device_metadata_clears_expected_config_hash_on_empty(
    tmp_path: Path,
) -> None:
    """``expected_config_hash=""`` actively clears the field.

    Different sentinel from ``ip`` because the use case is
    different: when the user edits a YAML, the previous compile's
    expected_config_hash is stale and must be cleared. Passing
    ``""`` is the explicit clear path; ``None`` means "no
    change".
    """
    set_device_metadata(tmp_path, "kitchen.yaml", expected_config_hash="abc12345")
    set_device_metadata(tmp_path, "kitchen.yaml", expected_config_hash="")

    assert "expected_config_hash" not in get_device_metadata(tmp_path, "kitchen.yaml")


def test_remove_device_metadata_clears_only_target(tmp_path: Path) -> None:
    """Removing one device's entry leaves siblings intact."""
    set_device_metadata(tmp_path, "a.yaml", board_id="esp32")
    set_device_metadata(tmp_path, "b.yaml", board_id="esp8266")

    remove_device_metadata(tmp_path, "a.yaml")

    assert get_device_metadata(tmp_path, "a.yaml") == {}
    assert get_device_metadata(tmp_path, "b.yaml") == {"board_id": "esp8266"}


def test_load_preferences_returns_defaults_on_missing(tmp_path: Path) -> None:
    """A fresh install has no preferences — fall back to the default object.

    Equality check (not just ``isinstance``) so a regression
    that builds a non-default preferences object on the
    missing-file path (e.g. one with ``dashboard_view=TABLE``
    instead of CARDS) breaks this test.
    """
    assert load_preferences(tmp_path) == UserPreferences()


def test_load_preferences_returns_defaults_on_bad_data(tmp_path: Path) -> None:
    """Corrupted preferences blob → default object, not partial recovery.

    ``UserPreferences.from_dict`` raises on unknown / malformed
    fields; without the except-fallback the dashboard wouldn't
    load when an older version's preferences file is read by a
    newer mashumaro schema. Equality with ``UserPreferences()``
    pins that the fallback is the same default object, not a
    silently-mutated one a regression could produce.
    """
    metadata_path = tmp_path / ".device-builder.json"
    metadata_path.write_bytes(b'{"_preferences": {"unknown_field": 42}}')

    assert load_preferences(tmp_path) == UserPreferences()


def test_save_preferences_round_trip(tmp_path: Path) -> None:
    """A non-default prefs blob round-trips through save → load.

    Pins the actual write path: round-tripping ``UserPreferences()``
    would also pass if save / load both silently lost data, so
    use a non-default value (``dashboard_view=TABLE``) to
    actually exercise the marshalling.
    """
    prefs = UserPreferences(dashboard_view=DashboardView.TABLE)
    save_preferences(tmp_path, prefs)
    assert load_preferences(tmp_path) == prefs


# ---------------------------------------------------------------------------
# ConfigController WS commands — verifies file I/O runs off the event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_prefs_returns_loaded_preferences(tmp_path: Path) -> None:
    """``get_prefs`` returns the persisted blob, not a fresh default.

    Persists a non-default preferences object and asserts it
    round-trips back. A regression that bypasses disk I/O and
    just constructs ``UserPreferences()`` would still claim
    ``isinstance`` but would fail this equality.

    The seeding ``save_preferences`` runs in a thread because
    ``metadata_transaction`` -> ``tempfile.mkstemp`` ->
    ``os.path.abspath`` blocks the event loop, and blockbuster
    flags it from inside an async test.
    """
    persisted = UserPreferences(dashboard_view=DashboardView.TABLE)
    await asyncio.to_thread(save_preferences, tmp_path, persisted)
    controller = _make_controller(tmp_path)

    prefs = await controller.get_prefs()
    assert prefs == persisted


@pytest.mark.asyncio
async def test_set_prefs_merges_partial_update(tmp_path: Path) -> None:
    """Partial-update merge: only the supplied field changes.

    Persist a known initial state, then call ``set_prefs`` with
    just one field. The unrelated fields must keep their
    persisted values, not snap back to dataclass defaults — a
    regression that re-constructs ``UserPreferences`` from
    kwargs alone (skipping the merge step) would clobber them
    silently.

    Seeding goes via ``asyncio.to_thread`` so the
    ``metadata_transaction`` -> ``tempfile.mkstemp`` write
    doesn't trip blockbuster on Linux CI.
    """
    initial = UserPreferences(dashboard_view=DashboardView.TABLE, theme=Theme.DARK)
    await asyncio.to_thread(save_preferences, tmp_path, initial)
    controller = _make_controller(tmp_path)

    # Update only ``theme``; ``dashboard_view`` should survive.
    result = await controller.set_prefs(theme=Theme.LIGHT)
    assert result.theme == Theme.LIGHT
    assert result.dashboard_view == DashboardView.TABLE
    # Persisted blob matches the merged state.
    persisted = await asyncio.to_thread(load_preferences, tmp_path)
    assert persisted == result


@pytest.mark.asyncio
async def test_get_secrets_returns_empty_when_missing(tmp_path: Path) -> None:
    """No secrets.yaml → empty list, not a raise.

    The dashboard's secrets dropdown loads on every config-edit
    open; a missing file shouldn't break the editor.
    """
    controller = _make_controller(tmp_path)
    keys = await controller.get_secrets()
    assert keys == []


@pytest.mark.asyncio
async def test_get_secrets_returns_sorted_keys(tmp_path: Path) -> None:
    """Returned secret names are sorted alphabetically.

    The dropdown renders them in document order otherwise, which
    drifts every time the user reorders the file. Pin the sort
    so the dashboard's UX stays stable.
    """
    (tmp_path / "secrets.yaml").write_text(
        "wifi_password: secret\nwifi_ssid: home\napi_key: token\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    keys = await controller.get_secrets()
    assert keys == ["api_key", "wifi_password", "wifi_ssid"]


@pytest.mark.asyncio
async def test_get_info_returns_storage_metadata_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: ``StorageJSON.load`` hits → handler returns the metadata dict.

    Pin the field-by-field projection (StorageJSON has more
    fields than we surface; the handler whitelists the
    drawer-relevant subset). A regression that returned
    ``storage.to_dict()`` directly would leak internal fields
    onto the wire and force the frontend to re-derive its UI
    contract from upstream's StorageJSON shape.
    """
    sidecar = write_storage_json(
        tmp_path,
        "kitchen.yaml",
        firmware_bin_path=Path("/firmware/kitchen.bin"),
        overrides={
            "name": "kitchen",
            "friendly_name": "Kitchen",
            "comment": "By the toaster",
            "address": "kitchen.local",
            "web_port": 80,
            "loaded_integrations": ["api", "wifi", "ota"],
            "target_platform": "esp32",
        },
    )
    # ``ext_storage_path`` keys off ``CORE.config_path`` in production;
    # redirect it onto our seeded sidecar so the handler's read lands
    # there without a real CORE setup.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.ext_storage_path",
        lambda configuration: sidecar.parent / f"{configuration}.json",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_info(configuration="kitchen.yaml")

    # ``firmware_bin_path`` deserialises into a ``Path`` upstream, so
    # the projection passes that through unchanged. The server's
    # ``send_json`` (orjson) serialises the ``Path`` to its string
    # form on the wire — this in-process assertion checks the
    # pre-serialisation shape, where the handler hasn't coerced.
    # Assert the integration set separately so the test doesn't
    # over-constrain ``loaded_integrations`` to a specific
    # collection type — the upstream ``StorageJSON`` could
    # legitimately switch between ``set`` / ``list`` / ``tuple``
    # without changing the JSON shape on the wire (an unordered
    # collection of strings).
    integrations = result.pop("loaded_integrations")
    assert set(integrations) == {"api", "wifi", "ota"}
    assert result == {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "comment": "By the toaster",
        "address": "kitchen.local",
        "web_port": 80,
        "target_platform": "esp32",
        "current_version": "2026.5.0-dev",
        "deployed_version": Path("/firmware/kitchen.bin"),
    }


@pytest.mark.asyncio
async def test_get_info_returns_none_when_storage_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No StorageJSON sidecar on disk → handler returns ``None``.

    The drawer treats ``None`` as "device hasn't been compiled
    yet" and renders a CTA to compile. A regression that raised
    or returned an empty dict would fail open in the wrong
    direction — either crashing the drawer or showing stale-
    looking blank fields instead of the compile prompt.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.ext_storage_path",
        lambda configuration: tmp_path / "missing-storage.json",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_info(configuration="kitchen.yaml")

    assert result is None


@pytest.mark.asyncio
async def test_get_info_rejects_path_traversal(make_settings: MakeSettingsFactory) -> None:
    """Traversal-shaped configuration raises ``CommandError(INVALID_ARGS)``.

    Wires the controller to the real ``DashboardSettings.rel_path``
    so we exercise the production traversal-detection logic, not a
    monkeypatched stub. ``rel_path`` translates the ``ValueError``
    raised by ``Path.relative_to`` into a ``CommandError`` so the
    WS dispatcher surfaces it as ``INVALID_ARGS`` instead of the
    generic ``INTERNAL_ERROR`` an unclassified ``ValueError`` would
    yield. A regression in either side of the boundary breaks the
    test.
    """
    settings = make_settings()

    controller = ConfigController.__new__(ConfigController)
    controller._db = MagicMock()
    controller._db.settings = settings

    with pytest.raises(CommandError) as excinfo:
        await asyncio.wait_for(controller.get_info(configuration="../etc/passwd"), timeout=2.0)
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Invalid configuration filename" in excinfo.value.message


@pytest.mark.parametrize(
    "payload",
    [
        "../etc/passwd",
        "../../etc/passwd",
        "subdir/../../escape.yaml",
        "/absolute/path/escape.yaml",
        "..",
    ],
)
def test_rel_path_translates_traversal_to_command_error(
    make_settings: MakeSettingsFactory, payload: str
) -> None:
    """``rel_path`` raises ``CommandError(INVALID_ARGS)`` on every traversal shape.

    This is the chokepoint behind issue #107 — every WS handler that
    builds a path from a user-supplied ``configuration`` flows
    through ``rel_path``, so this one parametrised test locks the
    dispatcher contract for all of them. The error message is
    truncated + ``!r``-quoted so a pathological payload can't break
    the JSON error response.
    """
    settings = make_settings()

    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "Invalid configuration filename" in excinfo.value.message


def test_rel_path_truncates_long_payload(make_settings: MakeSettingsFactory) -> None:
    """A multi-KB ``configuration`` payload is truncated in the error message.

    Keeps the JSON error response bounded so a pathological payload
    can't blow up the WS frame.
    """
    settings = make_settings()

    payload = "../" + "A" * 5000
    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert "..." in excinfo.value.message
    assert len(excinfo.value.message) < 200


def test_rel_path_bounds_control_byte_payload(make_settings: MakeSettingsFactory) -> None:
    r"""Control-heavy payloads stay bounded after ``!r`` expansion.

    A single ``\x00`` repr's to 4 chars; a naive "truncate the raw
    string then ``!r`` it" would let an 80-byte input balloon to
    320+ chars in the final message and re-introduce the
    unbounded-error hazard. ``!r`` runs *before* the bound, so a
    payload of 100 NUL bytes still produces a message ≤ 200 chars.
    """
    settings = make_settings()

    payload = "../" + "\x00" * 100
    with pytest.raises(CommandError) as excinfo:
        settings.rel_path(payload)
    assert len(excinfo.value.message) < 200


# ---------------------------------------------------------------------------
# get_serial_ports — config/serial_ports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_serial_ports_returns_path_and_desc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each upstream ``SerialPort`` round-trips as ``{port, desc}``.

    Pin the field renaming (``path`` → ``port``,
    ``description`` → ``desc``). The executor-route is asserted
    separately in ``test_get_serial_ports_runs_in_executor`` —
    monkeypatching ``get_serial_ports`` to a sync lambda here
    means a regression that dropped ``run_in_executor`` would
    still pass this test, so the contract gets its own dedicated
    pin.
    """
    fake_ports = [
        SerialPort(path="/dev/ttyUSB0", description="USB Serial"),
        SerialPort(path="/dev/ttyACM0", description="Arduino Uno"),
    ]
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.get_serial_ports",
        lambda: fake_ports,
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == [
        {"port": "/dev/ttyUSB0", "desc": "USB Serial"},
        {"port": "/dev/ttyACM0", "desc": "Arduino Uno"},
    ]


@pytest.mark.asyncio
async def test_get_serial_ports_runs_in_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_serial_ports`` runs in a worker thread, not the event loop.

    Capture the calling thread inside the monkeypatched lookup
    and assert it isn't the loop's thread. Pyserial's
    ``list_ports`` walks ``/dev`` synchronously on a busy host,
    so dropping ``run_in_executor`` would stall the dashboard
    until the scan finished — the failure mode this test catches
    is silent and platform-dependent (only shows up under load),
    which is exactly what blockbuster's per-frame check can't
    catch from a sync stub. Direct thread-identity assertion is
    what makes the executor route observable.
    """
    loop_thread = threading.get_ident()
    captured_thread: dict[str, int] = {}

    def _record_thread() -> list[SerialPort]:
        captured_thread["tid"] = threading.get_ident()
        return [SerialPort(path="/dev/ttyUSB0", description="USB Serial")]

    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.get_serial_ports",
        _record_thread,
    )
    controller = _make_controller(tmp_path)

    await controller.get_serial_ports_cmd()

    assert captured_thread.get("tid") is not None, "get_serial_ports was never invoked"
    assert captured_thread["tid"] != loop_thread, (
        "get_serial_ports ran on the event-loop thread — production needs "
        "run_in_executor so pyserial's /dev walk doesn't stall the loop on "
        "a busy host."
    )


@pytest.mark.asyncio
async def test_get_serial_ports_substitutes_path_for_na_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``description == "n/a"`` → ``desc`` falls back to the port path.

    pyserial returns the literal string ``"n/a"`` when it can't
    read a USB descriptor (common with adapter chips that don't
    expose product strings). Showing "n/a" in the dashboard's
    flash-target dropdown is unhelpful — a refactor that dropped
    the fallback would make the chooser look broken without
    actually being broken.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.get_serial_ports",
        lambda: [SerialPort(path="/dev/ttyUSB0", description="n/a")],
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == [{"port": "/dev/ttyUSB0", "desc": "/dev/ttyUSB0"}]


@pytest.mark.asyncio
async def test_get_serial_ports_returns_empty_when_no_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No serial ports → empty list, not None or an exception.

    The dashboard's flash-target dropdown renders "No serial
    ports available" off an empty list; a regression that
    returned ``None`` would break iteration in the frontend.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.config.get_serial_ports",
        lambda: [],
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_serial_ports_cmd()

    assert result == []
