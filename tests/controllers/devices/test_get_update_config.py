"""Tests for ``DevicesController.get_config`` / ``update_config``.

These two API commands back the in-browser YAML editor's load and
save flow. The matching upstream handlers in
``esphome/dashboard/web_server.py`` are ``EditRequestHandler.get``
and ``EditRequestHandler.post``; pin our shape against theirs so
HA's editor (and any future esphome-dashboard-api consumer) keeps
working unchanged.

Coverage targets:

* Round-trip: write content with ``update_config`` and re-read it
  with ``get_config``.
* Both methods route the underlying ``Path.read_text`` /
  ``Path.write_text`` through ``loop.run_in_executor`` so the
  blocking syscall doesn't stall the event loop on slow /
  network-mounted config dirs — exercised by asserting the file
  lands on disk after the await returns.
* ``update_config`` triggers a fresh scan + StorageJSON
  regenerate so the dashboard's UI reflects the new YAML without
  waiting for a full compile.
* Path-traversal is gated by ``settings.rel_path`` — already
  covered in ``tests/controllers/test_traversal_validation.py``,
  not duplicated here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController

from .conftest import MakeControllerFactory


def _stub_regenerate(controller: DevicesController) -> list[str]:
    """Replace ``_schedule_storage_regenerate`` with a list-append closure.

    The production helper is fire-and-forget that internally schedules
    a background task; capturing into a typed ``list[str]`` records
    the call site without dispatching the subprocess. Tests assert on
    the list directly (``regenerated == ["kitchen.yaml"]``) — no
    ``MagicMock`` attribute to misspell on the assertion side, and a
    typo'd reference to the helper name surfaces as ``NameError``.
    """
    regenerated: list[str] = []
    controller._schedule_storage_regenerate = regenerated.append  # type: ignore[method-assign]
    return regenerated


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_config_returns_yaml_content(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``get_config`` returns the file's text decoded as UTF-8.

    HA's editor pre-fills the textarea with whatever this returns
    — the dashboard's YAML round-trip starts here. Pin the
    plain-string shape (no JSON envelope) so a refactor that
    wraps the response would surface.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    yaml_content = "esphome:\n  name: kitchen\n  platform: ESP32\n"
    (tmp_path / "kitchen.yaml").write_text(yaml_content, encoding="utf-8")

    result = await controller.get_config(configuration="kitchen.yaml")

    assert result == yaml_content


@pytest.mark.asyncio
async def test_get_config_decodes_utf8_with_unicode(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """UTF-8 content (e.g., comments with non-ASCII) round-trips intact.

    The explicit ``"utf-8"`` arg to ``read_text`` keeps Windows
    safe — the platform default is cp1252 there, which would
    silently mojibake the YAML.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    yaml_content = "esphome:\n  name: küche\n  # comment with — em dash and ñ\n"
    (tmp_path / "kuche.yaml").write_text(yaml_content, encoding="utf-8")

    result = await controller.get_config(configuration="kuche.yaml")

    assert result == yaml_content


@pytest.mark.asyncio
async def test_get_config_propagates_file_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Missing file → ``FileNotFoundError`` bubbles to the dispatcher.

    The api dispatcher catches it and turns it into a generic
    error frame; we don't try to swallow / map it here. Pin the
    pass-through so a defensive refactor that returned ``""``
    instead would surface (the editor would silently load an
    empty buffer for a typo'd configuration).
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)

    with pytest.raises(FileNotFoundError):
        await controller.get_config(configuration="ghost.yaml")


# ---------------------------------------------------------------------------
# update_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_config_writes_content_to_disk(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``update_config`` lands the new YAML on disk under the configuration name."""
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    new_content = "esphome:\n  name: kitchen\n  friendly_name: Kitchen\n"

    result = await controller.update_config(configuration="kitchen.yaml", content=new_content)

    assert result is None  # API contract: no payload on success
    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == new_content


@pytest.mark.asyncio
async def test_update_config_overwrites_existing_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Writing over an existing YAML replaces it verbatim.

    The editor's "Save" button does this on every keystroke-driven
    save; pin the no-merge / clobber semantics so a refactor that
    accidentally appended would surface.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: stale\n", encoding="utf-8")
    new_content = "esphome:\n  name: kitchen\n"

    await controller.update_config(configuration="kitchen.yaml", content=new_content)

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == new_content


@pytest.mark.asyncio
async def test_update_config_writes_utf8_unicode_intact(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Non-ASCII content lands on disk as UTF-8.

    Companion to the read test — the explicit ``"utf-8"`` arg
    matters on Windows where the default would otherwise be
    cp1252. Without this, comments with em dashes / accents
    would silently corrupt on save.

    Asserts via ``read_text`` (not ``read_bytes``) because
    ``Path.write_text`` defaults to ``newline=None``, which
    translates LF to CRLF on Windows; the round-trip decode
    normalises newlines so the assertion is platform-independent.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    new_content = "esphome:\n  name: küche\n  # — and ñ\n"

    await controller.update_config(configuration="kuche.yaml", content=new_content)

    assert (tmp_path / "kuche.yaml").read_text(encoding="utf-8") == new_content


@pytest.mark.asyncio
async def test_update_config_triggers_scan(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Each successful write awakens the scanner so the device list refreshes.

    Without the post-write scan, a freshly-added YAML wouldn't
    show up in the device list until the next background scan tick
    (up to 60s on the file-poll cadence). The dashboard's
    "save and immediately see the device" UX depends on this.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)

    await controller.update_config(configuration="new.yaml", content="esphome:\n  name: new\n")

    assert controller._scanner.calls == [("scan",)]


@pytest.mark.asyncio
async def test_update_config_schedules_storage_regenerate(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Each successful write schedules a StorageJSON regenerate for that YAML.

    ``_schedule_storage_regenerate`` runs ``esphome compile
    --only-generate`` so ``address`` / ``loaded_integrations`` /
    ``config_hash`` reflect the new YAML without waiting for a
    full build. Mirrors upstream's ``async_schedule_storage_json_update``
    in ``EditRequestHandler``. Pin the call so a refactor that
    skipped the regen for "save" (vs "save and compile") would
    surface — devices showing stale metadata after edits is the
    regression class this prevents.
    """
    controller = make_controller(tmp_path)
    regenerated = _stub_regenerate(controller)

    await controller.update_config(
        configuration="kitchen.yaml", content="esphome:\n  name: kitchen\n"
    )

    assert regenerated == ["kitchen.yaml"]


@pytest.mark.asyncio
async def test_update_config_writes_before_scanner_runs(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The disk write completes before ``scanner.scan()`` fires.

    Scanner reads the YAML it scans; if scan ran first, it would
    see the stale on-disk version and dispatch DEVICE_UPDATED with
    the old metadata. Pin the ordering by reading the file
    inside the scan callback — once scan() runs, the new content
    must already be on disk.

    Read goes through ``asyncio.to_thread`` so blockbuster's
    event-loop guard doesn't fault on the synchronous ``read_text``
    on CI.

    Regenerate-after-scan is implicit: ``update_config`` awaits
    ``scan()`` before calling ``_schedule_storage_regenerate``,
    so any ordering of regen-vs-write follows from this scan
    check by virtue of the linear async control flow.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: stale\n", encoding="utf-8")

    new_content = "esphome:\n  name: fresh\n"
    seen_during_scan: list[str] = []

    async def _record_scan() -> None:
        seen_during_scan.append(await asyncio.to_thread(yaml_path.read_text, "utf-8"))

    controller._scanner.scan = AsyncMock(side_effect=_record_scan)

    await controller.update_config(configuration="kitchen.yaml", content=new_content)

    assert seen_during_scan == [new_content]


@pytest.mark.asyncio
async def test_round_trip_update_then_get_returns_written_content(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """End-to-end: ``update_config`` then ``get_config`` returns what was written.

    Higher-level confidence that the two halves of the editor's
    save / reload cycle line up against the same file at the same
    path. A future refactor that used different paths in
    ``rel_path`` for read vs write would surface here.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    written = "esphome:\n  name: roundtrip\n"

    await controller.update_config(configuration="rt.yaml", content=written)
    read_back = await controller.get_config(configuration="rt.yaml")

    assert read_back == written
