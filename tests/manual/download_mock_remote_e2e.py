#!/usr/bin/env python3
r"""
Manual e2e: real firmware/compile + firmware/download through two paired mocks.

Stands up an offloader + receiver, submits firmware/compile for the
provided YAML. The receiver runs a real ``esphome compile``; on completion
the offloader's runner pulls the tarball and materialises it. The script
then calls ``firmware/download`` and writes the staged firmware binary to
the requested output path.

Usage::

    python tests/manual/download_mock_remote_e2e.py \
        --yaml /path/to/device.yaml \
        --out  /tmp/device-firmware.bin \
        [--file firmware.bin]

Run from the repo root. Requires ``esphome`` on PATH.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
from pathlib import Path

from esphome_device_builder.models import EventType

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mock_remote_e2e import paired_dashboards, wait_for_job


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml", required=True, type=Path, help="device YAML to compile")
    parser.add_argument("--out", required=True, type=Path, help="where to write the binary")
    parser.add_argument(
        "--file",
        default="firmware.bin",
        help="binary basename: firmware.bin / firmware.uf2 / firmware.factory.bin",
    )
    parser.add_argument("--workdir", type=Path, help="run dir (default: tempdir under cwd)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    yaml_path = args.yaml.resolve()
    out_path = args.out.resolve()
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

        def _on_output(event: object) -> None:
            data = getattr(event, "data", None)
            if isinstance(data, dict) and (line := data.get("line")):
                sys.stdout.write(line)
                sys.stdout.flush()

        pair.offloader.bus.add_listener(EventType.JOB_OUTPUT, _on_output)

        job = await pair.offloader.firmware.compile(configuration=yaml_path.name)
        print(f"Submitted compile job_id={job.job_id} source={job.source.value}")
        if job.source.value != "remote":
            raise RuntimeError(
                f"scheduler picked source={job.source.value!r}; "
                "the manual e2e requires the REMOTE path"
            )

        terminal = await wait_for_job(pair.offloader, job.job_id)
        status = terminal.get("job").status.value if isinstance(terminal, dict) else "?"
        print(f"\nCompile terminal status: {status}")
        if status != "completed":
            if isinstance(terminal, dict):
                job_obj = terminal.get("job")
                if job_obj is not None and job_obj.error:
                    print(f"job.error: {job_obj.error}")
            return 1

        # firmware/download reads the offloader-side StorageJSON sidecar
        # that the materialiser just rewrote.
        result = await pair.offloader.firmware.download(
            configuration=yaml_path.name,
            file=args.file,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(result["data"]))
        print(f"Wrote {result['size']} bytes to {out_path} ({result['filename']})")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
