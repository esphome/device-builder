"""Test fixtures shared across the suite.

Currently just wires Blockbuster (https://github.com/cbornet/blockbuster)
in as an autouse fixture so any blocking call made from inside an
asyncio event loop fails the test instead of silently stalling the
loop. The dashboard is async-first; a stray ``time.sleep`` or sync
``open().read()`` on the request path would tank latency without
showing up in the build, so we let CI catch them.

The fixture only activates on Linux — that's what production runs on
(ESPHome container, HA add-on), so it's the only platform where a
blocking call would actually hit users. Windows and macOS just skip
the check; their CI runs are about catching platform-specific
regressions, not async hygiene.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest
from blockbuster import blockbuster_ctx

if TYPE_CHECKING:
    from collections.abc import Iterator

    from blockbuster import BlockBuster

# Call sites known to do bounded blocking I/O during one-time server
# startup, where the cost is paid once and not on the request path.
# Listed here as ``(filename, function_name)`` pairs so blockbuster
# only exempts that specific frame, not the function globally. Keep
# this list short — anything genuinely on a hot path should be
# refactored, not allowlisted.
_STARTUP_BLOCKING_OK: tuple[tuple[str, str], ...] = (
    # SessionStore reads the persisted JSON sessions file once at
    # AuthController construction time. Bounded by the dashboard's
    # own startup, not request volume.
    ("helpers/auth.py", "_load"),
    # Sync sibling of ``_persist_async``. Production callers always
    # wrap it via ``asyncio.to_thread`` (so blockbuster wouldn't see
    # it from a worker thread anyway); tests call it directly to seed
    # state without a round-trip through the executor.
    ("helpers/auth.py", "_persist"),
    # ``_register_frontend`` stat-checks ``index.html`` and ``assets/``
    # at app construction so a broken frontend wheel surfaces a clear
    # RuntimeError instead of mysterious 404s. Runs once at startup.
    ("device_builder.py", "_register_frontend"),
)


@pytest.fixture(autouse=True)
def blockbuster() -> Iterator[BlockBuster | None]:
    """Fail any test that performs a blocking call from inside the event loop.

    Linux-only: production targets are Linux containers, so other
    platforms skip the check and just yield ``None`` (the fixture
    return value isn't consumed by any test today).
    """
    if not sys.platform.startswith("linux"):
        yield None
        return
    # Scope the check to our package: a blocking call only fails the
    # test when the offending frame originates inside
    # ``esphome_device_builder``. Test-fixture niceties (sync
    # ``Path.mkdir`` for setup, ``tmp_path.write_text``, etc.) and
    # third-party library internals are intentionally exempt — we're
    # auditing the dashboard's request paths, not pytest itself.
    with blockbuster_ctx("esphome_device_builder") as bb:
        for fn in bb.functions.values():
            for filename, func_name in _STARTUP_BLOCKING_OK:
                fn.can_block_in(filename, func_name)
        yield bb
