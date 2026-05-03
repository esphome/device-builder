"""Shared fixtures for ``tests/controllers/devices/``.

The seed-a-real-device-on-disk shape (YAML + StorageJSON sidecar +
``.device-builder.json`` entry) was duplicated across ``test_archive.py``
and ``test_rename_inline_e2e.py`` and is the natural home for the
next handful of e2e tests that walk the file-ops layer. Centralising
keeps the YAML / StorageJSON shape one place to audit when ESPHome
adds fields to the upstream ``StorageJSON`` schema.

The sync ``set_device_metadata`` write goes through
``metadata_transaction`` which calls ``tempfile.mkstemp`` — that
trips ``blockbuster`` from an async test context. The fixture
wraps it in ``asyncio.to_thread`` so callers don't have to remember.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import set_device_metadata
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.models import EventType


def _make_board_stub(board_id: str) -> MagicMock:
    """Build a catalog-result stub with the right ``id`` shape.

    The catalog lookup methods (``find_by_pio_board`` /
    ``find_by_platform_variant`` / ``get_board``) return
    ``BoardCatalogEntry``-shaped objects; tests only ever read
    ``.id`` off the result, so a ``MagicMock`` with that one attr
    set is enough.
    """
    matched = MagicMock()
    matched.id = board_id
    return matched


class StubBoardLookups:
    """Stub the ``controller._db.boards`` lookup methods without poking the mock directly.

    Centralises the ``MagicMock(return_value=...)`` /
    ``AsyncMock(return_value=...)`` boilerplate that
    ``test_derive_board_id``, ``test_create``, and any future
    catalog-driven test would otherwise repeat. Each ``*_returns``
    method takes a board id (string → return a
    ``BoardCatalogEntry``-shaped stub with that id) or ``None``
    (the lookup misses), and returns the underlying mock so a
    test can later ``assert_called_once_with(...)`` on it.

    For tests that need a specific catalog entry passed back
    (e.g. with custom platform/template fields), assign directly
    to ``controller._db.boards.<method>``; this helper is for the
    common id-only shape that 90% of catalog-driven tests want.
    """

    def __init__(self, controller: DevicesController) -> None:
        self._boards = controller._db.boards

    def find_by_pio_board_returns(self, board_id: str | None) -> MagicMock:
        """Stub the PIO-board lookup. ``None`` → miss; ``str`` → match with that id."""
        mock = MagicMock(return_value=_make_board_stub(board_id) if board_id is not None else None)
        self._boards.find_by_pio_board = mock
        return mock

    def find_by_platform_variant_returns(self, board_id: str | None) -> MagicMock:
        """Stub the platform-variant fallback lookup."""
        mock = MagicMock(return_value=_make_board_stub(board_id) if board_id is not None else None)
        self._boards.find_by_platform_variant = mock
        return mock

    def get_board_returns(self, board_id: str | None) -> AsyncMock:
        """Stub the async ``get_board(board_id)`` lookup the create path uses."""
        mock = AsyncMock(return_value=_make_board_stub(board_id) if board_id is not None else None)
        self._boards.get_board = mock
        return mock


class StubBus:
    """Minimal event-bus stand-in for tests that exercise the listener wiring.

    ``add_listener`` records the registration and returns a
    callable that bumps ``unsub_calls`` when invoked — production
    ``EventBus`` returns a closure that removes the entry, so the
    return value's call-on-close behaviour is what
    ``DevicesController.stop()`` relies on. Tests that need a
    full ``DeviceBuilder``-shaped stub can pull this in via
    ``make_db``.
    """

    def __init__(self) -> None:
        self.listeners: list[tuple[EventType, Any]] = []
        self.unsub_calls = 0

    def add_listener(self, event_type: EventType, handler: Any) -> Any:
        self.listeners.append((event_type, handler))

        def _unsub() -> None:
            self.unsub_calls += 1

        return _unsub


class MakeDbFactory(Protocol):
    """Shape of the ``make_db`` fixture's return value."""

    def __call__(self, config_dir: Path) -> MagicMock: ...


@pytest.fixture
def make_db() -> MakeDbFactory:
    """Return a factory that builds a ``DeviceBuilder``-shaped stub.

    The shape is the minimum ``DevicesController.__init__`` reads:
    ``settings.config_dir`` / ``settings.absolute_config_dir`` /
    ``settings.password`` and ``bus`` (a :class:`StubBus`).
    Tests that construct a real ``DevicesController`` (i.e. don't
    bypass-init via ``make_controller``) want this; everything
    else just uses ``make_controller``.
    """

    def _make(config_dir: Path) -> MagicMock:
        db = MagicMock()
        db.settings.config_dir = config_dir
        db.settings.absolute_config_dir = config_dir.resolve()
        db.settings.password = ""  # ConfigController-side; harmless for tests
        db.bus = StubBus()
        return db

    return _make


class SeedDeviceFactory(Protocol):
    """Shape of the ``seed_device`` fixture's return value."""

    async def __call__(
        self,
        config_dir: Path,
        configuration: str,
        *,
        friendly_name: str | None = ...,
        storage_friendly: str | None = ...,
        board_id: str = ...,
        loaded_integrations: list[str] | None = ...,
        with_build_dir: bool = ...,
        address: str | None = ...,
        write_metadata: bool = ...,
    ) -> tuple[Path, Path]: ...


@pytest.fixture
def seed_device() -> SeedDeviceFactory:
    """Return a coroutine that seeds a fully-populated device on disk.

    Drops three artefacts mirroring what a real ``devices/create``
    + first compile would produce:

    - ``<config_dir>/<configuration>`` — a minimal but valid
      ESPHome YAML with ``esphome.name`` matching the filename
      stem and a configurable ``friendly_name``.
    - ``<config_dir>/.esphome/storage/<configuration>.json`` —
      a full ``StorageJSON`` sidecar shaped like what ``esphome
      compile --only-generate`` writes (every field the dashboard
      reads from is present).
    - ``<config_dir>/.device-builder.json`` — a sidecar metadata
      entry with ``board_id`` + ``friendly_name`` so tests that
      exercise the metadata-move branch (rename) or the
      identity-keep branch (archive) have something to assert.

    ``storage_friendly`` lets a caller pin the StorageJSON's
    ``friendly_name`` to a value distinct from the YAML stem so
    rename tests can verify the "friendly_name == old_name →
    rewrite" conditional branches the helpers under test.

    Async because the sidecar write goes through
    ``metadata_transaction`` (which calls ``tempfile.mkstemp``);
    blockbuster on the CI Linux runners flags the underlying
    ``os.path.abspath`` from a synchronous async-test context.
    Pushing the write to a thread keeps the seed call cheap to
    use from any ``@pytest.mark.asyncio`` test.
    """

    async def _seed(
        config_dir: Path,
        configuration: str,
        *,
        friendly_name: str | None = None,
        storage_friendly: str | None = None,
        board_id: str = "generic-esp32c3",
        loaded_integrations: list[str] | None = None,
        with_build_dir: bool = False,
        address: str | None = None,
        write_metadata: bool = True,
    ) -> tuple[Path, Path]:
        name = configuration.removesuffix(".yaml").removesuffix(".yml")
        yaml_path = config_dir / configuration
        yaml_text = (
            "esphome:\n"
            f"  name: {name}\n"
            f'  friendly_name: "{friendly_name or name.title()}"\n'
            "  platform: ESP32\n"
            "  board: esp32-c3-devkitm-1\n"
        )
        yaml_path.write_text(yaml_text, encoding="utf-8")

        # Build dir is opt-in — only delete / archive flows actually
        # care about its presence. Default off keeps the seed call
        # cheap for tests that just need YAML + storage + sidecar.
        build_path = config_dir / ".esphome" / "build" / name
        if with_build_dir:
            build_path.mkdir(parents=True, exist_ok=True)
            (build_path / "firmware.bin").write_bytes(b"\x00" * 16)
            (build_path / "src").mkdir()
            (build_path / "src" / "main.cpp").write_text("// fake\n", encoding="utf-8")

        storage_dir = config_dir / ".esphome" / "storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        (storage_dir / f"{configuration}.json").write_text(
            json.dumps(
                {
                    "storage_version": 1,
                    "name": name,
                    "friendly_name": (storage_friendly if storage_friendly is not None else name),
                    "comment": None,
                    "esphome_version": "2026.5.0-dev",
                    "src_version": 1,
                    "address": address if address is not None else f"{name}.local",
                    "web_port": None,
                    "esp_platform": "esp32",
                    "board": "esp32-c3-devkitm-1",
                    "build_path": str(build_path),
                    "firmware_bin_path": str(build_path / ".pioenvs" / "firmware.bin"),
                    "loaded_integrations": loaded_integrations
                    if loaded_integrations is not None
                    else ["api"],
                    "loaded_platforms": ["esp32"],
                    "no_mdns": False,
                    "framework": "esp-idf",
                    "core_platform": "esp32",
                    "target_platform": "esp32",
                }
            ),
            encoding="utf-8",
        )

        # ``metadata_transaction`` reaches into ``tempfile.mkstemp``
        # which is sync; push it to a thread so blockbuster doesn't
        # fault on the ``os.path.abspath`` from inside an async test.
        # Skip when the caller wants a bare seed (``write_metadata=False``)
        # — useful for tests that exercise "no identity fields ever
        # set" branches and need an empty ``.device-builder.json``.
        if write_metadata:
            await asyncio.to_thread(
                set_device_metadata,
                config_dir,
                configuration,
                board_id=board_id,
                friendly_name=friendly_name or name,
            )
        return yaml_path, build_path

    return _seed


class MakeControllerFactory(Protocol):
    """Shape of the ``make_controller`` fixture's return value."""

    def __call__(
        self,
        config_dir: Path,
        *,
        with_regenerate_state: bool = ...,
        with_state_monitor: bool = ...,
        with_boards: bool = ...,
        esphome_cmd: list[str] | None = ...,
    ) -> DevicesController: ...


@pytest.fixture
def make_controller() -> MakeControllerFactory:
    """Return a factory that builds a bypass-init ``DevicesController``.

    Most ``tests/controllers/devices/`` files exercise a single
    handler in isolation and don't want to spin up a full
    ``DeviceBuilder``. They've each grown their own
    ``__new__``-bypass helper that attaches:

    - ``_db`` (a ``MagicMock`` with ``settings.config_dir`` /
      ``settings.rel_path`` wired against the test's tmp dir);
    - ``_scanner`` (``MagicMock`` with ``scan`` / ``reload`` as
      ``AsyncMock``).

    The shape was duplicated across ``test_archive.py``,
    ``test_rename_inline_e2e.py``, and the new
    ``test_storage_regenerate_e2e.py``. Centralising means the
    next test in this directory inherits the same wiring without
    copy-pasting (and a ``DevicesController`` field rename only
    has to update one site).

    ``with_regenerate_state=True`` adds the three guards
    (``_regenerate_pending`` / ``_regenerate_failed`` /
    ``_regenerate_lock``) plus a real
    ``_db.create_background_task`` that records spawned tasks on
    ``controller._spawned_tasks`` (test-only attr) so callers can
    ``await`` them — used by the ``_schedule_storage_regenerate``
    tests where the controller really fires off background work
    via the production code path.

    ``esphome_cmd`` populates ``_esphome_cmd`` so the
    early-return guard at the top of
    ``_schedule_storage_regenerate`` doesn't short-circuit;
    leave it ``None`` for tests that don't reach that code path.

    ``with_state_monitor=True`` attaches a ``MagicMock``-shaped
    ``_state_monitor`` for tests that exercise paths reading the
    cached-address / get_cached_addresses lookup (``import_device``,
    ``create_device``). The monitor's ``get_cached_addresses``
    defaults to returning ``None`` so the fast-online branch
    short-circuits unless a test overrides it.

    ``with_boards=True`` attaches a ``MagicMock``-shaped
    ``_db.boards`` for tests that go through the
    board-catalog lookup path (``create_device`` and the
    board-id derivation).
    """

    def _make(
        config_dir: Path,
        *,
        with_regenerate_state: bool = False,
        with_state_monitor: bool = False,
        with_boards: bool = False,
        esphome_cmd: list[str] | None = None,
    ) -> DevicesController:
        controller = DevicesController.__new__(DevicesController)
        controller._db = MagicMock()
        controller._db.settings.config_dir = config_dir
        controller._db.settings.rel_path = lambda configuration: config_dir / configuration
        controller._scanner = MagicMock()
        controller._scanner.scan = AsyncMock()
        controller._scanner.reload = AsyncMock()

        if with_state_monitor:
            controller._state_monitor = MagicMock()
            controller._state_monitor.get_cached_addresses = MagicMock(return_value=None)

        if with_boards:
            controller._db.boards = MagicMock()

        if with_regenerate_state:
            spawned_tasks: list[asyncio.Task[object]] = []

            def _create_bg(coro: object) -> asyncio.Task[object]:
                task = asyncio.get_running_loop().create_task(coro)  # type: ignore[arg-type]
                spawned_tasks.append(task)
                return task

            controller._db.create_background_task = _create_bg
            controller._spawned_tasks = spawned_tasks  # type: ignore[attr-defined]
            controller._regenerate_pending = set()
            controller._regenerate_failed = set()
            controller._regenerate_lock = asyncio.Lock()

        if esphome_cmd is not None:
            controller._esphome_cmd = esphome_cmd

        return controller

    return _make


@pytest.fixture
def redirect_storage_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ``ext_storage_path`` at ``tmp_path/.esphome/storage/``.

    The real helper walks ``CORE.config_path`` which isn't set in
    isolated tests; redirecting keeps the file ops on disk under
    the test's tmp dir without spinning up a CORE. Used by tests
    that exercise the StorageJSON-move helpers (rename, archive).
    """
    storage_dir = tmp_path / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)

    def _ext(configuration: str) -> Path:
        return storage_dir / f"{configuration}.json"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.ext_storage_path",
        _ext,
    )
    # ``_archive_single`` lives in ``controller.py`` but delegates to
    # ``_wipe_device_build_dir`` and ``_remove_device_sidecars`` over
    # in ``helpers.py``. Both files import ``ext_storage_path``
    # independently; rebinding only one leaves the other path running
    # against the real CORE.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.helpers.ext_storage_path",
        _ext,
    )

    # ``StorageJSON.save`` itself uses the upstream ``ext_storage_path``
    # if a caller passes the configuration name without a path; for
    # safety, also patch that lookup so any indirect caller stays
    # under ``tmp_path``.
    monkeypatch.setattr(
        "esphome.storage_json.ext_storage_path",
        _ext,
        raising=False,
    )
