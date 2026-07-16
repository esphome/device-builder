"""Crash-backtrace decoding for the devices controller."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.storage_json import StorageJSON

from ...constants import TOOLCHAIN_ESP_IDF, TOOLCHAIN_SDK_NRF
from ...helpers.api import CommandError, ErrorCode
from ...helpers.async_ import run_in_executor
from ...helpers.config_hash import read_build_info_hash
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.storage_path import resolve_idedata_path, resolve_storage_path
from ...helpers.subprocess import run_subprocess_capture
from ..firmware.helpers import helper_cli_cmd

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# addr2line is spawned per address against an ELF that can run to tens of MB.
_HELPER_TIMEOUT_S = 60.0

# Each child imports ``esphome.components.<platform>`` (~70 MiB RSS) and holds
# it for up to the timeout above, and WS dispatch runs every command in its own
# task, so nothing else serialises a burst of decodes. Same reasoning (and the
# same low-RAM HA Green target) as ``_config_semaphore`` in
# ``helpers/device_yaml/_resolve.py``; a decode is user-initiated and rare, so
# the cap is tighter. Callers queue past it. Note this bounds decodes only:
# it and ``_config_semaphore`` are separate budgets, so a decode burst
# concurrent with a fleet config-resolve can still stack both pools' children.
_MAX_CONCURRENT_DECODES = 2
_decode_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DECODES)

# The frontend's crash excerpt is bounded (25 lines of context + at most 60
# after the marker). Cap anyway: every address in the input costs an addr2line
# spawn in the child, so an unbounded excerpt is an unbounded fan-out.
_MAX_LINES = 200
_MAX_LINE_LENGTH = 500

# Every address upstream's decoders match is 8 hex digits, optionally
# 0x-prefixed: ``PC: 0x400d1a2c``, ``BT0: 0x...``, ``Backtrace: 4008...``, the
# bare words of an esp8266 ``>>>stack>>>`` dump, nrf52's ``PC=0x...``. A batch
# with no such token can't decode to anything, so this is the necessary
# condition for the child to be worth an esphome import. Deliberately not the
# crash-marker grammar: that lives in the frontend's crash-detector, and a
# second copy here would be one more thing to keep in sync.
_ADDRESS_RE = re.compile(r"(?:0x)?[0-9a-fA-F]{8}\b")


async def decode_backtrace(
    controller: DevicesController, configuration: str, lines: list[str]
) -> dict[str, Any]:
    """
    Decode the crash-region *lines* against *configuration*'s local build.

    Answers ``{decoded, stale_build, unavailable_reason}``. A device that
    was never compiled here is a normal outcome, reported as
    ``unavailable_reason``, not raised.
    """
    # ``resolve_storage_path`` collapses to ``<data_dir>/storage/<basename>``,
    # so a traversal-shaped *configuration* could still reach an
    # attacker-chosen basename; ``rel_path`` is the gate. Do not reorder.
    yaml_path = controller._db.settings.rel_path(configuration)
    _validate_lines(lines)
    if not any(_ADDRESS_RE.search(line) for line in lines):
        # Cheapest refusal there is, and it runs before the disk touch: no
        # address in the batch means no crash signal to decode.
        return _result(unavailable_reason="no_backtrace")
    target = await run_in_executor(_resolve_target, configuration, yaml_path)
    if target.unavailable_reason:
        return _result(unavailable_reason=target.unavailable_reason)
    request = dumps(
        {
            "config_path": str(yaml_path),
            "storage_path": str(target.storage_path),
            "idedata_path": str(target.idedata_path),
            "lines": lines,
        }
    )
    reply = await _run_helper(configuration, request)
    if reply is None:
        return _result(unavailable_reason="helper_failed")
    decoded = _coerce_decoded(reply.get("decoded"))
    if decoded is None:
        # A shape drift between host and child is a broken contract, not the
        # successful empty decode an all-clear reason would read as; report it
        # so the client shows the raw dump instead of a symbol-less report.
        return _result(unavailable_reason="helper_failed")
    reason = str(reply.get("unavailable_reason") or "")
    if reason:
        # The child's stderr is DEVNULL, so this is the only place its reason
        # is ever seen. Unconditional on detail: a reason arriving without one
        # is itself worth knowing.
        detail = str(reply.get("detail") or "<no detail>")
        _LOGGER.warning("Backtrace decode for %s reported %s: %s", configuration, reason, detail)
    # Keyed on the frames, not the reason: the latch returns what it decoded
    # before giving up, and those frames need the caption too.
    stale = bool(decoded) and _is_stale(controller, configuration, target.local_config_hash)
    return _result(decoded=decoded, stale_build=stale, unavailable_reason=reason)


def _result(
    *,
    decoded: list[dict[str, Any]] | None = None,
    stale_build: bool = False,
    unavailable_reason: str = "",
) -> dict[str, Any]:
    return {
        "decoded": decoded or [],
        "stale_build": stale_build,
        "unavailable_reason": unavailable_reason,
    }


def _validate_lines(lines: Any) -> None:
    """Raise ``INVALID_ARGS`` unless *lines* is a bounded list of strings."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise CommandError(ErrorCode.INVALID_ARGS, "lines must be a list of strings")
    if len(lines) > _MAX_LINES:
        raise CommandError(ErrorCode.INVALID_ARGS, f"lines must be at most {_MAX_LINES} entries")
    if any(len(line) > _MAX_LINE_LENGTH for line in lines):
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"each line must be at most {_MAX_LINE_LENGTH} characters"
        )


@dataclass(frozen=True)
class _DecodeTarget:
    """Where the child's inputs live, or why they can't be used."""

    unavailable_reason: str = ""
    storage_path: Path | None = None
    idedata_path: Path | None = None
    local_config_hash: str = ""


def _resolve_target(configuration: str, yaml_path: Path) -> _DecodeTarget:
    """Locate the build artifacts for *configuration*; blocking, executor-only."""
    storage_path = resolve_storage_path(configuration)
    storage = StorageJSON.load(storage_path)
    if storage is None or not storage.build_path:
        return _DecodeTarget(unavailable_reason="no_build")
    idedata_path = resolve_idedata_path(configuration, name=storage.name)
    if not _artifacts_present(storage, idedata_path):
        return _DecodeTarget(unavailable_reason="no_build")
    return _DecodeTarget(
        storage_path=storage_path,
        idedata_path=idedata_path,
        local_config_hash=read_build_info_hash(yaml_path) or "",
    )


def _artifacts_present(storage: StorageJSON, idedata_path: Path) -> bool:
    """Report whether the decoder has everything it needs already on disk.

    Load-bearing, not an optimisation: without a build, ``_decode_pc`` walks
    into ``check_esp_idf_install()`` and downloads a whole ESP-IDF framework
    to serve a decode that cannot succeed. esphome/esphome#17597 fixes that
    upstream, but our floor (``esphome>=2024.1.0``) has no ceiling, so most
    versions this runs against never carry the fix.
    """
    build_path = Path(storage.build_path)
    if storage.toolchain == TOOLCHAIN_ESP_IDF:
        return (build_path / "build" / "CMakeCache.txt").is_file()
    if storage.toolchain == TOOLCHAIN_SDK_NRF:
        # nrf52 resolves addr2line off PATH and the ELF from the Zephyr tree,
        # reporting a miss itself rather than shelling out to find one.
        return build_path.is_dir()
    return idedata_path.is_file()


async def _run_helper(configuration: str, request: bytes) -> dict[str, Any] | None:
    """Run the decode in the helper child; ``None`` on any host-side miss.

    The child imports ``esphome.components.<platform>``, which the long-lived
    process must never do. Every failure degrades to ``None`` — a decode is an
    embellishment on a crash report, so a broken helper must not take the
    report down with it.
    """
    try:
        # Held around the spawn only: the coercion below is pure CPU and
        # holding through it would stall a queued decode for nothing.
        async with _decode_semaphore:
            result = await run_subprocess_capture(
                *helper_cli_cmd(),
                "decode-backtrace",
                timeout=_HELPER_TIMEOUT_S,
                stdin_data=request,
                merge_stderr=False,
            )
    except OSError:
        _LOGGER.warning("Could not spawn the backtrace decoder for %s", configuration)
        return None
    if result.timed_out:
        _LOGGER.warning("Backtrace decoding for %s timed out", configuration)
        return None
    if result.returncode != 0:
        # A well-formed dict on stdout from a child that still exited non-zero
        # is a partial/aborted decode, not a success. Same host-side-miss
        # stance as run_worker in _api_probe. Echo stdout: the child's stderr
        # is DEVNULL, so this is the only trace of a crashed child there is.
        _LOGGER.warning(
            "Backtrace decoder for %s exited %s: %r",
            configuration,
            result.returncode,
            result.stdout[:200],
        )
        return None
    try:
        parsed = loads(result.stdout) if result.stdout else None
    except (JSONDecodeError, ValueError):
        _LOGGER.warning(
            "Backtrace decoder for %s emitted unparsable output: %r",
            configuration,
            result.stdout[:200],
        )
        return None
    if not isinstance(parsed, dict):
        _LOGGER.warning(
            "Backtrace decoder for %s wrote no payload (rc=%s)", configuration, result.returncode
        )
        return None
    return parsed


def _coerce_decoded(payload: Any) -> list[dict[str, Any]] | None:
    """Return the child's ``{index, text}`` entries; ``None`` if any was malformed.

    ``None`` means the child broke its contract, so a drift can't masquerade
    as a successful empty decode.
    """
    if not isinstance(payload, list):
        _LOGGER.warning(
            "Backtrace decoder returned a %s for 'decoded', not a list", type(payload).__name__
        )
        return None
    entries = [
        {"index": entry["index"], "text": entry["text"]}
        for entry in payload
        if isinstance(entry, dict)
        and isinstance(entry.get("index"), int)
        and isinstance(entry.get("text"), str)
    ]
    dropped = len(payload) - len(entries)
    if dropped:
        _LOGGER.warning("Dropped %d malformed entries from the backtrace decoder's reply", dropped)
        return None
    return entries


def _is_stale(controller: DevicesController, configuration: str, local_config_hash: str) -> bool:
    """Report whether the running firmware was built from a different config.

    addr2line answers against whatever ELF is on disk, so a rebuild since the
    crash yields confident, wrong symbols. Only claim staleness when both
    hashes are known and differ; an unknown either side is not evidence.
    """
    device = controller.get_by_configuration(configuration)
    if device is None:
        return False
    deployed = device.runtime_state.deployed_config_hash
    if not local_config_hash or not deployed:
        return False
    return deployed != local_config_hash
