"""Crash-backtrace decoding for the devices controller."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.const import Toolchain
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError, ErrorCode
from ...helpers.async_ import run_in_executor
from ...helpers.config_hash import read_build_info_hash
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.storage_path import resolve_idedata_path, resolve_storage_path
from ...helpers.subprocess import run_subprocess_capture
from ..firmware.helpers import _find_sibling_cli

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

_HELPER_MODULE = "esphome_device_builder.helper_cli"
# addr2line is spawned per address against an ELF that can run to tens of MB.
_HELPER_TIMEOUT_S = 60.0

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
    return _result(
        decoded=_coerce_decoded(reply.get("decoded")),
        stale_build=_is_stale(controller, configuration, target.local_config_hash),
        unavailable_reason=str(reply.get("unavailable_reason") or ""),
    )


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

    The load-bearing guard, not an optimisation: without a build, the pinned
    esphome's ``_decode_pc`` walks into ``check_esp_idf_install()`` and starts
    downloading the whole ESP-IDF framework to serve a decode that cannot
    succeed (esphome/esphome#17597). Answer "no build" here and the child is
    never spawned.
    """
    build_path = Path(storage.build_path)
    if storage.toolchain == Toolchain.ESP_IDF:
        return (build_path / "build" / "CMakeCache.txt").is_file()
    if storage.toolchain == Toolchain.SDK_NRF:
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
        result = await run_subprocess_capture(
            # Same locator the download-types caller uses, so the two paths
            # can't end up resolving the helper differently.
            *_find_sibling_cli("device-builder-helper", _HELPER_MODULE),
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


def _coerce_decoded(payload: Any) -> list[dict[str, Any]]:
    """Keep only well-shaped ``{index, text}`` entries from the child's reply."""
    if not isinstance(payload, list):
        return []
    return [
        {"index": entry["index"], "text": entry["text"]}
        for entry in payload
        if isinstance(entry, dict)
        and isinstance(entry.get("index"), int)
        and isinstance(entry.get("text"), str)
    ]


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
