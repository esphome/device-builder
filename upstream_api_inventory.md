# ESPHome Upstream API Inventory for device-builder

## Executive Summary

**Total imported symbols:** 19 distinct symbols across 9 upstream modules
**esphome.dashboard.* imports blocking removal:** 1 (`friendly_name_slugify`)
**Symbols without test coverage:** 3 (`import_config`, `friendly_name_slugify`, `DashboardImportDiscovery`)
**Modules imported as packages:** 4 (`yaml_util`, `util`, `const`, `loader`, `config_validation`)

Device-builder maintains a narrow, well-scoped dependency on esphome, primarily for configuration storage, hardware discovery, and device management. The only blocking dependency is on the legacy dashboard module.

---

## esphome.const

### Symbols
- `__version__` — *String constant; semver of esphome package*

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/api/ws.py:15` — broadcasted as `esphome_version` in WebSocket messages
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:18` — logged/exposed at startup

**Upstream definition:** `/Users/bdraco/esphome/esphome/const.py:7`
**Classification:** Public API (top-level module constant)
**Test coverage:** Covered implicitly via broader esphome tests; no dedicated test for this symbol
**Suggested entry-point comment:**
```
# device-builder/api/ws.py, device-builder/controllers/config.py — broadcast esphome version to clients
```

---

## esphome.core

### Symbols
- `CORE` — *Singleton EsphomeCore instance managing build state and compilation context*

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:19` — initialize build context for configuration loading

**Upstream definition:** `/Users/bdraco/esphome/esphome/core/__init__.py:1098` (instance of `EsphomeCore` class at line 571)
**Classification:** Public API (module-level singleton, used throughout esphome)
**Test coverage:** Covered extensively in `/Users/bdraco/esphome/tests/unit_tests/test_core.py` and integration tests
**Suggested entry-point comment:**
```
# device-builder/controllers/config.py — initialize build context for config loading
```

---

## esphome.helpers

### Symbols
- `get_bool_env(var, default=False)` — *Parse bool from environment variable*
- `sort_ip_addresses(address_list)` — *Sort IP addresses numerically*

**Usage sites:**
- `get_bool_env`: `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:20` — check env for debug/CLI flags
- `sort_ip_addresses`: `/Users/bdraco/device-builder/esphome_device_builder/controllers/devices.py:17` — alphabetize discovered device list

**Upstream definitions:**
- `get_bool_env`: `/Users/bdraco/esphome/esphome/helpers.py:336`
- `sort_ip_addresses`: `/Users/bdraco/esphome/esphome/helpers.py:312`

**Classification:** Public API (no leading underscore, in helpers module)
**Test coverage:**
- `get_bool_env`: Covered by `/Users/bdraco/esphome/tests/unit_tests/test_helpers.py` (test_get_bool_env)
- `sort_ip_addresses`: Covered by `/Users/bdraco/esphome/tests/unit_tests/test_helpers.py` (test_sort_ip_addresses)

**Suggested entry-point comments:**
```
# device-builder/controllers/config.py — parse env flags for debug mode
# device-builder/controllers/devices.py — sort discovered device IPs for stable UI
```

---

## esphome.storage_json

### Symbols
- `StorageJSON` — *Dataclass wrapping YAML/JSON device metadata (build state, secrets, mDNS info)*
- `ext_storage_path(config_filename)` — *Resolve .storage JSON path from config filename*
- `ignored_devices_storage_path()` — *Resolve path for ignored-device list*

**Usage sites:**
- `StorageJSON`:
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:21`
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/firmware.py:23`
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/devices.py:18`
  - `/Users/bdraco/device-builder/esphome_device_builder/helpers/device_yaml.py:19`
  - `/Users/bdraco/device-builder/esphome_device_builder/helpers/config_hash.py:38`
- `ext_storage_path`:
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:21`
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/firmware.py:23`
  - `/Users/bdraco/device-builder/esphome_device_builder/helpers/device_yaml.py:19`
- `ignored_devices_storage_path`:
  - `/Users/bdraco/device-builder/esphome_device_builder/controllers/devices.py:18`

**Upstream definitions:**
- `StorageJSON` class: `/Users/bdraco/esphome/esphome/storage_json.py:48`
- `ext_storage_path` function: `/Users/bdraco/esphome/esphome/storage_json.py:23`
- `ignored_devices_storage_path` function: `/Users/bdraco/esphome/esphome/storage_json.py:31`

**Classification:** Public API (no underscore prefix, part of stable storage layer)
**Test coverage:** All three covered by `/Users/bdraco/esphome/tests/unit_tests/test_storage_json.py`
**Suggested entry-point comments:**
```
# esphome/storage_json.py — device-builder relies on StorageJSON for build cache and mDNS state
# esphome/storage_json.py — device-builder uses ext_storage_path to locate build artifacts
# esphome/storage_json.py — device-builder uses ignored_devices_storage_path for device filtering
```

---

## esphome.util

### Symbols
- `get_serial_ports()` — *List available serial ports on host*
- Module import (broader): `util` — Device-builder also imports the module itself

**Usage sites:**
- `get_serial_ports`: `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:22` — detect connected boards
- Module: `/Users/bdraco/device-builder/esphome_device_builder/controllers/_device_scanner.py:20` — for `util.get_*()` utilities

**Upstream definition:** `/Users/bdraco/esphome/esphome/util.py:361`
**Classification:** Public API (no underscore, in util module)
**Test coverage:** `get_serial_ports` is mocked/tested in `/Users/bdraco/esphome/tests/unit_tests/test_main.py`
**Suggested entry-point comment:**
```
# esphome/util.py — device-builder detects serial ports for firmware flashing
```

---

## esphome.yaml_util

### Symbols
- Module import (entire module)

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/api/legacy.py:23` — parse YAML config/secrets
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/config.py:17` — load device YAML files
- `/Users/bdraco/device-builder/esphome_device_builder/helpers/device_yaml.py:18` — YAML roundtrip operations

**Upstream definition:** `/Users/bdraco/esphome/esphome/yaml_util.py` (module)
**Classification:** Public API (no underscore prefix, widely used)
**Test coverage:** Heavily tested in `/Users/bdraco/esphome/tests/unit_tests/test_yaml_util.py`, `/Users/bdraco/esphome/tests/unit_tests/test_substitutions.py`
**Suggested entry-point comment:**
```
# esphome/yaml_util.py — device-builder parses and emits YAML device configurations
```

---

## esphome.config_validation

### Symbols
- Module import (via `config_validation as cv`)

**Usage sites:**
- `/Users/bdraco/device-builder/script/sync_components.py:2205` — schema validation during component catalog sync

**Upstream definition:** `/Users/bdraco/esphome/esphome/config_validation.py` (module)
**Classification:** Public API (no underscore prefix, widely exported)
**Test coverage:** Extensively tested in `/Users/bdraco/esphome/tests/unit_tests/test_config_validation.py`, `/Users/bdraco/esphome/tests/unit_tests/test_config_validation_paths.py`
**Suggested entry-point comment:**
```
# esphome/config_validation.py — device-builder uses cv.* validators for component definitions
```

---

## esphome.loader

### Symbols
- Module import

**Usage sites:**
- `/Users/bdraco/device-builder/script/sync_components.py:2027` — enumerate component metadata at build time

**Upstream definition:** `/Users/bdraco/esphome/esphome/loader.py` (module)
**Classification:** Public API (no underscore, part of component loading infrastructure)
**Test coverage:** No dedicated test for loader module itself (internal tool for build scripts); used indirectly
**Suggested entry-point comment:**
```
# esphome/loader.py — device-builder enumerates available components during catalog generation
```

---

## esphome.zeroconf

### Symbols
- `AsyncEsphomeZeroconf` — *Async mDNS listener for discovering ESPHome devices on network*
- `DashboardImportDiscovery` — *State tracker for importable device advertisements*
- `DiscoveredImport` — *Dataclass for device import metadata parsed from mDNS TXT records*

**Usage sites:**
- All three: `/Users/bdraco/device-builder/esphome_device_builder/controllers/_device_state_monitor.py:23-27` — discover and track device advertisements

**Upstream definitions:**
- `AsyncEsphomeZeroconf`: `/Users/bdraco/esphome/esphome/zeroconf.py:234`
- `DashboardImportDiscovery`: `/Users/bdraco/esphome/esphome/zeroconf.py:75`
- `DiscoveredImport`: `/Users/bdraco/esphome/esphome/zeroconf.py:62` (dataclass)

**Classification:** Public API (no underscore prefix, exported module-level symbols)
**Test coverage:**
- `AsyncEsphomeZeroconf`: Covered in `/Users/bdraco/esphome/tests/unit_tests/test_main.py` (mocked)
- `DiscoveredImport`: Covered in `/Users/bdraco/esphome/tests/dashboard/test_web_server.py`
- `DashboardImportDiscovery`: **No test coverage found** — internal to dashboard

**Suggested entry-point comments:**
```
# esphome/zeroconf.py:234 — device-builder discovers devices via ESPHome's mDNS announcements
# esphome/zeroconf.py:62 — device-builder parses device metadata from mDNS advertisements
```

---

## esphome.components.dashboard_import

### Symbols
- `import_config(path, name, friendly_name, project_name, import_url, network, encryption)` — *Create a new device config from an import URL*

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/devices.py:15` — initialize imported device YAML

**Upstream definition:** `/Users/bdraco/esphome/esphome/components/dashboard_import/__init__.py:83`
**Classification:** Public API (no underscore; component __init__ export)
**Test coverage:** **No test coverage found** — function is dashboard-internal
**Suggested entry-point comment:**
```
# esphome/components/dashboard_import/__init__.py:83 — device-builder creates device configs from dashboard imports
```

---

## esphome.components.esp32 (const)

### Symbols
- `VARIANTS` — *List of supported ESP32 hardware variants (esp32, esp32-s3, etc.)*

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/firmware.py:22` — enumerate supported boards for UI

**Upstream definition:** `/Users/bdraco/esphome/esphome/components/esp32/const.py:30` (imported into `/Users/bdraco/esphome/esphome/components/esp32/__init__.py:82`)
**Classification:** Public API (const submodule, no underscore)
**Test coverage:** Implicitly tested via component tests; no direct coverage
**Suggested entry-point comment:**
```
# esphome/components/esp32/const.py:30 — device-builder lists available ESP32 variants for firmware selection
```

---

## esphome.dashboard.util.text — **BLOCKING DEPENDENCY**

### Symbols
- `friendly_name_slugify(value)` — *Convert friendly device name to URL-safe slug with dashes*

**Usage sites:**
- `/Users/bdraco/device-builder/esphome_device_builder/controllers/devices.py:16` — slugify device names for filesystem paths

**Upstream definition:** `/Users/bdraco/esphome/esphome/dashboard/util/text.py:6`
**Classification:** Internal (part of `esphome.dashboard.*`, marked for removal)
**Test coverage:** **No test coverage found**
**Problem:** Legacy dashboard is being removed; this utility should be moved or inlined.

**Analysis:**
The function is a thin wrapper around `esphome.helpers.slugify()`:
```python
def friendly_name_slugify(value: str) -> str:
    return slugify(value).replace("_", "-")
```

**Remediation options:**
1. **Move to stable module** (preferred): Add to `esphome.helpers` since it's a pure-Python text utility with no dashboard dependencies
2. **Inline locally** (fallback): Device-builder can copy the 3-line implementation directly

**Recommended path:** Move `friendly_name_slugify` to `esphome.helpers` as a public function; it's dashboard-independent and low-risk.

---

## Suggested Upstream PR Shape

### Tests to Add
- `/Users/bdraco/esphome/tests/unit_tests/test_zeroconf.py`: Add unit test for `DashboardImportDiscovery` state transitions
- `/Users/bdraco/esphome/tests/unit_tests/test_dashboard_import.py`: Add integration test for `import_config()` YAML generation
- `/Users/bdraco/esphome/tests/unit_tests/test_helpers.py`: Add test for a new `friendly_name_slugify()` if moved to helpers

### Comments to Add (Entry-point notices)
- `/Users/bdraco/esphome/esphome/storage_json.py` (top-level docstring or class comment): "Used by device-builder for device metadata cache."
- `/Users/bdraco/esphome/esphome/zeroconf.py:62` (DiscoveredImport): "Used by device-builder to parse mDNS advertisements for device discovery."
- `/Users/bdraco/esphome/esphome/zeroconf.py:234` (AsyncEsphomeZeroconf): "Used by device-builder for real-time device network discovery."
- `/Users/bdraco/esphome/esphome/helpers.py`: Add `friendly_name_slugify()` with note "Used by device-builder; moved from dashboard.util.text to enable dashboard removal."

### Migration Steps
1. Add `friendly_name_slugify()` to `esphome.helpers` (copy from dashboard.util.text, update docstring)
2. Update device-builder to import from `esphome.helpers` instead of `esphome.dashboard.util.text`
3. Add unit test for the new helpers function
4. Deprecate `esphome.dashboard.util.text.friendly_name_slugify()` for one release, then remove

---

## Summary Table

| Module | Symbol | Classification | Test Coverage | Usage Count | Risk Level |
|--------|--------|-----------------|---------------|------------|-----------|
| esphome.const | `__version__` | Public | Implicit | 2 | ✅ Low |
| esphome.core | `CORE` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.helpers | `get_bool_env` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.helpers | `sort_ip_addresses` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.storage_json | `StorageJSON` | Public | ✅ Covered | 5 | ✅ Low |
| esphome.storage_json | `ext_storage_path` | Public | ✅ Covered | 3 | ✅ Low |
| esphome.storage_json | `ignored_devices_storage_path` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.util | `get_serial_ports` | Public | ✅ Covered (mocked) | 1 | ✅ Low |
| esphome.util | Module | Public | Partial | 1 | ✅ Low |
| esphome.yaml_util | Module | Public | ✅ Covered | 3 | ✅ Low |
| esphome.config_validation | Module | Public | ✅ Covered | 1 | ✅ Low |
| esphome.loader | Module | Public | Partial | 1 | ⚠️ Medium |
| esphome.zeroconf | `AsyncEsphomeZeroconf` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.zeroconf | `DiscoveredImport` | Public | ✅ Covered | 1 | ✅ Low |
| esphome.zeroconf | `DashboardImportDiscovery` | Public | ❌ None | 1 | ⚠️ Medium |
| esphome.components.dashboard_import | `import_config` | Public | ❌ None | 1 | ⚠️ Medium |
| esphome.components.esp32 | `VARIANTS` | Public | Implicit | 1 | ✅ Low |
| esphome.dashboard.util.text | `friendly_name_slugify` | **Internal** | ❌ None | 1 | 🚨 **BLOCKING** |
