"""
Real-compile proof: a provisioned venv builds with the offloader's esphome version.

Opt-in (``pytest tests/real_compile``); a real ``pip install`` + esp8266
compile, minutes per run.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from esphome.const import __version__ as _installed_version

from esphome_device_builder.controllers.remote_build.env_provisioner import EnvProvisioner

# A release different from what the test venv has installed, so the provisioner
# has to build a distinct venv rather than reuse the ambient esphome. One patch
# back from the committed catalog target (2026.6.4): same minor, so the same
# Python support, and it exists on PyPI.
_TARGET_VERSION = "2026.6.3"

_MINIMAL_ESP8266_YAML = """\
esphome:
  name: kitchen
esp8266:
  board: esp01_1m
"""


async def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run *argv* to completion off the event loop, capturing output."""
    return await asyncio.to_thread(
        subprocess.run,
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
    )


@pytest.mark.skipif(
    _installed_version == _TARGET_VERSION,
    reason=f"installed esphome is the provision target {_TARGET_VERSION}; no mismatch to prove",
)
@pytest.mark.timeout(900)
async def test_provisioned_venv_compiles_esp8266_with_target_version(tmp_path: Path) -> None:
    """Provision esphome==2026.6.3, then compile an esp8266 from that venv."""
    provisioner = EnvProvisioner(data_dir=tmp_path / "data")

    # Real ``python -m venv`` + ``pip install esphome==<target>``; the internal
    # health check already asserts the venv reports the target version.
    cmd = await provisioner.provision(_TARGET_VERSION)

    # Prove the resolved interpreter is the target version, not the installed one.
    version = await _run(*cmd, "version")
    assert version.returncode == 0, version.stderr[-2000:]
    assert _TARGET_VERSION in version.stdout
    assert _installed_version not in version.stdout

    # Real esp8266 compile driven by the provisioned venv.
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text(_MINIMAL_ESP8266_YAML)
    compiled = await _run(*cmd, "compile", str(yaml_path))
    assert compiled.returncode == 0, (
        f"provisioned compile failed:\nstdout:\n{compiled.stdout[-4000:]}\n"
        f"stderr:\n{compiled.stderr[-4000:]}"
    )
    firmware_bin = (
        tmp_path / ".esphome" / "build" / "kitchen" / ".pioenvs" / "kitchen" / "firmware.bin"
    )
    assert firmware_bin.is_file(), (
        f"firmware.bin missing after provisioned compile.\n"
        f"Last 2000 chars of stdout:\n{compiled.stdout[-2000:]}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
