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

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def test_sync_validate_rejects_traversal(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The sync core raises ``CommandError(INVALID_ARGS)`` on traversal.

    This is the single-source-of-truth helper used by both the async
    wrapper and the bulk validator — so a future change to validation
    logic only needs one update site.
    """
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        controller._sync_validate_configuration_boundary("../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


def test_sync_validate_rejects_empty_string(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Empty configuration raises — only ``RESET_BUILD_ENV`` legitimately wants it.

    ``reset_build_env`` doesn't go through the validator at all; every
    other handler does, so accepting ``""`` here would let a client call
    ``compile`` / ``upload`` / ``clean`` / ``install`` / ``rename`` with
    an empty ``configuration`` value, get back a queued ``FirmwareJob``,
    and only fail later when the runner hands the empty string to the
    CLI.
    """
    controller = firmware_controller_factory()
    with pytest.raises(CommandError) as excinfo:
        controller._sync_validate_configuration_boundary("")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must not be empty" in excinfo.value.message


async def test_validate_configuration_boundary_runs_in_executor(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The async wrapper runs ``rel_path`` in an executor.

    ``Path.resolve`` is a blocking syscall; running it on the event
    loop would fault under blockbuster on CI. The wrapper hands the
    sync core off to ``run_in_executor`` so the WS dispatcher stays
    non-blocking.
    """
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller._validate_configuration_boundary("../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_validate_configurations_boundary_raises_on_bad_entry(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The bulk validator raises on the first invalid entry.

    Bulk handlers reject the whole batch on bad input rather than
    silently dropping the offending entry — a typo in one of N
    configurations is something the caller wants to know about,
    not have masked by partial success. Rename-lock conflicts at
    enqueue time stay skip-and-continue (transient state, not bad
    input).
    """
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller._validate_configurations_boundary(
            ["kitchen.yaml", "../etc/passwd", "garage.yaml"]
        )
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_validate_configurations_boundary_all_valid(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A clean batch validates without raising and returns ``None``."""
    controller = firmware_controller_factory()

    await controller._validate_configurations_boundary(["kitchen.yaml", "garage.yaml"])


async def test_get_binaries_rejects_traversal(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/get_binaries`` re-validates because it bypasses ``rel_path``.

    The handler reads ``ext_storage_path(configuration)`` which lands
    in ``<data_dir>/storage/<configuration>.json`` — outside the
    config dir — so the boundary validator is the only gate.
    """
    controller = firmware_controller_factory()

    with pytest.raises(CommandError) as excinfo:
        await controller.get_binaries(configuration="../../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_rename_rejects_traversal_in_new_name(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/rename`` validates the derived ``<new_name>.yaml``.

    Direct WS clients can bypass ``DevicesController.rename_device``
    (which already validates) and call ``firmware/rename`` directly.
    Without a boundary check on ``new_name``, a value like
    ``../etc/passwd`` would land as the new device YAML path —
    traversal at flash time. The handler now reuses the same
    ``_validate_configuration_boundary`` to gate it.
    """
    controller = firmware_controller_factory()
    (tmp_path / "kitchen.yaml").write_text("")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


async def test_rename_rejects_collision_with_existing_yaml(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/rename`` rejects ``new_name`` colliding with another device.

    ``esphome rename`` does not check collisions itself — it blindly
    ``write_text``s the new YAML and OTA-installs it, silently
    overwriting the unrelated device's config and flashing that
    firmware to the wrong device. ``DevicesController.rename_device``
    checks before forwarding, but a direct WS client can bypass it;
    the firmware-layer check closes that gap.
    """
    controller = firmware_controller_factory()
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "livingroom.yaml").write_text("")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="livingroom")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "livingroom.yaml already exists" in excinfo.value.message


async def test_rename_rejects_same_name(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/rename`` rejects ``new_name`` matching the current stem.

    A same-name rename is a no-op at the YAML level but still queues
    a real ``esphome rename`` job that re-compiles and OTA-flashes
    the device — wasted work the caller almost certainly didn't
    intend. ``firmware/install`` is the correct command for "flash
    without renaming".
    """
    controller = firmware_controller_factory()
    (tmp_path / "kitchen.yaml").write_text("")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename(configuration="kitchen.yaml", new_name="kitchen")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message
