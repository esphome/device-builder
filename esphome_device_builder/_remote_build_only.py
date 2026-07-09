"""Headless ``--remote-build-only`` service loop: peer-link receiver, no HTTP dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from aiohttp.web import GracefulExit

from .controllers.remote_build.pairing_window import (
    BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS,
    set_pairing_window,
)
from .helpers.pairing_key import generate_pairing_key
from .helpers.pin_emoji import pin_emoji, pin_emoji_names
from .models import EventType

if TYPE_CHECKING:
    from .controllers.remote_build import ReceiverController
    from .device_builder import DeviceBuilder
    from .helpers.event_bus import Event
    from .helpers.peer_link_identity import PeerLinkIdentity
    from .models import (
        RemoteBuildPairingWindowChangedData,
        RemoteBuildPairStatusChangedData,
    )

_LOGGER = logging.getLogger(__name__)

# Refcount key for the bootstrap-held pairing window (normally keyed
# on WS clients; this mode has none).
_BOOTSTRAP_WINDOW_CLIENT = "remote-build-only-bootstrap"

_EXIT_NOT_SERVING = 1


def run_remote_build_only(db: DeviceBuilder) -> None:
    """
    Blocking service loop for ``--remote-build-only``.

    Drives the ``DeviceBuilder`` lifecycle directly (no aiohttp
    ``run_app`` — no HTTP site is bound). A stop signal lands as
    ``GracefulExit`` via ``__main__``'s trap and exits 0; raises
    ``SystemExit(1)`` when there's nothing to serve (listener bind
    failed, or the first-pair window lapsed with no pairing).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_code = 0
    main_task = loop.create_task(_serve(db))
    try:
        exit_code = loop.run_until_complete(main_task)
    except (GracefulExit, KeyboardInterrupt):
        # ``__main__``'s SIGTERM trap schedules ``_raise_graceful_exit``
        # via ``call_soon_threadsafe``; ``GracefulExit`` subclasses
        # ``SystemExit``, so asyncio's ``Handle._run`` re-raises it
        # (rather than routing it to the loop exception handler) and it
        # propagates out of ``run_until_complete`` here. The ``finally``
        # then cancels the still-parked ``_serve`` task.
        pass
    finally:
        if not main_task.done():
            main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(main_task)
        try:
            loop.run_until_complete(db.stop())
        except Exception:
            # Keep the original outcome (clean stop or SystemExit
            # below) rather than masking it with a teardown error.
            _LOGGER.exception("Error during remote-build-only shutdown")
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(None)
        loop.close()
    if exit_code:
        raise SystemExit(exit_code)


async def _serve(db: DeviceBuilder) -> int:
    """Start the builder, ensure a pairing exists (bootstrapping one if needed), then park."""
    await db.start()
    receiver = db.remote_build_receiver
    if receiver is None or not db.is_remote_build_listener_bound:
        _LOGGER.error(
            "Remote-build peer-link listener is not bound (see the error above — "
            "port %d already in use?); nothing to serve, exiting",
            db.settings.remote_build_port,
        )
        return _EXIT_NOT_SERVING
    if receiver.state.approved_peers:
        peer = next(iter(receiver.state.approved_peers.values()))
        _LOGGER.info(
            "Remote-build server already paired with %r — serving build requests",
            peer.label,
        )
    elif not await _bootstrap_first_pair(db, receiver):
        return _EXIT_NOT_SERVING
    return await _park_forever()


async def _bootstrap_first_pair(db: DeviceBuilder, receiver: ReceiverController) -> bool:
    """
    Open a one-shot auto-approve pairing window and await its outcome.

    Returns ``True`` once the first pair request lands (the window is
    closed behind it — exactly one pairing); ``False`` when the
    window lapses unpaired. The 15-minute lifetime
    (:data:`BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS`) is the pairing
    window's own idle deadline — the bootstrap client never extends it.

    Pairing requires the banner-printed key; ``bootstrap_pairing_key`` arms
    and clears together with ``auto_approve_first_pair``.
    """
    receiver.state.auto_approve_first_pair = True
    # Log the banner from this local, not the state field: an await sits before
    # the log, and a pair request landing in it clears the field to None.
    pairing_key = generate_pairing_key()
    receiver.state.bootstrap_pairing_key = pairing_key
    paired = asyncio.Event()
    window_closed = asyncio.Event()

    def _on_pair_status(event: Event[RemoteBuildPairStatusChangedData]) -> None:
        if event.data["status"] == "approved":
            paired.set()

    def _on_window_changed(event: Event[RemoteBuildPairingWindowChangedData]) -> None:
        if not event.data["open"]:
            window_closed.set()

    try:
        with (
            db.bus.listening([EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED], _on_pair_status),
            db.bus.listening([EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED], _on_window_changed),
        ):
            await set_pairing_window(
                receiver,
                open=True,
                client=_BOOTSTRAP_WINDOW_CLIENT,
                duration_seconds=BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS,
            )
            identity = await db.peer_link_identity_store.async_load()
            _log_pairing_banner(identity, db.settings.allow_pairing_sources, pairing_key)
            await _wait_first(paired, window_closed)
    finally:
        # Arm and disarm together so a cancellation / exception mid-wait can
        # never leave the receiver armed with a live key.
        receiver.state.auto_approve_first_pair = False
        receiver.state.bootstrap_pairing_key = None
    if not paired.is_set():
        _LOGGER.error(
            "No pairing request arrived within %d minutes; exiting. Re-run to "
            "open a fresh pairing window.",
            int(BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS // 60),
        )
        return False
    await set_pairing_window(receiver, open=False, client=_BOOTSTRAP_WINDOW_CLIENT)
    peer = next(iter(receiver.state.approved_peers.values()))
    _LOGGER.info("Paired with %r — remote-build server is now in service", peer.label)
    return True


def _log_pairing_banner(
    identity: PeerLinkIdentity, allow_pairing_sources: list[str], pairing_key: str | None
) -> None:
    """Print the fingerprint + one-shot pairing key the operator uses in the pair dialog."""
    banner = "=" * 70
    if allow_pairing_sources:
        source_line = f" Only auto-approving a request from: {', '.join(allow_pairing_sources)}\n"
    else:
        source_line = (
            " Any source may attempt to pair; the pairing key above is\n"
            " required to succeed (add --allow-pairing-source <IP> to also\n"
            " restrict the source address).\n"
        )
    _LOGGER.info(
        "\n%s\n"
        " REMOTE BUILD PAIRING — window open for %d minutes\n"
        " On your main Device Builder open Settings -> Send builds and\n"
        " pair this server. Verify the fingerprint shown there matches:\n"
        "\n"
        "   %s\n"
        "   (%s)\n"
        "   %s\n"
        "\n"
        " Enter this one-time pairing key in the pair dialog\n"
        " (it asks for the key once it detects this server):\n"
        "\n"
        "   %s\n"
        "\n"
        "%s"
        " The window closes on the first pairing (exactly one pairing).\n"
        " This process exits if nothing pairs before the window lapses.\n"
        "%s",
        banner,
        int(BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS // 60),
        pin_emoji(identity.pin_sha256),
        pin_emoji_names(identity.pin_sha256),
        identity.pin_sha256_formatted,
        pairing_key,
        source_line,
        banner,
    )


async def _wait_first(*events: asyncio.Event) -> None:
    """Return when the first of *events* is set; cancels the rest of the waiters."""
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _park_forever() -> int:
    """
    Serve until the runner's stop path cancels this task.

    The event is created once and never set; the ``while`` keeps the
    function statically non-returning (so the ``int`` return type
    holds without an unreachable ``return``) and, because the single
    ``wait()`` blocks until cancellation, the loop body only ever runs
    once — no per-iteration allocation.
    """
    never = asyncio.Event()
    while True:
        await never.wait()
