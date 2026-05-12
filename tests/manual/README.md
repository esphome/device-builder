# Manual remote-firmware e2e scripts

Two scripts that exercise the materialise-locally remote-firmware flow against
a real `esphome` toolchain by pairing two in-process mock dashboards. **Not
part of CI** — these are wet-test helpers for pre-release confidence.

## Scripts

### `run_mock_remote_e2e.py`

Submits `firmware/install` on the offloader. The receiver runs a real
`esphome compile`; the offloader materialises the artifacts and spawns a real
`esphome upload` against the configured device.

```sh
python tests/manual/run_mock_remote_e2e.py \
    --yaml apollo-r-pro-1-eth-5938e0.yaml \
    --device OTA
```

For a wired (serial) install, pass `--device /dev/ttyUSB0` (or the platform
equivalent). The apollo-r-pro-1-eth device that's been our wet-test target
is safe to re-upload to as many times as needed.

### `download_mock_remote_e2e.py`

Submits `firmware/compile` on the offloader, then calls `firmware/download`
to dump the staged binary to a local path. Proves the #624 download path
end-to-end.

```sh
python tests/manual/download_mock_remote_e2e.py \
    --yaml apollo-r-pro-1-eth-5938e0.yaml \
    --out  /tmp/apollo-firmware.bin
```

Use `--file firmware.factory.bin` to download the factory image instead of
the OTA binary; `--file firmware.uf2` for libretiny / RP2040.

## Prerequisites

- `esphome` installed in the active venv (the PIO toolchains install
  themselves on first compile / upload, no separate setup needed).
- A YAML the script can hand to `esphome compile`.

Both scripts create a tempdir under cwd (or `--workdir`) and stand up the
offloader + receiver dashboards there. The pairing handshake runs over a
real Noise XX peer-link WS on a loopback port; no network access needed.

## What they exercise

- The full receiver-side compile pipeline (real `esphome compile` subprocess).
- The wire round-trip (`submit_job` → fan-out → `download_artifacts`).
- The offloader-side `materialise_remote_artifacts` against real
  receiver-produced artifacts.
- For `run_*`: the local `esphome upload <yaml> --device <port>` subprocess
  the runner spawns against the staged tree.
- For `download_*`: the dashboard's `firmware/download` endpoint reading
  the staged StorageJSON sidecar.

If a script returns non-zero, inspect the printed `job.error` and the
streamed job output to find which side broke.
