"""ANSI escape handling shared by the subprocess-output consumers."""

from __future__ import annotations

import re

# Both the escape byte and the literal ``\\033`` spelling esphome's ``--dashboard`` mode emits.
ANSI_CSI_RE = re.compile(r"(?:\x1b|\\033)\[[0-9;?]*[A-Za-z]")
