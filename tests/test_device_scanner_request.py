"""
Wake-driven reload worker tests for ``DeviceScanner``.

The worker peels write-after-edit reloads off the WS hot path:
:meth:`DeviceScanner.request` is the entry point for in-process
mutators (``update_config`` / ``add_component`` / friendly-name
edit). Callers fire-and-forget, the worker drains the pending
set under :attr:`_lock`, and DEVICE_UPDATED reaches subscribers
via the existing on_change pipeline.

These tests pin the worker's contract — the broader scan
ordering / failure-mode coverage lives in
``test_device_scanner_branches.py`` and
``test_device_scanner_order.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
    ScanChange,
)
from esphome_device_builder.models import Device


def _stub_metadata(_config_dir: Path, _filename: str) -> DeviceFileMetadata:
    return DeviceFileMetadata(board_id="", ip="", expected_config_hash="")


def _make_scanner(config_dir: Path) -> tuple[DeviceScanner, list[tuple[ScanChange, Device]]]:
    events: list[tuple[ScanChange, Device]] = []
    scanner = DeviceScanner(
        config_dir=config_dir,
        get_metadata=_stub_metadata,
        on_change=lambda kind, device, _previous: events.append((kind, device)),
    )
    return scanner, events


def _write_yaml(config_dir: Path, name: str) -> Path:
    path = config_dir / f"{name}.yaml"
    path.write_text(f"esphome:\n  name: {name}\n", encoding="utf-8")
    return path


def _stub_load(path: Path, *_a: Any, **_kw: Any) -> Device:
    return Device(name=path.stem, friendly_name=path.stem, configuration=path.name)


async def test_request_drains_one_file(tmp_path: Path) -> None:
    """``request()`` + a worker tick reloads the named file and fires UPDATED."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=_stub_load,
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()  # initial load so reload() can find the path
        events.clear()
        scanner.start()
        try:
            scanner.request("kitchen.yaml")
            await scanner.wait_idle()
        finally:
            await scanner.stop()

    assert [(kind, dev.name) for kind, dev in events] == [(ScanChange.RELOADED, "kitchen")]


async def test_request_coalesces_duplicate_filenames(tmp_path: Path) -> None:
    """Repeated requests for one file produce a single reload per wake.

    A tight save-loop (the editor's keystroke-driven autosave is
    one realistic shape) must not pile up N reloads on the
    worker. The set semantics of pending carry this; pin it so a
    refactor to a list / queue would surface.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")

    reload_calls: list[str] = []
    real_reload = DeviceScanner.reload

    async def _record(self: DeviceScanner, filename: str) -> bool:
        reload_calls.append(filename)
        return await real_reload(self, filename)

    with (
        patch(
            "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
            side_effect=_stub_load,
        ),
        patch.object(DeviceScanner, "reload", _record),
    ):
        scanner, _ = _make_scanner(cfg)
        await scanner.scan()
        # Stage three requests before the worker starts so they
        # all land in the same drain (set collapse, not race).
        scanner.request("kitchen.yaml")
        scanner.request("kitchen.yaml")
        scanner.request("kitchen.yaml")
        scanner.start()
        try:
            await scanner.wait_idle()
        finally:
            await scanner.stop()

    assert reload_calls == ["kitchen.yaml"]


async def test_request_before_start_drains_on_start(tmp_path: Path) -> None:
    """A request fired before :meth:`start` waits in pending; the worker drains it.

    The save handler can run before the controller's ``start()``
    has spawned the worker (e.g. a test that creates the
    controller and hits a WS command without calling
    ``controller.start()`` first). Pending must survive across
    the start boundary so no save is dropped.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    with patch(
        "esphome_device_builder.controllers._device_scanner.load_device_from_storage",
        side_effect=_stub_load,
    ):
        scanner, events = _make_scanner(cfg)
        await scanner.scan()
        events.clear()
        scanner.request("kitchen.yaml")
        scanner.start()
        try:
            await scanner.wait_idle()
        finally:
            await scanner.stop()

    assert [(kind, dev.name) for kind, dev in events] == [(ScanChange.RELOADED, "kitchen")]


async def test_start_is_idempotent(tmp_path: Path) -> None:
    """Two ``start()`` calls don't double-spawn the worker."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    scanner, _ = _make_scanner(cfg)
    scanner.start()
    first_task = scanner._task
    try:
        scanner.start()
        assert scanner._task is first_task
    finally:
        await scanner.stop()


async def test_stop_cancels_idle_worker(tmp_path: Path) -> None:
    """``stop()`` cancels and awaits the worker even when it's parked on wake."""
    cfg = tmp_path / "configs"
    cfg.mkdir()
    scanner, _ = _make_scanner(cfg)
    scanner.start()
    task = scanner._task
    assert task is not None
    await scanner.stop()
    assert task.done()
    # Idempotent — a second stop call is a no-op.
    await scanner.stop()


async def test_worker_survives_failing_reload(tmp_path: Path) -> None:
    """A reload that raises is logged and the worker continues on the next wake.

    Real-world trigger: a YAML disappears between the save and
    the worker's drain (concurrent delete, atomic-save mid-edit
    where the temp file moved away). The save's request shouldn't
    poison the worker for the next save.
    """
    cfg = tmp_path / "configs"
    cfg.mkdir()
    _write_yaml(cfg, "kitchen")
    _write_yaml(cfg, "bedroom")

    reloaded: list[str] = []

    async def _boomy_reload(self: DeviceScanner, filename: str) -> bool:
        reloaded.append(filename)
        if filename == "kitchen.yaml":
            raise RuntimeError("simulated reload failure")
        return True

    with patch.object(DeviceScanner, "reload", _boomy_reload):
        scanner, _ = _make_scanner(cfg)
        scanner.start()
        try:
            scanner.request("kitchen.yaml")
            await scanner.wait_idle()
            scanner.request("bedroom.yaml")
            await scanner.wait_idle()
        finally:
            await scanner.stop()

    # Failing reload ran once and the worker survived to handle bedroom.
    assert reloaded == ["kitchen.yaml", "bedroom.yaml"]
