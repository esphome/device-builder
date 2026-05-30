"""Server-side chip / variant detection via esptool."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path

from ...helpers.subprocess import run_subprocess_capture

_LOGGER = logging.getLogger(__name__)

# Maps esptool's chip family string (lower-cased) to
# ``(chip_family, variant, platform)``. ``chip_family`` matches a
# ``WIZARD_BOARD_PLATFORMS.label`` value in the frontend so callers
# can hand it straight to the board-picker filter; ``variant`` and
# ``platform`` mirror ESPHome's own keys. Families not in this
# table cause ``_chip_family_to_descriptor`` to return ``None``,
# which the WS handler surfaces as ``_DETECT_UNKNOWN_CHIP``.
#
# esptool can only identify ESP chips. Non-ESP platforms (RP2040 /
# RP2350, BK72xx, RTL87xx, LN882x, nRF52) need their own probe path;
# they're not in this table.
_CHIP_FAMILY_MAP: dict[str, tuple[str, str, str]] = {
    "esp32": ("ESP32", "esp32", "esp32"),
    "esp32-s2": ("ESP32-S2", "esp32s2", "esp32"),
    "esp32-s3": ("ESP32-S3", "esp32s3", "esp32"),
    "esp32-c2": ("ESP32-C2", "esp32c2", "esp32"),
    "esp32-c3": ("ESP32-C3", "esp32c3", "esp32"),
    "esp32-c5": ("ESP32-C5", "esp32c5", "esp32"),
    "esp32-c6": ("ESP32-C6", "esp32c6", "esp32"),
    "esp32-c61": ("ESP32-C61", "esp32c61", "esp32"),
    "esp32-h2": ("ESP32-H2", "esp32h2", "esp32"),
    "esp32-p4": ("ESP32-P4", "esp32p4", "esp32"),
    "esp8266": ("ESP8266", "", "esp8266"),
}

# ESP-IDF ``esp_app_desc_t`` lives at the start of every IDF app
# image. With ESPHome's default partition layout the app partition
# starts at 0x10000 and the descriptor sits at offset 0x20 within,
# i.e. 0x10020 in flash. The layout is:
#
#   magic         u32       offset 0      0xabcd5432, little-endian
#   secure_ver    u32       offset 4
#   reserved      u8[8]     offset 8
#   version       char[32]  offset 16
#   project_name  char[32]  offset 48
#   …             (more fields we don't need)
#
# ESPHome populates ``project_name`` from ``esphome.name``, which
# vendors flashing factory firmware set to a catalogue board id —
# that's how the wizard auto-routes a starter-kit straight to its
# specific setup screen.
_APP_DESC_OFFSET = 0x10020
_APP_DESC_SIZE = 256
_APP_DESC_MAGIC = 0xABCD5432
_PROJECT_NAME_OFFSET = 48
_PROJECT_NAME_SIZE = 32

# Windows ``COM<n>`` port names. Linux / macOS ports start with
# ``/dev/`` and are validated separately. Catches accidental command
# injection via the port arg — only well-formed names reach esptool.
_WINDOWS_PORT_RE = re.compile(r"^COM\d{1,3}$", re.IGNORECASE)


def _is_valid_port_name(port: str) -> bool:
    """Reject port strings that don't look like a real device path.

    Defence-in-depth — esptool ultimately validates the port itself,
    but accepting arbitrary strings here would let a malicious caller
    pass an argv that triggers esptool to read from a path it
    shouldn't (e.g. a config file). Restrict to ``/dev/<basename>``
    (POSIX serial nodes) or ``COM<n>`` (Windows).
    """
    if port.startswith("/dev/"):
        # Reject path traversal and shell metacharacters.
        rest = port[len("/dev/") :]
        return (
            bool(rest)
            and "/" not in rest
            and ".." not in rest
            and all(c.isalnum() or c in "-_." for c in rest)
        )
    return bool(_WINDOWS_PORT_RE.match(port))


def _chip_family_to_descriptor(esptool_family: str) -> dict[str, str] | None:
    """Map ``"ESP32-C3"`` → ``{chip_family, variant, platform}``."""
    key = esptool_family.strip().lower()
    entry = _CHIP_FAMILY_MAP.get(key)
    if entry is None:
        return None
    family, variant, platform = entry
    return {"chip_family": family, "variant": variant, "platform": platform}


def _parse_project_name(blob: bytes) -> str | None:
    """Pull ``project_name`` out of a 256-byte ``esp_app_desc_t`` blob.

    Returns ``None`` whenever the magic word doesn't match (not an
    IDF app, or partition-layout drift) or the field is empty.
    Callers treat this as "no factory firmware present" and fall
    through to chip-family filtering.
    """
    if len(blob) < _PROJECT_NAME_OFFSET + _PROJECT_NAME_SIZE:
        return None
    magic = int.from_bytes(blob[0:4], "little")
    if magic != _APP_DESC_MAGIC:
        return None
    raw = blob[_PROJECT_NAME_OFFSET : _PROJECT_NAME_OFFSET + _PROJECT_NAME_SIZE]
    nul = raw.find(b"\x00")
    if nul != -1:
        raw = raw[:nul]
    try:
        name = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return name or None


# Timeouts for the two esptool subcommands ``detect_chip_cmd``
# invokes. ``chip-id`` against a healthy ESP usually completes in
# 2-3 s (reset pulse + ROM handshake + read MAC); the 30 s ceiling
# leaves headroom for slow USB hubs, macOS re-enumeration delays,
# and the occasional retry esptool does internally before giving up.
# ``read-flash`` of 256 B is similar but adds stub upload (a few
# hundred ms) — same ceiling is fine.
_CHIP_DETECT_TIMEOUT = 30.0
_READ_FLASH_TIMEOUT = 30.0


def _classify_esptool_failure(output: str) -> str:
    """Map an esptool error blob to one of the ``_DETECT_*`` reasons.

    Pattern-matches on substrings the esptool CLI prints today —
    fragile in principle, but the patterns have been stable across
    v4 → v5 (the underlying errors come from pyserial / the OS,
    not esptool itself).
    """
    lower = output.lower()
    if "no module named" in lower or "modulenotfounderror" in lower:
        return _DETECT_NO_ESPTOOL
    # POSIX EACCES (errno 13) and pyserial's PermissionError typically
    # mean the user isn't in the dialout group on Linux — different
    # fix from EBUSY (close another app), so they get their own bucket.
    if "errno 13" in lower or "permissionerror" in lower or "permission denied" in lower:
        return _DETECT_PERMISSION
    if (
        "resource busy" in lower
        or "could not open port" in lower
        or "port is busy" in lower
        or "errno 16" in lower
        or "access is denied" in lower  # Windows equivalent of EBUSY
    ):
        return _DETECT_BUSY
    if (
        "failed to connect" in lower
        or "no serial data received" in lower
        or "wrong boot mode detected" in lower
    ):
        return _DETECT_NO_RESPONSE
    return _DETECT_UNKNOWN


def _parse_chip_family_line(output: str) -> dict[str, str] | None:
    r"""Pull the chip family out of an esptool stdout blob.

    esptool prints the family in three places we can target, listed
    here from most to least reliable:

    1. ``"Chip type:          ESP32-C3 (QFN32) (revision v0.3)"`` —
       prints unconditionally after a successful detect+connect,
       happens *after* the collapsing stage finishes (so escape
       codes never overwrite it).
    2. ``"Connected to ESP32-C3 on /dev/..."`` — same post-stage
       guarantee, but the family is embedded mid-line so the
       extraction is slightly more fragile.
    3. ``"Detecting chip type... ESP32-C3"`` — what ``_verify_chip``
       in the firmware controller parses. Lives *inside* the
       collapsible stage; when esptool's "smart features" are
       active (TERM set + colours enabled) the line is still in
       the byte stream but the post-stage ``\x1b[1A\x1b[2K``
       sequence visually erases it. The bytes themselves survive,
       so the parser still finds the line — kept as a final
       fallback for completeness.
    """
    # 1) "Chip type:" line — most reliable, immune to stage collapsing.
    for line in output.splitlines():
        idx = line.find("Chip type:")
        if idx != -1:
            after = line[idx + len("Chip type:") :].strip()
            # Strip the parenthesised package / revision suffix
            # (``ESP32-C3 (QFN32) (revision v0.3)`` → ``ESP32-C3``).
            family = after.split("(")[0].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    # 2) "Connected to X on" line.
    for line in output.splitlines():
        idx = line.find("Connected to ")
        if idx != -1:
            after = line[idx + len("Connected to ") :]
            family = after.split(" on ")[0].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    # 3) "Detecting chip type..." legacy fallback.
    for line in output.splitlines():
        if "Detecting chip type" in line:
            family = line.split("...")[-1].strip()
            descriptor = _chip_family_to_descriptor(family)
            if descriptor is not None:
                return descriptor

    return None


async def _run_esptool(args: list[str], timeout: float) -> tuple[int, bytes, bool]:
    """Spawn esptool with *args* and capture stdout+stderr.

    Uses :func:`controllers.firmware.helpers._find_esptool_cmd` to
    pick the right invocation (sibling script preferred over
    ``python -m esptool``) and runs through
    :func:`helpers.subprocess.run_subprocess_capture` — the same
    one-shot helper :func:`_verify_esphome_importable` uses.

    Returns ``(returncode, stdout, timed_out)``. The caller treats
    ``timed_out`` separately from a normal non-zero exit so the WS
    error message can recommend an unplug/replug rather than
    pointing at the cable.

    Lazy-imported to avoid a ``config`` ↔ ``firmware.persistence``
    circular import (persistence reaches back into config for
    ``_load_metadata`` / ``metadata_transaction``).
    """
    from ..firmware.helpers import _find_esptool_cmd  # noqa: PLC0415

    cmd = _find_esptool_cmd()
    result = await run_subprocess_capture(*cmd, *args, timeout=timeout)
    rc = result.returncode if result.returncode is not None else -1
    return rc, result.stdout, result.timed_out


async def _detect_chip_via_esptool(
    port: str,
) -> tuple[dict[str, str] | None, str | None]:
    """Run ``esptool chip-id`` and parse the chip family.

    Returns ``(descriptor, None)`` on success or
    ``(None, failure_reason)`` on failure. ``failure_reason`` is one
    of the ``_DETECT_*`` constants — the handler maps it to a
    user-facing message.
    """
    returncode, stdout, timed_out = await _run_esptool(
        ["--port", port, "chip-id"], _CHIP_DETECT_TIMEOUT
    )
    if timed_out:
        _LOGGER.debug("esptool chip-id on %s timed out after %ss", port, _CHIP_DETECT_TIMEOUT)
        return None, _DETECT_TIMEOUT
    output = stdout.decode("utf-8", errors="replace")
    if returncode != 0:
        _LOGGER.debug("esptool chip-id on %s exited %d: %s", port, returncode, output)
        return None, _classify_esptool_failure(output)
    descriptor = _parse_chip_family_line(output)
    if descriptor is None:
        _LOGGER.debug(
            "esptool chip-id on %s succeeded but family wasn't in our map: %s",
            port,
            output,
        )
        return None, _DETECT_UNKNOWN_CHIP
    return descriptor, None


def _make_descriptor_tempfile() -> str:
    """Allocate (and close) a tempfile for esptool's ``read-flash`` output."""
    fd, path = tempfile.mkstemp(prefix="esp_app_desc_", suffix=".bin")
    os.close(fd)
    return path


def _read_descriptor_file(path: str) -> str | None:
    """Read *path* and decode ``project_name`` from the app descriptor."""
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return None
    return _parse_project_name(blob)


def _unlink_quietly(path: str) -> None:
    """``Path(path).unlink()`` swallowing ``OSError``."""
    with suppress(OSError):
        Path(path).unlink()


async def _read_app_descriptor_board_id(port: str) -> str | None:
    """Best-effort: read 256 B at 0x10020 and decode project_name.

    Failure here is non-fatal — the caller still has chip-family
    info to narrow the picker with. Uses a tempfile because
    esptool's ``read-flash`` writes the binary payload to a named
    file, not stdout. The tempfile-create / read / unlink are sync
    FS calls so they run via ``asyncio.to_thread`` to keep
    blockbuster happy.
    """
    path = await asyncio.to_thread(_make_descriptor_tempfile)
    try:
        returncode, _stdout, timed_out = await _run_esptool(
            [
                "--port",
                port,
                "read-flash",
                hex(_APP_DESC_OFFSET),
                str(_APP_DESC_SIZE),
                path,
            ],
            _READ_FLASH_TIMEOUT,
        )
        if timed_out or returncode != 0:
            return None
        return await asyncio.to_thread(_read_descriptor_file, path)
    finally:
        await asyncio.to_thread(_unlink_quietly, path)


# Failure classifications for ``_detect_chip_via_esptool``. The
# handler in ``detect_chip_cmd`` maps each to a user-facing message
# — they all surface as ``UNAVAILABLE`` to the WS client, the
# distinction is in the human text. ``BUSY`` is the load-bearing one
# (a serial monitor or stale WebSerial session is the single most
# common reason detection fails); without it the user gets a
# misleading "is a device connected?" message even though the cable
# is plugged in.
_DETECT_BUSY = "busy"
_DETECT_PERMISSION = "permission"
_DETECT_NO_RESPONSE = "no_response"
_DETECT_TIMEOUT = "timeout"
_DETECT_NO_ESPTOOL = "no_esptool"
_DETECT_UNKNOWN_CHIP = "unknown_chip"
_DETECT_UNKNOWN = "unknown"


# Per-reason message templates. ``{port}`` is interpolated by
# ``_detect_failure_message`` so each template stays a plain string
# literal — easier to scan than nested f-strings inside a branchy
# function (and keeps ruff happy about the if/elif chain length).
_DETECT_FAILURE_MESSAGES: dict[str, str] = {
    _DETECT_BUSY: (
        "{port} is already in use by another application. Close any "
        "browser tab using Web Serial or any serial monitor connected "
        "to this port, then try again."
    ),
    _DETECT_PERMISSION: (
        "Permission denied opening {port}. On Linux your user may "
        "need to be in the ``dialout`` group "
        "(``sudo usermod -a -G dialout $USER`` and log back in)."
    ),
    _DETECT_NO_RESPONSE: (
        "No response from a chip on {port}. Check the USB cable, "
        "and on boards without auto-reset try holding the BOOT button "
        "while you plug it in."
    ),
    _DETECT_TIMEOUT: (
        "esptool didn't finish in time on {port}. The chip may be "
        "unresponsive — unplug and replug, then try again."
    ),
    _DETECT_UNKNOWN_CHIP: (
        "Detected a device on {port}, but it isn't a recognised ESP "
        "chip family. This command only supports ESP32 / ESP8266 "
        "variants — pick a board manually from the list."
    ),
    _DETECT_NO_ESPTOOL: (
        "Could not run esptool on the server. Make sure esptool is "
        "installed in the dashboard's Python environment."
    ),
}


def _detect_failure_message(reason: str | None, port: str) -> str:
    """Translate a ``_DETECT_*`` reason into a user-facing message."""
    template = _DETECT_FAILURE_MESSAGES.get(
        reason or "",
        "Could not detect a chip on {port}. Is a supported ESP device connected?",
    )
    return template.format(port=port)
