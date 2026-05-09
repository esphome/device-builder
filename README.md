# ESPHome Device Builder Dashboard

[![PyPI version](https://img.shields.io/pypi/v/esphome-device-builder.svg)](https://pypi.org/project/esphome-device-builder/) [![codecov](https://codecov.io/gh/esphome/device-builder/branch/main/graph/badge.svg)](https://codecov.io/gh/esphome/device-builder) [![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://codspeed.io/esphome/device-builder)

> **Status:** in active development. Roughly alpha, closing on beta. Issues
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
