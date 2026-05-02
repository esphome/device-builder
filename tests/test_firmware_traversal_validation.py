"""Tests that firmware WS handlers reject traversal payloads at the boundary.

Every public handler that takes ``configuration`` flows through
``_validate_configuration_boundary`` (single-call) or
``_validate_configurations_boundary`` (batched) before reaching code
that builds paths. ``CommandError(INVALID_ARGS)`` propagates back
to the WS dispatcher synchronously — instead of being accepted,
queued, and materialising later as a failed job. The validation
runs inside an executor so blockbuster's blocking-syscall guard
on CI doesn't fault the request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _controller(tmp_path: Path) -> FirmwareController:
    """Stub controller wired to a real ``DashboardSettings.rel_path``."""
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {}
    controller._db = type("DB", (), {"settings": settings})()
    return controller


def test_sync_validate_rejects_traversal(tmp_path: Path) -> None:
    """The sync core raises ``CommandError(INVALID_ARGS)`` on traversal.

    This is the single-source-of-truth helper used by both the async
    wrapper and the bulk validator — so a future change to validation
    logic only needs one update site.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        controller._sync_validate_configuration_boundary("../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


def test_sync_validate_rejects_empty_string(tmp_path: Path) -> None:
    """Empty configuration raises — only ``RESET_BUILD_ENV`` legitimately wants it.

    ``reset_build_env`` doesn't go through the validator at all; every
    other handler does, so accepting ``""`` here would let a client call
    ``compile`` / ``upload`` / ``clean`` / ``install`` / ``rename`` with
    an empty ``configuration`` value, get back a queued ``FirmwareJob``,
    and only fail later when the runner hands the empty string to the
    CLI.
    """
    controller = _controller(tmp_path)
    with pytest.raises(CommandError) as excinfo:
        controller._sync_validate_configuration_boundary("")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must not be empty" in excinfo.value.message


@pytest.mark.asyncio
async def test_validate_configuration_boundary_runs_in_executor(tmp_path: Path) -> None:
    """The async wrapper runs ``rel_path`` in an executor.

    ``Path.resolve`` is a blocking syscall; running it on the event
    loop would fault under blockbuster on CI. The wrapper hands the
    sync core off to ``run_in_executor`` so the WS dispatcher stays
    non-blocking.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller._validate_configuration_boundary("../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_validate_configurations_boundary_raises_on_bad_entry(
    tmp_path: Path,
) -> None:
    """The bulk validator raises on the first invalid entry.

    Bulk handlers reject the whole batch on bad input rather than
    silently dropping the offending entry — a typo in one of N
    configurations is something the caller wants to know about,
    not have masked by partial success. Rename-lock conflicts in
    phase 2 stay skip-and-continue (transient state, not bad
    input).
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller._validate_configurations_boundary(
            ["kitchen.yaml", "../etc/passwd", "garage.yaml"]
        )
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_validate_configurations_boundary_all_valid(tmp_path: Path) -> None:
    """A clean batch validates without raising and returns ``None``."""
    controller = _controller(tmp_path)

    await controller._validate_configurations_boundary(["kitchen.yaml", "garage.yaml"])


@pytest.mark.asyncio
async def test_get_binaries_rejects_traversal(tmp_path: Path) -> None:
    """``firmware/get_binaries`` re-validates because it bypasses ``rel_path``.

    The handler reads ``ext_storage_path(configuration)`` which lands
    in ``<data_dir>/storage/<configuration>.json`` — outside the
    config dir — so the boundary validator is the only gate.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.get_binaries(configuration="../../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_download_rejects_traversal(tmp_path: Path) -> None:
    """``firmware/download`` re-validates for the same reason."""
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.download(configuration="../etc/passwd", file="firmware.bin")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_rename_rejects_traversal_in_new_name(tmp_path: Path) -> None:
    """``firmware/rename`` validates the derived ``<new_name>.yaml``.

    Direct WS clients can bypass ``DevicesController.rename_device``
    (which already validates) and call ``firmware/rename`` directly.
    Without a boundary check on ``new_name``, a value like
    ``../etc/passwd`` would land as the new device YAML path —
    traversal at flash time. The handler now reuses the same
    ``_validate_configuration_boundary`` to gate it.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
