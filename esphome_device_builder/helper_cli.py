"""Internal subprocess helpers that need ``esphome.components`` imported.

Run as ``device-builder-helper <command>`` (the package's ``[project.scripts]``
entry point) or ``python -m esphome_device_builder.helper_cli``. The dashboard
spawns this so its long-lived process never imports heavy ``esphome.components``
modules (esp32 pulls espidf -> requests -> esphome.config); the child does the
import, prints JSON to stdout, and exits.

Commands:
  download-types <storage-json-path> <component>
      Print ``[{title, description, file}]`` from
      ``esphome.components.<component>.get_download_types`` for the device whose
      StorageJSON sidecar is at the given path. Used for the build-dir-dependent
      platforms (libretiny / nrf52) the generated catalog can't precompute.

  decode-backtrace
      Read ``{config_path, storage_path, idedata_path, lines}`` as JSON on stdin
      and print ``{decoded: [{index, text}], unavailable_reason}``. Feeds the
      lines through ``esphome.components.<platform>.process_stacktrace`` so a
      crash captured over Web Serial (no inline decoder) gets the same addr2line
      output ``esphome logs`` produces. Request rides stdin rather than argv:
      log lines are untrusted and unbounded, and argv is both size-capped and
      world-readable in the process table.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import logging
import re
import sys
from pathlib import Path
from typing import Any

from esphome.const import KEY_CORE
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from .definitions import coerce_download_entries
from .helpers.json import dumps_str, loads

_LOGGER = logging.getLogger(__name__)

# esphome component module names are lowercase identifiers. Validate before
# interpolating into the import path so a crafted target_platform can't steer
# ``import_module`` to a dotted sub-path or an unexpected module (defence in
# depth: the name is already confined to the ``esphome.components.`` prefix and
# import_module is not eval, but keep the surface minimal).
_COMPONENT_RE = re.compile(r"[a-z0-9_]+")

# esphome ``Toolchain`` values as the plain strings the sidecar stores; see
# controllers/devices/backtrace.py for why these aren't ``Toolchain`` members.
_TOOLCHAIN_ESP_IDF = "esp-idf"
_TOOLCHAIN_SDK_NRF = "sdk-nrf"


def _cmd_download_types(args: argparse.Namespace) -> int:
    storage = StorageJSON.load(Path(args.storage_path))
    if storage is None or not _COMPONENT_RE.fullmatch(args.component):
        sys.stdout.write(dumps_str([]))
        return 0
    # Keep stdout pure JSON: route anything esphome prints to stdout during the
    # component import / get_download_types to stderr, so the parent's parse
    # can't choke on a banner or deprecation notice. ``coerce_download_entries``
    # shapes + tolerates a malformed entry (no string ``file``) instead of one
    # bad entry aborting the whole reply.
    with contextlib.redirect_stdout(sys.stderr):
        module = importlib.import_module(f"esphome.components.{args.component}")
        entries = coerce_download_entries(module.get_download_types(storage))
    sys.stdout.write(dumps_str(entries))
    return 0


def _cmd_decode_backtrace(args: argparse.Namespace) -> int:
    request = loads(sys.stdin.buffer.read())
    # Same stdout-purity contract as download-types: the component import and
    # addr2line shell-outs both print.
    with contextlib.redirect_stdout(sys.stderr):
        result = _decode_backtrace(
            config_path=Path(request["config_path"]),
            storage_path=Path(request["storage_path"]),
            idedata_path=Path(request["idedata_path"]),
            lines=request["lines"],
        )
    sys.stdout.write(dumps_str(result))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="device-builder-helper", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download_types = sub.add_parser(
        "download-types", help="Print get_download_types JSON for a device's storage."
    )
    download_types.add_argument("storage_path", help="Path to the StorageJSON sidecar.")
    download_types.add_argument("component", help="esphome.components.<component> to query.")
    download_types.set_defaults(func=_cmd_download_types)
    decode_backtrace = sub.add_parser(
        "decode-backtrace", help="Decode crash-region log lines; JSON request on stdin."
    )
    decode_backtrace.set_defaults(func=_cmd_decode_backtrace)
    args = parser.parse_args()
    return int(args.func(args))


def _unavailable(reason: str) -> dict[str, Any]:
    return {"decoded": [], "unavailable_reason": reason}


def _decode_backtrace(
    *, config_path: Path, storage_path: Path, idedata_path: Path, lines: list[str]
) -> dict[str, Any]:
    """Run *lines* through the target platform's ``process_stacktrace``."""
    storage = StorageJSON.load(storage_path)
    if storage is None:
        return _unavailable("no_build")
    # apply_to_core() sets name / build_path / toolchain / target_platform but
    # not config_path, which CORE.config_dir and CORE.data_dir resolve off.
    CORE.config_path = config_path
    storage.apply_to_core()
    platform = CORE.target_platform
    # esp-idf reads its toolchain out of the CMake cache and nrf52 walks the
    # Zephyr build tree; only the PlatformIO path (also where an older sidecar
    # with no recorded toolchain lands) can reach get_idedata. Read off the
    # sidecar's plain string rather than CORE.using_toolchain_*: those
    # properties track an enum that gains members, so one missing on an older
    # esphome would break every decode.
    uses_idedata = storage.toolchain not in (_TOOLCHAIN_ESP_IDF, _TOOLCHAIN_SDK_NRF)
    if uses_idedata and not _pin_idedata(idedata_path):
        return _unavailable("no_build")
    process_stacktrace, reason = _load_decoder(platform)
    if process_stacktrace is None:
        return _unavailable(reason)
    return _run_decoder(process_stacktrace, platform, lines)


def _load_decoder(platform: str) -> tuple[Any | None, str]:
    """Find *platform*'s ``process_stacktrace``; ``(None, reason)`` when absent.

    *platform* is read off the on-disk sidecar and interpolated into an import
    path, so the name gate lives here, next to the interpolation it protects,
    rather than at the caller where the next caller could miss it.
    """
    if not _COMPONENT_RE.fullmatch(platform):
        return None, "unsupported_platform"
    try:
        module = importlib.import_module(f"esphome.components.{platform}")
    except ImportError as err:
        if isinstance(err, ModuleNotFoundError) and err.name == f"esphome.components.{platform}":
            return None, "unsupported_platform"
        # An ImportError raised from *inside* the package is a broken esphome
        # install (a missing native dep, a half-installed toolchain), not a
        # platform without a decoder. Reporting it as the latter would tell
        # the user esp32 can't be decoded, which is a lie they'd act on.
        _LOGGER.warning("Importing the %s decoder failed", platform, exc_info=True)
        return None, "decode_failed"
    # Discovery is an attribute lookup, same as esphome's own log clients; a
    # platform without a decoder (libretiny, host, ...) just doesn't have one.
    process_stacktrace = getattr(module, "process_stacktrace", None)
    if process_stacktrace is None:
        return None, "unsupported_platform"
    return process_stacktrace, ""


def _pin_idedata(idedata_path: Path) -> bool:
    """Seed ``CORE``'s idedata memo from the on-disk cache; False if unusable.

    Load-bearing, not an optimisation. ``get_idedata`` returns the memo when
    set, so this is what stops ``_load_idedata`` from deciding the cache is
    stale (it compares against ``platformio.ini``'s mtime, which a re-render
    bumps) and shelling out to a full ``pio run -t idedata`` from what is
    supposed to be a read-only decode. Failing closed here costs a decode;
    failing open costs a compile.
    """
    try:
        # Kept local, and inside the try, so an upstream move of the module
        # fails this decode rather than the whole helper's import (which
        # download-types shares).
        from esphome.platformio.toolchain import KEY_IDEDATA, IDEData  # noqa: PLC0415

        CORE.data[KEY_CORE][KEY_IDEDATA] = IDEData(loads(idedata_path.read_bytes()))
    except Exception:
        _LOGGER.warning("Could not pin idedata from %s", idedata_path, exc_info=True)
        return False
    return True


def _run_decoder(process_stacktrace: Any, platform: str, lines: list[str]) -> dict[str, Any]:
    """Feed *lines* through *process_stacktrace*, collecting what it logs.

    Decoded output is only ever emitted through the platform module's logger
    (``Decoded %s`` at WARNING on esp32 / esp8266, ERROR on rp2040 / nrf52), so
    capture the records rather than parsing anything. Because this drives the
    loop, records raised while line *i* is in flight belong to line *i*, which
    is what lets the caller interleave decoded output against its source line.
    """
    collector = _RecordCollector()
    logger = logging.getLogger(f"esphome.components.{platform}")
    previous_level = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.WARNING)
    decoded: list[dict[str, Any]] = []
    reason = ""
    backtrace_state = False
    try:
        for index, line in enumerate(lines):
            collector.messages.clear()
            try:
                # Empty config: every decode path we allow reads its toolchain
                # from CORE or the pinned idedata, never from the config dict.
                backtrace_state = process_stacktrace({}, line, backtrace_state)
            except Exception:
                # Latch off. A crash dump repeats the same failing lookup once
                # per backtrace frame, and each attempt can be a subprocess.
                _LOGGER.warning("Backtrace decoding failed on line %d", index, exc_info=True)
                reason = "decode_failed"
                break
            decoded.extend(
                {"index": index, "text": text}
                for message in collector.messages
                # addr2line reports inlined frames as extra lines inside one
                # record; split so they arrive as the caller's log would show.
                for text in message.splitlines()
            )
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
    return {"decoded": decoded, "unavailable_reason": reason}


class _RecordCollector(logging.Handler):
    """Buffers the messages a decoder logs while one line is processed."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
