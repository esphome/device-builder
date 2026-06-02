"""Construct Device models from YAML + StorageJSON, with package resolution."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

from esphome import const, yaml_util
from esphome.const import CONF_PACKAGES
from esphome.core import EsphomeError
from esphome.storage_json import StorageJSON

from ...models import Device, DeviceState
from ..mac_addresses import derive_interface_macs
from ..storage_path import resolve_storage_path
from ._parsing import (
    _extract_resolved_substitutions,
    _is_valid_esphome_name,
    _pick_meta,
    config_has_top_level_block,
    configuration_stem,
    detect_platform_from_yaml,
    extract_directly_referenced_integrations,
    extract_esphome_meta_from_config,
    get_api_encryption_block,
    parse_esphome_meta,
    yaml_has_api_encryption,
    yaml_has_top_level_block,
)

# Prefer the upstream single-call seam when present (the
# ``resolve_packages`` proposal landing as esphome/esphome#16235).
# Fall back to the two-step ``do_packages_pass`` + ``merge_packages``
# that ESPHome's own ``validate_config`` pipeline strings together
# today. Both imports are guarded by ``try/except ImportError``:
# a future esphome that ships only ``resolve_packages`` (deprecating
# or moving the two-step helpers) would otherwise break our module-
# load. Once the dashboard's dep floor moves past the release that
# shipped ``resolve_packages``, the entire fallback path can be
# deleted in one commit.
try:
    from esphome.components.packages import (  # type: ignore[attr-defined]
        resolve_packages as _resolve_packages,
    )
except ImportError:
    _resolve_packages = None

try:
    from esphome.components.packages import (
        do_packages_pass as _do_packages_pass,
    )
    from esphome.components.packages import (
        merge_packages as _merge_packages,
    )
except ImportError:
    _do_packages_pass = None  # type: ignore[assignment]
    _merge_packages = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device construction
# ---------------------------------------------------------------------------


def load_device_from_storage(
    path: Path,
    board_id: str = "",
    ip: str = "",
    expected_config_hash: str = "",
    mac_address: str = "",
    build_size_bytes: int = 0,
    labels: tuple[str, ...] = (),
    *,
    deployed_config_hash: str = "",
    deployed_version: str = "",
    api_encryption_active: str | None = None,
    previous: Device | None = None,
) -> Device:
    """
    Build a Device model from a YAML config file and its StorageJSON.

    User-editable fields (name / friendly_name / comment) come from the
    YAML when present so the dashboard reflects edits immediately,
    without having to wait for the next compile to refresh StorageJSON.

    *ip* is the last-known resolved address from the device-builder
    metadata sidecar. Loading it back on startup lets the OTA address
    cache hand the CLI a usable IP before the first ping/mDNS sweep.

    *expected_config_hash* is the YAML's last-compiled config hash,
    typically read back from the metadata sidecar. Pair with the
    deployed hash from mDNS to tell "device runs the latest compile"
    apart from "device has older firmware"; empty when the device
    hasn't been compiled yet, in which case ``has_pending_changes``
    falls back to the mtime check.

    *mac_address* is the canonical ``XX:XX:XX:XX:XX:XX`` MAC
    observed in the device's mDNS ``mac`` TXT (normalized at
    ingest), persisted to the sidecar so the drawer / table
    render the address immediately on startup — ESPHome devices
    stay mDNS-silent until probed, and the sidecar bridges the gap
    until the discovery sweep prompts a fresh announcement.

    *build_size_bytes* is the cached total size of the per-device
    ``.esphome/build/<name>/`` tree at the freshness pair
    (build-dir mtime + ``build_info.json`` mtime) captured by the
    last walk. Threaded through the scanner so the drawer / table
    render the size immediately on startup; recomputation is
    driven by :class:`BuildSizeRefresher` (the single-worker
    refresh queue) gated on the freshness-pair equality check, so
    a steady-state re-scan stays off the heavy I/O path.

    *labels* is the per-device list of label IDs (opaque
    ``uuid.uuid4().hex`` references into the global ``_labels``
    catalog at ``.device-builder.json``). The metadata round-trip
    is the source of truth, so a reload triggered by an unrelated
    YAML edit picks up label changes the same way it picks up
    board-id edits.

    *previous* is the prior in-memory Device for this path, when one
    exists. Runtime-only fields populated by monitors (``state``,
    ``deployed_config_hash``, ``ip_addresses``,
    ``api_encryption_active``) carry forward from it so a reload
    doesn't wipe what mDNS / ping has already discovered.
    """
    filename = path.name
    storage = StorageJSON.load(resolve_storage_path(filename))

    try:
        yaml_content = path.read_text(encoding="utf-8")
    except OSError:
        yaml_content = ""
    # Full resolved config (``!include`` / packages / ``!secret``
    # expanded) drives the api-encryption flag — a bare regex on raw
    # YAML would miss configs that pull the api block in via include
    # or split it across packages. ``None`` on parse failure is fine;
    # ``api_encrypted`` falls back to False.
    resolved_config = load_device_yaml(path)
    # Feed the merged ``substitutions:`` from the resolved config back
    # into the meta readers so ``esphome.friendly_name: $room`` resolves
    # against substitutions contributed by ``packages:`` / ``!include``
    # — not just the ones inline in this file (#917).
    extra_subs = _extract_resolved_substitutions(resolved_config)
    yaml_meta = parse_esphome_meta(yaml_content, extra_substitutions=extra_subs)
    # The resolved config carries the ``esphome:`` block the raw-text
    # reader can't see when it lives in a merge-key / ``!include`` /
    # ``packages:`` file, so this fills meta the main file omits (#1153).
    cfg_meta = extract_esphome_meta_from_config(resolved_config, extra_subs)

    fallback_name = configuration_stem(filename)
    storage_name = storage.name if storage else None
    # Pick the first valid ESPHome slug from (yaml_name, storage_name).
    # Real ESPHome ``esphome.name`` values are ``[a-z0-9-]+`` — a parsed
    # value with dots / spaces / uppercase is something else (a package
    # id like ``ratgdo.esphome``, a friendly_name leaked through, etc.).
    # ``device.name`` is the key the state monitor uses to match mDNS
    # announcements, so duplicates across multiple YAMLs (which is what
    # happens when several configs share the same ``dashboard_import``
    # package) collapse all those devices onto a single Device row.
    # Falling back to the filename when the parsed name is invalid
    # keeps the catalog key unique.
    name = next(
        (
            n
            for n in (yaml_meta.name, cfg_meta.name, storage_name)
            if n and _is_valid_esphome_name(n)
        ),
        fallback_name,
    )

    storage_friendly = storage.friendly_name if storage else None
    ef_friendly = _pick_meta(yaml_meta.friendly_name, cfg_meta.friendly_name, storage_friendly)
    friendly_name = ef_friendly if ef_friendly is not None else name

    storage_comment = storage.comment if storage else None
    comment = _pick_meta(yaml_meta.comment, cfg_meta.comment, storage_comment)

    # ``StorageJSON`` carries ``area`` on esphome builds that persist it
    # (resolved post-compile); the resolved-config and raw-YAML tokens
    # cover older builds and never-compiled devices.
    storage_area = getattr(storage, "area", None) if storage else None
    area = _pick_meta(yaml_meta.area, cfg_meta.area, storage_area) or ""

    yaml_mtime = path.stat().st_mtime if path.exists() else None
    bin_mtime: float | None = None
    if storage and storage.firmware_bin_path and storage.firmware_bin_path.exists():
        bin_mtime = storage.firmware_bin_path.stat().st_mtime

    # Carry-forward: in-memory *previous* wins over the store kwarg
    # (an apply since the last scan is fresher than disk).
    if previous is not None:
        deployed_config_hash = previous.deployed_config_hash
        deployed_version = previous.deployed_version
        api_encryption_active = previous.api_encryption_active
    state = previous.state if previous else DeviceState.UNKNOWN
    ip_addresses = list(previous.ip_addresses) if previous else []

    has_pending = compute_has_pending_changes(
        yaml_mtime=yaml_mtime,
        bin_mtime=bin_mtime,
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
    )

    update_available = bool(deployed_version and deployed_version != const.__version__)

    # ``Device.target_platform`` is the lowercase platform *key*
    # (``esp32``, ``esp8266``, ``rp2040``, …) — the value the
    # frontend's PLATFORM column renders. Source order:
    #
    # 1. ``storage.core_platform`` — post-codegen ground truth
    #    written by upstream's ``StorageJSON.from_esphome_core``
    #    (esphome#9028, 2025.6+). Always the lowercase platform
    #    key, never the chip variant. ``getattr`` with a default
    #    keeps this working on older ``esphome`` installs where
    #    ``StorageJSON`` doesn't carry the attribute at all —
    #    pyproject's floor is ``esphome>=2024.1.0`` so the
    #    pre-#9028 path is reachable.
    # 2. ``detect_platform_from_yaml`` — the YAML's top-level
    #    platform key, also lowercase. Picks up never-compiled
    #    devices and pre-2025.6 ``StorageJSON`` files that don't
    #    carry ``core_platform`` yet.
    #
    # ``storage.target_platform`` is deliberately NOT a fallback:
    # upstream uppercases it AND resolves it to the chip variant
    # for ESP32 boards (``ESP32S3``, ``ESP32C3``), so a fleet with
    # mixed compile states would otherwise show ``esp32s3`` next
    # to ``esp32`` for the same family — the inconsistency
    # frontend issue #137 was opened against. Chip-variant info
    # for ``_verify_chip`` is read from StorageJSON directly at
    # the call site, where the variant *is* the right level of
    # detail.
    target_platform = ""
    if storage:
        core_platform = getattr(storage, "core_platform", None)
        if core_platform:
            target_platform = core_platform.lower()
    if not target_platform:
        target_platform = detect_platform_from_yaml(yaml_content, resolved_config)

    loaded_integrations = sorted(storage.loaded_integrations) if storage else []
    # Subset of loaded_integrations the user directly wrote — top-
    # level keys + ``- platform:`` stems. Frontend's device-drawer
    # splits the loaded list into "direct" (these) and "indirect"
    # (the rest, all auto-loaded dependencies). Resolved config is
    # the right source so package contents are direct (the user
    # imported them); falls back to the empty list when resolved
    # config is unavailable so the frontend can render the flat
    # loaded list as a graceful degrade. Issue #422.
    user_referenced = set(extract_directly_referenced_integrations(resolved_config))
    directly_referenced_integrations = [
        name for name in loaded_integrations if name in user_referenced
    ]
    # ``api_enabled`` / ``api_encrypted`` get the union of every signal
    # we have:
    #   1. Resolved YAML config — catches local ``api:`` blocks pulled
    #      in via ``!include`` / local packages.
    #   2. Raw-text scan — keeps the indicator stable mid-edit when
    #      ``yaml_util.load_yaml`` fails on an invalid draft.
    #   3. ``StorageJSON.loaded_integrations`` — the compile-time
    #      ground truth. Required for configs that pull the api block
    #      in from a remote ``dashboard_import`` package (Apollo, etc.):
    #      ``yaml_util.load_yaml`` doesn't fetch URLs so the resolved
    #      config has no ``api:`` at the top level, but the compiled
    #      device still loads it.
    api_enabled = (
        ("api" in loaded_integrations)
        or config_has_top_level_block(resolved_config, "api")
        or yaml_has_top_level_block(yaml_content, "api")
    )
    # Derived interface MACs: deterministic from
    # ``mac_address`` + ``target_platform`` + ``loaded_integrations``,
    # so we recompute on construction rather than persisting in the
    # sidecar — a YAML edit that toggles bluetooth picks up the new
    # derived MAC on the next reload, no stale-cache window.
    ethernet_mac, bluetooth_mac = derive_interface_macs(
        mac_address, target_platform, loaded_integrations
    )
    # ``api_encrypted`` mirrors the same union shape as ``api_enabled``:
    #
    #   1. Resolved YAML config — primary source for local
    #      ``!include`` / package-merged encryption blocks.
    #   2. Raw-text scan — keeps the indicator stable mid-edit
    #      when ``yaml_util.load_yaml`` fails on an invalid draft.
    #   3. Live mDNS broadcast (``api_encryption_active`` truthy)
    #      — authoritative when the YAML pass diverges from the
    #      running firmware. ESPHome's compile pipeline runs the
    #      Jinja preprocessor over packages before YAML parsing
    #      (``api: |\n  # set ns = ...  ${ns.cfg}``), the
    #      dashboard's ``yaml_util.load_yaml`` doesn't, so the
    #      YAML pass can come back ``False`` for a fully-
    #      encrypted device. The wire signal is the truthful
    #      one; without this the dashboard mislabels the device
    #      as plaintext (issue #437) and hides the
    #      "Show API key" affordance even though encryption is
    #      live on the firmware.
    api_encrypted = (
        get_api_encryption_block(resolved_config) is not None
        or yaml_has_api_encryption(yaml_content)
        or bool(api_encryption_active)
    )
    return Device(
        name=name,
        friendly_name=friendly_name,
        configuration=filename,
        comment=comment,
        area=area,
        board_id=board_id,
        target_platform=target_platform,
        # StorageJSON only exists after a successful compile, so a
        # freshly-added (or never-built) device would otherwise carry
        # an empty ``address`` and fall out of the ping sweep — stuck
        # in UNKNOWN forever. Fall back to ``<filename-stem>.local``
        # (NOT ``<name>.local``): the filename is canonical and
        # matches what the user types, while ``name`` is parsed from
        # YAML and can come back as a friendly_name or a package
        # import URL when the YAML doesn't carry a slug-shaped
        # ``esphome.name`` (e.g. configs that get the name from a
        # remote ``dashboard_import`` package). The scanner refreshes
        # this on the next compile if the device picks a different
        # ``esphome.address``.
        address=(storage.address if storage and storage.address else f"{fallback_name}.local"),
        ip=ip,
        ip_addresses=ip_addresses,
        web_port=storage.web_port if storage else None,
        current_version=const.__version__,
        deployed_version=deployed_version,
        expected_config_hash=expected_config_hash,
        deployed_config_hash=deployed_config_hash,
        loaded_integrations=loaded_integrations,
        directly_referenced_integrations=directly_referenced_integrations,
        state=state,
        has_pending_changes=has_pending,
        update_available=update_available,
        # ``uses_mqtt`` keeps its prior shape — the resolved config
        # wins, raw-text fills in mid-edit, and we don't have a
        # ``loaded_integrations`` entry that maps cleanly to "uses
        # mqtt for dashboard discovery" the way ``"api"`` does.
        uses_mqtt=(
            config_has_top_level_block(resolved_config, "mqtt")
            if resolved_config is not None
            else yaml_has_top_level_block(yaml_content, "mqtt")
        ),
        api_enabled=api_enabled,
        api_encrypted=api_encrypted,
        api_encryption_active=api_encryption_active,
        mac_address=mac_address,
        ethernet_mac=ethernet_mac,
        bluetooth_mac=bluetooth_mac,
        build_size_bytes=build_size_bytes,
        labels=list(labels),
    )


def compute_has_pending_changes(
    *,
    yaml_mtime: float | None,
    bin_mtime: float | None,
    expected_config_hash: str,
    deployed_config_hash: str,
) -> bool:
    """
    Decide whether a device's running firmware is out of sync with its YAML.

    Decision order, first match wins:

    1. Both ``expected_config_hash`` and ``deployed_config_hash``
       known → pending iff they differ. The deployed hash comes from
       mDNS (esphome/esphome#16145), the expected hash is read from
       ``build_info.json`` (firmware-canonical, post-codegen). The
       hash comparison is authoritative for any device on
       broadcast-capable firmware: if they match, the running
       firmware is built from the same logical config the YAML now
       resolves to, even if the YAML's mtime ticked forward
       (whitespace-only / comment-only edits, ``--only-generate``
       rewriting StorageJSON, etc.) or the local ``firmware.bin``
       was wiped (``clean`` job, fresh checkout, ``--only-generate``
       only). If they differ, the device is running older firmware
       than the latest compile — failed OTA, flashed elsewhere, etc.
    2. Hashes aren't both known and there's no firmware binary on
       disk yet → pending. We don't have a comparable pair of
       hashes (either the YAML's never been compiled or the device
       isn't broadcasting), and we don't even have a local artefact
       to mtime-check against, so we definitionally have unflushed
       edits.
    3. YAML edited after the last compile → pending. Mtime is the
       fallback for devices that pre-date the ``config_hash`` TXT
       broadcast or for the brief window between an edit and the
       background ``--only-generate`` updating
       ``expected_config_hash``.
    4. Otherwise → not pending. Devices on firmware that predates
       the broadcast and haven't been edited stay quiet.
    """
    if expected_config_hash and deployed_config_hash:
        return expected_config_hash != deployed_config_hash
    if bin_mtime is None:
        return True
    return yaml_mtime is not None and yaml_mtime > bin_mtime


def load_device_yaml(path: Path) -> dict | None:
    """Load *path* with ESPHome's YAML loader; return the top-level mapping.

    Resolves ``!secret`` / ``!include`` / etc. like a real compile,
    flattens any ``packages:`` block into the main config via
    ESPHome's package-resolution internals (the single-call
    ``resolve_packages`` on newer ESPHome, the two-step
    ``do_packages_pass`` + ``merge_packages`` fallback on older
    builds), so callers see what the compiler actually sees —
    ``api:`` / ``wifi:`` / target-platform blocks contributed by
    packages register as top-level keys here — and returns
    ``None`` when the file isn't a mapping or fails to parse.

    Package resolution is best-effort: a remote (git) package needs
    network access, an invalid package definition fails ESPHome's
    voluptuous validator, etc. When the package pass throws we keep
    the unmerged config rather than dropping it — better to surface
    the local YAML than nothing, and the unmerged shape was the
    pre-fix behaviour the dashboard already handled.

    Centralised so callers that need a parsed config — API-key
    extraction, encryption-status checks, top-level-block detection
    used by the device-card flags — share one entry point with the
    same error handling and the same package-merge contract.
    """
    try:
        # ``yaml_util.load_yaml`` calls ``.open()`` on its argument, so
        # pass the ``Path`` directly — handing it a stringified path
        # raises ``AttributeError`` deep inside the loader.
        config = yaml_util.load_yaml(path)
    except EsphomeError:
        return None
    if not isinstance(config, dict):
        return None
    # ``packages:`` is a separate pass in the ESPHome pipeline (see
    # ``do_packages_pass`` + ``merge_packages`` in
    # ``esphome.config.validate_config``): packages need to be loaded
    # and merged so blocks they contribute (api / wifi / target-platform
    # / …) become top-level keys. Without this step a config that
    # puts those blocks behind ``packages:`` comes back from
    # ``yaml_util.load_yaml`` with everything still nested under
    # ``packages:``, and the dashboard's flag detection silently
    # misses them. We delegate to ESPHome internals — the loader
    # and merge algorithm live upstream.
    #
    # ``load_device_yaml`` runs on a worker thread (the device
    # scanner uses ``run_in_executor``; the devices controller
    # threads it through too), NOT the WS event loop. Same trade
    # the validate path lives with: ESPHome's ``vscode`` subprocess
    # runs the full resolve on every validate call, so a
    # remote-package config that fires ``git clone`` synchronously
    # blocks this worker thread for as long as the clone takes
    # (minutes on a slow connection / large repo) but the dashboard
    # stays responsive to other clients. The follow-up that mirrors
    # the validate-style subprocess pattern for metadata refresh
    # (so a slow remote clone doesn't stall the whole scan) is
    # tracked separately; this PR closes the local-package gap
    # behind #288 without trying to solve the remote-package
    # latency problem at the same time.
    if isinstance(config.get(CONF_PACKAGES), (dict, list)):
        try:
            if _resolve_packages is not None:
                config = _resolve_packages(config)
            elif _do_packages_pass is not None and _merge_packages is not None:
                config = _do_packages_pass(config)
                config = _merge_packages(config)
        except Exception:
            # Best-effort: a bad / unreachable package shouldn't
            # blank the device's metadata. Keep the unmerged shape
            # so the raw-YAML fallback paths at the call sites
            # still work.
            _LOGGER.debug(
                "Package merge failed for %s; using unmerged config",
                path,
                exc_info=True,
            )
    # ``config`` came from ``yaml_util.load_yaml`` (esphome,
    # untyped) and was further passed through the package-merge
    # helpers (also untyped), so it stays ``Any`` despite the
    # ``isinstance(config, dict)`` narrowing earlier. Cast at the
    # return so the public ``dict | None`` signature is honest
    # without forcing every caller to re-narrow on receive.
    return cast("dict[Any, Any] | None", config)
