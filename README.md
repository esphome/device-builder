# ESPHome Device Builder Dashboard

[![PyPI version](https://img.shields.io/pypi/v/esphome-device-builder.svg)](https://pypi.org/project/esphome-device-builder/) [![codecov](https://codecov.io/gh/esphome/device-builder/branch/main/graph/badge.svg)](https://codecov.io/gh/esphome/device-builder) [![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://codspeed.io/esphome/device-builder)

> **Status:** in active development. Base functions are in late beta;
> remote / offload functions are in early beta. Expect undocumented
> breaking changes until the project is marked stable. Issues
> and feedback welcome — please check existing issues / the
> [project board](https://github.com/orgs/esphome/projects/7/views/1?filterQuery=project%3A%22device-builder-dashboard%22)
> first, and join the [Discord channel](https://discord.gg/Rf2jWGVjaK)
> for live discussion.

A new dashboard for [ESPHome](https://github.com/esphome/esphome) — a guided
interface for composing device configs, exploring components and boards,
managing automations, and pushing firmware updates.

## Try it

The dashboard ships as an **opt-in preview** in the official Home Assistant
add-on and in [ESPHome Desktop](https://github.com/esphome/esphome-desktop).
Pick the path that matches how you run ESPHome today:

### Home Assistant add-on

Open the ESPHome add-on configuration (Stable, Beta, or Dev — all three
carry the toggle), flip **Use new Device Builder Preview** on, and restart
the add-on. The container's init step pip-installs the latest prerelease
of `esphome-device-builder` and the supervisor service launches it instead
of the classic dashboard. The toggle is reversible — turn it off + restart
to fall back to the classic dashboard.

The add-on's data layout stays the same (`/config/esphome/` for YAMLs,
`/data/` for build artefacts) so flipping the toggle doesn't move or
duplicate any state.

### ESPHome Desktop (macOS / Windows / Linux)

Install [ESPHome Desktop](https://github.com/esphome/esphome-desktop)
v0.7.0 or later, then click the system-tray icon and pick **Backend →
ESPHome Builder (stable)** or **ESPHome Builder (beta)**. The daemon
restarts under the chosen backend and the tray badge updates to reflect
which one is running. Switch back to **Classic ESPHome Dashboard** the
same way.

### Standalone (PyPI)

For developers, headless servers, or anyone running outside the
add-on / Desktop shapes:

```bash
python -m venv .venv && source .venv/bin/activate
pip install esphome-device-builder

esphome-device-builder ~/esphome-configs
```

For the beta channel, pass `--pre` to opt the resolver into
prereleases — e.g. `pip install --pre esphome-device-builder` for a
fresh install, or `pip install --upgrade --pre esphome-device-builder`
to pull the newest beta on top of an existing install. `--pre` only
opts the *current* command into prereleases; rerun the upgrade
command to refresh.

The server starts on `http://localhost:6052`. Run with `--help` for
the full flag set.

<details>
<summary>Install from a GitHub release</summary>

Every build is published to PyPI, so the install above is the
preferred path. The same wheels are mirrored on the
[GitHub releases page](https://github.com/esphome/device-builder/releases) —
handy as a fallback if PyPI is unreachable.

```bash
python -m venv .venv && source .venv/bin/activate

# Replace <version> with a release tag (X.Y.Z stable, X.Y.ZbN beta).
pip install "https://github.com/esphome/device-builder/releases/download/<version>/esphome_device_builder-<version>-py3-none-any.whl"

esphome-device-builder ~/esphome-configs
```

</details>

<details>
<summary>From source (contributors)</summary>

Requires [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/esphome/device-builder
cd device-builder
script/setup
source .venv/bin/activate
esphome-device-builder ./configs --log-level debug --dev
```

`--dev` serves `index.html` with `Cache-Control: no-cache` so a
re-deployed frontend wheel isn't masked by a browser-cached SPA
shell pointing at a now-deleted hashed bundle. Hashed bundles
themselves stay `immutable` regardless. Skip `--dev` in production —
the browser's default heuristic is fine when you're not rebuilding
every few minutes.

</details>

## Send builds to another dashboard

Compiling ESPHome firmware is CPU-heavy, especially for ESP-IDF
targets. If your dashboard runs on a low-power host, say the Home
Assistant add-on on a Raspberry Pi or HA Green, you can pair it to
a beefier dashboard on the same LAN, for example ESPHome Desktop
running on a workstation, and offload compiles there. The firmware
bytes still install from the original dashboard; only the build
runs elsewhere.

Two roles:

- **Build server**, the dashboard that lends its CPU. Surfaced
  under **Settings → Build server**. Accepts pair requests,
  compiles incoming jobs, returns artefacts.
- **Send builds**, the dashboard that delegates compiles.
  Surfaced under **Settings → Send builds**. Lists dashboards
  the LAN discovered and the ones you've paired with.

A single dashboard can play both roles at once. The Home Assistant
add-on defaults to send-only, since it doesn't accept inbound
build jobs without opt-in, which is the sensible default for a
typically-shared host; ESPHome Desktop and standalone installs
default to both roles on.

### Pairing in four steps

The receiving dashboard only accepts new pair requests while its
**Pairing requests** screen is open — open that screen *before*
clicking Pair on the sending side, and keep it open until you've
clicked Accept. Step 2 is the prerequisite, not the wrap-up.

1. Start both dashboards on the same subnet, or with a working
   mDNS reflector between subnets. Outside the Home Assistant
   add-on, a dashboard advertises itself over mDNS as soon as it
   starts, independently of whether **Build server** is enabled.
   The receiver's peer-link port lands in the same TXT record
   only once **Build server** is enabled and the listener has
   bound; a dashboard without **Build server** enabled still
   appears in **Known dashboards** but can't be paired with until
   the receiving side flips that toggle. (HA add-on instances
   stay silent on the network; two add-on dashboards on the same
   LAN need the manual-entry flow below.)
2. **On the receiving dashboard**, open **Settings → Build
   server → Pairing requests**. This opens the pairing window;
   the receiver will refuse any pair request that arrives while
   this screen isn't mounted. Leave the screen open through
   step 4.
3. **On the sending dashboard**, open **Settings → Send builds →
   Known dashboards**, find the receiver in the list, and click
   **Pair**. Both dashboards now display a pairing **fingerprint**
   rendered as an emoji grid. Compare the two fingerprints out
   of band; they must match for the pairing to be safe to
   accept. Hex bytes are tucked behind a **Show hex bytes**
   disclosure if you prefer that form, but the emoji grid is the
   primary verification surface.
4. Back on the receiving dashboard's still-open **Pairing
   requests** screen, the new request now shows up — click
   **Accept**. The pairing persists on both sides and survives
   restarts.

If a dashboard you expected to show up doesn't appear in
**Known dashboards**, run `esphome-device-builder-discover` on
the sending host before troubleshooting the UI. The CLI browses
the same mDNS service the dashboard does and prints what it
sees, including the receiver's peer-link port and identity
fingerprint:

```
Status |Name |Address:Port        |Server   |ESPHome   |RB Port |Pin (sha256)
-------+-----+--------------------+---------+----------+--------+--------------
ONLINE |mac  |192.168.1.75:6052   |0.1.0b39 |2026.4.5  |6055    |3968ef58…
```

If the receiver shows up in the CLI but not in the UI, the
discovery layer is fine and the gap is somewhere downstream; if
neither side sees the other, mDNS isn't crossing the network
(different subnet without a reflector, container without host
networking, firewall blocking 5353/udp).

After pairing, clicking Install on a device automatically routes
through the paired receiver as soon as one is online. The
scheduler prefers an idle receiver, but if every paired receiver
is busy it queues the install behind the in-flight work rather
than silently building locally; that keeps the toolchain warm and
the artefacts coming from one place. The install dialog shows a
"Building on `{receiver}`" sub-line so you can see which side is
doing the work. You can override per-install via the **Build
locally instead** link in the install dialog, or disable
auto-routing entirely from **Settings → Send builds →
Auto-route installs to remote build**.

### Manual entry (no mDNS)

If the dashboards are on different subnets, or if either side is
running as the Home Assistant add-on (which doesn't advertise
itself on mDNS), use the **Pair with another dashboard** section
beneath **Known dashboards**. Open the receiving dashboard's
**Pairing requests** screen first (same prerequisite as the
discovered-dashboard flow above), then click **Pair with a build
server** on the sending side, type the receiver's hostname and
port, and submit; the pairing flow runs identically to the
discovered-dashboard case from there. The peer-link is a
WebSocket served at `/remote-build/peer-link` over TCP port
6055 by default; if a reverse proxy or firewall sits between
the two dashboards it needs to allow WebSocket upgrades on
that path. The wire is Noise-encrypted regardless of how you
reach it, and the emoji-fingerprint comparison still gates
pairing the same way.

### Known limitations

Remote build works end-to-end for OTA installs over Wi-Fi or
Ethernet across every chip family ESPHome's OTA component
supports: ESP32, ESP8266, RP2040 / RP2350, the LibreTiny family
(BK72xx, RTL87xx, LN882x), and the nRF52 line. Open follow-ups
tracked separately:

- Serial installs (USB-attached devices) don't route through a
  paired receiver yet; the runner's local flash step expects a
  single-image upload, but a wired flash needs the full
  bootloader / partitions / firmware set stitched at their own
  offsets. See [#570](https://github.com/esphome/device-builder/issues/570).
- A toggle to allow major-version mismatches between paired
  dashboards is planned but not shipped. Pairings whose receiver
  runs a different ESPHome major version than the sender still
  build today, with no enforcement gate yet; that gate lands
  together with the toggle. See
  [#607](https://github.com/esphome/device-builder/issues/607).

## Roadmap

- ✅ Standalone backend with WS-first API, persistent compile queue, mDNS device discovery
- ✅ Curated board + component catalogs (nightly catalog sync from upstream ESPHome)
- ✅ Functional parity with the legacy dashboard
  (one intentional decline: the HA Supervisor `/auth` POST flow —
  the new backend's HA add-on path is ingress-only by design, see
  [issue #85](https://github.com/esphome/device-builder/issues/85))
- ✅ Opt-in preview toggle in the Home Assistant add-on
  (`use_new_device_builder` config option, available on the Stable, Beta,
  and Dev channels)
- ✅ Backend selector in [ESPHome Desktop](https://github.com/esphome/esphome-desktop)
  ≥ v0.7.0 (system tray → Backend)
- 🚧 Same toggle in the standalone ESPHome Docker image
  (`ghcr.io/esphome/esphome`) — currently only the HA-addon image carries
  it
- 🗺️ See the
  [project backlog](https://github.com/orgs/esphome/projects/7/views/1?filterQuery=project%3A%22device-builder-dashboard%22)
  for in-progress work and what's planned next

## Documentation

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — controllers, event bus,
  firmware queue, catalog sync, deployment.
- **[docs/ARCHITECTURE.md § Remote build](docs/ARCHITECTURE.md#remote-build)**,
  the internals of the pair flow, peer-link transport, and build
  scheduler behind the "Send builds" feature above.
- **[docs/API.md](docs/API.md)** — every WebSocket command, request/response
  shapes, event types.
- **[esphome_device_builder/definitions/README.md](esphome_device_builder/definitions/README.md)** —
  contributor guide for board manifests.

## Contributing

Contributions welcome — board definitions especially
([definitions/README.md](esphome_device_builder/definitions/README.md)).

Every PR needs **exactly one** label from this set so it lands in the right
release-notes section: `breaking-change`, `new-feature`, `enhancement`,
`bugfix`, `refactor`, `docs`, `maintenance`, `ci`, `dependencies`. CI enforces
the rule via [`pr-labels.yaml`](.github/workflows/pr-labels.yaml).

Bugs / feature ideas: open an issue and the chooser will route you to the
right venue (this repo for dashboard bugs, esphome core for compile/firmware
issues, org Discussions for ideas, Discord for chat).

## License

Apache-2.0 — Maintained by [Open Home Foundation](https://www.openhomefoundation.io/).
