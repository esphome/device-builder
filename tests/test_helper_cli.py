"""Tests for the device-builder-helper subprocess and the runtime invariant.

``device-builder-helper download-types`` is how the dashboard answers the
build-dir-dependent platforms (libretiny / nrf52) without importing
``esphome.components.*`` in its own process. These pin that the child's JSON
matches the in-process ``get_download_types`` it replaces, and that running the
download path never pulls those modules into the main process.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from esphome_device_builder import helper_cli
from esphome_device_builder.controllers.firmware.download import _helper_cmd


def _make_storage(tmp_path: Path, target_platform: str, *build_files: str) -> tuple[Path, Path]:
    """Write a StorageJSON sidecar + build dir; return ``(storage_path, build_dir)``."""
    build_dir = tmp_path / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    for name in build_files:
        path = build_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    storage = StorageJSON(
        storage_version=1,
        name="demo",
        friendly_name=None,
        comment=None,
        esphome_version=None,
        src_version=None,
        address="demo.local",
        web_port=None,
        target_platform=target_platform,
        build_path=str(build_dir),
        firmware_bin_path=str(build_dir / "firmware.bin"),
        loaded_integrations=[],
        loaded_platforms=[],
        no_mdns=False,
    )
    storage_path = tmp_path / "demo.json"
    storage.save(storage_path)
    return storage_path, build_dir


@pytest.mark.parametrize(
    ("target_platform", "component", "build_files"),
    [
        ("bk72xx", "libretiny", ("firmware.uf2",)),
        ("nrf52", "nrf52", ("zephyr/zephyr.uf2", "firmware.zip")),
    ],
)
def test_helper_download_types_matches_in_process(
    tmp_path: Path, target_platform: str, component: str, build_files: tuple[str, ...]
) -> None:
    """The helper child emits the same entries as an in-process get_download_types call.

    Runs the production command (``_helper_cmd()``) end to end, so the installed
    ``device-builder-helper`` console-script entry point is exercised under CI
    (and the ``-m`` fallback in an editable dev checkout).
    """
    storage_path, _build = _make_storage(tmp_path, target_platform, *build_files)

    result = subprocess.run(  # noqa: S603 — args fully test-controlled
        [*_helper_cmd(), "download-types", str(storage_path), component],
        check=True,
        capture_output=True,
        text=True,
    )
    child = json.loads(result.stdout)

    module = importlib.import_module(f"esphome.components.{component}")
    expected = [
        {
            "title": entry.get("title", ""),
            "description": entry.get("description", ""),
            "file": entry["file"],
        }
        for entry in module.get_download_types(StorageJSON.load(storage_path))
    ]
    assert child == expected
    assert child, "fixture should produce at least one downloadable entry"


def test_cmd_download_types_prints_entries(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """In-process: the subcommand prints the platform's download-type JSON."""
    storage_path, _build = _make_storage(tmp_path, "bk72xx", "firmware.uf2")
    args = SimpleNamespace(storage_path=str(storage_path), component="libretiny")

    assert helper_cli._cmd_download_types(args) == 0  # type: ignore[arg-type]

    entries = json.loads(capsys.readouterr().out)
    assert entries and entries[0]["file"] == "firmware.uf2"


@pytest.mark.parametrize("bad", ["esp32.boards", "../evil", "esp32;rm -rf", "a/b", "ESP32", ""])
def test_cmd_download_types_rejects_invalid_component(
    tmp_path: Path, capsys: pytest.CaptureFixture, bad: str
) -> None:
    """A component name outside ``[a-z0-9_]+`` is rejected before any import."""
    storage_path, _build = _make_storage(tmp_path, "bk72xx", "firmware.uf2")
    args = SimpleNamespace(storage_path=str(storage_path), component=bad)

    assert helper_cli._cmd_download_types(args) == 0  # type: ignore[arg-type]

    assert json.loads(capsys.readouterr().out) == []


def test_cmd_download_types_missing_storage_prints_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A missing sidecar prints ``[]`` rather than raising."""
    args = SimpleNamespace(storage_path=str(tmp_path / "absent.json"), component="libretiny")

    assert helper_cli._cmd_download_types(args) == 0  # type: ignore[arg-type]

    assert json.loads(capsys.readouterr().out) == []


def test_main_dispatches_download_types(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` parses argv and dispatches the download-types subcommand."""
    storage_path, _build = _make_storage(tmp_path, "ESP8266", "firmware.bin")
    monkeypatch.setattr(
        sys, "argv", ["device-builder-helper", "download-types", str(storage_path), "esp8266"]
    )

    assert helper_cli.main() == 0

    assert any(entry["file"] == "firmware.bin" for entry in json.loads(capsys.readouterr().out))


def test_run_decoder_latches_off_after_the_first_failure() -> None:
    """The first failure disables decoding for the rest of the dump."""
    calls: list[str] = []

    def _raising(config: dict, line: str, state: bool) -> bool:
        calls.append(line)
        raise FileNotFoundError("no such directory: /data/build/ol/build")

    result = helper_cli._run_decoder(_raising, "esp32", ["BT0: 0x400d1a2c"] * 12)

    assert calls == ["BT0: 0x400d1a2c"]
    assert result == {"decoded": [], "unavailable_reason": "decode_failed"}


def test_run_decoder_tags_each_message_with_its_source_line() -> None:
    """Decoded output is attributed to the line in flight when it was logged."""
    logger = logging.getLogger("esphome.components.esp32")

    def _decode(config: dict, line: str, state: bool) -> bool:
        if "0x400d1a2c" in line:
            logger.warning("Decoded %s", "0x400d1a2c: loop() at main.cpp:42")
        return state

    result = helper_cli._run_decoder(_decode, "esp32", ["boot ok", "PC: 0x400d1a2c", "rebooting"])

    assert result["decoded"] == [{"index": 1, "text": "Decoded 0x400d1a2c: loop() at main.cpp:42"}]
    assert result["unavailable_reason"] == ""


def test_run_decoder_splits_inlined_frames_into_lines() -> None:
    """addr2line reports inlined frames inside one record; they arrive split."""
    logger = logging.getLogger("esphome.components.esp32")

    def _decode(config: dict, line: str, state: bool) -> bool:
        logger.warning("Decoded %s", "0x400d1a2c: loop()\n  (inlined by) tick() at main.cpp:11")
        return state

    result = helper_cli._run_decoder(_decode, "esp32", ["PC: 0x400d1a2c"])

    assert result["decoded"] == [
        {"index": 0, "text": "Decoded 0x400d1a2c: loop()"},
        {"index": 0, "text": "  (inlined by) tick() at main.cpp:11"},
    ]


def test_run_decoder_threads_backtrace_state_across_lines() -> None:
    """esp8266's ``>>>stack>>>`` dump only decodes if the state is fed back in."""
    seen: list[bool] = []

    def _decode(config: dict, line: str, state: bool) -> bool:
        seen.append(state)
        return state or ">>>stack>>>" in line

    helper_cli._run_decoder(_decode, "esp8266", [">>>stack>>>", "4020 4021", "<<<stack<<<"])

    assert seen == [False, True, True]


def test_run_decoder_restores_the_logger_it_borrowed() -> None:
    """The capture handler and level are transient, not a lasting side effect."""
    logger = logging.getLogger("esphome.components.esp32")
    logger.setLevel(logging.CRITICAL)
    before = list(logger.handlers)

    helper_cli._run_decoder(lambda config, line, state: state, "esp32", ["PC: 0x400d1a2c"])

    assert logger.handlers == before
    assert logger.level == logging.CRITICAL


def test_decode_backtrace_missing_storage_is_unavailable(tmp_path: Path) -> None:
    """A device with no sidecar reports ``no_build`` rather than raising."""
    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "absent.yaml",
        storage_path=tmp_path / "absent.json",
        idedata_path=tmp_path / "absent-idedata.json",
        lines=["PC: 0x400d1a2c"],
    )

    assert result == {"decoded": [], "unavailable_reason": "no_build"}


def test_pin_idedata_refuses_a_missing_cache(tmp_path: Path) -> None:
    """Failing closed here costs a decode; failing open costs a ``pio run``."""
    assert helper_cli._pin_idedata(tmp_path / "absent.json") is False


def _write_idedata(tmp_path: Path) -> Path:
    idedata_path = tmp_path / "idedata.json"
    idedata_path.write_text(json.dumps({"prog_path": "/build/firmware.elf", "cc_path": "/bin/gcc"}))
    return idedata_path


def _stub_platform_module(monkeypatch: pytest.MonkeyPatch, module: object) -> None:
    """Stand in for ``esphome.components.<platform>`` without importing it."""
    monkeypatch.setattr(helper_cli.importlib, "import_module", lambda name: module)


def test_decode_backtrace_pins_idedata_then_runs_the_platform_decoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path: CORE bootstrapped off the sidecar, decoder driven."""
    storage_path, _build = _make_storage(tmp_path, "ESP32", "firmware.bin")
    seen: list[str] = []

    def _decode(config: dict, line: str, state: bool) -> bool:
        seen.append(line)
        logging.getLogger("esphome.components.esp32").warning("Decoded %s", "0x400d1a2c: loop()")
        return state

    _stub_platform_module(monkeypatch, SimpleNamespace(process_stacktrace=_decode))

    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "demo.yaml",
        storage_path=storage_path,
        idedata_path=_write_idedata(tmp_path),
        lines=["PC: 0x400d1a2c"],
    )

    assert seen == ["PC: 0x400d1a2c"]
    assert result == {
        "decoded": [{"index": 0, "text": "Decoded 0x400d1a2c: loop()"}],
        "unavailable_reason": "",
    }
    # apply_to_core() left CORE pointed at the sidecar's build, which is how
    # the decoder resolves its ELF without a read_config.
    assert CORE.name == "demo"


def test_decode_backtrace_without_the_idedata_cache_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PlatformIO build with no cached idedata refuses before decoding.

    Decoding anyway is what lets ``get_idedata`` fall through to a full
    ``pio run -t idedata``.
    """
    storage_path, _build = _make_storage(tmp_path, "ESP32", "firmware.bin")
    _stub_platform_module(
        monkeypatch,
        SimpleNamespace(process_stacktrace=lambda config, line, state: state),
    )

    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "demo.yaml",
        storage_path=storage_path,
        idedata_path=tmp_path / "absent.json",
        lines=["PC: 0x400d1a2c"],
    )

    assert result == {"decoded": [], "unavailable_reason": "no_build"}


def test_decode_backtrace_platform_without_a_decoder_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery is an attribute lookup, same as esphome's own log clients."""
    storage_path, _build = _make_storage(tmp_path, "ESP32", "firmware.bin")
    _stub_platform_module(monkeypatch, SimpleNamespace())  # no process_stacktrace

    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "demo.yaml",
        storage_path=storage_path,
        idedata_path=_write_idedata(tmp_path),
        lines=["PC: 0x400d1a2c"],
    )

    assert result == {"decoded": [], "unavailable_reason": "unsupported_platform"}


def test_decode_backtrace_absent_platform_package_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No component package for the platform at all: nothing to decode with."""
    storage_path, _build = _make_storage(tmp_path, "ESP32", "firmware.bin")

    def _absent(name: str) -> object:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(helper_cli.importlib, "import_module", _absent)

    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "demo.yaml",
        storage_path=storage_path,
        idedata_path=_write_idedata(tmp_path),
        lines=["PC: 0x400d1a2c"],
    )

    assert result == {"decoded": [], "unavailable_reason": "unsupported_platform"}


def test_decode_backtrace_broken_esphome_install_is_not_called_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dependency missing inside the platform package reports ``decode_failed``."""
    storage_path, _build = _make_storage(tmp_path, "ESP32", "firmware.bin")

    def _broken(name: str) -> object:
        raise ModuleNotFoundError("No module named 'serial'", name="serial")

    monkeypatch.setattr(helper_cli.importlib, "import_module", _broken)

    with caplog.at_level(logging.WARNING):
        result = helper_cli._decode_backtrace(
            config_path=tmp_path / "demo.yaml",
            storage_path=storage_path,
            idedata_path=_write_idedata(tmp_path),
            lines=["PC: 0x400d1a2c"],
        )

    assert result == {"decoded": [], "unavailable_reason": "decode_failed"}
    assert "Importing the esp32 decoder failed" in caplog.text


@pytest.mark.parametrize(
    "platform",
    [
        pytest.param("esp32.evil", id="dotted_subpath"),
        pytest.param("..evil", id="relative_escape"),
        pytest.param("esp32/../../evil", id="path_traversal"),
        pytest.param("esp32;rm -rf", id="shell_metacharacters"),
        pytest.param("esp32\nevil", id="newline"),
        pytest.param("ESP32", id="uppercase"),
        pytest.param("esp32-evil", id="dash"),
        pytest.param("", id="empty"),
    ],
)
def test_load_decoder_never_imports_an_unvetted_platform_name(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """A platform name outside ``[a-z0-9_]+`` is rejected before any import."""

    def _no_import(name: str) -> object:
        raise AssertionError(f"must not import {name!r}")

    monkeypatch.setattr(helper_cli.importlib, "import_module", _no_import)

    assert helper_cli._load_decoder(platform) == (None, "unsupported_platform")


def test_decode_backtrace_rejects_a_platform_name_it_would_have_to_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end from a sidecar carrying a name shaped to escape the package."""
    storage_path, _build = _make_storage(tmp_path, "esp32.evil", "firmware.bin")

    def _no_import(name: str) -> object:
        raise AssertionError(f"must not import {name!r}")

    monkeypatch.setattr(helper_cli.importlib, "import_module", _no_import)

    result = helper_cli._decode_backtrace(
        config_path=tmp_path / "demo.yaml",
        storage_path=storage_path,
        idedata_path=_write_idedata(tmp_path),
        lines=["PC: 0x400d1a2c"],
    )

    assert result == {"decoded": [], "unavailable_reason": "unsupported_platform"}


def test_main_dispatches_decode_backtrace(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main`` parses argv and reads the decode-backtrace request off stdin."""
    request = {
        "config_path": str(tmp_path / "demo.yaml"),
        "storage_path": str(tmp_path / "absent.json"),
        "idedata_path": str(tmp_path / "absent-idedata.json"),
        "lines": ["PC: 0x400d1a2c"],
    }
    monkeypatch.setattr(sys, "argv", ["device-builder-helper", "decode-backtrace"])
    monkeypatch.setattr(
        helper_cli.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
    )

    assert helper_cli.main() == 0

    assert json.loads(capsys.readouterr().out) == {"decoded": [], "unavailable_reason": "no_build"}


def test_helper_decode_backtrace_round_trips_through_the_child(tmp_path: Path) -> None:
    """End to end through the real child: stdin request in, decoded JSON out.

    Also pins the idedata pin: with no ``platformio.ini`` here, an unpinned
    ``get_idedata`` judges the cache stale and shells out to ``pio run -t
    idedata``, landing in the latch as ``decode_failed``.
    """
    storage_path, build_dir = _make_storage(tmp_path, "ESP32", "firmware.bin")
    idedata_path = tmp_path / "idedata.json"
    idedata_path.write_text(
        json.dumps({"prog_path": str(build_dir / "firmware.elf"), "cc_path": "/nonexistent/gcc"})
    )
    request = json.dumps(
        {
            "config_path": str(tmp_path / "demo.yaml"),
            "storage_path": str(storage_path),
            "idedata_path": str(idedata_path),
            "lines": ["Backtrace: 0x400d1a2c:0x3ffb1f60 0x400d9150:0x3ffb1f80"],
        }
    )

    proc = subprocess.run(  # noqa: S603 — args fully test-controlled
        [*_helper_cmd(), "decode-backtrace"],
        input=request,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    payload = json.loads(proc.stdout)
    assert payload["unavailable_reason"] == ""
    assert [entry["text"] for entry in payload["decoded"]] == [
        "Found stack trace! Trying to decode it"
    ]


def test_download_path_does_not_import_esphome_components(tmp_path: Path) -> None:
    """Resolving downloads for esp32 + libretiny leaves the main process esphome-free.

    esp32 is answered from the precomputed index; libretiny goes through the
    helper child. Neither should land ``esphome.components.{esp32,libretiny}`` in
    the calling process's ``sys.modules`` (checked by the probe in a fresh
    interpreter, since this test process has esphome loaded already).
    """
    repo_root = Path(__file__).resolve().parents[1]
    probe = repo_root / "tests" / "_probe_download_no_components.py"
    # Put the repo root on the child's path so it imports this checkout's source
    # (a bare ``python file.py`` puts the script's dir on sys.path[0], not cwd).
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(repo_root), os.environ.get("PYTHONPATH", "")]),
    }
    result = subprocess.run(  # noqa: S603 — args fully test-controlled
        [sys.executable, str(probe), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert result.returncode == 0, f"leaked:\n{result.stdout}\nstderr:\n{result.stderr}"
