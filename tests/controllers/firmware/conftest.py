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
fixture. The factory exposes three independent opt-ins
(``with_settings`` / ``with_queue`` / ``with_terminate``) so
each test file gets exactly the surface its handler-under-test
actually touches — a refactor that accidentally reaches further
into the controller (e.g. a ``get_jobs`` call that suddenly
hits ``_queue``) crashes with ``AttributeError`` instead of
silently absorbing into a stub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import FirmwareJob


class FirmwareControllerFactory(Protocol):
    """
    Type for the ``firmware_controller_factory`` fixture.

    Exported so test files can annotate their fixture parameter
    without each redeclaring the callable shape — pylance / mypy
    then know that ``factory(...)`` returns a
    ``FirmwareController`` and that the kit flags are
    keyword-only.
    """

    def __call__(
        self,
        *jobs: FirmwareJob,
        with_settings: bool = ...,
        with_queue: bool = ...,
        with_terminate: bool = ...,
    ) -> FirmwareController: ...


@pytest.fixture
def firmware_controller_factory(
    tmp_path: Path,
) -> FirmwareControllerFactory:
    """
    Build stub ``FirmwareController`` instances wired to ``tmp_path``.

    Returns a callable: ``factory(*jobs, with_settings=True,
    with_queue=False, with_terminate=False)``.

    Three kit flags compose, each adding only the attributes the
    relevant code path reads — keeps the test surface honest
    about what each test exercises:

    - ``with_settings=True`` (default): wire ``self._db.settings``
      to a ``DashboardSettings`` whose ``config_dir`` is
      ``tmp_path``. Needed by every handler that calls
      ``rel_path``. Pass ``False`` for in-memory job inspectors
      where reading ``settings`` should hard-fail rather than
      silently use a stub.

    - ``with_queue=False`` (default): when set ``True``, install
      ``AsyncMock`` stubs for ``_queue`` and
      ``_supersede_active_jobs``. The submission handlers
      (``compile`` / ``upload`` / ``install`` / ``rename`` /
      ``compile_bulk`` / ``install_bulk`` / ``reset_build_env``)
      need this kit. The validator-only tests
      (``test_traversal_validation`` / ``test_get_binaries`` /
      ``test_download``) do not — leaving ``_queue``
      unattributed makes a regression that suddenly tries to
      enqueue a rejected request crash visibly.

    - ``with_terminate=False`` (default): when set ``True``,
      install ``_current_job`` / ``_current_process`` /
      ``_cancel_requested`` / ``_terminate_current_process``.
      Only ``cancel`` reaches into these.

    Always present: ``_jobs`` (populated from positional
    arguments), ``_persist_jobs`` (``AsyncMock``), ``_db.bus``
    (``MagicMock``). These three are universal enough that
    every test either expects them to be called or asserts they
    were not.
    """

    def _make(
        *jobs: FirmwareJob,
        with_settings: bool = True,
        with_queue: bool = False,
        with_terminate: bool = False,
    ) -> FirmwareController:
        controller = FirmwareController.__new__(FirmwareController)
        controller._jobs = {j.job_id: j for j in jobs}
        controller._persist_jobs = AsyncMock()

        bus = MagicMock()
        db_attrs: dict[str, Any] = {"bus": bus}
        if with_settings:
            settings = DashboardSettings()
            settings.config_dir = tmp_path
            settings.absolute_config_dir = tmp_path.resolve()
            db_attrs["settings"] = settings
        controller._db = type("DB", (), db_attrs)()

        if with_queue:
            controller._queue = AsyncMock()
            controller._supersede_active_jobs = AsyncMock()

        if with_terminate:
            controller._current_job = None
            controller._current_process = None
            controller._cancel_requested = set()
            controller._terminate_current_process = AsyncMock()

        return controller

    return _make
