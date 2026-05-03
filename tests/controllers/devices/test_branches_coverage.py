"""Coverage for the smaller-but-load-bearing branches in ``controller.py``.

Each test pins one specific branch that the existing per-feature
suites either skip or cover only via a deeper helper. They are
short and surgical — when the branch they protect regresses, the
test that fails should make the regression obvious from its name
alone, even before reading the assertion.

Grouped by surface:

- **API command wiring** (delete / delete_bulk / get_api_key /
  add_component error branches) — these are the public commands
  that go through the WS layer; pin both the happy-path return
  shape and the typed-error branches the dashboard relies on.
- **Scan-callback fan-out** (``_on_ip_change`` no-op when IP is
  unchanged, ``_on_firmware_job_completed`` RENAME / empty-config
  guards) — small early-returns that prevent the bus from firing
  redundant events; uncovered means a regression that floods the
  WS clients can land silently.
- **Storage / file-ops glue** (``_persist_storage_version_async``
  thread bridge, ``_list_archived_sync`` OSError fallback,
  ``_manual_rename`` collision guard, ``_stream_subprocess``
  ``line_transform`` hook) — each of these is the code path that
  keeps a specific feature working when the FS misbehaves; pinning
  them keeps the feature surface honest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers.devices.controller import StorageJSON
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import (
    ConfigEntry,
    ConfigEntryType,
    Device,
    DeviceState,
    EventType,
    JobStatus,
    JobType,
)
from tests._storage_fixtures import write_storage_json

from .conftest import (
    MakeControllerFactory,
    SeedDeviceFactory,
    capture_devices_events,
)


def _device(name: str, *, ip: str = "") -> Device:
    return Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.ONLINE,
        ip=ip,
    )


# ---------------------------------------------------------------------------
# create_device file_content + board derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_writes_file_content_verbatim(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``file_content`` is written as-is and the catalog derives ``board_id`` from it.

    The dashboard's adoption flow ships an entire YAML through
    ``file_content`` (the upstream ``DashboardImportDiscovery``
    URL fetches the project YAML and the user just confirms).
    Pin: the YAML lands verbatim *and* the catalog's PIO-board
    lookup runs against the parsed platform/board so the new
    device picks up its catalog entry without the user having
    to choose one manually.
    """
    controller = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    matched = MagicMock()
    matched.id = "esp32-c3-devkitm-1"
    controller._db.boards.find_by_pio_board = MagicMock(return_value=matched)
    controller._db.boards.find_by_platform_variant = MagicMock(return_value=None)

    yaml_text = (
        "esphome:\n  name: kitchen\nesp32:\n  board: esp32-c3-devkitm-1\n  variant: esp32c3\n"
    )
    result = await controller.create_device(name="kitchen", file_content=yaml_text)

    assert result.configuration == "kitchen.yaml"
    written = (tmp_path / "kitchen.yaml").read_text(encoding="utf-8")
    # Verbatim — no template generation override.
    assert written == yaml_text
    # Catalog's PIO-board lookup ran against the parsed YAML.
    controller._db.boards.find_by_pio_board.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_falls_back_to_platform_variant_lookup(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """When the PIO-board lookup misses, the platform/variant fallback runs.

    Generic ``esp32:`` configs without a specific ``board:``
    entry still need a catalog match so the dashboard can show
    a generic-esp32-c3 entry rather than an unmapped board.
    Pin the fallback so a regression that dropped it would leave
    catalog-less placeholders on every wizard run with a generic
    ESP32 template.
    """
    controller = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    # PIO miss; platform/variant hit.
    matched = MagicMock()
    matched.id = "generic-esp32-c3"
    controller._db.boards.find_by_pio_board = MagicMock(return_value=None)
    controller._db.boards.find_by_platform_variant = MagicMock(return_value=matched)

    yaml_text = "esphome:\n  name: kitchen\nesp32:\n  variant: esp32c3\n"
    await controller.create_device(name="kitchen", file_content=yaml_text)

    controller._db.boards.find_by_platform_variant.assert_called_once()


# ---------------------------------------------------------------------------
# delete_device / delete_bulk public-API wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_device_unlinks_yaml_then_scans(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """``devices/delete`` removes the YAML and kicks the scanner.

    The scan-after-delete is what makes the dashboard's device
    list refresh without a manual reload — pin so a regression
    that drops the trailing ``scan()`` would leave a phantom
    card visible until the next periodic poll.
    """
    controller = make_controller(tmp_path)
    yaml_path, _ = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    assert yaml_path.exists()

    await controller.delete_device(configuration="kitchen.yaml")

    assert not yaml_path.exists()
    assert ("scan",) in controller._scanner.calls


@pytest.mark.asyncio
@pytest.mark.usefixtures("redirect_storage_path")
async def test_delete_bulk_returns_per_device_success_with_mixed_outcomes(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
) -> None:
    """``delete_bulk`` returns one ``{configuration, success, error?}`` per item.

    Mixed success+failure shape is the contract the dashboard's
    bulk-delete dialog leans on (it shows a per-row checkmark /
    error message). Pin: existing devices report success, missing
    ones report success=False with the FileNotFoundError message,
    and the scanner only fires once for the whole batch (not per
    device — that would N-square the bus traffic on bulk teardown).
    """
    controller = make_controller(tmp_path)
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    await seed_device(tmp_path, "bedroom.yaml", with_build_dir=True)

    results = await controller.delete_bulk(
        configurations=["kitchen.yaml", "ghost.yaml", "bedroom.yaml"]
    )

    assert results == [
        {"configuration": "kitchen.yaml", "success": True},
        {
            "configuration": "ghost.yaml",
            "success": False,
            "error": "File not found: ghost.yaml",
        },
        {"configuration": "bedroom.yaml", "success": True},
    ]
    # Only one scan for the whole batch.
    scan_calls = [c for c in controller._scanner.calls if c == ("scan",)]
    assert len(scan_calls) == 1


# ---------------------------------------------------------------------------
# get_api_key public-API wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_api_key_resolves_through_yaml_loader(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``devices/get_api_key`` returns the resolved encryption key.

    The handler runs through ESPHome's YAML loader so ``!secret``
    references resolve the same way they do at compile time —
    pin the wire shape with an inline key (``!secret`` resolution
    is covered in the helper-level tests; here we're just verifying
    the controller threads the result into the WS response shape
    correctly).
    """
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\napi:\n  encryption:\n    key: a/c+inline-key==\n",
        encoding="utf-8",
    )

    result = await controller.get_api_key(configuration="kitchen.yaml")

    assert result == {"key": "a/c+inline-key=="}


@pytest.mark.asyncio
async def test_get_api_key_returns_empty_when_no_encryption(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A device without ``api.encryption`` returns ``{"key": ""}``.

    Pin the empty-key sentinel — frontend treats it as the "open
    the editor and check" signal. A regression that propagates
    the loader's ``None`` would JSON-serialise as ``null`` and
    crash the dashboard's string-only schema.
    """
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text(
        "esphome:\n  name: kitchen\napi:\n",
        encoding="utf-8",
    )

    result = await controller.get_api_key(configuration="kitchen.yaml")

    assert result == {"key": ""}


# ---------------------------------------------------------------------------
# add_component error branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_component_unknown_id_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An unknown component id raises ``ValueError`` before touching the YAML.

    Frontend should never send an unknown id (the catalog is the
    source of suggestions), but pin the guard so a desync between
    catalog / frontend can't silently corrupt a device's YAML by
    appending an empty block.
    """
    controller = make_controller(tmp_path)
    controller._db.components = MagicMock()
    controller._db.components.get_component = AsyncMock(return_value=None)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown component: never-heard-of-this"):
        await controller.add_component(
            configuration="kitchen.yaml",
            component_id="never-heard-of-this",
        )


@pytest.mark.asyncio
async def test_add_component_missing_required_field_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A required field absent from ``fields`` raises before serialising.

    The frontend's input form already enforces required fields
    via the schema, but the backend's guard catches API clients
    bypassing the form (the WS surface is public). Pin it so a
    regression that defaulted required fields silently can't slip
    through and produce an invalid YAML the user has to discover
    at compile time.
    """
    controller = make_controller(tmp_path)
    component = MagicMock()
    component.config_entries = [
        ConfigEntry(key="pin", type=ConfigEntryType.PIN, label="Pin", required=True),
        ConfigEntry(key="name", type=ConfigEntryType.STRING, label="Name", required=False),
    ]
    controller._db.components = MagicMock()
    controller._db.components.get_component = AsyncMock(return_value=component)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Missing required field: pin"):
        await controller.add_component(
            configuration="kitchen.yaml",
            component_id="dht",
            fields={"name": "Bedroom Temp"},
        )


# ---------------------------------------------------------------------------
# _on_ip_change short-circuit
# ---------------------------------------------------------------------------


def test_on_ip_change_skips_when_ip_unchanged(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A duplicate IP broadcast is a no-op — no event, no persist task.

    The mDNS browser fires the IP-change callback every time it
    re-resolves a record; without the dedupe the bus would tick
    on every keep-alive and the ``_persist_device_metadata_async``
    background task would re-write the sidecar on every TTL
    refresh. Pin the dedupe so a regression that always emits
    becomes obvious as a write-amplification regression in
    benchmarks (not just a soft "feels slow" complaint).
    """
    controller = make_controller(tmp_path)
    device = _device("kitchen", ip="192.168.1.42")
    controller._scanner._devices_by_name = {"kitchen": [device]}  # type: ignore[attr-defined]
    captured = capture_devices_events(controller, EventType.DEVICE_UPDATED)
    spawned: list[Any] = []
    controller._db.create_background_task = spawned.append

    # Same IP → no-op.
    controller._on_ip_change("kitchen", "192.168.1.42")

    assert captured == []
    assert spawned == []


# ---------------------------------------------------------------------------
# _on_firmware_job_completed early-return branches
# ---------------------------------------------------------------------------


def _job_event(
    *,
    job_type: JobType,
    status: JobStatus = JobStatus.COMPLETED,
    configuration: str = "kitchen.yaml",
) -> Event:
    """Build an ``Event`` carrying a job-shaped object for the handler."""
    job = MagicMock()
    job.status = status
    job.job_type = job_type
    job.configuration = configuration
    return Event(event_type=EventType.JOB_COMPLETED, data={"job": job})


def test_on_firmware_job_completed_rename_triggers_full_scan(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """RENAME jobs trigger a full ``scanner.scan()`` and skip the per-device refresh.

    ``esphome rename`` deletes the old YAML and writes a new one
    with a different filename — the per-config ``configuration``
    field on the job points at the *old* name, so a per-device
    refresh would walk a dead path. Pin the RENAME → scan
    short-circuit so a regression that fell through to the
    per-device branch leaves a stale "old" entry in the device
    list until the next poll.
    """
    controller = make_controller(tmp_path)
    spawned: list[Any] = []
    controller._db.create_background_task = lambda coro: spawned.append(coro) or None

    controller._on_firmware_job_completed(_job_event(job_type=JobType.RENAME))

    # One coroutine spawned — ``scanner.scan()``. No per-device refresh.
    assert len(spawned) == 1
    # Drop the un-awaited coro to avoid a RuntimeWarning in test output.
    spawned[0].close()


def test_on_firmware_job_completed_skips_when_configuration_empty(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A COMPILE / UPLOAD / INSTALL job with no configuration is ignored.

    Defensive guard against malformed events on the bus — without
    it, the per-device refresh would walk a config of empty-string
    name and the scanner-side lookups (``rel_path("")``) would do
    something surprising on the FS.
    """
    controller = make_controller(tmp_path)
    spawned: list[Any] = []
    controller._db.create_background_task = lambda coro: spawned.append(coro) or None

    controller._on_firmware_job_completed(_job_event(job_type=JobType.COMPILE, configuration=""))

    assert spawned == []


# ---------------------------------------------------------------------------
# _persist_storage_version_async — thread bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_storage_version_async_writes_through_executor(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async wrapper hands off to the executor so the event loop stays free.

    Pin the actual write reaches the on-disk StorageJSON — the
    sync helper's coverage proves the write logic itself, so
    this test only verifies the async wrapper threads the
    arguments through unchanged.
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esphome_version": "2026.4.0"})

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.ext_storage_path",
        lambda configuration: storage_dir / f"{configuration}.json",
    )

    controller = make_controller(tmp_path)
    await controller._persist_storage_version_async("kitchen.yaml", "2026.5.1")

    saved = StorageJSON.load(storage_dir / "kitchen.yaml.json")
    assert saved is not None
    assert saved.esphome_version == "2026.5.1"


# ---------------------------------------------------------------------------
# _list_archived_sync OSError fallback
# ---------------------------------------------------------------------------


def test_list_archived_skips_unreadable_yaml(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError on ``read_text`` for one archived YAML doesn't tank the listing.

    The legacy dashboard crashed the whole listing on a single
    permission-denied file. We log + skip so the user can still
    see and manage the rest of their archived devices — pin the
    skip behaviour so a refactor that re-raises silently breaks
    "Show archived devices" for everyone with one bad file.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    # Full ``esphome:`` meta on the good YAML so the storage-fallback
    # branch (which calls ``ext_storage_path`` and would crash without
    # a CORE) doesn't fire — we're targeting the OSError branch, not
    # the meta-fallback one.
    (archive_dir / "good.yaml").write_text(
        'esphome:\n  name: good\n  friendly_name: "Good"\n  comment: "fine"\n',
        encoding="utf-8",
    )
    (archive_dir / "broken.yaml").write_text("esphome:\n  name: broken\n", encoding="utf-8")

    real_read_text = Path.read_text

    def _maybe_fail(self: Path, *args: Any, **kwargs: Any) -> str:
        if self.name == "broken.yaml":
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _maybe_fail)

    controller = make_controller(tmp_path)
    rows = controller._list_archived_sync()

    assert [r["configuration"] for r in rows] == ["good.yaml"]


# ---------------------------------------------------------------------------
# _manual_rename collision guard
# ---------------------------------------------------------------------------


def test_manual_rename_raises_file_exists_error_when_target_taken(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An existing target file blocks the rename with ``FileExistsError``.

    The public ``rename_device`` already checks this up-front, but
    a race could let a file appear between that check and the
    actual rename. Pin the inner guard so a regression that
    dropped it would silently overwrite an unrelated YAML —
    losing the user's other config and (after the next compile)
    flashing this device's firmware to the wrong device.
    """
    controller = make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    (tmp_path / "livingroom.yaml").write_text("esphome:\n  name: livingroom\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"livingroom\.yaml"):
        controller._manual_rename("kitchen.yaml", "livingroom")


def test_manual_rename_raises_file_not_found_when_source_missing(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A missing source file raises ``FileNotFoundError`` with the configuration name.

    Sibling guard to the collision check — a deleted source
    YAML would otherwise crash deep inside ``read_text``. The
    typed exception is what ``rename_device`` translates into
    a user-facing ``CommandError(NOT_FOUND)``, so callers depend
    on the precise type.
    """
    controller = make_controller(tmp_path)

    with pytest.raises(FileNotFoundError, match=r"ghost\.yaml"):
        controller._manual_rename("ghost.yaml", "livingroom")


# ---------------------------------------------------------------------------
# _stream_subprocess line_transform hook
# ---------------------------------------------------------------------------


def test_get_devices_bridge_returns_scanner_property(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``_get_devices`` is the sync bridge the state monitor calls.

    Pin that it returns ``self._scanner.devices`` directly — the
    state monitor's per-name fan-out reads it synchronously from
    its callbacks, so swapping for an async or scan-triggering
    version would deadlock the monitor's state-change chain.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen"), _device("bedroom")]

    assert [d.name for d in controller._get_devices()] == ["kitchen", "bedroom"]


# ---------------------------------------------------------------------------
# _persist_device_ip_async — thin wrapper around _persist_device_metadata_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_device_ip_async_routes_through_metadata_helper(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The IP-only persist forwards to the generic metadata helper.

    Trivially-thin wrapper, but pin the keyword name (``ip=...``)
    — a regression that flipped to a positional or renamed kwarg
    would silently write nothing because
    ``_persist_device_metadata_async`` ignores unknown fields.
    """
    controller = make_controller(tmp_path)
    captured: dict[str, Any] = {}

    async def _capture(configuration: str, **fields: Any) -> None:
        captured["configuration"] = configuration
        captured["fields"] = fields

    controller._persist_device_metadata_async = _capture  # type: ignore[method-assign]

    await controller._persist_device_ip_async("kitchen.yaml", "192.168.1.42")

    assert captured == {"configuration": "kitchen.yaml", "fields": {"ip": "192.168.1.42"}}


# ---------------------------------------------------------------------------
# _on_scan_change UPDATED / REMOVED bookkeeping
# ---------------------------------------------------------------------------


def test_on_scan_change_updated_clears_regenerate_failed_marker(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A YAML edit clears the prior storage-regenerate failure marker.

    The marker is sticky to spare us spamming a known-broken YAML
    with retried ``--only-generate`` calls. An UPDATED / REMOVED
    scan change is the user's signal that the file might be
    fixed, so the marker has to clear or the device sits with no
    storage refresh forever.
    """
    controller = make_controller(tmp_path, with_state_monitor=True, with_regenerate_state=True)
    controller._regenerate_failed.add("kitchen.yaml")
    device = _device("kitchen")

    controller._on_scan_change(ScanChange.UPDATED, device)

    assert "kitchen.yaml" not in controller._regenerate_failed


def test_on_scan_change_removed_revisits_importables(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A device delete kicks ``revisit_all_importables`` to re-emit cached discoveries.

    Upstream's ``DashboardImportDiscovery`` only fires
    ``on_update`` on first sight (``is_new`` check), so without
    the revisit a deleted-then-rediscoverable device would stay
    silent until it re-announced — minutes for a quiet device.
    Pin the REMOVED → revisit edge.
    """
    controller = make_controller(tmp_path, with_state_monitor=True, with_regenerate_state=True)
    device = _device("kitchen")

    controller._on_scan_change(ScanChange.REMOVED, device)

    assert ("revisit_all_importables",) in controller._state_monitor.calls


# ---------------------------------------------------------------------------
# _stream_subprocess line_transform hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_subprocess_applies_line_transform(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``line_transform`` runs against every output line before it leaves the WS handler.

    Used by ``validate_config`` to scrub resolved ``!secret``
    values out of the stream when ``show_secrets`` is off
    (``esphome config`` doesn't actually redact in that mode —
    it wraps values with the ANSI conceal SGR which browsers
    don't honour). Pin the hook so a regression that dropped
    the per-line invocation would leak resolved secrets into
    the dashboard's logs view.
    """
    controller = make_controller(tmp_path)

    async def _fake_iter_lines(_stream: Any) -> Any:
        for line in (b"first\n", b"second\n"):
            yield line.decode()

    async def _fake_create_subprocess_exec(*_args: Any, **_kwargs: Any) -> Any:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.iter_lines_with_progress",
        _fake_iter_lines,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    client = MagicMock()
    client.register_stream = MagicMock()
    client.unregister_stream = MagicMock()
    client.send_event = AsyncMock()

    await controller._stream_subprocess(
        ["echo", "hi"],
        client,
        message_id="msg-1",
        line_transform=lambda s: f"<{s}>",
    )

    # Two transformed output events + a final result event.
    output_events = [call for call in client.send_event.await_args_list if call.args[1] == "output"]
    payloads = [call.args[2] for call in output_events]
    assert payloads == ["<first>", "<second>"]
