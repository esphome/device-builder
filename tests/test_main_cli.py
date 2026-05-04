"""Tests for the ``--version`` CLI flag and its supporting helpers.

Two layers exercise the version path:

* ``_resolve_version`` (constants) — reads the installed package
  version from wheel metadata, falling back to ``0.0.0`` when the
  package isn't installed (source checkouts).
* ``_esphome_version`` / ``_format_version`` (``__main__``) — build
  the string ``--version`` prints. The bundled ESPHome version is
  optional; the formatter must degrade cleanly when the
  ``[esphome]`` extra is absent.

The end-to-end test pins the ``argparse action="version"`` exit
behaviour (stdout + ``SystemExit(0)``) so a future swap to a custom
action doesn't silently break the CLI shape that the bug-report
template relies on.
"""

from __future__ import annotations

import builtins
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from esphome_device_builder import __main__ as main_module
from esphome_device_builder import constants

# ---------------------------------------------------------------------------
# constants._resolve_version
# ---------------------------------------------------------------------------


def test_resolve_version_returns_metadata_version() -> None:
    """Real installs return the version stamped into ``pyproject.toml``."""
    with patch("esphome_device_builder.constants.version", return_value="2026.5.0"):
        assert constants._resolve_version() == "2026.5.0"


def test_resolve_version_falls_back_when_package_missing() -> None:
    """Source checkouts without an editable install fall back to ``0.0.0``."""
    with patch(
        "esphome_device_builder.constants.version",
        side_effect=PackageNotFoundError("esphome-device-builder"),
    ):
        assert constants._resolve_version() == "0.0.0"


# ---------------------------------------------------------------------------
# __main__._esphome_version
# ---------------------------------------------------------------------------


def test_esphome_version_returns_string_when_installed() -> None:
    """With the ``[esphome]`` extra installed the function returns the version."""
    # The test environment installs ``esphome``, so the helper resolves
    # to whatever ``esphome.const.__version__`` is. Pin only the shape
    # (a non-empty string) so the test doesn't break on every esphome
    # bump.
    result = main_module._esphome_version()
    assert isinstance(result, str)
    assert result


def test_esphome_version_returns_none_when_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the ``[esphome]`` extra the function returns ``None``.

    Simulated by replacing ``__import__`` with one that raises for
    ``esphome`` / ``esphome.const``. The function-level import inside
    ``_esphome_version`` re-runs each call, so the patch fully
    controls which branch fires.
    """
    real_import = builtins.__import__

    def _block_esphome(
        name: str,
        globals: object = None,  # noqa: A002 — matches ``__import__`` signature
        locals: object = None,  # noqa: A002
        fromlist: object = (),
        level: int = 0,
    ) -> object:
        if name == "esphome" or name.startswith("esphome."):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _block_esphome)

    assert main_module._esphome_version() is None


# ---------------------------------------------------------------------------
# __main__._format_version
# ---------------------------------------------------------------------------


def test_format_version_includes_esphome_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both versions are present → ``... X.Y.Z (esphome A.B.C)``."""
    monkeypatch.setattr(main_module, "__version__", "2026.5.0")
    monkeypatch.setattr(main_module, "_esphome_version", lambda: "2026.3.1")
    assert main_module._format_version() == "esphome-device-builder 2026.5.0 (esphome 2026.3.1)"


def test_format_version_omits_esphome_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``[esphome]`` extra → the parenthetical is dropped entirely."""
    monkeypatch.setattr(main_module, "__version__", "2026.5.0")
    monkeypatch.setattr(main_module, "_esphome_version", lambda: None)
    assert main_module._format_version() == "esphome-device-builder 2026.5.0"


# ---------------------------------------------------------------------------
# end-to-end ``main(['--version'])``
# ---------------------------------------------------------------------------


def test_main_version_flag_prints_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--version`` writes the formatted string to stdout and exits ``0``."""
    monkeypatch.setattr("sys.argv", ["esphome-device-builder", "--version"])
    monkeypatch.setattr(main_module, "__version__", "2026.5.0")
    monkeypatch.setattr(main_module, "_esphome_version", lambda: "2026.3.1")

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert "esphome-device-builder 2026.5.0 (esphome 2026.3.1)" in captured.out
