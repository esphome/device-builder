r"""
Windows MAX_PATH junction-canonicalization probe (run on a windows-latest CI runner).

Answers two gates that decide the desktop Windows path-shortening design:

* **Build-root gate**: point ``ESPHOME_DATA_DIR`` at a short directory *junction*
  (``C:\\esphb``) into a deliberately long real dir, compile an ESP-IDF config, and check
  whether the generated build files reference object paths via the short junction string
  (junction survives → clean-uninstall short root is viable) or the canonical long target
  (PlatformIO/CMake canonicalized the build dir → junction useless for the build root).
* **``.platformio`` gate**: same run, ``PLATFORMIO_CORE_DIR`` junction (``C:\\esphb-pio``).
  ESP-IDF's ``idf.cmake`` REALPATHs ``IDF_PATH``, so the mbedtls source paths mirrored into
  ``CMakeFiles/<target>.dir/`` are expected to use the canonical long path (junction
  defeated). This run confirms or refutes that empirically.

The decisive signal is the path *string* recorded in ``compile_commands.json`` /
``build.ninja`` / ``CMakeCache.txt``, not compile pass/fail (whose threshold is finicky).
The probe never raises on a failed compile; it inspects whatever cmake emitted and prints a
verdict (and writes one to ``$GITHUB_STEP_SUMMARY``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

# Short, *recognizable* junction roots: what the desktop app would plausibly use so a user
# inspecting their disk knows what it is (``esphb`` = ESPHome builder).
JUNC_DATA = Path(r"C:\esphb")
JUNC_PIO = Path(r"C:\esphb-pio")

# ~70-char padding segment so the *real* (canonical) targets are long: if cmake canonicalizes
# the junction away, the deepest object path blows past 260; via the junction it stays short.
_PAD = "padding-" * 9  # 72 chars
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
    api:
      encryption:
        key: "bm90aGluZ3Rvc2VlaGVyZW5vdGhpbmd0b3NlZQ=="
    """
)


def main() -> int:
    """Set up junctions, run one ESP-IDF compile, classify the recorded paths, report."""
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
    real_root = workspace / "maxpath_probe" / _PAD
    real_data = real_root / "data"
    real_pio = real_root / "pio"
    real_data.mkdir(parents=True, exist_ok=True)
    real_pio.mkdir(parents=True, exist_ok=True)

    _make_junction(JUNC_DATA, real_data)
    _make_junction(JUNC_PIO, real_pio)

    config = workspace / "maxpath_probe" / "probe.yaml"
    config.write_text(_PROBE_YAML, encoding="utf-8")

    env = {
        **os.environ,
        "ESPHOME_DATA_DIR": str(JUNC_DATA),
        "PLATFORMIO_CORE_DIR": str(JUNC_PIO),
    }
    print(f"[probe] ESPHOME_DATA_DIR={JUNC_DATA} -> {real_data}", flush=True)
    print(f"[probe] PLATFORMIO_CORE_DIR={JUNC_PIO} -> {real_pio}", flush=True)
    print("[probe] running: esphome compile probe.yaml", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "esphome", "compile", str(config)],
        env=env,
        check=False,
    )
    print(f"[probe] esphome compile exit code: {proc.returncode}", flush=True)

    report = _classify(real_data, real_pio)
    _emit(report, proc.returncode)
    # Always exit 0: the verdict in the logs / step summary is the deliverable, not pass/fail.
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


def _classify(real_data: Path, real_pio: Path) -> dict[str, object]:
    """Walk the emitted build files + tree and tally junction-prefix vs canonical-prefix use."""
    build_files = (
        list(JUNC_DATA.rglob("compile_commands.json"))
        + list(JUNC_DATA.rglob("build.ninja"))
        + list(JUNC_DATA.rglob("CMakeCache.txt"))
    )
    blob = ""
    for path in build_files:
        try:
            blob += path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    data_junction = _count(blob, JUNC_DATA)
    data_canonical = _count(blob, real_data)
    pio_junction = _count(blob, JUNC_PIO)
    pio_canonical = _count(blob, real_pio)

    deepest_junction, deepest_canonical = _deepest_path(real_data)

    return {
        "build_files_found": [str(p) for p in build_files],
        "data_junction_hits": data_junction,
        "data_canonical_hits": data_canonical,
        "pio_junction_hits": pio_junction,
        "pio_canonical_hits": pio_canonical,
        "build_root_junction_survives": _verdict(data_junction, data_canonical),
        "platformio_junction_survives": _verdict(pio_junction, pio_canonical),
        "deepest_path_via_junction": deepest_junction,
        "deepest_path_via_canonical": deepest_canonical,
    }


def _count(blob: str, prefix: Path) -> int:
    """Count case-insensitive occurrences of ``prefix`` in both slash flavors."""
    needle = str(prefix).lower()
    haystack = blob.lower()
    return haystack.count(needle) + haystack.count(needle.replace("\\", "/"))


def _verdict(junction_hits: int, canonical_hits: int) -> str:
    """Map hit counts to survives / canonicalized / inconclusive."""
    if junction_hits == 0 and canonical_hits == 0:
        return "inconclusive (no path hits)"
    if junction_hits > canonical_hits:
        return "SURVIVES (short junction path used)"
    if canonical_hits > junction_hits:
        return "CANONICALIZED (long real path used)"
    return "mixed"


def _deepest_path(real_data: Path) -> tuple[int, int]:
    r"""Return (deepest length via the C:\esphb junction, deepest length via the real path)."""
    longest = 0
    for root, _dirs, files in os.walk(JUNC_DATA):
        # Measure the joined string length without os.path.join (separator + name).
        longest = max((longest, *(len(root) + 1 + len(name) for name in files)))
    if longest == 0:
        return 0, 0
    canonical = longest - len(str(JUNC_DATA)) + len(str(real_data))
    return longest, canonical


def _emit(report: dict[str, object], compile_rc: int) -> None:
    """Print the verdict and append a markdown block to ``$GITHUB_STEP_SUMMARY``."""
    lines = [
        "# Windows MAX_PATH junction probe",
        "",
        f"- esphome compile exit code: `{compile_rc}`",
        f"- build files inspected: {len(report['build_files_found'])}",
        f"- **build-root junction (`{JUNC_DATA}`): "
        f"{report['build_root_junction_survives']}** "
        f"(junction hits {report['data_junction_hits']}, "
        f"canonical hits {report['data_canonical_hits']})",
        f"- **`.platformio` junction (`{JUNC_PIO}`): "
        f"{report['platformio_junction_survives']}** "
        f"(junction hits {report['pio_junction_hits']}, "
        f"canonical hits {report['pio_canonical_hits']})",
        f"- deepest path via junction: `{report['deepest_path_via_junction']}` chars",
        f"- deepest path via real target: `{report['deepest_path_via_canonical']}` chars "
        "(this is what the compiler sees if the junction is canonicalized)",
    ]
    block = "\n".join(lines)
    print("\n" + block, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(block + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
