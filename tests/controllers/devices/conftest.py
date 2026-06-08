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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._reachability_tracker import ReachabilityTracker
from esphome_device_builder.controllers.config import get_device_metadata, set_device_metadata
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._metadata_store import DeviceMetadataStore
from esphome_device_builder.controllers.devices._shared_sidecar import SharedSidecarClient
from esphome_device_builder.controllers.devices._state import DevicesState
from esphome_device_builder.controllers.devices._yaml_search_cache import YamlSearchCache
from esphome_device_builder.helpers.device_yaml import configuration_stem
from esphome_device_builder.helpers.event_bus import Event, EventBus
from esphome_device_builder.helpers.hostname import normalize_hostname
from esphome_device_builder.models import AdoptableDevice, Device, DeviceState, EventType
from tests._recording_scanner import RecordingScanner
from tests._storage_fixtures import write_storage_json
from tests.conftest import make_device


class RecordingStateMonitor:
    """Test fake for ``DeviceStateMonitor`` that captures every call.

    Mirrors every public method on the production
    ``DeviceStateMonitor`` (``apply`` / ``apply_ip`` /
    ``apply_version`` / ``apply_api_encryption`` /
    ``apply_config_hash`` / ``get_cached_addresses`` /
    ``get_cached_dns_addresses`` / ``probe_device`` /
    ``probe_device_ping`` / ``priority_for`` /
    ``revisit_importable`` /
    ``revisit_all_importables`` / ``get_importable_devices``)
    without any of the real monitor's I/O. Calls land in
    ``self.calls`` as flat tuples ``(method_name, *args)`` —
    assertion-time comparisons read like
    ``calls == [("apply", "kitchen", ONLINE, "mdns", True), ...]``
    instead of three scattered ``MagicMock.assert_called_*`` lines.

    Why a typed fake rather than ``MagicMock``: a typo (e.g.
    ``probe_devicee.assert_called_once``) silently passes against a
    ``MagicMock`` because it spawns a fresh attribute on access; a
    refactor renaming a real method (``apply_ip`` → ``set_ip``)
    similarly breaks the contract without breaking the assertion.
    Pinning the surface here means both classes of drift surface as
    ``AttributeError`` immediately. Mirroring the *full* public
    surface (rather than just what the first batch of tests
    needed) means a controller path like ``_on_scan_change(…,
    REMOVED)`` that calls ``revisit_importable`` won't blow up
    against the fake just because no earlier test exercised it.

    ``cached_addresses`` and ``cached_dns_addresses`` accept
    ``hostname → [ips]`` maps. Lookup keys are normalised through
    ``normalize_hostname`` so tests can pass production-equivalent
    inputs like ``Kitchen.local.`` and still hit the seeded entry.

    ``importable_devices`` lets tests pre-seed a list returned by
    ``get_importable_devices``; defaults to ``[]``.

    ``priority_map`` lets tests override the per-source priority
    returned by ``priority_for``; lookup falls back to ``"unknown"``
    for unmapped sources, matching production's default branch.
    """

    def __init__(
        self,
        *,
        cached_addresses: dict[str, list[str]] | None = None,
        cached_dns_addresses: dict[str, list[str]] | None = None,
        importable_devices: list[AdoptableDevice] | None = None,
        priority_map: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._cached = {normalize_hostname(k): v for k, v in (cached_addresses or {}).items()}
        self._cached_dns = {
            normalize_hostname(k): v for k, v in (cached_dns_addresses or {}).items()
        }
        self._importable = list(importable_devices or [])
        self._priority = priority_map or {}

    def apply(self, name: str, state: DeviceState, source: str, *, claim: bool = False) -> bool:
        self.calls.append(("apply", name, state, source, claim))
        return True

    def apply_ip(self, name: str, ip: str) -> bool:
        self.calls.append(("apply_ip", name, ip))
        return True

    def apply_ip_addresses(self, name: str, addresses: list[str]) -> bool:
        self.calls.append(("apply_ip_addresses", name, list(addresses)))
        return True

    def apply_version(self, name: str, version: str) -> bool:
        self.calls.append(("apply_version", name, version))
        return True

    def apply_api_encryption(self, name: str, encryption: str) -> bool:
        self.calls.append(("apply_api_encryption", name, encryption))
        return True

    def apply_config_hash(self, name: str, config_hash: str) -> bool:
        self.calls.append(("apply_config_hash", name, config_hash))
        return True

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        self.calls.append(("get_cached_addresses", host_name))
        return self._cached.get(normalize_hostname(host_name))

    def get_cached_dns_addresses(self, host_name: str) -> list[str] | None:
        self.calls.append(("get_cached_dns_addresses", host_name))
        return self._cached_dns.get(normalize_hostname(host_name))

    def probe_device(self, device_name: str, service_name: str | None = None) -> None:
        self.calls.append(("probe_device", device_name, service_name))

    def probe_device_ping(self, device_name: str) -> None:
        self.calls.append(("probe_device_ping", device_name))

    def priority_for(self, name: str) -> str:
        self.calls.append(("priority_for", name))
        return self._priority.get(name, "unknown")

    def revisit_importable(self, device_name: str) -> None:
        self.calls.append(("revisit_importable", device_name))

    def revisit_all_importables(self) -> None:
        self.calls.append(("revisit_all_importables",))

    def forget(self, name: str) -> None:
        self.calls.append(("forget", name))

    def get_importable_devices(self) -> list[AdoptableDevice]:
        self.calls.append(("get_importable_devices",))
        return list(self._importable)


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
    use from any async test.
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
        name = configuration_stem(configuration)
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

        write_storage_json(
            config_dir,
            configuration,
            firmware_bin_path=build_path / ".pioenvs" / "firmware.bin",
            build_path=build_path,
            overrides={
                "friendly_name": (storage_friendly if storage_friendly is not None else name),
                "address": address if address is not None else f"{name}.local",
                "loaded_integrations": (
                    loaded_integrations if loaded_integrations is not None else ["api"]
                ),
                "loaded_platforms": ["esp32"],
            },
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

    ``with_state_monitor=True`` attaches a typed
    :class:`RecordingStateMonitor` for tests that exercise paths
    reading the cached-address / get_cached_addresses lookup
    (``import_device``, ``create_device``). The fake's
    ``get_cached_addresses`` returns ``None`` for an empty cache
    so the fast-online branch short-circuits unless a test
    overrides it (``ctrl._state_monitor =
    RecordingStateMonitor(cached_addresses=...)``). Tests get the
    typo-resistant typed surface by default — no opt-in flag.

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
        controller.state = DevicesState()
        controller._yaml_write_locks = {}
        controller._db.secrets_write_lock = asyncio.Lock()
        controller._db.settings.config_dir = config_dir
        controller._db.settings.rel_path = lambda configuration: config_dir / configuration
        # Version history off by default — a MagicMock's
        # ``record_configuration`` isn't awaitable, and the YAML-write
        # tests don't care about commits. The version_history suite
        # exercises the commit path directly.
        controller._db.version_history = None
        # ``data_dir`` collapsed onto ``config_dir`` for tmp_path
        # cleanup; real stores so round-trips hit disk.
        controller._shutdown_callbacks = []
        controller._metadata_store = DeviceMetadataStore(
            config_dir=config_dir,
            data_dir=config_dir,
            shutdown_register=controller._shutdown_callbacks.append,
        )
        controller._shared_sidecar = SharedSidecarClient(config_dir)
        # Default the editor's ``validate_yaml`` to a passing result
        # so any handler that runs YAML through it (currently
        # ``edit_friendly_name``) doesn't end up awaiting the
        # MagicMock auto-attr and tripping. Tests that exercise the
        # validation-failure branch override this directly with an
        # ``AsyncMock`` that returns the error shape they want to pin.
        controller._db.editor.validate_yaml = AsyncMock(
            return_value={"yaml_errors": [], "validation_errors": []}
        )
        # ``RecordingScanner`` rather than a MagicMock-shaped scanner so a
        # typo or rename of ``scan``/``reload`` surfaces as
        # ``AttributeError`` instead of silently passing the assertion.
        controller._scanner = RecordingScanner()
        # ``yaml/search`` reads through this cache; tests that
        # exercise the search command need a real instance, and
        # the rest can ignore it. Cheap to instantiate
        # (just an asyncio.Lock + empty dict) so set it on every
        # bypass-init controller for parity with __init__.
        controller._yaml_search_cache = YamlSearchCache()
        controller._yaml_search_lock = asyncio.Lock()

        # Per-signal reachability tracker. Real instance (not a
        # mock) because the surface is small and tests that drive
        # ``_on_scan_change(REMOVED)`` reach into ``clear()``;
        # giving everyone the production class keeps the bypass
        # closer to ``__init__``'s wiring.
        controller._reachability = ReachabilityTracker()

        if with_state_monitor:
            controller._state_monitor = RecordingStateMonitor()

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
            controller.state.regenerate_pending = set()
            controller.state.regenerate_failed = set()
            controller._regenerate_lock = asyncio.Lock()

        controller.state.esphome_cmd = esphome_cmd if esphome_cmd is not None else []

        return controller

    return _make


CaptureDevicesEventsFactory = Callable[..., list[Event]]


@pytest.fixture
def capture_devices_events() -> Iterator[CaptureDevicesEventsFactory]:
    """Yield a factory that swaps a controller's bus for a real ``EventBus``.

    Devices-side sibling of ``capture_firmware_events`` from the
    firmware conftest. The ``make_controller`` factory wires
    ``self._db`` as a ``MagicMock`` so ``_db.bus.fire`` is a
    ``MagicMock`` auto-attribute; assertions on it have to walk
    ``mock_calls`` or use ``assert_called_with``. Replacing with a
    real bus + listener gives a flat ``[Event, …]`` log that
    captures both event type and payload, with no coupling to the
    handler's internal call shape.

    Tests pull the fixture in by adding ``capture_devices_events``
    to their signature and call it as a factory:
    ``captured = capture_devices_events(controller, EventType.X, ...)``.
    The fixture wrapper tracks every swap and restores
    ``controller._db.bus`` to its original value on teardown so a
    test that holds a controller reference past the assertion sees
    the original bus, not a stale fake.
    """
    swaps: list[tuple[DevicesController, Any]] = []

    def _factory(
        controller: DevicesController,
        *event_types: EventType,
    ) -> list[Event]:
        bus = EventBus()
        captured: list[Event] = []
        for event_type in event_types:
            bus.add_listener(event_type, captured.append)
        swaps.append((controller, controller._db.bus))
        controller._db.bus = bus
        return captured

    yield _factory

    for controller, original_bus in swaps:
        controller._db.bus = original_bus


@pytest.fixture
def stub_create_device_metadata_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub the three module-level helpers ``create_device`` reaches into.

    The wizard / adoption flow's happy path persists through
    ``ext_storage_path`` (resolved against ``CORE.config_path``,
    unset in isolated tests) and the metadata-sidecar helpers
    (``set_device_metadata`` / ``remove_device_metadata``, which
    write through ``metadata_transaction`` and trip blockbuster
    on the inner ``tempfile.mkstemp``). Tests that don't care
    about the sidecar's contents — only that the YAML lands on
    disk and the catalog lookup ran — get all three patched
    together. Tests that *do* care (e.g. "archived metadata is
    cleared on stub create") want the real ``set_device_metadata``
    and should NOT request this fixture.
    """
    storage_path = tmp_path / "storage.json"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.mutations_create.resolve_storage_path",
        lambda _filename: storage_path,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices._shared_sidecar.set_device_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices._shared_sidecar.remove_device_metadata",
        lambda *_args, **_kwargs: None,
    )


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

    # ``archive.py`` and ``helpers.py`` each import
    # ``resolve_storage_path`` independently — rebinding only one
    # leaves the other running against the real CORE.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.archive.resolve_storage_path",
        _ext,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.helpers.resolve_storage_path",
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


class ReloadingScanner(RecordingScanner):
    """``RecordingScanner`` whose ``reload`` rehydrates the live device.

    The production scanner reruns the metadata resolver on every
    ``reload`` and replaces the ``Device`` in its index. The bare
    ``RecordingScanner`` only records the call, so controller methods
    that read the live ``Device`` back from ``self._scanner.devices``
    after reload would always see the stale pre-populated stub. This
    fake walks ``self.devices`` for the matching configuration and
    copies the freshly-persisted ``labels`` off the sidecar onto it.
    """

    def __init__(self, config_dir: Path, devices: list[Device]) -> None:
        super().__init__()
        self._config_dir = config_dir
        self.devices = list(devices)

    async def reload(self, filename: str) -> bool:
        self.calls.append(("reload", filename))
        md = await asyncio.to_thread(get_device_metadata, self._config_dir, filename)
        raw_labels = md.get("labels", [])
        new_labels = (
            [item for item in raw_labels if isinstance(item, str)]
            if isinstance(raw_labels, list)
            else []
        )
        for device in self.devices:
            if device.configuration == filename:
                device.labels = new_labels
        return True


def make_label_test_device(
    filename: str = "kitchen.yaml", labels: list[str] | None = None
) -> Device:
    """Build a ``Device`` whose ``configuration`` / ``labels`` round-trip the sidecar."""
    name = configuration_stem(filename)
    return make_device(
        name=name,
        friendly_name=name,
        configuration=filename,
        address="",
        labels=list(labels or []),
    )


def attach_reloading_scanner(
    controller: DevicesController, config_dir: Path, devices: list[Device]
) -> ReloadingScanner:
    """Swap the default ``RecordingScanner`` for the reload-aware one seeded with N devices.

    Each seeded device gets a backing YAML on disk so the
    "scanner has device X" / "X.yaml exists" invariant holds —
    ``set_device_labels`` refuses to write a sidecar entry for a
    configuration whose YAML is gone.
    """
    for device in devices:
        yaml_path = config_dir / device.configuration
        if not yaml_path.exists():
            yaml_path.write_text(
                f"esphome:\n  name: {configuration_stem(device.configuration)}\n",
                encoding="utf-8",
            )
    scanner = ReloadingScanner(config_dir, devices)
    controller._scanner = scanner
    return scanner
