# Device Builder USB flasher (development / testing only)

> **This is a development site, not production.** The production flasher is
> <https://web.esphome.io/>. This page exists only to develop and verify the
> dashboard flashing flow end to end, so we can prove it out **without touching
> the production site**. The real change is a follow-up PR to web.esphome.io
> (`esphome/dashboard`) that adds the same `postMessage`-ingest mode; once that
> lands the dashboard points there and this page retires.

A tiny static page that flashes ESP firmware over **Web Serial**, used to unblock
USB flashing from the Home Assistant add-on dashboard.

## Why this exists

The HA add-on dashboard is served over plain `http://` (ingress), and browsers
only expose Web Serial in a secure context (`https://` or `localhost`). This page
is served over `https://` from GitHub Pages, so Web Serial works here. The
dashboard opens this page in a new tab and hands it the locally built firmware
**tab-to-tab via `postMessage`** (a transferable `ArrayBuffer`); the firmware
never touches a server.

This is the **test target**. The same `postMessage` contract is being added to
the official flasher at <https://web.esphome.io/> in a follow-up; once that lands,
the dashboard points there and this page can retire.

## Message contract

See `src/protocol.ts`. The opener origin is unknown (the dashboard runs on an
arbitrary http origin), so the channel is authenticated by a one-time `nonce`
plus an `event.source === window.opener` check, not an origin allowlist.

The `nonce` travels one way only (opener to flasher): inbound firmware must
carry it, but no outbound frame echoes it, so the pre-handoff `ready` broadcast
leaks no secret. The opener correlates outbound frames by window source. As
defense in depth the opener should still open the flasher with
`#origin=<dashboard-origin>` so outbound targets that origin from frame zero;
without it the page falls back to `*` until the first inbound frame reveals it.

1. Dashboard opens `…/#nonce=<random>` (ideally also `&origin=<dashboard-origin>`).
2. Flasher posts `{type:"esphome-web-flash:ready", version}` to its opener (no nonce).
3. Dashboard posts `{type:"esphome-web-flash:firmware", nonce, name?, erase?, parts:[{address, data:ArrayBuffer}]}`.
4. User presses **Connect & install**, picks the serial port (the required user
   gesture), and the page flashes via esptool-js.
5. Flasher posts `state` / `progress` updates back so the dashboard can mirror them.

The esptool-js calls mirror `esphome/dashboard`'s `src/web-serial/*` so the
behavior matches web.esphome.io.

## Develop

```sh
npm install
npm run dev        # esbuild serve with watch
npm run build      # -> dist/
npm run typecheck  # tsc --noEmit
npm test           # build + headless Puppeteer check of the postMessage contract
```

`npm test` runs the postMessage handshake in headless Chromium (ready frame,
re-announcement, nonce + source rejection, malformed-payload error, firmware
acceptance). Web Serial flashing itself needs real hardware and is not covered.
CI runs the same via `.github/workflows/flasher-ci.yml`.

Open the dev URL directly to use the manual file-picker mode (flash a factory
`.bin` without a dashboard), or drive it from the dashboard for the postMessage flow.

## Deploy (GitHub Pages)

`.github/workflows/flasher-pages.yml` builds `flasher/` and publishes `flasher/dist`
to GitHub Pages on pushes to `main` that touch `flasher/**`.

1. Repo **Settings -> Pages -> Build and deployment -> Source = GitHub Actions**.
2. The site serves at `https://esphome.github.io/device-builder/`.

Assets use relative paths, so the `/device-builder/` project-page subpath needs no
extra config. Pages sets no `Cross-Origin-Opener-Policy`, so the `window.opener`
handle survives for the postMessage handoff.
