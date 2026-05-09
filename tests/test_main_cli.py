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
import logging
import sys
import threading
from collections.abc import Generator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from esphome_device_builder import __main__ as main_module
from esphome_device_builder import constants
from esphome_device_builder.helpers.logging import LoggingQueueHandler
from esphome_device_builder.helpers.single_instance import SingleInstanceLock

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


# ---------------------------------------------------------------------------
# __main__._setup_logging — uncaught-exception hooks
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_logging_globals() -> Generator[None]:
    """
    Snapshot the global state ``_setup_logging`` mutates and restore it.

    ``_setup_logging`` reassigns ``sys.excepthook`` /
    ``threading.excepthook`` and adds handlers (including a
    ``LoggingQueueHandler`` whose listener thread keeps running) to
    ``logging.root``. Without restoration these leak into other tests
    in the suite, and a stuck-running listener thread can hold
    references to handlers that the next test then mutates.
    """
    saved_sys_hook = sys.excepthook
    saved_thread_hook = threading.excepthook
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    logging.root.handlers = []
    try:
        yield
    finally:
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        for handler in saved_handlers:
            logging.root.addHandler(handler)
        logging.root.setLevel(saved_level)
        sys.excepthook = saved_sys_hook
        threading.excepthook = saved_thread_hook


def test_setup_logging_routes_uncaught_main_thread_exception_through_logger(
    _isolated_logging_globals: None,
) -> None:
    """``sys.excepthook`` forwards ``(type, value, tb)`` to ``logger.exception``."""
    main_module._setup_logging("info")

    # ``_setup_logging`` replaces the default — the new hook must
    # forward the triple verbatim so ``logging.Formatter`` can render
    # the traceback.
    assert sys.excepthook is not sys.__excepthook__

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()

    with patch("logging.getLogger") as mock_get_logger:
        sys.excepthook(*exc_info)

    mock_get_logger.assert_called_once_with()
    mock_get_logger.return_value.exception.assert_called_once_with(
        "Uncaught exception", exc_info=exc_info
    )


def test_setup_logging_routes_uncaught_thread_exception_through_logger(
    _isolated_logging_globals: None,
) -> None:
    """``threading.excepthook`` unpacks ``ExceptHookArgs`` into ``exc_info``."""
    main_module._setup_logging("info")

    assert threading.excepthook is not threading.__excepthook__

    args = MagicMock(spec=threading.ExceptHookArgs)
    args.exc_type = RuntimeError
    args.exc_value = RuntimeError("boom")
    args.exc_traceback = None
    args.thread = None

    with patch("logging.getLogger") as mock_get_logger:
        threading.excepthook(args)

    mock_get_logger.assert_called_once_with()
    mock_get_logger.return_value.exception.assert_called_once_with(
        "Uncaught thread exception",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def test_setup_logging_excepthooks_log_through_queue_listener(
    _isolated_logging_globals: None,
) -> None:
    """End-to-end: a hook firing reaches a handler behind the queue listener."""
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    capture = _Capture()
    logging.root.addHandler(capture)

    main_module._setup_logging("debug")

    try:
        raise RuntimeError("boom-from-main-thread")
    except RuntimeError:
        sys.excepthook(*sys.exc_info())

    # The queue handler offloads emission to a worker thread; drain by
    # stopping the listener so the assertion sees the record.
    queue_handler = next(h for h in logging.root.handlers if isinstance(h, LoggingQueueHandler))
    listener = queue_handler.listener
    assert listener is not None
    listener.stop()
    queue_handler.listener = None

    # ``QueueHandler.prepare`` bakes the traceback into ``record.msg``
    # and nulls ``exc_info`` / ``exc_text`` so the record stays
    # picklable on the way through the queue.
    messages = [r.getMessage() for r in captured]
    assert any(
        "Uncaught exception" in m and "RuntimeError: boom-from-main-thread" in m for m in messages
    )


# ---------------------------------------------------------------------------
# main() <> single-instance lock integration
#
# The ``with ensure_single_execution(...)`` block in ``main()`` has two
# user-visible outcomes: success (proceed to build the dashboard) and
# contention (exit non-zero before constructing ``DeviceBuilder``). Both
# tests patch the helper to yield a synthesised lock so we don't need to
# spin up a real subprocess holder, and patch ``DeviceBuilder`` so the
# success path doesn't actually start the dashboard's network sockets /
# mDNS browsers.
#
# Both tests use ``_isolated_logging_globals`` because ``main()`` calls
# ``_setup_logging`` which mutates ``sys.excepthook`` /
# ``threading.excepthook`` and adds a ``LoggingQueueHandler`` to
# ``logging.root`` whose listener thread can leak across tests
# otherwise.
# ---------------------------------------------------------------------------


@contextmanager
def _fake_lock_yielding(
    exit_code: int | None,
) -> Generator[Generator[SingleInstanceLock]]:
    """
    Build a stand-in for ``ensure_single_execution`` that yields a fixed lock.

    Returned as a context-manager factory: callers ``patch(...)``
    it onto ``ensure_single_execution`` and the dashboard's
    ``main()`` then receives a ``SingleInstanceLock`` with the
    requested ``exit_code`` instead of touching the real
    filesystem / flock.
    """

    @contextmanager
    def _inner(_config_dir: Path) -> Generator[SingleInstanceLock]:
        yield SingleInstanceLock(exit_code=exit_code)

    yield _inner


def test_main_exits_when_lock_contention_blocks_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_logging_globals: None,
) -> None:
    """
    A failed lock acquisition aborts ``main()`` before ``DeviceBuilder``.

    Drives the contention shape: ``ensure_single_execution``
    yields ``exit_code=1`` and ``main()`` is expected to
    ``sys.exit(1)`` without ever constructing the
    ``DeviceBuilder`` (which would otherwise start real
    sockets / mDNS browsers).
    """
    monkeypatch.setattr("sys.argv", ["esphome-device-builder", str(tmp_path)])
    device_builder_ctor = MagicMock()
    with (
        _fake_lock_yielding(exit_code=1) as fake_lock,
        patch(
            "esphome_device_builder.helpers.single_instance.ensure_single_execution",
            fake_lock,
        ),
        patch(
            "esphome_device_builder.device_builder.DeviceBuilder",
            device_builder_ctor,
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        main_module.main()

    assert excinfo.value.code == 1
    # The contention path must not reach the constructor — that's
    # the whole point of refusing to start.
    device_builder_ctor.assert_not_called()


def test_main_runs_device_builder_when_lock_acquired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_logging_globals: None,
) -> None:
    """
    Lock acquired → ``DeviceBuilder.run()`` runs.

    Mirrors the contention test but with the success outcome
    (``exit_code=None``) so the ``with`` body's "construct +
    run" path gets coverage too. ``DeviceBuilder.run`` is
    patched to a ``MagicMock`` so the unit test doesn't actually
    start the dashboard.
    """
    monkeypatch.setattr("sys.argv", ["esphome-device-builder", str(tmp_path)])
    instance = MagicMock()
    device_builder_ctor = MagicMock(return_value=instance)
    with (
        _fake_lock_yielding(exit_code=None) as fake_lock,
        patch(
            "esphome_device_builder.helpers.single_instance.ensure_single_execution",
            fake_lock,
        ),
        patch(
            "esphome_device_builder.device_builder.DeviceBuilder",
            device_builder_ctor,
        ),
    ):
        main_module.main()

    device_builder_ctor.assert_called_once()
    instance.run.assert_called_once()
