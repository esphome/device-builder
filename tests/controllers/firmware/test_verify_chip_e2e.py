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

The chip-id check spawns ``[*_find_esptool_cmd(), '--port', <port>,
'chip-id']`` via ``create_subprocess_exec``. The resolved command is
either a sibling ``esptool`` script next to ``sys.executable`` or
``[sys.executable, '-m', 'esptool']`` as a fallback (see
``_find_esptool_cmd`` in ``controllers.firmware.helpers``). Tests
substitute ``create_subprocess_exec`` module-level so each one's
"esptool" output can be controlled while the real subsequent build
still runs through the same wrapper (substitute returns a no-op
success-exit script for non-esptool calls).

The expected chip variant comes from a real StorageJSON sidecar
seeded via ``write_storage_json`` — ``_verify_chip`` reads
``StorageJSON.target_platform`` directly (the upstream-canonical
chip variant) rather than ``Device.target_platform`` (which now
carries the platform *key*, not the variant). The global
``_core_config_path_in_tmp`` autouse fixture in ``tests/conftest.py``
pins ``CORE.config_path`` onto ``tmp_path`` so production
``resolve_storage_path`` resolves to
``tmp_path/.esphome/storage/<configuration>.json`` — the same path
``write_storage_json`` writes to. No per-module redirect required.

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
- StorageJSON missing → skip the chip check, build still runs
  (pre-compile install has no compile-time truth to verify
  against; esphome's own flash error catches a wrong-chip case).
- StorageJSON with empty ``target_platform`` → skip the chip
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
from esphome_device_builder.controllers.firmware import runner as runner_module
from esphome_device_builder.models import EventType, JobStatus
from tests._storage_fixtures import write_storage_json

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# ---------------------------------------------------------------------------
# Test scaffolding (mirrors test_execute_job_e2e — same runner pattern)
# ---------------------------------------------------------------------------


def _wire_real_queue(controller: FirmwareController) -> None:
    controller.state.queue = asyncio.Queue()

    async def _supersede(_configuration: str, *, exclude_job_id: str) -> None:
        return

    controller._supersede_active_jobs = _supersede  # type: ignore[assignment]
    controller.state.current_job = None
    controller.state.current_process = None
    controller.state.cancel_requested = set()
    controller.state.cancel_events = {}


def _seed_yaml(tmp_path: Path, name: str = "kitchen.yaml") -> None:
    (tmp_path / name).write_text("esphome:\n  name: kitchen\n", encoding="utf-8")


def _seed_storage(
    tmp_path: Path,
    *,
    configuration: str = "kitchen.yaml",
    target_platform: str = "ESP32C3",
) -> Path:
    """Write a StorageJSON sidecar so ``_verify_chip`` can read the chip variant.

    Defaults to ``ESP32C3`` to match upstream's wire format —
    ``StorageJSON.from_esphome_core`` resolves ESP32 variants to
    their uppercase short name (no hyphen). Pass another value
    (``ESP32S3``, ``ESP8266``, …) to drive the mismatch and
    no-detection branches.
    """
    return write_storage_json(
        tmp_path,
        configuration,
        overrides={"esp_platform": target_platform, "target_platform": target_platform},
    )


# A no-op script for the actual build subprocess. Exit 0 produces
# a clean COMPLETED job once chip-id has passed.
_BUILD_SCRIPT_OK = "import sys\nsys.exit(0)\n"

# The build subprocess for the install path: same as compile but
# the runner wires it in via ``_esphome_cmd`` so we use the same
# script. ``--no-logs`` is passed by ``_build_command`` so the
# script doesn't need to handle anything special.


@dataclass
class _StubDevices:
    """Narrow ``DevicesController`` stand-in.

    ``_verify_chip`` no longer reads from the devices controller —
    chip variant comes from ``StorageJSON`` directly — but the
    runner's ``_build_cache_args`` still calls
    ``get_address_cache_args`` / ``get_ota_address_cache_args`` on
    the install/upload/rename paths. Returning ``[]`` for both
    keeps the build command shape minimal — orthogonal to the
    chip-id branches these tests exercise.
    """

    def get_address_cache_args(self, _configuration: str) -> list[str]:
        return []

    def get_ota_address_cache_args(self, _configuration: str, _port: str) -> list[str]:
        return []


def _wire_devices(controller: FirmwareController) -> None:
    """Attach a no-op ``DevicesController`` stub for ``_build_cache_args``."""
    controller._db.devices = _StubDevices()  # type: ignore[attr-defined]


def _is_esptool_spawn(args: tuple[Any, ...]) -> bool:
    """Match either esptool-spawn argv shape ``_find_esptool_cmd`` produces.

    Sibling script (``<bin>/esptool``) on dev venvs;
    ``[sys.executable, '-m', 'esptool']`` fallback on slim images.
    """
    if not args:
        return False
    if Path(str(args[0])).name in ("esptool", "esptool.exe"):
        return True
    return len(args) >= 3 and args[0] == sys.executable and args[1] == "-m" and args[2] == "esptool"


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
    real = runner_module.create_subprocess_exec

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        if _is_esptool_spawn(args):
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

    monkeypatch.setattr(runner_module, "create_subprocess_exec", _wrapper)
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
    controller.state.esphome_cmd = [sys.executable, "-c", "pass"]


# ---------------------------------------------------------------------------
# Chip MATCH / MISMATCH / no-detection branches
# ---------------------------------------------------------------------------


async def test_install_serial_chip_match_proceeds_to_completed(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detected chip matches StorageJSON's chip variant → build runs to COMPLETED.

    The happy path through the chip-id pre-flight: the runner
    sees a serial port, loads StorageJSON, spawns esptool, parses
    ``Detecting chip type... ESP32-C3``, normalises both sides
    (``esp32c3`` matches ``esp32c3``), confirms equality, and
    returns without raising. ``_execute_job`` then proceeds to
    the actual build subprocess.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

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
async def test_serial_chip_mismatch_marks_failed_with_message(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    submit_command: str,
) -> None:
    """Mismatch raises → status FAILED, error message names both sides.

    StorageJSON records ``ESP32C3``, esptool detects ``ESP32-S3`` —
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
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

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
    assert "esp32c3" in job.error.lower().replace("-", "")
    assert "esp32s3" in job.error.lower().replace("-", "")
    assert "wrong board" in job.error.lower()
    assert captured["job_failed"]
    assert captured["job_failed"][0]["job"] is job
    assert captured["job_completed"] == []


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
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

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
# Skip branches: non-serial port, missing StorageJSON, empty target_platform
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
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

    record = _patch_subprocess(
        monkeypatch,
        chip_id_output=b"should never be invoked",
    )

    job = await controller.install(configuration="kitchen.yaml", port=port)
    await _run_until_terminal(controller)

    assert record["esptool_calls"] == [], f"non-/dev/ port {port!r} must not invoke esptool"
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED


async def test_install_serial_no_storage_skips_check(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No StorageJSON sidecar for this YAML → skip check.

    A serial install for a YAML that's never been compiled (or
    whose ``.esphome/storage/`` cache was wiped) has no
    compile-time ground truth to compare against. Skipping is
    the safer default — the user explicitly chose the port and
    esphome's own flash error covers the wrong-chip case below
    us. Failing the install here would block first-time flashes
    for any new YAML.

    Pin both halves of the contract: esptool must NOT spawn
    (would block forever) AND the build subprocess must STILL
    run (otherwise the install silently no-ops, which would be
    indistinguishable from a successful run from the dashboard's
    POV).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    # No ``_seed_storage`` — sidecar absent.

    record = _patch_subprocess(monkeypatch, chip_id_output=b"never invoked")

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    await _run_until_terminal(controller)

    # No StorageJSON → no platform → early return before esptool spawn.
    assert record["esptool_calls"] == []
    # The build subprocess MUST still run — a regression that
    # short-circuited the job entirely on missing-storage would
    # also leave esptool_calls empty + status COMPLETED, but
    # silently no-op the install.
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED


async def test_install_serial_storage_without_target_platform_skips_check(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """StorageJSON present but ``target_platform`` empty → skip.

    ``StorageJSON.target_platform`` is normally populated by
    ``from_esphome_core`` after a successful compile, but a
    partially-written or hand-edited sidecar can carry an empty
    string. Without a chip variant there's nothing to compare
    against; skip and let the build proceed for the real reason.

    Pin both halves: esptool skipped AND build still runs (same
    contract as the no-storage case).
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="")

    record = _patch_subprocess(monkeypatch, chip_id_output=b"never invoked")

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    await _run_until_terminal(controller)

    assert record["esptool_calls"] == []
    assert record["build_calls"], "build subprocess should still have run"
    assert job.status == JobStatus.COMPLETED


# ---------------------------------------------------------------------------
# Early-cancel race: cancel arrives while ``_verify_chip`` is running
# ---------------------------------------------------------------------------


async def test_cancel_during_hanging_verify_chip_terminates_subprocess(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel during a hanging verify-chip terminates the spawn quickly.

    The user-visible regression from issue #136: pick the wrong
    serial port, esptool hangs talking to a non-ESP device for
    ~30s, user clicks Stop, **nothing happens** — the cancel
    flag was set but ``_current_process`` was ``None`` (the main
    install hadn't spawned yet) so ``_terminate_current_process``
    no-op'd. The verify subprocess kept running until esptool
    gave up on its own.

    Drive that path: stub ``create_subprocess_exec`` so the
    "esptool" call sleeps ~30s. Submit the install, wait for the
    runner to enter ``_execute_job``, fire the public ``cancel``
    handler, and assert the job reaches CANCELLED in well under
    the sleep duration. The only way that's possible is if the
    SIGTERM actually landed on the verify subprocess — which
    requires the spawn to have been registered as
    ``_current_process``.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

    real = runner_module.create_subprocess_exec
    verify_spawned = asyncio.Event()

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        if _is_esptool_spawn(args):
            verify_spawned.set()
            # Sleep long enough that any non-cancelled run would
            # blow the test timeout — the assertion that the test
            # finishes in seconds is the proof that SIGTERM landed.
            return await real(
                sys.executable,
                "-c",
                "import time\ntime.sleep(30)\n",
                **kwargs,
            )
        # Build subprocess must not run — verify raises before it.
        msg = "build subprocess spawned despite mid-verify cancel"
        raise AssertionError(msg)

    monkeypatch.setattr(runner_module, "create_subprocess_exec", _wrapper)

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")

    # Run the queue and, in parallel, fire the cancel as soon as
    # the verify subprocess has spawned. ``_run_until_terminal``
    # finishes when JOB_CANCELLED lands.
    async def _cancel_when_verify_starts() -> None:
        await verify_spawned.wait()
        # Wait until the runner has assigned the verify subprocess
        # to ``_current_process``. The wrapper's ``verify_spawned``
        # fires INSIDE the ``await create_subprocess_exec`` call —
        # ``_verify_chip`` hasn't received the proc back yet, so
        # firing the cancel right here would hit the very race we're
        # trying to guard against (``_current_process`` still None,
        # ``_terminate_current_process`` no-ops). The poll proves the
        # registration happens BEFORE the runner waits on the proc,
        # which is the contract that makes mid-verify cancel work.
        while controller.state.current_process is None:
            await asyncio.sleep(0.01)
        await controller.cancel(job_id=job.job_id)

    canceller = asyncio.create_task(_cancel_when_verify_starts())
    try:
        captured = await _run_until_terminal(controller, timeout=5.0)
    finally:
        canceller.cancel()
        with suppress(asyncio.CancelledError):
            await canceller

    assert job.status == JobStatus.CANCELLED
    assert captured["job_cancelled"]
    assert captured["job_failed"] == []


async def test_cancel_during_verify_chip_marks_job_cancelled(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel during chip verify ends the job as CANCELLED, not FAILED.

    Repros issue #136: the user picks a serial port, the runner
    enters ``_verify_chip`` and spawns esptool against it, the
    user clicks Stop, the WS handler sets ``_cancel_requested``
    and terminates ``_current_process`` (the esptool spawn
    courtesy of the registration covered by the previous test).
    Once esptool is gone, ``_verify_chip`` raises ValueError to
    short-circuit the main install spawn, and ``_execute_job``'s
    generic ``except Exception`` honours the cancel flag and
    marks the job CANCELLED + fires JOB_CANCELLED — instead of
    surfacing the synthetic ValueError as a generic FAILED.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    _seed_storage(tmp_path, target_platform="ESP32C3")

    real = runner_module.create_subprocess_exec
    cancel_armed = False

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal cancel_armed
        if _is_esptool_spawn(args):
            # Simulate the in-flight cancel: queue the flag set
            # before the verify subprocess returns. The fake exits
            # quickly (no real hang) so the runner reaches the post-
            # wait cancel check inside ``_verify_chip`` and raises.
            if controller.state.current_job is not None:
                controller.state.cancel_requested.add(controller.state.current_job.job_id)
                cancel_armed = True
            return await real(
                sys.executable,
                "-c",
                'import sys\nsys.stdout.buffer.write(b"Detecting chip type... ESP32-C3\\n")\n',
                **kwargs,
            )
        # The build subprocess MUST NOT run — the cancel-during-verify
        # path raises before the spawn site. Fail loudly if it does.
        msg = "build subprocess spawned despite mid-verify cancel"
        raise AssertionError(msg)

    monkeypatch.setattr(runner_module, "create_subprocess_exec", _wrapper)

    job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    captured = await _run_until_terminal(controller)

    assert cancel_armed, "test bug: cancel flag was never set"
    assert job.status == JobStatus.CANCELLED
    assert captured["job_cancelled"]
    assert captured["job_cancelled"][0]["job"] is job
    assert captured["job_failed"] == []
    # The cancel flag is consumed by the except-branch finalisation —
    # not strictly required (no other path reads it after) but pin it
    # so a future refactor that forgets the discard surfaces here.
    assert job.job_id not in controller.state.cancel_requested


# ---------------------------------------------------------------------------
# Hardening: the ``_tracked_subprocess`` helper itself
# ---------------------------------------------------------------------------


async def test_tracked_subprocess_registers_and_clears_current_process(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``_tracked_subprocess`` parks the spawned process on the controller.

    This is the helper that future pre-flight checks
    (``_verify_chip``-style) MUST go through to keep
    ``firmware/cancel`` working — a fresh probe that calls
    ``create_subprocess_exec`` directly would silently regress
    the issue-#136 fix because the cancel handler walks
    ``_current_process`` and no-ops on ``None``.

    Pin the contract:

    1. Inside the ``async with`` block, ``_current_process`` IS
       the spawned proc (so SIGTERM via ``cancel`` lands on it).
    2. After the block exits cleanly, the field returns to its
       prior value (``None`` here, but the helper restores
       whatever was there to compose safely with future nested
       use).
    """
    # ``with_terminate=True`` initialises ``_current_process = None``
    # and ``_cancel_requested = set()`` so the helper has a clean
    # slate to assign onto. The mocked ``_terminate_current_process``
    # is unused by this test (we never trip the CancelledError or
    # post-spawn cancel paths) but is harmless.
    controller = firmware_controller_factory(with_settings=False, with_terminate=True)
    assert controller.state.current_process is None

    async with controller._tracked_subprocess(
        sys.executable,
        "-c",
        "import sys\nsys.exit(0)\n",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    ) as proc:
        assert controller.state.current_process is proc
        await proc.wait()

    # Restored on exit.
    assert controller.state.current_process is None


async def test_tracked_subprocess_restores_prior_value_on_exit(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``_tracked_subprocess`` restores the prior ``_current_process``.

    The helper saves whatever was registered before it spawned
    and restores it on exit, so a future caller that uses the
    helper inside an outer one (or just after another spawn site
    that's already populated the field) doesn't accidentally
    null out the active process reference. ``None`` is the
    common case but the contract is "restore the prior value".
    """
    # ``with_terminate=True`` initialises ``_current_process = None``
    # and ``_cancel_requested = set()`` so the helper has a clean
    # slate to assign onto. The mocked ``_terminate_current_process``
    # is unused by this test (we never trip the CancelledError or
    # post-spawn cancel paths) but is harmless.
    controller = firmware_controller_factory(with_settings=False, with_terminate=True)
    sentinel = object()
    controller.state.current_process = sentinel  # type: ignore[assignment]

    async with controller._tracked_subprocess(
        sys.executable,
        "-c",
        "import sys\nsys.exit(0)\n",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    ) as proc:
        assert controller.state.current_process is proc  # registered for the duration
        await proc.wait()

    assert controller.state.current_process is sentinel  # restored


async def test_tracked_subprocess_restores_prior_value_on_exception(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The restore happens even when the body raises.

    Without the ``try/finally`` shape inside the helper, an
    exception thrown inside the ``async with`` body would leave
    the controller pointing at a defunct process — the next
    ``firmware/cancel`` would either no-op (if the field went
    back to ``None``) or signal the wrong process (if it stayed
    pointing at the dead one). Pin both halves.
    """
    # ``with_terminate=True`` initialises ``_current_process = None``
    # and ``_cancel_requested = set()`` so the helper has a clean
    # slate to assign onto. The mocked ``_terminate_current_process``
    # is unused by this test (we never trip the CancelledError or
    # post-spawn cancel paths) but is harmless.
    controller = firmware_controller_factory(with_settings=False, with_terminate=True)
    assert controller.state.current_process is None

    with pytest.raises(RuntimeError, match="boom"):
        async with controller._tracked_subprocess(
            sys.executable,
            "-c",
            "import sys\nsys.exit(0)\n",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        ) as proc:
            # Reap the subprocess before raising so the transport
            # tears down inside the event loop's lifetime; without
            # this, a later GC pass surfaces an unraisable
            # ``BaseSubprocessTransport.__del__`` warning because
            # the transport's deferred ``connection_lost`` call hits
            # an already-closed loop.
            await proc.wait()
            msg = "boom"
            raise RuntimeError(msg)

    assert controller.state.current_process is None


async def test_cancel_in_gap_between_verify_and_main_spawn_terminates(
    firmware_controller_factory: FirmwareControllerFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landed during the verify→main-spawn gap → terminate fires.

    The runner's tracked-subprocess block clears
    ``_current_process`` on ``_verify_chip`` exit, then assigns the
    main install subprocess to ``_current_process`` a moment later.
    A ``firmware/cancel`` that arrived in that gap sets
    ``_cancel_requested`` but ``_terminate_current_process`` walked
    a ``None`` field and no-op'd. Without the post-spawn flag check
    inside ``_execute_job`` (the ``if job.job_id in
    self.state.cancel_requested: await self._terminate_current_process()``
    branch right after the main spawn), the install would run to
    completion before the post-``proc.wait()`` cancel handler saw
    the flag — the issue-#136 symptom for the "cancel arrived
    after verify but before main-spawn returned" sub-case.

    Drive that path: pre-load the cancel flag from inside the
    ``create_subprocess_exec`` substitute so by the time the
    runner re-enters ``_execute_job`` and assigns
    ``_current_process``, the flag is set. The immediate post-
    spawn check should fire ``_terminate_current_process`` on the
    (test fake) build subprocess. Spy on the terminate call to
    pin both halves of the contract:

    1. ``_terminate_current_process`` runs at the gap-check
       site (counter increments).
    2. It runs against a non-``None`` ``_current_process`` —
       i.e. the post-spawn assignment happened first, so the
       SIGTERM has somewhere to land.
    """
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    _set_esphome_cmd(controller)
    _seed_yaml(tmp_path)
    # Port is OTA so ``_verify_chip`` returns before reading storage,
    # but seed it anyway to keep the test independent of skip-branch
    # ordering — the post-spawn cancel check is what's under test.
    _seed_storage(tmp_path, target_platform="ESP32C3")

    real = runner_module.create_subprocess_exec
    terminate_calls: list[asyncio.subprocess.Process | None] = []
    real_terminate = controller._terminate_current_process

    async def _spy_terminate() -> None:
        terminate_calls.append(controller.state.current_process)
        await real_terminate()

    monkeypatch.setattr(controller, "_terminate_current_process", _spy_terminate)

    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        # OTA port → ``_verify_chip`` returns before any spawn,
        # so the only ``create_subprocess_exec`` call here is the
        # build subprocess. Pre-load the cancel flag right before
        # returning the proc — this is the "cancel arrived in
        # the verify→main-spawn gap" scenario the post-spawn
        # check guards.
        if controller.state.current_job is not None:
            controller.state.cancel_requested.add(controller.state.current_job.job_id)
        return await real(sys.executable, "-c", _BUILD_SCRIPT_OK, **kwargs)

    monkeypatch.setattr(runner_module, "create_subprocess_exec", _wrapper)

    job = await controller.install(configuration="kitchen.yaml", port="OTA")
    captured = await _run_until_terminal(controller)

    # The post-spawn flag check fired terminate exactly once,
    # against the just-assigned build subprocess (not ``None``).
    assert len(terminate_calls) == 1, "post-spawn cancel check should fire terminate once"
    assert terminate_calls[0] is not None, (
        "_current_process must be set when terminate fires — that's the whole point of "
        "the post-spawn check (vs. the no-op None path)"
    )
    # Job finalises as CANCELLED via the post-``proc.wait()`` cancel
    # handler, not FAILED.
    assert job.status == JobStatus.CANCELLED
    assert captured["job_cancelled"]
    assert captured["job_failed"] == []
