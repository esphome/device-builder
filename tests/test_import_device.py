"""Tests for the ``devices/import`` command path.

Covers two regressions discovered while wiring up the adoption flow:

* ``import_config`` lives at ``esphome.components.dashboard_import``,
  not ``esphome.config_helpers``. The previous import path silently
  became ``None`` and every adoption attempt raised a generic
  RuntimeError before doing anything.
* When the target YAML already exists, ``import_config`` raises
  ``FileExistsError``. We re-surface it as a ``CommandError`` so the
  dashboard can show a useful message instead of the WS layer's
  generic ``Command failed`` fallback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers import devices as devices_module
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _make_controller(tmp_path: Any) -> DevicesController:
    """Stand up a controller without booting the full DeviceBuilder."""
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = MagicMock()
    ctrl._db.settings.rel_path = lambda name: tmp_path / name
    ctrl._scanner = MagicMock()
    ctrl._scanner.scan = AsyncMock()
    return ctrl


def test_import_config_resolves_at_import_time() -> None:
    """Regression guard for the import path move.

    ``import_config`` used to be imported via ``esphome.config_helpers``
    behind a try/except, so a wrong path silently became ``None`` and
    every adoption attempt raised. The current call site imports
    directly from ``esphome.components.dashboard_import``; if that
    module ever moves we want the test suite to fail loudly here, not
    a user's first adoption attempt.
    """
    assert devices_module.import_config is not None
    assert callable(devices_module.import_config)


async def test_import_device_invokes_import_config_and_returns_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: write the YAML, run a scan, return the configuration name."""
    captured: dict[str, Any] = {}

    def fake_import_config(*args: Any, **_kwargs: Any) -> None:
        captured["args"] = args

    monkeypatch.setattr(devices_module, "import_config", fake_import_config)
    ctrl = _make_controller(tmp_path)

    result = await ctrl.import_device(
        name="kitchen-1a2b3c",
        project_name="acme.kitchen",
        package_import_url="github://acme/firmware.yaml@main",
        friendly_name="Kitchen",
        encryption="true",
    )

    assert result == {"configuration": "kitchen-1a2b3c.yaml"}
    # Argument order matters — upstream signature is
    # ``(path, name, friendly_name, project_name, import_url, network, encryption)``.
    args = captured["args"]
    assert args[0] == tmp_path / "kitchen-1a2b3c.yaml"
    assert args[1] == "kitchen-1a2b3c"
    assert args[2] == "Kitchen"
    assert args[3] == "acme.kitchen"
    assert args[4] == "github://acme/firmware.yaml@main"
    assert args[6] == "true"  # encryption flag forwarded
    ctrl._scanner.scan.assert_awaited_once()


async def test_import_device_translates_file_exists_to_command_error(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FileExistsError`` becomes a user-facing ``CommandError``.

    The WS layer turns generic exceptions into ``Command failed: …``;
    the dashboard's adopt dialog can't surface that meaningfully. The
    handler catches ``FileExistsError`` and re-raises as a
    ``CommandError`` carrying ``INVALID_ARGS`` and a message that
    names the offending file.
    """

    def raises_file_exists(*_args: Any, **_kwargs: Any) -> None:
        raise FileExistsError

    monkeypatch.setattr(devices_module, "import_config", raises_file_exists)
    ctrl = _make_controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.import_device(
            name="kitchen",
            project_name="x",
            package_import_url="github://x",
        )

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "kitchen.yaml already exists" in excinfo.value.message
    # Scan must NOT run when the YAML write failed — otherwise we'd
    # falsely advertise a successful adoption to subscribers.
    ctrl._scanner.scan.assert_not_called()


async def test_import_device_returns_even_when_post_scan_fails(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan failure after a successful YAML write must not roll back.

    The YAML is on disk; failing the WS command would leave the user
    in a state where retrying produces ``FileExistsError`` despite
    nothing being wrong. Best-effort scan; the periodic poll picks up
    whatever this attempt missed.
    """
    monkeypatch.setattr(devices_module, "import_config", lambda *a, **kw: None)
    ctrl = _make_controller(tmp_path)
    ctrl._scanner.scan = AsyncMock(side_effect=RuntimeError("transient"))

    result = await ctrl.import_device(
        name="kitchen",
        project_name="x",
        package_import_url="github://x",
    )

    assert result == {"configuration": "kitchen.yaml"}
