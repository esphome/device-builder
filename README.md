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

The dashboard isn't yet wired into the ESPHome container or the Home Assistant
add-on as an opt-in preview — that's coming soon. In the meantime:

**Install from [PyPI](https://pypi.org/project/esphome-device-builder/):**

```bash
python -m venv .venv && source .venv/bin/activate
pip install esphome-device-builder

esphome-device-builder ~/esphome-configs
```

To try a pre-release (beta), pass `--pre` to `pip install`.

The server starts on `http://localhost:6052`. Run with `--help` for the
full flag set.

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

**From source** (requires [uv](https://docs.astral.sh/uv/)):

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

## Roadmap

- ✅ Standalone backend with WS-first API, persistent compile queue, mDNS device discovery
- ✅ Curated board + component catalogs (nightly catalog sync from upstream ESPHome)
- ✅ Functional parity with the legacy dashboard
  (one intentional decline: the HA Supervisor `/auth` POST flow —
  the new backend's HA add-on path is ingress-only by design, see
  [issue #85](https://github.com/esphome/device-builder/issues/85))
- 🚧 Beta toggle in the official ESPHome container and Home Assistant add-on
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
