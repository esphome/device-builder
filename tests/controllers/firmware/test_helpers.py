"""Tests for module-level helpers in ``controllers/firmware/helpers.py``.

The firmware controller has a few pure helpers at file scope
that aren't covered elsewhere:

* ``_trim_job_output`` — caps ``job.output`` and accumulates
  the elided count across repeated trims.
* ``_names_touched_by_job`` — feeds the rename-lock collision
  check; a rename touches two YAMLs (old + new), every other
  job type touches one.
* ``_verify_esphome_importable`` — startup probe, returns
  ``(True, version)`` on success and ``(False, reason)`` on
  exit-code failure / error-pattern detection / OSError /
  timeout.

The other module-level helpers are already covered by their
own dedicated test files:

* ``_validate_port`` → ``test_install_to_specific_address.py``
* ``_parse_progress`` → ``test_progress.py``
* ``_mark_job_terminal`` → ``test_mark_job_terminal.py``

Per Copilot's review, this PR doesn't re-cover those — keeping
expectations in one place avoids drift.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

import pytest

from esphome_device_builder.controllers.firmware import helpers as _helpers
from esphome_device_builder.controllers.firmware.constants import (
    _MAX_OUTPUT_LINES_RETAINED,
    _OUTPUT_TRIM_NOTICE_PREFIX,
)
from esphome_device_builder.controllers.firmware.helpers import (
    _find_esphome_cmd,
    _names_touched_by_job,
    _trim_job_output,
    _verify_esphome_importable,
)
from esphome_device_builder.models.firmware import (
    FirmwareJob,
    JobType,
)


def _make_job(**overrides: Any) -> FirmwareJob:
    """Minimal FirmwareJob — only the fields the helpers under test read."""
    defaults: dict[str, Any] = {
        "job_id": "j-1",
        "configuration": "kitchen.yaml",
        "job_type": JobType.COMPILE,
    }
    defaults.update(overrides)
    return FirmwareJob(**defaults)


# ---------------------------------------------------------------------------
# _trim_job_output
# ---------------------------------------------------------------------------


def test_trim_job_output_no_op_when_under_cap() -> None:
    """Below the cap → output untouched, no trim notice prepended."""
    job = _make_job(output=["line\n"] * 10)
    _trim_job_output(job)
    assert len(job.output) == 10
    assert not any(line.startswith(_OUTPUT_TRIM_NOTICE_PREFIX) for line in job.output)


def test_trim_job_output_caps_long_output() -> None:
    """Above the cap → output trimmed to the most recent N lines plus notice.

    Cap is the constant ``_MAX_OUTPUT_LINES_RETAINED`` so the
    test scales with the source. The trim notice goes in slot 0
    so the user sees "X lines elided" before the kept tail.
    """
    job = _make_job(output=[f"line {i}\n" for i in range(_MAX_OUTPUT_LINES_RETAINED + 50)])
    _trim_job_output(job)

    # Notice + cap == total length.
    assert len(job.output) == _MAX_OUTPUT_LINES_RETAINED + 1
    assert job.output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX)
    assert "50 earlier line(s) elided" in job.output[0]
    # Tail kept — last line is the most recent.
    assert job.output[-1] == f"line {_MAX_OUTPUT_LINES_RETAINED + 49}\n"


def test_trim_job_output_accumulates_elided_count_across_calls() -> None:
    """Repeated trims grow the elided count instead of resetting to 1.

    The trim notice carries the cumulative count so a long-
    running job that gets trimmed multiple times reports the
    true total ("1234 earlier lines elided") instead of falsely
    claiming it just dropped one batch.
    """
    job = _make_job(output=[f"line {i}\n" for i in range(_MAX_OUTPUT_LINES_RETAINED + 30)])
    _trim_job_output(job)
    first_count = int(re.search(r"(\d+) earlier", job.output[0]).group(1))  # type: ignore[union-attr]

    # Append more output and trim again.
    job.output.extend(f"line {i}\n" for i in range(50))
    _trim_job_output(job)
    second_count = int(re.search(r"(\d+) earlier", job.output[0]).group(1))  # type: ignore[union-attr]

    assert second_count > first_count
    # The new count should be first + new lines elided this round.
    assert second_count == first_count + 50


# ---------------------------------------------------------------------------
# _names_touched_by_job
# ---------------------------------------------------------------------------


def test_names_touched_by_compile_job_is_just_configuration() -> None:
    """Compile / upload / install / clean each touch one YAML.

    The rename-lock collision check uses this set to decide
    whether two queued jobs can run in parallel. A compile of
    ``kitchen.yaml`` only has ``kitchen.yaml`` in its working
    set.
    """
    job = _make_job(configuration="kitchen.yaml", job_type=JobType.COMPILE)
    assert _names_touched_by_job(job) == {"kitchen.yaml"}


def test_names_touched_by_rename_includes_old_and_new() -> None:
    """A rename collides on both the source and the target YAML.

    Without the second name, a queued compile of the *new* name
    could start before the rename's install lands and fight
    over the same StorageJSON sidecar.
    """
    job = _make_job(
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        new_name="kitchen-2",
    )
    assert _names_touched_by_job(job) == {"kitchen.yaml", "kitchen-2.yaml"}


def test_names_touched_by_rename_without_new_name_falls_back() -> None:
    """A rename job missing ``new_name`` only locks the source.

    Defensive: an enqueue that didn't fill ``new_name`` (test
    fixture, paranoid caller) shouldn't blow up the lock-check
    helper. Falling back to the source-only set means the
    collision detector still runs sensibly.
    """
    job = _make_job(configuration="kitchen.yaml", job_type=JobType.RENAME)
    assert _names_touched_by_job(job) == {"kitchen.yaml"}


def test_names_touched_by_job_with_empty_configuration_is_empty() -> None:
    """Reset-build-env-style jobs have no configuration → empty set.

    ``reset_build_env`` operates on the platformio cache, not a
    specific YAML. The empty set says "doesn't conflict with
    anything", which is the desired behaviour.
    """
    job = _make_job(configuration="", job_type=JobType.RESET_BUILD_ENV)
    assert _names_touched_by_job(job) == set()


# ---------------------------------------------------------------------------
# _verify_esphome_importable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_esphome_importable_success_with_known_module() -> None:
    """A trivial Python ``-c`` that prints its version returns ``(True, output)``.

    Exercises the spawn path against a known-importable command —
    we don't need the real ``esphome`` CLI for this; a one-liner
    that exits 0 with no error patterns is enough to lock the
    happy-path tuple shape.
    """
    cmd = [sys.executable, "-c", "print('1.2.3')"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert ok
    assert "1.2.3" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_no_module_named() -> None:
    """Even on a 0 exit, output containing ``No module named`` flips the result.

    Captures the case where a wrapper script exits 0 but its
    stderr/stdout still complains about a missing module — the
    historical class of failure that motivated this probe.
    """
    cmd = [sys.executable, "-c", "import sys; print(\"No module named 'esphome'\"); sys.exit(0)"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "No module named" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_nonzero_exit() -> None:
    """Non-zero exit → ``(False, output_or_exit_marker)``."""
    cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "exit 3" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_oserror() -> None:
    """A missing executable returns ``(False, "FileNotFoundError: ...")``.

    Pre-migration the sync version caught ``OSError`` directly;
    the async version uses the same except branch around
    ``create_subprocess_exec``.
    """
    cmd = ["/this/path/does/not/exist/no-such-binary"]
    ok, detail = await _verify_esphome_importable(cmd)
    assert not ok
    assert "FileNotFoundError" in detail or "OSError" in detail


@pytest.mark.asyncio
async def test_verify_esphome_importable_returns_false_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timed-out probe surfaces a clear message rather than blocking startup.

    Real-world trigger: a wrapper script that hangs on a network
    call before importing ``esphome``. The dashboard mustn't
    block indefinitely on a broken probe.

    Subprocess plumbing (kill + wait) lives in
    :func:`helpers.subprocess.run_subprocess_capture`; this test
    monkeypatches that helper to return ``timed_out=True``
    directly so the probe's mapping branch fires without an
    actual wall-clock wait.
    """
    from esphome_device_builder.helpers.subprocess import (  # noqa: PLC0415
        CapturedSubprocess,
    )

    async def _fake_timed_out(*_args: Any, **_kwargs: Any) -> CapturedSubprocess:
        return CapturedSubprocess(returncode=None, stdout=b"", timed_out=True)

    monkeypatch.setattr(_helpers, "run_subprocess_capture", _fake_timed_out)

    ok, detail = await _verify_esphome_importable(["whatever"])

    assert not ok
    assert detail == "TimeoutExpired: 15s probe didn't return"


# ---------------------------------------------------------------------------
# _find_esphome_cmd
# ---------------------------------------------------------------------------


def test_find_esphome_cmd_prefers_sibling_binary_when_present(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone ``esphome`` next to ``sys.executable`` wins.

    A sibling script in the same bin directory is slightly cheaper
    than ``python -m esphome`` (one fewer import-system warmup)
    and surfaces a friendlier traceback when something goes wrong
    inside esphome — the wrapper's ``sys.exit(main())`` shape
    raises with a clear top frame, vs. the ``runpy`` shim that
    ``-m`` adds to a stack trace.

    Pin the preference so a refactor that swaps the order would
    surface here.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    # Match the helper's own ``os.name == "nt"`` check exactly.
    # ``sys.platform`` and ``os.name`` can disagree on MSYS / Cygwin
    # so basing the fixture on ``os.name`` keeps the test in lockstep
    # with whichever branch the helper is about to take.
    sibling = bin_dir / ("esphome.exe" if os.name == "nt" else "esphome")
    sibling.write_text("#!/bin/sh\necho fake-esphome\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = _find_esphome_cmd()

    assert cmd == [str(sibling)]


def test_find_esphome_cmd_falls_back_to_python_dash_m(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sibling binary → ``[sys.executable, '-m', 'esphome']``.

    The fallback path is what runs in most production
    deployments — pip-installed esphome creates the sibling
    script in dev / venv installs but not in every package
    layout (e.g. some Docker images strip the script wrapper
    to keep the image small). ``python -m esphome`` always works
    when the package is importable, which is the same condition
    the dashboard's own startup already requires, so it's the
    safe default.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    # Deliberately don't create a sibling esphome script.

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = _find_esphome_cmd()

    assert cmd == [str(fake_python), "-m", "esphome"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX-extension branch")
def test_find_esphome_cmd_picks_bare_esphome_on_posix(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On POSIX (``os.name != 'nt'``) the helper picks ``esphome`` (no extension).

    Layered with the Windows-only twin below so the CI matrix
    (Linux + macOS + Windows) covers both branches end-to-end.
    Faking ``os.name`` mid-process doesn't work — pathlib
    instantiates ``PosixPath`` / ``WindowsPath`` based on the
    real ``os.name`` at import time, and switching makes
    pathlib raise ``NotImplementedError`` — so we let the
    host OS pick.

    Skip predicate matches the helper's branch exactly
    (``os.name == "nt"``) rather than ``sys.platform`` so MSYS /
    Cygwin (where the two can disagree) routes to the
    correct test.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "esphome").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = _find_esphome_cmd()

    assert cmd == [str(bin_dir / "esphome")]


@pytest.mark.skipif(os.name != "nt", reason="Windows-extension branch")
def test_find_esphome_cmd_picks_esphome_exe_on_windows(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows the helper picks ``esphome.exe``, not ``esphome``.

    Companion to the POSIX test above. Pinned because the
    wrong-extension lookup is silent in production: a regression
    that hard-coded ``esphome`` (or flipped the ternary) would
    just fall through to ``python -m esphome``. The fallback
    works, but loses the perf + traceback wins the sibling
    path provides.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python.exe"
    fake_python.write_text("MZ\n", encoding="utf-8")
    (bin_dir / "esphome.exe").write_text("MZ\n", encoding="utf-8")

    monkeypatch.setattr(sys, "executable", str(fake_python))
    cmd = _find_esphome_cmd()

    assert cmd == [str(bin_dir / "esphome.exe")]


def test_find_esphome_cmd_does_not_substitute_sibling_python(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling ``python`` next to a non-``python`` executable is irrelevant.

    Documents the deliberate non-feature: the helper anchors on
    ``sys.executable`` exactly and never tries to find "the
    python next door". A previous draft of this code looked for
    a sibling ``python`` interpreter and used *that* to run
    ``python -m esphome``; on Linux the running interpreter is
    sometimes ``/opt/python3.12/bin/python3.12`` while a system
    ``/usr/bin/python`` (no esphome) is on PATH — substituting
    silently produced "No module named esphome" at compile
    time. Anchoring on ``sys.executable`` only is the cure.

    Verify by pointing ``sys.executable`` at a non-``python``-named
    script with a sibling ``python``: the helper must use the
    weird name verbatim, not the sibling.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    weird = bin_dir / "python3.12"
    weird.write_text("#!/bin/sh\n", encoding="utf-8")
    (bin_dir / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    # No sibling esphome → expect the fallback.

    monkeypatch.setattr(sys, "executable", str(weird))
    cmd = _find_esphome_cmd()

    assert cmd == [str(weird), "-m", "esphome"]
    assert str(bin_dir / "python") not in cmd
