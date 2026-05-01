"""Tests for the centralised subprocess spawn helper."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

from esphome_device_builder.helpers import subprocess as subprocess_helper


async def test_create_subprocess_exec_forces_close_fds_false() -> None:
    """The wrapper must always pass ``close_fds=False`` even when the caller doesn't."""
    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock:
        await subprocess_helper.create_subprocess_exec(
            "echo",
            "hi",
            stdout=asyncio.subprocess.PIPE,
        )

    args, kwargs = mock.call_args
    assert args == ("echo", "hi")
    assert kwargs["close_fds"] is False
    assert kwargs["stdout"] == asyncio.subprocess.PIPE


async def test_create_subprocess_exec_caller_close_fds_is_overridden() -> None:
    """Callers can't accidentally restore the slow default."""
    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock:
        # If a caller passes close_fds=True, the helper still overrides it
        # by explicitly setting kwargs["close_fds"] = False before delegating
        # to asyncio. Documented here so a future refactor preserves the
        # actual mechanism, not the wrong "later kwarg wins" rationale.
        await subprocess_helper.create_subprocess_exec("echo", "hi", close_fds=True)

    _, kwargs = mock.call_args
    assert kwargs["close_fds"] is False


async def test_create_subprocess_exec_actually_runs() -> None:
    """End-to-end smoke: the helper produces a working ``Process``."""
    proc = await subprocess_helper.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('subprocess-helper-ok')",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0
    assert b"subprocess-helper-ok" in stdout


async def test_no_call_site_uses_asyncio_create_subprocess_exec_directly() -> None:
    """Guard against regressions: no callsite should bypass the helper.

    Catches future commits that re-introduce a direct
    ``asyncio.create_subprocess_exec`` call (which would skip the
    ``close_fds=False`` optimisation) anywhere outside the helper itself.
    """
    import esphome_device_builder

    pkg_root = (
        # Resolve the package's filesystem location without depending on
        # ``__file__`` typing nuances under from __future__ annotations.
        __import__("pathlib").Path(esphome_device_builder.__file__).parent
    )
    helper_path = pkg_root / "helpers" / "subprocess.py"

    offenders: list[str] = []
    for path in pkg_root.rglob("*.py"):
        if path == helper_path:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "asyncio.create_subprocess_exec" in line:
                offenders.append(f"{path.relative_to(pkg_root)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Found direct asyncio.create_subprocess_exec calls — use "
        "esphome_device_builder.helpers.subprocess.create_subprocess_exec "
        "instead so close_fds=False is applied:\n  " + "\n  ".join(offenders)
    )
