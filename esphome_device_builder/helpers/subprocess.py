"""Subprocess helpers.

Centralises ``asyncio.create_subprocess_exec`` so every spawn forces
``close_fds=False``. Python <3.14's default (``close_fds=True``) makes
the subprocess module ``fork()`` the parent and have the child iterate
``/proc/self/fd`` to close descriptors before ``exec()``; on
memory-pressured systems that copies a non-trivial amount of page
tables for nothing. None of our spawns rely on inherited descriptors
being closed at the boundary, and the upstream esphome dashboard uses
the same pattern in ``esphome.dashboard.util.subprocess``.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def create_subprocess_exec(
    *args: str,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Spawn a subprocess via ``asyncio.create_subprocess_exec``.

    Positional and keyword arguments are forwarded to the underlying
    call, except ``close_fds`` is always overridden to ``False``.
    Callers must not rely on overriding ``close_fds`` or on kwargs
    that require ``close_fds=True`` (e.g. ``pass_fds``). Use this
    helper everywhere instead of calling
    ``asyncio.create_subprocess_exec`` directly.
    """
    kwargs["close_fds"] = False
    return await asyncio.create_subprocess_exec(*args, **kwargs)
