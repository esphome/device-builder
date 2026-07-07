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


# UI pairing-window lifetime. Auto-closes after this much idle;
# the frontend extends on each activity tick.
PAIRING_WINDOW_DURATION_SECONDS = 300.0

# ``--remote-build-only`` first-pair bootstrap window. Longer than the
# UI window because pairing is gated on the console-printed key (the
# tight-window race pressure is gone), so the operator gets time to
# bring up the main builder and transcribe the key. The UI "Pairing
# requests" window keeps the shorter default — it's admin-Accept-gated,
# not auto-approve, so it doesn't need widening.
BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS = 900.0


async def set_pairing_window(
    controller: ReceiverController,
    *,
    open: bool,  # noqa: A002 — wire format names this field "open"
    client: Hashable,
    duration_seconds: float | None = None,
) -> PairingWindowState:
    """
    Open, extend, or close the pairing window for the calling client.

    Refcounted per WS client: ``open=true`` adds/refreshes
    the caller's entry, ``open=false`` removes it. Window is
    open iff any client has a non-stale entry. Crashed tabs
    age out via the idle timeout; a graceful close from
    one tab leaves the window open for others.

    ``client`` is the WS connection injected by the
    dispatcher — used as the refcount key so two tabs get
    distinct entries. Required kwarg (a default would
    silently bucket every caller under the same key).

    ``duration_seconds`` sets this entry's auto-close lifetime
    (defaults to :data:`PAIRING_WINDOW_DURATION_SECONDS`, read at
    call time so tests can monkeypatch it); the headless bootstrap
    passes the longer :data:`BOOTSTRAP_PAIRING_WINDOW_DURATION_SECONDS`.
    Not forwarded by the WS command, so a client can't widen its own
    window.

    Fires :attr:`EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED`
    only on real state transitions; idempotent calls don't.
    """
    if not isinstance(open, bool):
        msg = "remote_build/set_pairing_window: 'open' must be a bool"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if duration_seconds is None:
        duration_seconds = PAIRING_WINDOW_DURATION_SECONDS

    was_open = is_pairing_window_open(controller)
    if open:
        # Store the absolute deadline so a per-entry lifetime rides
        # with each client without a companion duration map.
        controller.state.pairing_window_clients[client] = time.monotonic() + duration_seconds
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
    """Seconds until the latest client's deadline, or ``None`` if closed."""
    _prune_stale_pairing_window_clients(controller)
    if not controller.state.pairing_window_clients:
        return None
    latest_deadline = max(controller.state.pairing_window_clients.values())
    return max(0.0, latest_deadline - time.monotonic())


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
    """Drop client entries whose deadline has passed."""
    if not controller.state.pairing_window_clients:
        return
    now = time.monotonic()
    controller.state.pairing_window_clients = {
        client: deadline
        for client, deadline in controller.state.pairing_window_clients.items()
        if deadline >= now
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
