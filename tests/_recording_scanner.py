"""
Shared typed fake for ``DeviceScanner`` — captures scan / reload / get_by_name calls.

Lives in ``tests/`` (not in any ``conftest.py``) so cross-suite
imports — devices tests and firmware tests both reach for it —
don't create a hidden cross-conftest dependency. Future fixture
or pytest-plugin changes in either suite's ``conftest.py`` won't
break the other suite at import time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class RecordingScanner:
    """Test fake for ``DeviceScanner`` capturing recorded method calls.

    Mirrors every public method on the production ``DeviceScanner``
    (``scan`` / ``reload`` / ``request`` / ``start`` / ``stop`` /
    ``get_by_name`` / ``devices`` / ``by_path``). Calls to the
    recordable ones land in ``self.calls`` as
    ``(method_name, *args)``; tests assert on the list directly
    instead of scattering ``MagicMock.assert_awaited_*`` lines.

    Signatures match production exactly — ``start`` is sync,
    ``stop`` is async, ``request`` is sync. A future rename or a
    sync/async change on the real class surfaces immediately
    against this fake.

    Why a typed fake rather than ``MagicMock``: a typo
    (``scann.assert_awaited_once``) silently passes against a
    ``MagicMock`` because it spawns a fresh attribute on access; a
    refactor renaming a real method (``reload`` → ``refresh_one``)
    similarly breaks the contract without breaking the assertion.
    Mirroring the *full* public surface means a controller path
    that touches ``get_by_name``, ``by_path`` or the wake-worker
    methods (``request`` / ``start`` / ``stop``) won't blow up
    against the fake just because no earlier test exercised it.

    ``reload_returns`` controls the truthy return — production's
    ``reload`` returns ``False`` when the file isn't tracked, which
    a few tests exercise. ``devices_by_name`` lets tests pre-seed
    the name index that ``get_by_name`` reads from.
    """

    def __init__(
        self,
        *,
        reload_returns: bool = True,
        devices_by_name: dict[str, list[object]] | None = None,
    ) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._reload_returns = reload_returns
        self._devices_by_name = devices_by_name or {}
        # Mirrors ``DeviceScanner.devices`` / ``by_path`` — empty by
        # default; tests that need a populated catalog can assign.
        self.devices: list[object] = []
        self.by_path: dict[Path, object] = {}

    async def scan(self) -> None:
        self.calls.append(("scan",))

    async def reload(self, filename: str) -> bool:
        self.calls.append(("reload", filename))
        return self._reload_returns

    def request(self, filename: str) -> None:
        self.calls.append(("request", filename))

    def start(self) -> None:
        self.calls.append(("start",))

    async def stop(self) -> None:
        self.calls.append(("stop",))

    def get_by_name(self, name: str) -> list[object]:
        self.calls.append(("get_by_name", name))
        # Fresh list snapshot — mirrors production semantics so
        # callers can iterate / mutate without poisoning the index.
        return list(self._devices_by_name.get(name, []))

    def get_by_configuration(self, configuration: str) -> object | None:
        # A read accessor like ``devices`` / ``by_path`` — not recorded in
        # ``calls`` (those track side-effecting / awaitable operations).
        return next(
            (d for d in self.devices if getattr(d, "configuration", None) == configuration),
            None,
        )
