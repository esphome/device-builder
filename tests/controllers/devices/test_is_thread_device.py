"""Tests for ``DevicesController.is_thread_device``."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import make_device
from tests.controllers.devices.conftest import MakeControllerFactory


def test_openthread_in_loaded_integrations(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)
    controller._scanner.devices = [
        make_device("mesh", loaded_integrations=["api", "openthread"]),
        make_device("kitchen", loaded_integrations=["api", "wifi"]),
        make_device("fresh", loaded_integrations=[]),
    ]

    assert controller.is_thread_device("mesh.yaml") is True
    assert controller.is_thread_device("kitchen.yaml") is False
    assert controller.is_thread_device("fresh.yaml") is False


def test_unknown_configuration_is_not_thread(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    controller = make_controller(tmp_path)

    assert controller.is_thread_device("ghost.yaml") is False
