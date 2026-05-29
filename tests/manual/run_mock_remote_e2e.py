#!/usr/bin/env python3
r"""
Manual e2e: real firmware/install through two paired mock dashboards.

Stands up an offloader + receiver, submits a firmware/install for the
provided YAML against the provided device port. The offloader's runner
sends the bundle to the receiver, which runs a real ``esphome compile``;
on completion the offloader pulls the tarball back, materialises it, and
spawns a real ``esphome upload`` against the configured device.

Usage::

    python tests/manual/run_mock_remote_e2e.py \
        --yaml /path/to/device.yaml \
        --device /dev/ttyUSB0
        # or --device OTA for OTA installs

Run from the repo root. Requires ``esphome`` on PATH (or installed in the
active venv). The PIO toolchains install on first compile; no manual prep.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from esphome_device_builder.models import EventType

# Add tests/ to sys.path so the shared helper imports as a sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mock_remote_e2e import paired_dashboards, wait_for_job


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml", required=True, type=Path, help="device YAML to install")
    parser.add_argument(
        "--device",
        required=True,
        help="install target: ``OTA`` or a serial path like ``/dev/ttyUSB0``",
    )
    parser.add_argument("--workdir", type=Path, help="run dir (default: tempdir under cwd)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    yaml_path = args.yaml.resolve()
    if not yaml_path.is_file():
        print(f"YAML not found: {yaml_path}", file=sys.stderr)
        return 2

    if args.workdir is None:
        import tempfile

        workdir = Path(tempfile.mkdtemp(prefix="mock-remote-e2e-"))
        print(f"Working under: {workdir}")
    else:
        workdir = args.workdir.resolve()

    async with paired_dashboards(root_dir=workdir, yaml_source=yaml_path) as pair:
        print(f"Paired pin_sha256={pair.pin_sha256}")
        print(f"Offloader dashboard_id (as tracked by receiver)={pair.offloader_dashboard_id}")

        # Stream log lines as the receiver compiles + the offloader uploads.
        def _on_output(event: object) -> None:
            data = getattr(event, "data", None)
            if isinstance(data, dict) and (line := data.get("line")):
                sys.stdout.write(line)
                sys.stdout.flush()

        pair.offloader.bus.add_listener(EventType.JOB_OUTPUT, _on_output)

        # Submit install via the offloader's firmware controller. The
        # runner routes REMOTE because the scheduler sees the paired
        # receiver as idle + APPROVED.
        job = await pair.offloader.firmware.install(
            configuration=yaml_path.name,
            port=args.device,
        )
        print(f"Submitted install job_id={job.job_id} source={job.source.value}")
        # Fail loud — the whole point of this script is exercising
        # the REMOTE path. A silent local fallback would compile
        # everything on the offloader and look like a green run.
        if job.source.value != "remote":
            raise RuntimeError(
                f"scheduler picked source={job.source.value!r}; "
                "the manual e2e requires the REMOTE path"
            )

        terminal = await wait_for_job(pair.offloader, job.job_id)
        status = terminal.get("job").status.value if isinstance(terminal, dict) else "?"
        print(f"\nJob terminal status: {status}")
        if isinstance(terminal, dict):
            job_obj = terminal.get("job")
            if job_obj is not None and job_obj.error:
                print(f"job.error: {job_obj.error}")
        return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
