"""
Compute the ``config_hash`` of a device YAML.

ESPHome internally hashes a fully-resolved-and-sorted dump of the
config (FNV-1a 32-bit) and exposes it as ``CORE.config_hash``. The
running firmware exposes the same value via ``App.get_config_hash()``,
which esphome/esphome#16145 also publishes on the
``_esphomelib._tcp`` mDNS service as the ``config_hash`` TXT record.

Computing the hash on the dashboard side requires running ESPHome's
``read_config()`` — that resolves substitutions / packages, validates
component schemas, and populates ``CORE.config``. It also mutates
process-global state (``CORE``), which is why we run it in a
subprocess instead of in-process.

The hash is the 8-char lowercase hex format used by the mDNS TXT
record. Returns ``None`` when the YAML doesn't validate or the
subprocess fails for any reason — ``has_pending_changes`` then falls
back to its mtime check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Inline script run as ``python -c <SCRIPT> <yaml_path>``. Run in a
# subprocess because ``read_config()`` mutates the process-global
# ``CORE`` and importing it in our event-loop process would leak that
# state into every later compile.
_HASH_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from esphome.__main__ import read_config\n"
    "from esphome.core import CORE\n"
    # ESPHome's loader uses ``CORE.config_path`` via ``Path`` ops, so a
    # bare string raises ``AttributeError`` deep inside ``yaml_util``.
    "CORE.config_path = Path(sys.argv[1])\n"
    # ``read_config`` returns the resolved config dict and *sets*
    # ``CORE.raw_config`` — but it does NOT assign ``CORE.config``.
    # The CLI's ``run_esphome`` does that step manually after the
    # call, and the ``config_hash`` property hashes ``CORE.config``
    # specifically (a ``None`` here yields a meaningless-but-stable
    # hash for every config). Mirror the CLI's assignment.
    "config = read_config({})\n"
    "if config is None:\n"
    "    sys.exit(1)\n"
    "CORE.config = config\n"
    "print(f'{CORE.config_hash:08x}')\n"
)

# 8 lowercase hex chars — same shape the firmware broadcasts.
_HASH_RE = re.compile(r"\b([0-9a-f]{8})\b")

# Computing a hash for a "normal" device runs in well under a second on
# warm caches; the 60s ceiling protects against pathological YAMLs that
# pull a slow external component or a remote package on a cold cache.
_HASH_TIMEOUT_SECONDS = 60.0


async def compute_yaml_config_hash(yaml_path: Path) -> str | None:
    """
    Return the 8-char lowercase hex ``config_hash`` for *yaml_path*.

    Returns ``None`` when the YAML can't be validated or the hash
    can't be computed for any other reason. Callers should treat
    ``None`` as "fall back to mtime-based change detection" rather
    than as an error to propagate.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _HASH_SCRIPT,
            str(yaml_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        _LOGGER.exception("Could not start config_hash subprocess for %s", yaml_path)
        return None

    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=_HASH_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.warning(
            "config_hash computation timed out after %.0fs for %s",
            _HASH_TIMEOUT_SECONDS,
            yaml_path,
        )
        # Race: the subprocess may have exited between the timeout
        # firing and ``kill()`` being called — swallow the lookup
        # error so a recoverable timeout doesn't bubble up.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None

    if proc.returncode != 0:
        _LOGGER.debug(
            "config_hash subprocess for %s exited %s — falling back to mtime check",
            yaml_path,
            proc.returncode,
        )
        return None

    # ``read_config`` itself prints validation warnings to stdout, so we
    # scan rather than assume the hash is the only line. The script's
    # final ``print(f'{...:08x}')`` always emits exactly 8 hex chars on
    # its own line, so the *last* match wins if multiple show up.
    text = stdout_bytes.decode("utf-8", errors="replace")
    matches = _HASH_RE.findall(text)
    return matches[-1] if matches else None
