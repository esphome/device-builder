"""Tests for ``helpers/api.py`` — ``api_command`` decorator and registry scan.

The whole WS dispatch surface is built from
``collect_api_commands(controller)`` walking each controller's
public methods at startup. A regression that drops the marker
attribute, hides decorated methods behind underscore prefixes, or
trips on dynamic attributes during ``dir()`` would silently
unregister real commands — pin the contract here so a refactor
that changes the scan rules surfaces.
"""

from __future__ import annotations

import asyncio

from esphome_device_builder.helpers.api import api_command, collect_api_commands


def test_collect_api_commands_picks_up_decorated_methods() -> None:
    """Methods carrying the ``_api_command`` marker are registered by name."""

    class _Controller:
        @api_command("ns/foo")
        async def foo(self) -> None: ...

        @api_command("ns/bar")
        async def bar(self) -> None: ...

    handlers = collect_api_commands(_Controller())

    assert set(handlers) == {"ns/foo", "ns/bar"}
    # The dict values are bound methods, not the underlying functions —
    # callers invoke them as ``await handlers["ns/foo"](...)``.
    assert handlers["ns/foo"].__self__.__class__ is _Controller  # type: ignore[attr-defined]


def test_collect_api_commands_skips_undecorated_methods() -> None:
    """Plain methods without the marker don't sneak into the registry.

    Without this, every public helper on a controller would land on
    the WS surface, exposing internals the dispatcher never meant to
    advertise.
    """

    class _Controller:
        @api_command("ns/wired")
        async def wired(self) -> None: ...

        async def helper(self) -> None: ...

        def sync_helper(self) -> None: ...

    handlers = collect_api_commands(_Controller())

    assert set(handlers) == {"ns/wired"}


def test_collect_api_commands_skips_underscore_prefixed_names() -> None:
    """Even a marker-bearing method is skipped if its name starts with ``_``.

    The scan filters on ``name.startswith("_")`` *before* checking the
    marker, so internal helpers can't accidentally end up on the wire
    by reusing the decorator. Pin this so a refactor that swaps the
    order (marker check first, name filter second) surfaces.
    """

    class _Controller:
        @api_command("ns/public")
        async def public(self) -> None: ...

        @api_command("ns/private")
        async def _private(self) -> None: ...

    handlers = collect_api_commands(_Controller())

    assert set(handlers) == {"ns/public"}


def test_collect_api_commands_returns_empty_for_object_without_handlers() -> None:
    """A bare object with no decorated methods yields an empty dict.

    DeviceBuilder calls this once per controller at startup, including
    on lightweight controllers that may not have wired any commands
    yet — the empty-dict shape lets the caller merge results with
    ``dict.update`` without a None-guard.
    """

    class _NoHandlers:
        async def helper(self) -> None: ...

    assert collect_api_commands(_NoHandlers()) == {}


def test_api_command_decorator_attaches_marker() -> None:
    """The decorator's only side effect is setting ``_api_command`` on the func.

    The function's own behaviour and signature are untouched — the
    marker is what ``collect_api_commands`` keys off, and the WS
    dispatcher invokes the bound method directly. Pin this so a
    "let's wrap the function" refactor can't break the contract
    silently.
    """

    @api_command("ns/example")
    async def handler() -> str:
        return "ok"

    assert handler._api_command == "ns/example"  # type: ignore[attr-defined]
    # And the function still behaves like a plain coroutine function —
    # not wrapped, not transformed.
    assert asyncio.run(handler()) == "ok"


def test_collect_api_commands_handles_duplicate_command_names_last_wins() -> None:
    """Two decorated methods with the same command name → last-in-dir wins.

    Documents the actual behaviour rather than asserting safety here:
    ``dir()`` returns sorted names, so this is deterministic — but
    silently overriding is a footgun. If we ever want to *reject*
    duplicates instead, this test will be the first thing to update.
    """

    class _Controller:
        @api_command("ns/dup")
        async def aaa(self) -> str:
            return "first"

        @api_command("ns/dup")
        async def bbb(self) -> str:
            return "second"

    handlers = collect_api_commands(_Controller())

    # ``dir()`` returns names alphabetically; ``bbb`` is iterated after
    # ``aaa`` and overwrites the entry.
    assert set(handlers) == {"ns/dup"}
    assert handlers["ns/dup"].__name__ == "bbb"


async def test_collect_api_commands_returns_callable_bound_methods() -> None:
    """Each entry is awaitable and bound to the original instance.

    Spot-check this so a refactor that returns the underlying
    function (losing ``self``) gets caught — the WS dispatcher
    awaits the entry directly without re-binding.
    """

    class _Controller:
        def __init__(self, value: int) -> None:
            self.value = value

        @api_command("ns/get")
        async def get(self) -> int:
            return self.value

    controller = _Controller(value=42)
    handlers = collect_api_commands(controller)

    assert await handlers["ns/get"]() == 42
