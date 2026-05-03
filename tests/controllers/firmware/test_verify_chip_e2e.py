"""End-to-end coverage for ``FirmwareController._verify_chip``.

Runner-level integration test. Drives the chip-id pre-flight by
submitting via the public ``firmware/install`` and
``firmware/upload`` handlers, then ticking the runner via
``_run_queue`` to pump the queue. The chip-id helper itself is
never called directly — observable side effects (job status,
``job.error`` message, JOB_FAILED / JOB_COMPLETED broadcasts,
and the recorded subprocess invocations) drive the assertions.
``_run_queue`` is the only underscore-prefixed method this file
touches; it's the runner entry point and exists to be driven
in tests this way.

The chip-id check spawns ``[sys.executable, '-m', 'esptool',
'--port', <port>, 'chip-id']`` via ``create_subprocess_exec``.
Tests substitute that helper module-level so each one's "esptool"
output can be controlled while the real subsequent build still
runs through the same wrapper (substitute returns a no-op
success-exit script for non-esptool calls).

Branches the runner depends on:

- Chip matches → no error, build proceeds, status COMPLETED.
- Chip mismatch (parametrised over ``install`` AND ``upload``)
  → ``ValueError`` with the chip-mismatch message inside
  ``_execute_job``'s generic ``except Exception``, status
  FAILED, JOB_FAILED carries the message. Both job types must
  trigger the check or a regression that drops one would let
  wrong-chip flashes through.
- esptool output without "Detecting chip type..." line → falls
  through (logged as warning), build proceeds.
- Non-serial port shapes (``OTA``, IPv4, hostname, Windows
  ``COMx``) → no esptool call (the helper only probes
  ``/dev/*`` paths).
- ``self._db.devices`` returns no matching device → skip the
  chip check, build still runs.
- Device matched but ``target_platform`` empty → skip the chip
  check, build still runs.

Without these the chip-id pre-flight (~54 lines, the longest
helper after ``_execute_job``) had zero direct or indirect
coverage.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware import controller as controller_module
from esphome_device_builder.models import EventType, JobStatus

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# ---------------------------------------------------------------------------
# Test scaffolding (mirrors test_execute_job_e2e — same runner pattern)
# ---------------------------------------------------------------------------


def _wire_real_queue(controller: FirmwareController) -> None:
    controller._queue = asyncio.Queue()

    async def _supersede(_configuration: str, *, exclude_job_id: str) -> None:
        return

    controller._supersede_active_jobs = _supersede  # type: ignore[assignment]
    controller._current_job = None
    controller._current_process = None
    controller._cancel_requested = set()


def _seed_yaml(tmp_path: Path, name: str = "kitchen.yaml") -> None:
    (tmp_path / name).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")


# A no-op script for the actual build subprocess. Exit 0 produces
# a clean COMPLETED job once chip-id has passed.
_BUILD_SCRIPT_OK = "import sys\nsys.exit(0)\n"

# The build subprocess for the install path: same as compile but
# the runner wires it in via ``_esphome_cmd`` so we use the same
# script. ``--no-logs`` is passed by ``_build_command`` so the
# script doesn't need to handle anything special.


@dataclass
class _StubDevice:
    """Narrow stand-in for ``Device`` — only the attributes ``_verify_chip`` reads.

    Mirrors the conftest's "minimal test doubles fail fast on
    unexpected attribute access" pattern. A regression that
    teaches ``_verify_chip`` to read a new field (e.g. ``board``
    or ``platform``) crashes with ``AttributeError`` here
    instead of silently conjuring a ``MagicMock`` value.
    """

    name: str
    target_platform: str


@dataclass
class _StubDevices:
    """Narrow ``DevicesController`` stand-in.

    Exposes only the surface ``_verify_chip`` and
    ``_build_cache_args`` actually read:

    - ``get_devices()`` — for the chip lookup by YAML name.
    - ``get_address_cache_args(configuration)`` — for the OTA
      address-cache CLI flags ``_build_cache_args`` adds to
      install/upload commands when ``port == "OTA"``.

    Returning ``[]`` for the address-cache args keeps the build
    command shape minimal (``--mdns-address-cache`` / ``--dns-
    address-cache`` skipped) — orthogonal to the chip-id branches
    these tests exercise.
    """

    devices: list[_StubDevice]

    def get_devices(self) -> list[_StubDevice]:
        return self.devices

    def get_address_cache_args(self, _configuration: str) -> list[str]:
        return []


def _wire_devices(
    controller: FirmwareController, *, name: str = "kitchen", target_platform: str = "esp32-c3"
) -> None:
    """Attach a fake ``DevicesController`` whose ``get_devices`` returns one entry."""
    controller._db.devices = _StubDevices(  # type: ignore[attr-defined]
        devices=[_StubDevice(name=name, target_platform=target_platform)]
    )


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chip_id_output: bytes,
    chip_id_exit_code: int = 0,
) -> dict[str, list]:
    """Replace ``create_subprocess_exec`` with a controllable wrapper.

    Calls whose first argv element is ``sys.executable`` followed
    by ``-m esptool`` go through a fake process that emits
    *chip_id_output* and exits with *chip_id_exit_code*.
    Everything else (the actual esphome build subprocess) is
    rerouted to a quick ``[sys.executable, '-c',
    _BUILD_SCRIPT_OK]`` invocation so the build doesn't hang or
    require a real esphome install.

    Returns a record dict so tests can assert what was spawned —
    in particular whether the esptool call fired at all.
    """
    record: dict[str, list] = {"esptool_calls": [], "build_calls": []}
    real = controller_module.create_subprocess_exec

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        # esptool spawn:
        # ``sys.executable -m esptool --port <port> chip-id``.
        if (
            len(args) >= 3
            and args[0] == sys.executable
            and args[1] == "-m"
            and args[2] == "esptool"
        ):
            record["esptool_calls"].append(args)
            return await real(
                sys.executable,
                "-c",
                # Emit literal bytes via ``sys.stdout.buffer.write``
                # so the runner sees exactly what each test wants
                # to drive (including missing "Detecting chip type"
                # lines or chip-name typos).
                "import sys\n"
                f"sys.stdout.buffer.write({chip_id_output!r})\n"
                f"sys.exit({chip_id_exit_code})\n",
                **kwargs,
            )
        # The build subprocess — first argv element is the
        # ``_esphome_cmd`` we set on the controller. Reroute to
        # the no-op build script.
        record["build_calls"].append(args)
        return await real(sys.executable, "-c", _BUILD_SCRIPT_OK, **kwargs)

    monkeypatch.setattr(controller_module, "create_subprocess_exec", _wrapper)
    return record


async def _run_until_terminal(
    controller: FirmwareController, *, timeout: float = 10.0
) -> dict[str, list]:
    captured: dict[str, list] = {
        "job_started": [],
        "job_output": [],
        "job_completed": [],
        "job_failed": [],
        "job_cancelled": [],
    }
    terminal = asyncio.Event()
    bus = controller._db.bus
    real_fire = bus.fire

    def _capture(event_type: EventType, data: dict) -> None:
        key = event_type.value
        if key in captured:
            captured[key].append(data)
        if key in ("job_completed", "job_failed", "job_cancelled"):
            terminal.set()
        real_fire(event_type, data)

    bus.fire = _capture
    runner = asyncio.create_task(controller._run_queue())
    try:
        await asyncio.wait_for(terminal.wait(), timeout=timeout)
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
    return captured


def _set_esphome_cmd(controller: FirmwareController) -> None:
    """Bare ``_esphome_cmd`` placeholder — the wrapper reroutes builds anyway.

    The wrapper's ``record["build_calls"]`` ignores the actual
    argv and always invokes the no-op script, so this only has
    to be a list with at least one element so ``_build_command``
    has something to splat.
    """
    controller._esphome_cmd = [sys.executable, "-c", "pass"]


# ---------------------------------------------------------------------------
# Chip MATCH / MISMATCH / no-detection branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_serial_chip_match_proceeds_to_completed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detected chip matches device's ``target_platform`` → build runs to COMPLETED.

    The happy path through the chip-id pre-flight: the runner
    sees a serial port, looks up the device, spawns esptool,
    parses ``Detecting chip type... ESP32-C3``, normalises both
    sides (``esp32-c3`` → ``esp32c3``), confirms equality, and
    returns without raising. ``_execute_job`` then proceeds to
    the actual build subprocess.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller, name="kitchen", target_platform="esp32-c3")
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(
        monkeypatch,
        chip_id_output=b"esptool.py v4.7.0\nDetecting chip type... ESP32-C3\n",
    )

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    captured = await _run_until_terminal(controller)

    assert len(record["esptool_calls"]) == 1, "expected exactly one esptool chip-id spawn"
    assert "/dev/ttyUSB0" in record["esptool_calls"][0]
    assert record["build_calls"], "build subprocess should have run after chip match"
    assert job.status == JobStatus.COMPLETED
    assert captured["job_completed"]
    assert captured["job_failed"] == []


@pytest.mark.parametrize("submit_command", ["install", "upload"])
@pytest.mark.asyncio
async def test_serial_chip_mismatch_marks_failed_with_message(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    submit_command: str,
) -> None:
    """Mismatch raises → status FAILED, error message names both sides.

    Device YAML says ``esp32-c3``, esptool detects ``ESP32-S3`` —
    a wrong-board misconfiguration that would otherwise let the
    user flash a build for a different chip and brick the device.
    The ``ValueError`` thrown by the chip check propagates into
    ``_execute_job``'s ``except Exception`` and surfaces as
    ``job.error`` + a JOB_FAILED broadcast.

    Parametrised over ``install`` AND ``upload`` because
    ``_execute_job`` triggers ``_verify_chip`` for both
    ``JobType.INSTALL`` and ``JobType.UPLOAD``. A regression
    that drops one of the two would let wrong-chip flashes
    through on that path.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller, name="kitchen", target_platform="esp32-c3")
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(
        monkeypatch,
        chip_id_output=b"esptool.py v4.7.0\nDetecting chip type... ESP32-S3\n",
    )

    handler = getattr(controller, submit_command)
    job = await handler(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    captured = await _run_until_terminal(controller)

    assert len(record["esptool_calls"]) == 1
    # Build did NOT run — chip check raised before the spawn.
    assert record["build_calls"] == []
    assert job.status == JobStatus.FAILED
    assert job.error is not None
    assert "esp32-c3" in job.error.lower()
    assert "esp32s3" in job.error.lower().replace("-", "")
    assert "wrong board" in job.error.lower()
    assert captured["job_failed"]
    assert captured["job_failed"][0]["job"] is job
    assert captured["job_completed"] == []


@pytest.mark.asyncio
async def test_install_serial_no_chip_detected_proceeds_to_completed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output without ``Detecting chip type...`` skips check, build still runs.

    esptool's output shape is "best-effort parse" — older
    versions, error states, or future format changes can produce
    output the runner can't extract a chip name from. Rather
    than fail the user's install on parse failure, log a warning
    and proceed (the user explicitly chose this serial port).

    Pin the contract: the runner mustn't surface a JOB_FAILED
    when esptool's output is unrecognised — that would make
    the chip-id check a regression source whenever esptool's
    upstream rev changes its output template.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller, name="kitchen", target_platform="esp32-c3")
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(
        monkeypatch,
        # esptool failure mode: connection error / no chip detected.
        chip_id_output=(
            b"A fatal error occurred: Failed to connect to ESP32: No serial data received.\n"
        ),
        chip_id_exit_code=2,
    )

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    captured = await _run_until_terminal(controller)

    assert len(record["esptool_calls"]) == 1
    # Build proceeded despite esptool's unhelpful output.
    assert record["build_calls"], "build should have run even when chip detection failed"
    assert job.status == JobStatus.COMPLETED
    assert captured["job_completed"]
    assert captured["job_failed"] == []


# ---------------------------------------------------------------------------
# Skip branches: OTA, missing device, missing target_platform
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port",
    [
        # OTA: the install default, no local serial port at all.
        "OTA",
        # Explicit IPv4 OTA target (re-flash by IP).
        "192.168.1.42",
        # ``.local`` mDNS hostname.
        "kitchen.local",
        # Windows COM port — uses a serial wire but ``_verify_chip``
        # only probes ``/dev/*`` paths so the COMx case takes the
        # same skip branch as the network ones above.
        "COM3",
    ],
    ids=["ota", "ipv4", "mdns_hostname", "windows_com"],
)
@pytest.mark.asyncio
async def test_install_non_dev_port_skips_chip_check(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    port: str,
) -> None:
    """Any port that isn't a ``/dev/*`` path skips the chip-id probe.

    ``_verify_chip`` only probes serial ports under ``/dev/`` —
    OTA / IP / hostname targets reach the device over the network
    (no local serial bus to read), and Windows ``COMx`` ports use
    a different prefix that the helper deliberately doesn't
    handle (the chip-id check is Linux/macOS-only at this point;
    Windows users are gated out by the prefix check).

    Spawning esptool against a non-serial path would block forever
    waiting for chip data that never arrives, hanging the user's
    install indefinitely. Pin the early-return so a regression
    that broadens the chip check to all ports surfaces on every
    parametrised case (a partial regression covering only one
    branch would still be caught).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller, name="kitchen", target_platform="esp32-c3")
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(
        monkeypatch,
        chip_id_output=b"should never be invoked",
    )

    job = await controller.install(configuration="kitchen.yaml", port=port)
    await _run_until_terminal(controller)

    assert record["esptool_calls"] == [], f"non-/dev/ port {port!r} must not invoke esptool"
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_install_serial_no_matching_device_skips_check(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No device in the scanner's list with this YAML's name → skip check.

    A serial install for a YAML the scanner hasn't seen yet
    (e.g. just-created file the periodic scan hasn't picked up)
    has no ``target_platform`` to compare against. Skipping is
    the safer default — the user explicitly chose the port and
    we'd rather let the build proceed than fail on a check we
    can't run.

    Pin both halves of the contract: esptool must NOT spawn
    (would block forever) AND the build subprocess must STILL
    run (otherwise the install silently no-ops, which would be
    indistinguishable from a successful run from the dashboard's
    POV).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    # Devices controller is present but has no matching device.
    controller._db.devices = _StubDevices(devices=[])  # type: ignore[attr-defined]
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(monkeypatch, chip_id_output=b"never invoked")

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    await _run_until_terminal(controller)

    # No device match → no platform → early return before esptool spawn.
    assert record["esptool_calls"] == []
    # The build subprocess MUST still run — a regression that
    # short-circuited the job entirely on missing-device would
    # also leave esptool_calls empty + status COMPLETED, but
    # silently no-op the install.
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_install_serial_device_without_target_platform_skips_check(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Device matched but ``target_platform`` empty → skip.

    ``Device.target_platform`` carries the chip family
    (``esp32-c3`` / ``esp8266`` / ...). It can come from a few
    sources — the StorageJSON sidecar after a successful compile,
    YAML detection on first scan, or restored monitor state —
    so empty is uncommon but possible (e.g. a YAML the parser
    couldn't extract a platform from). Without something to
    compare against the chip-id check has nothing to act on;
    skip and let the build proceed for the real reason.

    Pin both halves: esptool skipped AND build still runs (same
    contract as the no-matching-device case).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller, name="kitchen", target_platform="")
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)

    record = _patch_subprocess(monkeypatch, chip_id_output=b"never invoked")

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    await _run_until_terminal(controller)

    assert record["esptool_calls"] == []
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED
