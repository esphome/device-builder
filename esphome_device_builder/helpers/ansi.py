"""ANSI escape handling shared by the subprocess-output consumers."""

from __future__ import annotations

import re

# The escape byte, or the literal spelling ``--dashboard`` output uses.
ANSI_ESC = r"(?:\x1b|\\033)"
ANSI_CSI_RE = re.compile(rf"{ANSI_ESC}\[[0-9;?]*[A-Za-z]")
