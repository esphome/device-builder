"""
Static configuration for the devices controller.

Module-level constants shared between the controller and helpers.
Pure data — no I/O, no controller state.
"""

from __future__ import annotations

import re

# Match an ANSI conceal-wrapped run. ESPHome's ``command_config``
# emits this around every ``password|key|psk|ssid`` value when
# ``--show-secrets`` is off, on the assumption that the terminal
# will hide it via the Concealed SGR (8) and reveal it again with
# 28. Browsers don't honour those codes, so the resolved secret
# bytes render plainly in our HTML ansi-log.
#
# Two byte representations to handle:
#
# - ``\x1b[8m...\x1b[28m`` — raw ESC byte. What you get reading
#   ``esphome config`` directly without ``--dashboard``.
# - ``\033[8m...\033[28m`` — the literal four-character escape
#   sequence. ``--dashboard`` mode replaces every real ANSI escape
#   with this so the dashboard can re-decode them on render. We
#   pass ``--dashboard`` on every validate, so this is the form we
#   actually see on the wire.
#
# Match both so a future ESPHome change to either side stays
# scrubbed. Replace the whole wrapped run including the escape
# codes — leaving the literal ``\033[8m`` bytes in the output
# would still expose the secret to anyone screen-recording the
# network tab even if a hypothetical conceal-aware renderer hid
# the visible glyphs.
_CONCEALED_SECRET_RE = re.compile(r"(?:\x1b|\\033)\[8m.*?(?:\x1b|\\033)\[28m")
