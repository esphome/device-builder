# Adafruit BLE catalog refresh

This build-time refresh adds the `ble_trigger` field and
`ota.adafruit_ble.enter_bootloader` action only when both the installed ESPHome
core and its schema export support manual bootloader entry. It leaves the
bundled release catalog unchanged until explicitly run on a catalog copy.

Export the schema from the **same core checkout/interpreter** that will be
packaged, then run from the Device Builder repository:

```sh
python /path/to/esphome/script/build_language_schema.py --output-path /path/to/schema
python -m script.refresh_adafruit_ble \
  --version 2026.12.0-dev \
  --schema-dir /path/to/schema \
  --definitions-dir /path/to/copied/definitions
```

Use the installed core's actual version, not necessarily the example version.
Record its immutable source commit alongside the generated artifact: a version
string does not distinguish forks. The command checks the installed version and
whether the export and live registry both expose the new capability. A matching
older core/export (including official 2026.10.2) produces no changes. Never
package a feature-generated catalog with a core lacking that feature.

Only these paths can change:

- `components/ota.adafruit_ble.json`
- `automations/actions/ota.adafruit_ble.enter_bootloader.json`
- `components.index.json` (only the Adafruit component entry)
- `automations.index.json` (only the new action entry)

Other bodies and index entries are retained, including custom field help.
Index-wide schema-version stamps remain those of the original catalog; the
refresh is a scoped overlay, not a whole-catalog regeneration. Repeating the
command is a no-op, including after a description-only update.

## Compatibility boundary

The component is restricted to nRF52. Its implicit Zephyr framework dependency
is represented by the nRF52 platform requirement, not an unaddable `zephyr:`
YAML hub. The Adafruit bootloader requirement remains validated by ESPHome.
The action's typed ID uses the existing `provides`/`references_component`
contract; an omitted ID remains automatically resolved by ESPHome.

`ble_trigger` is optional and defaults to true. A false value disables only the
application's BLE restart-request service, **not** the bootloader's BLE update
transport. The component and its switch must remain accessible without
`zephyr_ble_server` so users can select false.

The current catalog model has unconditional component dependencies and
field-visibility predicates, but no value-conditional component requirement.
Do not turn the server requirement into `depends_on_component`: that would hide
the switch needed to disable the service. Instead, the component description
states the conditional requirement and ESPHome validates true/default configurations
against `zephyr_ble_server`. This is a help-text plus core-validation fallback,
not a claim that the editor automatically adds the server or validates the
conditional requirement before saving. No frontend model change is included.

## Tests

```sh
ESPHOME_SCHEMA_DIR=/path/to/schema python -m pytest tests/test_adafruit_ble_catalog.py
```

Run once with the feature core/export and once with an official stable
core/export in separate interpreters. Without `ESPHOME_SCHEMA_DIR`, the
schema-export integration tests skip; the description unit tests still run.
The tests cover unchanged stable catalogs, capability/version mismatch refusal,
serialized bodies, typed/automatic IDs, nRF52-only metadata, unchanged unrelated
entries, description-only refreshes and idempotence. They do not replace browser,
firmware or physical BLE transfer testing.
