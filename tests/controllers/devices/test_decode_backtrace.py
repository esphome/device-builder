"""Tests for ``devices/decode_backtrace``.

The guard tests here are the point of the suite: the decoder child must be
spawned only when there is a crash signal *and* a local build to decode it
against. Without the second, esphome walks into an ESP-IDF framework install to
serve a request that cannot succeed; esphome/esphome#17597 fixes that upstream,
but our dependency is a floor with no ceiling, so the guard can't assume it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.devices import backtrace
from esphome_device_builder.helpers.api import CommandError, ErrorCode
from esphome_device_builder.helpers.json import dumps, loads
from esphome_device_builder.helpers.subprocess import CapturedSubprocess
from esphome_device_builder.models import Device

from ..._storage_fixtures import write_storage_json
from .conftest import MakeControllerFactory, SeedDeviceFactory

_CRASH_LINES = [
    "Guru Meditation Error: Core  0 panic'ed (StoreProhibited). Exception was unhandled.",
    "PC      : 0x400d1a2c  PS      : 0x00060730",
    "Backtrace: 0x400d1a2c:0x3ffb1f60 0x400d9150:0x3ffb1f80",
]

_DECODED_REPLY = {
    "decoded": [
        {"index": 2, "text": "Decoded 0x400d1a2c: loop() at main.cpp:42"},
        {"index": 2, "text": "  (inlined by) tick() at main.cpp:11"},
    ],
    "unavailable_reason": "",
}


def _stub_helper(
    monkeypatch: pytest.MonkeyPatch, reply: Any, *, calls: list[bytes] | None = None
) -> None:
    """Answer the decoder child with *reply* instead of spawning it."""

    async def _fake(*args: str, stdin_data: bytes | None = None, **kwargs: Any):
        if calls is not None:
            calls.append(stdin_data or b"")
        return CapturedSubprocess(returncode=0, stdout=dumps(reply), timed_out=False)

    monkeypatch.setattr(backtrace, "run_subprocess_capture", _fake)


def _forbid_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything tries to spawn the decoder child."""

    async def _boom(*args: str, **kwargs: Any):
        raise AssertionError("the decoder child must not be spawned")

    monkeypatch.setattr(backtrace, "run_subprocess_capture", _boom)


def _write_idedata(tmp_path: Path, name: str = "kitchen") -> Path:
    """Seed the cached idedata that makes a PlatformIO build look decodable."""
    idedata_path = tmp_path / ".esphome" / "idedata" / f"{name}.json"
    idedata_path.parent.mkdir(parents=True, exist_ok=True)
    idedata_path.write_text('{"prog_path": "/build/firmware.elf", "cc_path": "/bin/gcc"}')
    return idedata_path


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_returns_decoded_frames(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash excerpt against a built device comes back decoded, tagged by line."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    calls: list[bytes] = []
    _stub_helper(monkeypatch, _DECODED_REPLY, calls=calls)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == ""
    assert result["decoded"] == _DECODED_REPLY["decoded"]
    # The child is a pure function of its request: it gets resolved paths and
    # the lines, never a configuration name to re-resolve.
    request = loads(calls[0])
    assert request["lines"] == _CRASH_LINES
    assert request["storage_path"].endswith("kitchen.yaml.json")


@pytest.mark.usefixtures("redirect_storage_path")
async def test_devices_decode_backtrace_command_reaches_the_decoder(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The WS command's kwargs land on the submodule unchanged."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, _DECODED_REPLY)

    result = await controller.decode_backtrace(configuration="kitchen.yaml", lines=_CRASH_LINES)

    assert result["decoded"] == _DECODED_REPLY["decoded"]


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_without_crash_signal_does_not_spawn(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary log lines carry no address, so nothing is spawned to decode them."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _forbid_helper(monkeypatch)

    result = await backtrace.decode_backtrace(
        controller,
        "kitchen.yaml",
        ["[I][app:029]: Running through setup()", "[I][wifi:303]: WiFi connected"],
    )

    assert result == {"decoded": [], "stale_build": False, "unavailable_reason": "no_backtrace"}


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_without_build_does_not_spawn(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cached idedata means no decode, and crucially no child (#17597).

    Reaching the child here is what lets upstream's ``_decode_pc`` start
    installing an ESP-IDF framework to answer a doomed request.
    """
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    controller = make_controller(tmp_path)
    _forbid_helper(monkeypatch)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "no_build"
    assert result["decoded"] == []


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_uncompiled_device_does_not_spawn(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device with no StorageJSON at all is reported, not decoded."""
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", encoding="utf-8")
    controller = make_controller(tmp_path)
    _forbid_helper(monkeypatch)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "no_build"


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_esp_idf_without_a_cmake_cache_does_not_spawn(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The esp-idf gate is the one that matters.

    Reaching the child without a CMake cache is what starts an ESP-IDF
    framework download to serve a doomed decode (#17597).
    """
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    write_storage_json(
        tmp_path,
        "kitchen.yaml",
        build_path=tmp_path / ".esphome" / "build" / "kitchen",
        overrides={"toolchain": "esp-idf"},
    )
    _write_idedata(tmp_path)  # present, and deliberately not what esp-idf reads
    controller = make_controller(tmp_path)
    _forbid_helper(monkeypatch)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "no_build"


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_esp_idf_with_a_cmake_cache_decodes(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An esp-idf build is decodable off its CMake cache, with no idedata."""
    _yaml, build_path = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    write_storage_json(
        tmp_path, "kitchen.yaml", build_path=build_path, overrides={"toolchain": "esp-idf"}
    )
    (build_path / "build").mkdir(parents=True, exist_ok=True)
    (build_path / "build" / "CMakeCache.txt").write_text("CMAKE_ADDR2LINE:FILEPATH=/bin/addr2line")
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, _DECODED_REPLY)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == ""
    assert result["decoded"] == _DECODED_REPLY["decoded"]


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_nrf52_needs_no_idedata(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nrf52 finds its own ELF in the Zephyr tree.

    So the idedata cache isn't the precondition it is for a PlatformIO build.
    """
    _yaml, build_path = await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    write_storage_json(
        tmp_path, "kitchen.yaml", build_path=build_path, overrides={"toolchain": "sdk-nrf"}
    )
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, _DECODED_REPLY)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == ""


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_non_dict_reply_degrades(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid JSON that isn't an object is still a broken contract."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, ["not", "an", "object"])

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "helper_failed"


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_non_list_decoded_is_a_broken_contract(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``decoded`` that isn't a list is drift, surfaced rather than swallowed."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, {"decoded": {"not": "a list"}, "unavailable_reason": ""})

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result == {"decoded": [], "stale_build": False, "unavailable_reason": "helper_failed"}


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_dropped_entries_surface_as_helper_failed(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial shape drift is a broken contract, not a short backtrace.

    The contract reads an empty ``decoded`` with no reason as a clean decode,
    so silently dropping the bad entries would hide the loss from the client.
    """
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(
        monkeypatch,
        {
            "decoded": [
                {"index": 0, "text": "Decoded 0x400d1a2c: loop()"},
                {"index": "nope", "text": "bad index"},
            ],
            "unavailable_reason": "",
        },
    )

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result == {"decoded": [], "stale_build": False, "unavailable_reason": "helper_failed"}


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_unavailable_reply_is_never_flagged_stale(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stale_build`` qualifies a decode, so it can't ride an unavailable one.

    The build can lose its race with the host-side gate and come back
    ``no_build`` from the child, with device state still saying stale.
    """
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    device.runtime_state.deployed_config_hash = "5a94a12d"
    controller._scanner.get_by_configuration = lambda configuration: device
    monkeypatch.setattr(backtrace, "read_build_info_hash", lambda yaml_path: "f3e21d5a")
    _stub_helper(monkeypatch, {"decoded": [], "unavailable_reason": "no_build"})

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result == {"decoded": [], "stale_build": False, "unavailable_reason": "no_build"}


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_helper_spawn_failure_degrades(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken helper costs the decode, not the crash report."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)

    async def _boom(*args: str, **kwargs: Any):
        raise OSError("no such executable")

    monkeypatch.setattr(backtrace, "run_subprocess_capture", _boom)

    with caplog.at_level(logging.WARNING):
        result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "helper_failed"
    assert "Could not spawn the backtrace decoder for kitchen.yaml" in caplog.text


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_timeout_degrades(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that overruns its timeout reports unavailable rather than hanging."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)

    async def _slow(*args: str, **kwargs: Any):
        return CapturedSubprocess(returncode=None, stdout=b"", timed_out=True)

    monkeypatch.setattr(backtrace, "run_subprocess_capture", _slow)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "helper_failed"


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_all_good_entries_pass_the_boundary(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply whose entries are all well-shaped passes through intact."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(
        monkeypatch,
        {
            "decoded": [
                {"index": 0, "text": "Decoded 0x400d1a2c: loop()"},
                {"index": 0, "text": "  (inlined by) tick()"},
            ],
            "unavailable_reason": "",
        },
    )

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["decoded"] == [
        {"index": 0, "text": "Decoded 0x400d1a2c: loop()"},
        {"index": 0, "text": "  (inlined by) tick()"},
    ]
    assert result["unavailable_reason"] == ""


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_non_json_reply_degrades(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-JSON on stdout is a broken contract, reported as a helper failure."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)

    async def _garbage(*args: str, **kwargs: Any):
        return CapturedSubprocess(returncode=0, stdout=b"not json{", timed_out=False)

    monkeypatch.setattr(backtrace, "run_subprocess_capture", _garbage)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "helper_failed"


@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_reports_child_unavailable_reason(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's own verdict (an undecodable platform) reaches the caller."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    _stub_helper(monkeypatch, {"decoded": [], "unavailable_reason": "unsupported_platform"})

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["unavailable_reason"] == "unsupported_platform"


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param("PC: 0x400d1a2c", id="not_a_list"),
        pytest.param([b"PC: 0x400d1a2c"], id="not_strings"),
        pytest.param(["PC: 0x400d1a2c"] * 201, id="too_many_lines"),
        pytest.param(["x" * 501], id="line_too_long"),
    ],
)
@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_rejects_bad_lines(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    lines: Any,
) -> None:
    """Bounds are enforced before any work: each address costs an addr2line spawn."""
    controller = make_controller(tmp_path)
    _forbid_helper(monkeypatch)

    with pytest.raises(CommandError) as err:
        await backtrace.decode_backtrace(controller, "kitchen.yaml", lines)

    assert err.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    ("deployed", "local", "expected"),
    [
        pytest.param("5a94a12d", "f3e21d5a", True, id="hashes_differ"),
        pytest.param("5a94a12d", "5a94a12d", False, id="hashes_match"),
        pytest.param("", "5a94a12d", False, id="device_hash_unknown"),
        pytest.param("5a94a12d", "", False, id="build_hash_unknown"),
    ],
)
@pytest.mark.usefixtures("redirect_storage_path")
async def test_decode_backtrace_flags_stale_build(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    seed_device: SeedDeviceFactory,
    monkeypatch: pytest.MonkeyPatch,
    deployed: str,
    local: str,
    expected: bool,
) -> None:
    """Stale only when both hashes are known and disagree; unknown is not evidence."""
    await seed_device(tmp_path, "kitchen.yaml", with_build_dir=True)
    _write_idedata(tmp_path)
    controller = make_controller(tmp_path)
    device = Device(name="kitchen", friendly_name="Kitchen", configuration="kitchen.yaml")
    device.runtime_state.deployed_config_hash = deployed
    controller._scanner.get_by_configuration = lambda configuration: device
    monkeypatch.setattr(backtrace, "read_build_info_hash", lambda yaml_path: local or None)
    _stub_helper(monkeypatch, _DECODED_REPLY)

    result = await backtrace.decode_backtrace(controller, "kitchen.yaml", _CRASH_LINES)

    assert result["stale_build"] is expected
