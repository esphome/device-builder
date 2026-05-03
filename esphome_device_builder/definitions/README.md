# Board & Component Definitions

This directory contains the board and component definitions used by the ESPHome Device Builder.

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
  platform: esp32              # esp32, esp8266, rp2040, bk72xx, rtl87xx
  board: esp32-s3-devkitc-1    # PlatformIO board ID
  variant: esp32s3             # ESP32 chip variant (omit for esp8266/rp2040)
  framework: esp-idf           # arduino or esp-idf (omit for platform default)
  flash_size: 8MB              # 2MB, 4MB, 8MB, 16MB (omit for board default)

# Optional metadata
tags: [esp32-s3, wifi, bluetooth, usb, rgb-led]
docs_url: "https://example.com/docs"
is_generic: false              # true only for generic fallback boards

# Images — URLs or paths relative to this manifest (first = primary)
images:
  - "https://example.com/board.png"
  - "images/pinout.png"

# Pin definitions (see below)
pins:
  - gpio: 0
    # ...
```

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

The validator (`script/validate_definitions.py`) cross-checks every
featured component against `components.json`: the `component_id` must
exist, every key in `fields:` must match a real `ConfigEntry.key`, and
pin values / suggestions must reference GPIOs declared in the board's
`pins:` list.

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
