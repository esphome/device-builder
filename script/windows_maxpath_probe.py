r"""
Windows MAX_PATH fix-layout validation probe (run on a windows-latest CI runner).

Validates the proposed desktop-Windows path-shortening layout for issue #1190 end to end:

* the build tree lives under a short directory *junction* ``C:\esphb`` (junctions survive
  PlatformIO/CMake path handling for the build dir);
* the PlatformIO core / framework (``.platformio``) lives at a *real* short dir
  ``C:\esphb-pio`` -- it must be real, not a junction, because ESP-IDF's ``idf.cmake``
  REALPATHs ``IDF_PATH`` and resolves a junction back to its long target.

Both facts were established by run 26959353731 (build-root junction SURVIVES; ``.platformio``
junction CANONICALIZED, and the canonicalized long include paths blew past the Windows
command-line limit). This run compiles an ESP-IDF config with ``api: encryption:`` (the deep
mbedtls / tf-psa-crypto tree that overflows MAX_PATH) under the short-root layout and reports
the compile exit code plus the deepest on-disk path length. Success = the layout builds where
the long default would fail.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

# Short, *recognizable* roots (``esphb`` = ESPHome builder) so a user inspecting their disk
# knows what they are. Build tree via a junction (clean uninstall); PlatformIO core as a real
# dir (a junction is canonicalized away by ESP-IDF).
JUNC_DATA = Path(r"C:\esphb")
PIO_REAL = Path(r"C:\esphb-pio")

_DEVICE_NAME = "maxpath-probe-esp32-idf"

_PROBE_YAML = textwrap.dedent(
    f"""\
    esphome:
      name: {_DEVICE_NAME}
    esp32:
      board: esp32dev
      framework:
        type: esp-idf
    logger:
    wifi:
      ssid: "probe-ssid"
      password: "probe-password"
    api:
      encryption:
        key: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    """
)


def main() -> int:
    """Set up the short-root layout, run one ESP-IDF compile, report exit code + depth."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    # Junction target length is irrelevant: the junction is what the compiler sees.
    real_data = workspace / "maxpath_probe" / "data"
    real_data.mkdir(parents=True, exist_ok=True)
    PIO_REAL.mkdir(parents=True, exist_ok=True)
    _make_junction(JUNC_DATA, real_data)

    config = workspace / "maxpath_probe" / "probe.yaml"
    config.write_text(_PROBE_YAML, encoding="utf-8")

    env = {
        **os.environ,
        "ESPHOME_DATA_DIR": str(JUNC_DATA),
        "PLATFORMIO_CORE_DIR": str(PIO_REAL),
    }
    print(f"[probe] ESPHOME_DATA_DIR={JUNC_DATA} (junction) -> {real_data}", flush=True)
    print(f"[probe] PLATFORMIO_CORE_DIR={PIO_REAL} (real short dir)", flush=True)
    print("[probe] running: esphome compile probe.yaml", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", str(config)],
        env=env,
        check=False,
    )
    print(f"[probe] esphome compile exit code: {proc.returncode}", flush=True)

    _emit(proc.returncode)
    # Always exit 0: the verdict in the logs / step summary is the deliverable.
    return 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _make_junction(link: Path, target: Path) -> None:
    """Create a directory junction ``link`` -> ``target`` (no admin needed; idempotent)."""
    if link.exists():
        print(f"[probe] junction already exists: {link}", flush=True)
        return
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    print(
        f"[probe] mklink /J {link} {target}: rc={result.returncode} "
        f"{result.stdout.strip()}{result.stderr.strip()}",
        flush=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to create junction {link} -> {target}: {result.stderr}")


def _deepest(root: Path) -> int:
    """Return the longest full file-path string length under ``root`` (0 if empty)."""
    longest = 0
    for current, _dirs, files in os.walk(root):
        longest = max((longest, *(len(current) + 1 + len(name) for name in files)))
    return longest


def _emit(compile_rc: int) -> None:
    """Print the verdict and append a markdown block to ``$GITHUB_STEP_SUMMARY``."""
    outcome = "COMPILED (short-root layout builds)" if compile_rc == 0 else "FAILED"
    lines = [
        "# Windows MAX_PATH fix-layout validation",
        "",
        f"- **esphome compile: {outcome}** (exit code `{compile_rc}`)",
        f"- build tree: `{JUNC_DATA}` (junction)",
        f"- PlatformIO core: `{PIO_REAL}` (real short dir)",
        f"- deepest path under build tree: `{_deepest(JUNC_DATA)}` chars",
        f"- deepest path under PlatformIO core: `{_deepest(PIO_REAL)}` chars",
        "- threshold: Windows `MAX_PATH` = 260",
    ]
    block = "\n".join(lines)
    print("\n" + block, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(block + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
