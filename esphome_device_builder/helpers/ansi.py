"""ANSI escape handling shared by the subprocess-output consumers."""

from __future__ import annotations

import re

# The escape byte, or the literal spelling ``--dashboard`` output uses.
ANSI_CSI_RE = re.compile(r"(?:\x1b|\\033)\[[0-9;?]*[A-Za-z]")
