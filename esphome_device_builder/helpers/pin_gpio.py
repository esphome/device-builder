"""
Resolve an ESPHome pin literal to a board GPIO number.

Single backend source of truth, kept in lockstep with the frontend
``src/util/pin-gpio.ts`` ``parsePinGpio``, so the catalog the sync scripts
write and the picker that reads it agree on what ``P23`` / ``PA02`` mean.

Pin forms across the platforms ESPHome supports:

* esp / esp8266 / rp2040     : bare int or ``GPIOn``
* bk72xx (LibreTiny/Beken)   : ``P{n}`` (``P23``); ``n`` is the GPIO
* rtl87xx (LibreTiny/Realtek): ``PA{n}`` (``PA02``); single-port, ``n`` is the GPIO
* ln882x (LibreTiny)         : ``PA{n}`` (GPIO ``n``) and ``PB{n}`` (GPIO 16 + n)
* nRF52                      : ``P{port}.{pin}`` = port*32 + pin

Every form is globally unambiguous, so resolving doesn't need the platform.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# ln882x port B is GPIO 16-31.
_LN882X_PORT_B_OFFSET = 16


def _nrf52_gpio(match: re.Match[str]) -> int | None:
    # Reject pin >= 32 rather than fold ``P0.33`` to a different valid pin.
    pin = int(match.group(2))
    return int(match.group(1)) * 32 + pin if pin < 32 else None


# Ordered ``(pattern, transform)``: the dotted nRF52 and lettered PA/PB forms are
# tried before the bare ``P{n}`` so the letter/dot isn't swallowed by it. First
# match wins; its transform result (even ``None``) is returned.
_PIN_FORMS: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], int | None]], ...] = (
    # Bare int / ``GPIOn`` (esp / esp8266 / rp2040 / libretiny GPIO form).
    (re.compile(r"^\s*(?:GPIO)?(\d+)\s*$", re.IGNORECASE), lambda m: int(m.group(1))),
    # nRF52 ``P{port}.{pin}`` = port*32 + pin.
    (re.compile(r"^\s*P(\d+)\.(\d+)\s*$", re.IGNORECASE), _nrf52_gpio),
    # LibreTiny port-A ``PA{n}`` (rtl87xx, ln882x).
    (re.compile(r"^\s*PA(\d+)\s*$", re.IGNORECASE), lambda m: int(m.group(1))),
    # LibreTiny port-B ``PB{n}`` (ln882x).
    (
        re.compile(r"^\s*PB(\d+)\s*$", re.IGNORECASE),
        lambda m: _LN882X_PORT_B_OFFSET + int(m.group(1)),
    ),
    # bk72xx bare ``P{n}``.
    (re.compile(r"^\s*P(\d+)\s*$", re.IGNORECASE), lambda m: int(m.group(1))),
)


def parse_board_gpio(value: object) -> int | None:
    """Board GPIO int from a pin value in any platform form, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    for pattern, transform in _PIN_FORMS:
        if match := pattern.match(value):
            return transform(match)
    return None
