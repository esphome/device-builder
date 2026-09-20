"""Temporary: time the OS primitives every test pays for, to compare CI runners."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def main() -> None:
    """Print ms per op for each primitive."""
    print(f"platform={sys.platform} python={sys.version.split()[0]} cpus={os.cpu_count()}")
    roots = {"TEMP": Path(tempfile.gettempdir())}
    if runner_temp := os.environ.get("RUNNER_TEMP"):
        roots["RUNNER_TEMP"] = Path(runner_temp)
    roots["CWD"] = Path.cwd()
    for label, root in roots.items():
        _report(f"tmp tree ({label}={root})", 300, lambda root=root: _tmp_tree(root))
    _report("new_event_loop + close", 300, _loop_cycle)
    _report("asyncio.run(noop)", 300, lambda: asyncio.run(_noop()))
    _report("socket.socketpair", 300, _socketpair)
    _report("asyncio.sleep(0.001)", 100, lambda: asyncio.run(asyncio.sleep(0.001)))
    asyncio.run(_sleep_loop())
    _report("spawn python -c pass", 20, lambda: _spawn("pass"))
    _report("spawn python import device_builder", 3, lambda: _spawn(_IMPORT))


_IMPORT = "import esphome_device_builder.device_builder"


def _report(label: str, count: int, func: Callable[[], object]) -> None:
    func()
    start = time.perf_counter()
    for _ in range(count):
        func()
    elapsed = time.perf_counter() - start
    print(f"{label:<60} {elapsed / count * 1000:9.3f} ms/op")


def _tmp_tree(root: Path) -> None:
    base = Path(tempfile.mkdtemp(dir=root))
    try:
        sub = base / ".esphome" / "storage"
        sub.mkdir(parents=True)
        for name in ("a.yaml", "b.json", "c.json"):
            (sub / name).write_text("key: value\n" * 20)
            (sub / name).read_text()
    finally:
        shutil.rmtree(base)


def _loop_cycle() -> None:
    asyncio.new_event_loop().close()


async def _noop() -> None:
    return None


def _socketpair() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()


async def _sleep_loop() -> None:
    for delay in (0.001, 0.01, 0.05):
        start = time.perf_counter()
        for _ in range(50):
            await asyncio.sleep(delay)
        actual = (time.perf_counter() - start) / 50 * 1000
        print(
            f"{'await asyncio.sleep(' + str(delay) + ') inside one loop':<60} {actual:9.3f} ms/op"
        )


def _spawn(code: str) -> None:
    subprocess.run([sys.executable, "-c", code], check=True)


if __name__ == "__main__":
    main()
