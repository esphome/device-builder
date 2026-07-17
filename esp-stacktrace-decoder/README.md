# Device Builder stack trace decoder

A tiny static page that decodes ESP crash backtraces in the browser, served from
GitHub Pages at
<https://esphome.github.io/device-builder/esp-stacktrace-decoder/>. The dashboard
embeds it in a hidden iframe and hands it a firmware ELF plus a crash dump over
`postMessage`; it answers with the decoded frames.

The decoding is done by [esp-stacktrace-decoder][decoder], ESPHome's Rust
decoder, compiled to WebAssembly. This page is only the ingest: a handshake, a
call into `decode()`, and a reply.

## Why this exists

A device compiled on a remote build server has no CMake build tree on the
dashboard that flashes it, and native ESP-IDF resolves `addr2line` **only**
through that tree's CMake cache. So the backend decoder reports `no_build` and
the crash stays raw, over `esphome logs` and Web Serial alike.

The build tree cannot be shipped: `CMakeCache.txt` is full of the builder's
absolute paths, and CMake refuses a relocated cache outright. The receiver
cannot be asked either, because the crash may arrive weeks later, long after the
job log has aged out and with no record of which peer built it. Installing
ESP-IDF on the dashboard to get one `addr2line` costs **4.4 GB**, on a target
that includes the HA Green.

What the dashboard does have is the ELF, which it already serves for exactly
this purpose (`firmware/download`'s "ELF (for debugging)" entry). An ELF is all
a decoder needs, so the decode happens here instead.

## Your firmware never leaves the browser

Not a promise, a constraint. The page is served with:

```
default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; connect-src 'self'; form-action 'none'; base-uri 'none'
```

`connect-src 'self'` allows exactly one request, the wasm sitting beside this
page (wasm-bindgen's `init()` resolves it relative to its own module URL), and
the browser refuses every other destination. `default-src 'none'` closes the
side doors, image beacons in particular. The ELF arrives over `postMessage`,
which is an in-browser channel, and is decoded in wasm memory. Even a
compromised bundle could not upload it.

`test/csp.test.mjs` pins the header, and `test/handshake.test.mjs` asserts that
a real decode makes no off-origin request at all.

## Hosting, and moving it later

This URL is load-bearing for dashboards already in the field: the frontend ships
prebuilt inside the backend wheel, so every released version has its decoder URL
baked in. If the page moves (`device-builder-decoder.esphome.io`, say), **this
one keeps serving** until those versions have aged out. Retiring it early breaks
decoding for everyone who has not upgraded.

Nothing here is bound to the origin, so a move is a redeploy plus a constant in
the dashboard: assets load relatively, and `connect-src 'self'` follows whatever
host serves the page.

The dashboard treats the decoder as optional. If this page cannot be reached,
GitHub being down or an install with no internet, the handshake below simply
never completes, the dashboard gives up after a timeout, and the crash stays
readable in its raw form. A decode is an embellishment on a log, never a
prerequisite for one, so an outage here must degrade to "no decode" and nothing
worse.

## Message contract

See `src/protocol.ts`. It mirrors `flasher/src/protocol.ts`: the embedder origin
is unknown (the dashboard runs on an arbitrary http origin), so the channel is
authenticated by a one-time `nonce` plus an `event.source === window.parent`
check, not an origin allowlist. The nonce travels one way only, so the
pre-handshake `ready` broadcast leaks nothing.

The one difference from the flasher: this page is **framed, not opened**. The
flasher needs a tab because Web Serial demands a secure context and a user
gesture; a decode needs neither, and crash regions arrive mid-stream, so a tab
per region would be unusable.

1. Dashboard creates a hidden iframe at `…/#nonce=<random>&origin=<dashboard-origin>`.
2. Page posts `{type:"esphome-stacktrace-decode:ready", version}`, repeating until the first request.
3. Dashboard posts `{type:"esphome-stacktrace-decode:request", nonce, id, elf, dump}` (the ELF as a transferable `ArrayBuffer`).
4. Page replies `{type:"esphome-stacktrace-decode:result", id, frames}` or `{…":error", id, message}`.

`id` correlates the reply, because one page answers many regions over a log
session.

## Develop

```sh
npm ci
npm run dev        # esbuild serve on a local port
npm run typecheck
npm test           # builds, then checks the CSP and drives the handshake headlessly
```

The wasm is not vendored. `build.mjs` downloads the pinned
[esp-stacktrace-decoder][decoder] release and verifies its sha256 before
unpacking; a retagged or tampered asset fails the build. Bumping the decoder
means updating `DECODER_TAG` and `DECODER_SHA256` together.

[decoder]: https://github.com/esphome/esp-stacktrace-decoder
