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

from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

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


async def test_update_config_triggers_scan(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Each successful write queues a targeted reload on the scanner.

    Save acks on the disk write alone; the reload runs in the
    background via :meth:`DeviceScanner.request` and fires
    DEVICE_UPDATED through the on-change pipeline when the worker
    drains the pending set. Pin the request call so a refactor
    that dropped it would surface — the editor's "save and
    immediately see the device" UX depends on the worker getting
    poked.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)

    await controller.update_config(configuration="new.yaml", content="esphome:\n  name: new\n")

    assert controller._scanner.calls == [("request", "new.yaml")]


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


async def test_update_config_writes_before_requesting_reload(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``request()`` fires only after the atomic disk write returns.

    Reordering the two would make the scanner's worker pick up
    the stale on-disk bytes and dispatch DEVICE_UPDATED with the
    old metadata. Trace both calls and pin the order so a refactor
    that swapped them would surface.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: stale\n", encoding="utf-8")
    new_content = "esphome:\n  name: fresh\n"
    order: list[str] = []
    real_write = controller._write_yaml_atomic_async

    async def _trace_write(path: Path, content: str) -> None:
        await real_write(path, content)
        order.append("write")

    def _trace_request(filename: str) -> None:
        order.append(f"request:{filename}")

    monkeypatch.setattr(controller, "_write_yaml_atomic_async", _trace_write)
    monkeypatch.setattr(controller._scanner, "request", _trace_request)

    await controller.update_config(configuration="kitchen.yaml", content=new_content)

    assert order == ["write", "request:kitchen.yaml"]
    assert yaml_path.read_text(encoding="utf-8") == new_content


async def test_update_config_refuses_empty_content(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Empty content raises ``INVALID_ARGS``; file and side effects untouched."""
    controller = make_controller(tmp_path)
    regenerated = _stub_regenerate(controller)
    target = tmp_path / "secrets.yaml"
    original = "wifi_password: hunter2\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.update_config(configuration="secrets.yaml", content="")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert target.read_text(encoding="utf-8") == original
    assert regenerated == []
    assert controller._scanner.calls == []


async def test_update_config_refuses_whitespace_only_content(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Whitespace-only content raises ``INVALID_ARGS``; file and side effects untouched."""
    controller = make_controller(tmp_path)
    regenerated = _stub_regenerate(controller)
    target = tmp_path / "secrets.yaml"
    original = "wifi_password: hunter2\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.update_config(configuration="secrets.yaml", content="  \n\t\n")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert target.read_text(encoding="utf-8") == original
    assert regenerated == []
    assert controller._scanner.calls == []


async def test_update_config_writes_valid_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A valid whole-file secrets.yaml save lands on disk (via the shared lock branch)."""
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    content = "wifi_ssid: home\nwifi_password: hunter2\n"

    result = await controller.update_config(configuration="secrets.yaml", content=content)

    assert result is None
    assert (tmp_path / "secrets.yaml").read_text(encoding="utf-8") == content


async def test_update_config_rejects_invalid_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A malformed ``secrets.yaml`` is rejected and the prior good file survives."""
    controller = make_controller(tmp_path)
    regenerated = _stub_regenerate(controller)
    target = tmp_path / "secrets.yaml"
    original = "wifi_ssid: home\nwifi_password: hunter2\n"
    target.write_text(original, encoding="utf-8")
    bad = 'wifi_ssid: "myssid"\nwifi_password: "mypassword"\nxx:xxx\na:a\n'

    with pytest.raises(CommandError) as excinfo:
        await controller.update_config(configuration="secrets.yaml", content=bad)

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "line" in excinfo.value.message
    assert target.read_text(encoding="utf-8") == original
    assert regenerated == []
    assert controller._scanner.calls == []


async def test_update_config_rejects_non_mapping_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``secrets.yaml`` whose top level is a list/scalar is rejected."""
    controller = make_controller(tmp_path)
    regenerated = _stub_regenerate(controller)
    target = tmp_path / "secrets.yaml"
    original = "wifi_ssid: home\n"
    target.write_text(original, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.update_config(configuration="secrets.yaml", content="- a\n- b\n")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "mapping" in excinfo.value.message
    assert target.read_text(encoding="utf-8") == original
    assert regenerated == []


async def test_update_config_accepts_valid_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A well-formed ``secrets.yaml`` mapping writes through unchanged."""
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    content = "wifi_ssid: home\nwifi_password: hunter2\n"

    await controller.update_config(configuration="secrets.yaml", content=content)

    assert (tmp_path / "secrets.yaml").read_text(encoding="utf-8") == content


async def test_update_config_accepts_comment_only_secrets_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A comment-only ``secrets.yaml`` (parses to ``None``) is a legitimate file."""
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    content = "# add your secrets below\n"

    await controller.update_config(configuration="secrets.yaml", content=content)

    assert (tmp_path / "secrets.yaml").read_text(encoding="utf-8") == content


async def test_update_config_does_not_validate_device_yaml(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Device YAML with ``!secret`` tags (not plain-safe-loadable) still writes.

    The parse guard is scoped to ``secrets.yaml``; device configs carry
    ESPHome tags a safe loader rejects and must land so the user can
    repair them in the editor.
    """
    controller = make_controller(tmp_path)
    _stub_regenerate(controller)
    content = "wifi:\n  ssid: !secret wifi_ssid\n  password: !secret wifi_password\n"

    await controller.update_config(configuration="kitchen.yaml", content=content)

    assert (tmp_path / "kitchen.yaml").read_text(encoding="utf-8") == content


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
