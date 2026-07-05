"""Receiver-side pairing-window gate for ``intent="pair_request"`` Noise frames."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Hashable
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import (
    ErrorCode,
    EventType,
    PairingWindowState,
    RemoteBuildPairingWindowChangedData,
)

if TYPE_CHECKING:
    from .receiver import ReceiverController


# Pairing-window lifetime. Auto-closes after this much idle;
# the frontend extends on each activity tick.
PAIRING_WINDOW_DURATION_SECONDS = 300.0


async def set_pairing_window(
    controller: ReceiverController,
    *,
    open: bool,  # noqa: A002 — wire format names this field "open"
    client: Hashable,
) -> PairingWindowState:
    """
    Open, extend, or close the pairing window for the calling client.

    Refcounted per WS client: ``open=true`` adds/refreshes
    the caller's entry, ``open=false`` removes it. Window is
    open iff any client has a non-stale entry. Crashed tabs
    age out via the 5min idle timeout; a graceful close from
    one tab leaves the window open for others.

    ``client`` is the WS connection injected by the
    dispatcher — used as the refcount key so two tabs get
    distinct entries. Required kwarg (a default would
    silently bucket every caller under the same key).

    Fires :attr:`EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED`
    only on real state transitions; idempotent calls don't.
    """
    if not isinstance(open, bool):
        msg = "remote_build/set_pairing_window: 'open' must be a bool"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    was_open = is_pairing_window_open(controller)
    if open:
        controller.state.pairing_window_clients[client] = time.monotonic()
    else:
        controller.state.pairing_window_clients.pop(client, None)
    _reschedule_pairing_window_close(controller)
    is_open = bool(controller.state.pairing_window_clients)

    # Fire on state transitions AND on every extend (so the
    # frontend countdown re-syncs against the bumped deadline).
    if was_open != is_open or (open and is_open):
        _fire_pairing_window_changed(controller)
    if was_open and not is_open:
        clear_pending_peers_on_window_close(controller)
    return _pairing_window_state(controller)


def is_pairing_window_open(controller: ReceiverController) -> bool:
    """Return whether the pairing window is currently open (post-prune)."""
    _prune_stale_pairing_window_clients(controller)
    return bool(controller.state.pairing_window_clients)


def clear_pending_peers_on_window_close(controller: ReceiverController) -> None:
    """Drop every PENDING peer + fire ``status="removed"`` for each.

    Wakes any in-flight ``lookup_peer_for_status`` long-poll
    so its offloader sees REJECTED.
    """
    if not controller.state.pending_peers:
        return
    cleared = list(controller.state.pending_peers)
    controller.state.pending_peers.clear()
    for dashboard_id in cleared:
        controller._fire_pair_status_changed(dashboard_id, "removed")


def _pairing_window_remaining(controller: ReceiverController) -> float | None:
    """Seconds until the latest-extend deadline, or ``None`` if closed."""
    _prune_stale_pairing_window_clients(controller)
    if not controller.state.pairing_window_clients:
        return None
    latest_extend = max(controller.state.pairing_window_clients.values())
    return max(0.0, latest_extend + PAIRING_WINDOW_DURATION_SECONDS - time.monotonic())


def _pairing_window_state(controller: ReceiverController) -> PairingWindowState:
    """Project the in-memory client map into a wire-shape response."""
    remaining = _pairing_window_remaining(controller)
    if remaining is None:
        return PairingWindowState(open=False, expires_in_seconds=None)
    return PairingWindowState(open=True, expires_in_seconds=remaining)


def _fire_pairing_window_changed(controller: ReceiverController) -> None:
    """Fire ``REMOTE_BUILD_PAIRING_WINDOW_CHANGED`` with the current state."""
    state = _pairing_window_state(controller)
    payload: RemoteBuildPairingWindowChangedData = {
        "open": state.open,
        "expires_in_seconds": state.expires_in_seconds,
    }
    controller._db.bus.fire(EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED, payload)


def _prune_stale_pairing_window_clients(controller: ReceiverController) -> None:
    """Drop client entries whose last-extend timestamp aged out."""
    if not controller.state.pairing_window_clients:
        return
    cutoff = time.monotonic() - PAIRING_WINDOW_DURATION_SECONDS
    controller.state.pairing_window_clients = {
        client: extended_at
        for client, extended_at in controller.state.pairing_window_clients.items()
        if extended_at >= cutoff
    }


def _reschedule_pairing_window_close(controller: ReceiverController) -> None:
    """
    Cancel any pending close handle and schedule a fresh one.

    Called after every :func:`set_pairing_window` mutation. The
    handle always reflects the current latest-extend deadline,
    so on every extend we cancel and reschedule rather than
    letting an old handle wake up and re-check; this avoids the
    duplicate-close-event class of bug where an old handle
    would fire after an explicit close.

    When the client map is empty (the explicit-close case where
    the last client just dropped out), no new handle is
    scheduled and ``state.pairing_window_handle`` stays ``None``.
    """
    if controller.state.pairing_window_handle is not None:
        controller.state.pairing_window_handle.cancel()
        controller.state.pairing_window_handle = None
    remaining = _pairing_window_remaining(controller)
    if remaining is None:
        return
    loop = asyncio.get_running_loop()
    controller.state.pairing_window_handle = loop.call_later(
        remaining, lambda: _on_pairing_window_deadline(controller)
    )


def _on_pairing_window_deadline(controller: ReceiverController) -> None:
    """
    Sync callback fired by the TimerHandle when the deadline lapses.

    The handle was scheduled to the latest-extend deadline; if
    any later extend had bumped the deadline, the handle would
    have been cancelled and rescheduled, so by the time we run
    every client has aged out. Clear the client refcount + the
    in-memory PENDING peers dict, fire the close event +
    cancellation events, done.
    """
    controller.state.pairing_window_handle = None
    controller.state.pairing_window_clients.clear()
    _fire_pairing_window_changed(controller)
    clear_pending_peers_on_window_close(controller)
