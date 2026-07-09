# Board & Component Definitions

This directory contains the board and component definitions used by the ESPHome Device Builder.

## Editing boards: the build pipeline

> **Read this first.** Editing a `manifest.yaml` is only half the job; if you
> stop there, your change never reaches the dashboard and CI fails.

The `manifest.yaml` files under `boards/<id>/` are the only thing you hand-edit.
The dashboard does **not** read them at runtime. It reads three generated
artefacts, all written by `script/sync_boards.py`:

| Artefact | What it is |
|----------|------------|
| `boards.index.json` | Slim per-board index; powers the board picker. |
| `board_bodies/<id>.json` | Full body (hardware, pins, featured components); lazy-loaded when a board is opened. |
| `featured_components.index.json` | Aggregated featured-components map read once at startup. |

After editing a manifest, run the one-step helper from the project venv:

```bash
python script/update_board.py            # auto-detects the board you edited
python script/update_board.py my-board   # or name it explicitly
```

It regenerates that board's JSON, validates the definitions, and prints the
files to commit. That is all most contributors need.

Under the hood it runs the two scripts you can also call directly:

```bash
# regenerate the JSON. Pass a board id to rewrite just that board; omit it
# to rebuild the whole catalog (~990 body files).
python script/sync_boards.py my-board

# validate the manifests against the schema and cross-references
python script/validate_definitions.py
```

Passing the board id (the folder name under `boards/`) rewrites only that
board's `board_bodies/<id>.json` and refreshes the two index files, so the diff
stays scoped to what you edited.

Commit the manifest **and** the regenerated JSON together. **Never hand-edit the
JSON** (`boards.index.json`, `board_bodies/*.json`); it is overwritten on the
next sync, and `tests/test_boards_json.py::test_split_artefacts_match_manifests`
compares every manifest against its generated body and fails CI on any drift.

`validate_definitions.py --check-images` additionally fetches each image URL to
confirm it resolves (network, opt-in); the consistency test exempts `images`, so
a broken image URL passes the sync but is caught here.

### Run the sync with the project venv

`sync_boards.py` imports ESPHome: it generates the boards no manifest covers
straight from your installed ESPHome's board tables, and fills curated boards'
pin aliases the same way. Run it from the project venv:

```bash
source .venv/bin/activate    # or call .venv/bin/python directly
python script/sync_boards.py my-board
```

Single-board mode (and `update_board.py`) rewrites one body but rebuilds the
shared index from every board, so the installed ESPHome must match the version
the committed catalog was generated against (stamped as `esphome_version` in
`boards.index.json` by the last full sync, betas canonicalized to their base
release) or their index entries silently drift; it refuses on a mismatch and
prints the version to install. A full `python script/sync_boards.py` regenerates
everything against your installed ESPHome and re-stamps that version, so it does
not check; still run it from the venv so you don't commit catalog-wide changes
from a different ESPHome.

### Curated vs generated vs imported boards

- **Curated** (most hand-written manifests): a `manifest.yaml` with no `source:`
  block. Edit freely; this is the normal case.
- **Generated**: no manifest at all. The board comes from ESPHome's board tables
  on every sync, with a synthesized pin map and a generic image. To customise
  one (real pinout, photo, featured components), add a curated manifest at
  `boards/<esphome.board>/manifest.yaml`.
- **Imported**: a manifest carrying a `source:` block (e.g.
  `type: esphome-devices`). It is owned by the importer and regenerated from
  upstream, so hand edits are overwritten; change the upstream source instead.

## Adding a Board

Create a new subfolder in `boards/` with a `manifest.yaml`:

```
boards/
└── my-awesome-board/
    ├── manifest.yaml
    └── images/           (optional)
        ├── board-top.png
        └── pinout.png
```

### Board Manifest Schema

```yaml
# Required fields
id: my-awesome-board           # Unique ID, must match the folder name
name: "My Awesome Board"       # Human-readable name
description: |                 # Markdown-supported description
  A great ESP32-S3 board with built-in RGB LED and USB-C.
manufacturer: "Acme Corp"

# ESPHome configuration — maps directly to the ESPHome YAML platform block
esphome:
  platform: esp32              # esp32, esp8266, rp2040, bk72xx, rtl87xx, ln882x, nrf52, host
  board: esp32-s3-devkitc-1    # PlatformIO board ID
  variant: esp32s3             # ESP32 chip variant only (omit otherwise)
  framework: esp-idf           # arduino, esp-idf, or zephyr (omit for platform default)
  logger_hardware_uart: UART0  # explicit logger console for generated configs (e.g.
                               # UART0 for a CH343-bridge console); may restate the chip
                               # default. UART0, UART1, UART2, USB_CDC, or
                               # USB_SERIAL_JTAG (omit to keep ESPHome's default).
                               # engineering_sample is NOT set here — it's derived at
                               # sync time from the pio board id.

# Hardware specs (all optional)
hardware:
  flash_size: 8MB              # 2MB, 4MB, 8MB, 16MB
  ram_size: 327680             # bytes
  cpu_frequency: 240MHz
  connectivity: [wifi, bluetooth]

# Optional metadata
tags: [compact, usb-c, rgb-led]   # only the enum values in board.schema.json
docs_url: "https://esphome.io/components/esp32.html"
product_url: "https://example.com/my-awesome-board"
is_generic: false              # true only for generic fallback boards

# Images; URLs or paths relative to this manifest (first = primary).
# Prefer a bundled local asset over a hotlinked vendor URL, which can rot.
images:
  - "images/board-top.png"
  - "images/pinout.png"

# Pin definitions (see below)
pins:
  - gpio: 0
    # ...
```

`tags` accepts only the values enumerated in
[`schemas/board.schema.json`](schemas/board.schema.json) (`compact`, `dev-kit`,
`usb-c`, `rgb-led`, `poe`, ...); platform, variant, and connectivity live in
their own fields, not in `tags`.

### Pin Definitions

The pin map is the most valuable part of a board definition. It enables the
Device Builder to guide users when selecting pins for components — showing
which GPIOs are available, what they support, and warning when a pin is
already used by an onboard component.

```yaml
pins:
  - gpio: 0                    # GPIO number
    label: "GPIO0"             # Silkscreen label on the physical board
    features: [adc, touch, pwm, strapping, boot_button]
    available: true            # true  = exposed on headers
                               # false = not broken out / internal only
                               # omit or null = unknown (for generic boards)
    occupied_by: "BOOT button" # Onboard component using this pin (omit if free)
    notes: "Directly connected to BOOT button, directly usable otherwise"
```

#### Feature vocabulary

| Feature | Meaning |
|---------|---------|
| `adc` | Analog-to-digital converter input |
| `dac` | Digital-to-analog converter output |
| `touch` | Capacitive touch sensor |
| `pwm` | PWM (LEDC) output capable |
| `i2c_sda` | Default I2C data line |
| `i2c_scl` | Default I2C clock line |
| `spi_mosi` | Default SPI MOSI |
| `spi_miso` | Default SPI MISO |
| `spi_clk` | Default SPI clock |
| `spi_cs` | Default SPI chip select |
| `uart_tx` | Default UART transmit |
| `uart_rx` | Default UART receive |
| `usb_dp` | USB D+ line |
| `usb_dm` | USB D- line |
| `rgb_led` | Connected to onboard RGB LED |
| `jtag` | JTAG debug interface |
| `strapping` | Strapping pin — affects boot mode, use with care |
| `input_only` | Cannot be used as output (e.g. GPIO34-39 on ESP32) |
| `boot_button` | Connected to BOOT/FLASH button |

#### Generic boards

Generic boards (e.g. "Generic ESP32-S3 Board") should list **all GPIOs the
chip variant provides**, with `available` set to `null`. The Device Builder
shows a warning that not every pin may be physically accessible on the user's
specific board.

#### Occupied pins

Use `occupied_by` when a GPIO is connected to an onboard component. Examples:

```yaml
- gpio: 2
  label: "GPIO2"
  features: [adc, touch, pwm, strapping]
  occupied_by: "Built-in LED"
  notes: "Can still be used, but LED will reflect state"

- gpio: 48
  label: "GPIO48"
  features: [rgb_led]
  occupied_by: "WS2812 RGB LED"
```

This tells the Device Builder to warn users before assigning these pins.

### Featured Components

A board manifest can recommend specific components for the Add Component
dialog under a `featured_components:` section. Each entry references an
existing catalog component by `component_id` (e.g. `switch.gpio`) and
optionally pre-fills any of its config fields. Three preset modes:

```yaml
featured_components:
  # 1) Recommend-only — points users at a component, no config preset.
  - id: dht
    component_id: sensor.dht
    name: Temperature & Humidity (DHT)

  # 2) Locked — fixed value the user cannot change.
  - id: relay
    component_id: switch.gpio
    name: Onboard Relay
    description: 10 A relay wired to GPIO12.
    fields:
      pin: { value: 12, locked: true }
      name: Relay   # primitive shorthand → value="Relay", locked=false

  # 3) Suggestions — short list of allowed values (frontend renders a picker).
  - id: pir_motion
    component_id: binary_sensor.gpio
    name: PIR Motion Module
    fields:
      pin:
        suggestions: [4, 5]
        value: 4    # initial pick
      device_class: motion
```

`id` must be lowercase letters / digits / underscores (no hyphens) and must
not equal the domain of `component_id` — e.g. `id: output` under
`component_id: output.gpio` would clash with the ESPHome `output:` block;
use `output_relay` instead.

Inside `fields:`:

- A bare primitive / list is shorthand for `{ value: <x>, locked: false }`.
- The full mapping form is `{ value, locked, suggestions }`. `locked: true`
  and `suggestions: [...]` are mutually exclusive.
- Pin values can be either a bare integer or the rich ESPHome pin form
  (`{ number: 0, mode: { input: true, pullup: true }, inverted: true }`)
  for cases like the Sonoff button that need pull-ups and inversion.

**Bundles** group multiple featured components that go together — typical
case is a status LED that needs both `output.gpio` and `light.binary`:

```yaml
featured_bundles:
  - id: status_led
    name: Status LED (full setup)
    description: GPIO output plus a binary light entity.
    component_ids: [status_led_output, status_led_light]
```

`component_ids` references the local `id` of entries in
`featured_components:` on the same board. The frontend adds bundle
members sequentially via the regular `devices/add_component` flow.

The sync also **collapses** a `full_config` board (a complete onboard device;
defaults to whether the board is a devices.esphome.io import, overridable per
manifest with `full_config: true|false`) down to a **single** "(full setup)"
bundle, since one imported device is one config and a partial sub-bundle just
sets up half of it. The per-consumer `featured_bundles` you author are dropped
in favour of that one bundle:

- when an existing bundle already covers every featured component it's kept as
  the sole bundle (its siblings are pruned);
- otherwise a board-named `all_recommended` bundle covering every featured
  component replaces the lot. It's baked into the generated body only, so don't
  hand-add an `all_recommended` bundle to a manifest.

Collapse is skipped — leaving your authored sub-bundles in place — for boards
with fewer than two featured components, and for boards where two featured
components claim the same board GPIO without `allow_other_uses` (the combined
config wouldn't compile, so the partial bundles are the only valid options).

**Default components** are installed automatically in every new device
created from this board. Unlike `featured_components` (opt-in via the
Recommended tab) and `featured_bundles` (opt-in via the bundle picker),
these land in the initial YAML without any user clicks. Use this for
board-specific config the device can't compile or work without:

```yaml
default_components:
  - accessory_power   # string shorthand: local featured_components.id — picks up its full presets
  - id: web_server    # object form: bare catalog component_id with inline preset overrides
    fields:
      version: '3'
```

Each entry's `id` resolves through a two-step lookup: first as a
local `featured_components.id` on the same board (picks up the full
field presets, including locked values), falling through to a bare
catalog `component_id` (emits a minimal block). The optional `fields:`
dict layers on top of any featured presets with inline `key: value`
overrides — useful for board-specific tweaks to a generic component
(e.g. pinning `web_server` to `version: 3`).

Default components only fire at device creation; existing devices
keep whatever YAML they already have, and users are free to delete
or edit any default block — the dashboard won't re-add it.

The validator (`script/validate_definitions.py`) cross-checks every
featured component against the component catalog: the `component_id` must
exist, every key in `fields:` must match a real `ConfigEntry.key`, and
pin values / suggestions must reference GPIOs declared in the board's
`pins:` list. Each `default_components` entry must resolve to either
a local `featured_components.id` or a known catalog `component_id`.

## Adding a Component

Create a new subfolder in `components/` with a `manifest.yaml`:

```
components/
└── my_component/
    └── manifest.yaml
```

### Component Manifest Schema

```yaml
id: binary_sensor
name: "Binary Sensor"
description: "Detects on/off states such as buttons, door contacts, and PIR sensors."
docs_url: "https://esphome.io/components/binary_sensor/index.html"
icon: electric-switch

platforms:
  - id: gpio
    name: "GPIO"
    description: "Read a binary state from a GPIO pin."
    yaml_template: |
      binary_sensor:
        - platform: gpio
          pin: {pin}
          name: {name}
    fields:
      - key: pin
        label: "GPIO Pin"
        type: pin           # pin, string, number, boolean, select
        required: true
      - key: name
        label: "Name"
        type: string
        required: true
```

Field types: `string`, `number`, `boolean`, `select`, `pin`.
For `select` fields, provide an `options` list and optionally a `default`.
