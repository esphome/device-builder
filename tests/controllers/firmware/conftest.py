"""Shared fixtures for ``tests/controllers/firmware/``.

Most handler-level tests in this package were each carrying their
own ``_controller(tmp_path)`` helper that built a stub
``FirmwareController`` with ``__new__``, wired a real
``DashboardSettings`` for path validation, and stubbed the
queue / persistence / supersede / bus surface. The bodies were
nearly identical across a dozen files; centralising the build
here keeps them in sync when the controller's attribute set
shifts (every refactor that adds a new ``self._something`` had
to chase the same pattern across every test file before this).

Tests instantiate via the ``firmware_controller_factory``
fixture: pass any ``FirmwareJob`` instances positionally to
preload ``_jobs``, set ``with_settings=False`` to skip the
``DashboardSettings`` binding for tests that don't exercise
``rel_path``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import FirmwareJob


@pytest.fixture
def firmware_controller_factory(
    tmp_path: Path,
) -> Callable[..., FirmwareController]:
    """
    Build stub ``FirmwareController`` instances wired to ``tmp_path``.

    Returns a callable: ``factory(*jobs, with_settings=True)``.

    Wires the surface a typical handler-wiring test reaches for:

    - ``_jobs`` — populated from the positional ``FirmwareJob``
      arguments (so tests can preload running / queued / terminal
      jobs without touching the dict directly).
    - ``_queue`` / ``_persist_jobs`` / ``_supersede_active_jobs`` /
      ``_terminate_current_process`` — ``AsyncMock`` stubs.
    - ``_current_job`` / ``_current_process`` — ``None``.
    - ``_cancel_requested`` — empty set.
    - ``_db`` — a small namespace exposing ``.bus`` (``MagicMock``)
      and (when ``with_settings=True``, the default) ``.settings``
      (a real ``DashboardSettings`` whose ``config_dir`` is
      ``tmp_path``).

    Pass ``with_settings=False`` for tests that don't exercise
    ``rel_path`` (in-memory job inspectors, rename-lock checks,
    etc.). Handlers that nevertheless try to read ``settings``
    will then ``AttributeError`` rather than silently use a
    stub — which keeps the test surface honest about what each
    test actually exercises.
    """

    def _make(*jobs: FirmwareJob, with_settings: bool = True) -> FirmwareController:
        controller = FirmwareController.__new__(FirmwareController)
        controller._jobs = {j.job_id: j for j in jobs}
        controller._current_job = None
        controller._current_process = None
        controller._cancel_requested = set()
        controller._queue = AsyncMock()
        controller._persist_jobs = AsyncMock()
        controller._supersede_active_jobs = AsyncMock()
        controller._terminate_current_process = AsyncMock()

        bus = MagicMock()
        db_attrs: dict[str, Any] = {"bus": bus}
        if with_settings:
            settings = DashboardSettings()
            settings.config_dir = tmp_path
            settings.absolute_config_dir = tmp_path.resolve()
            db_attrs["settings"] = settings
        controller._db = type("DB", (), db_attrs)()
        return controller

    return _make
