"""
Devices controller package — public surface.

Re-exports ``DevicesController`` so existing
``from .controllers.devices import DevicesController`` imports keep
resolving after the subpackage split. Submodules:

- ``constants`` — module-level regexes and other static config.
- ``helpers`` — pure free functions (``_remove_device_sidecars``,
  ``_apply_featured_presets``, ``_build_address_cache_args``,
  ``friendly_name_slugify`` re-export).
- ``add_component`` — ``devices/add_component`` WS command
  body (featured-id resolution + manifest-driven preset
  merge + atomic YAML rewrite).
- ``api_key`` — Native API encryption-key resolver
  (in-process YAML loader fast path + ``esphome config``
  subprocess fallback).
- ``archive`` — archive / unarchive / delete helpers + the
  bulk-fan-out runner.
- ``firmware_sync`` — firmware-job → device-state sync helpers
  (post-flash hash refresh, deployed-hash sync, StorageJSON
  version write).
- ``importable`` — discovery / adoption helpers
  (``import_device``, ``toggle_ignore``, importable-cache
  callbacks, ignored-set load / save).
- ``logs`` — per-connection log streaming
  (``stream_logs``, ``stop_stream``) plus the shared
  ``stream_subprocess`` helper that ``validate_config``
  reuses.
- ``mutations_clone`` — ``devices/clone`` WS command body
  (duplicate an existing YAML under a fresh hostname,
  fresh friendly_name, and fresh API encryption key).
- ``mutations_create`` — ``devices/create`` WS command body
  (the wizard's three-flow YAML creation: file_content /
  template / stub).
- ``mutations_simple`` — the smaller four mutation WS
  commands: ``devices/update``, ``devices/set_labels``,
  ``devices/rename``, ``devices/edit_friendly_name``.
- ``mutations_yaml`` — shared helpers for the device-mutation
  WS commands (``yaml_content_for_create`` provenance pick,
  ``validate_rewritten_yaml_or_raise`` editor-validate gate).
- ``metadata`` — ``DeviceMetadataBase`` carrying
  ``_resolve_device_metadata`` /
  ``_derive_board_id_from_yaml`` /
  ``_persist_device_metadata_async`` /
  ``_delete_device_metadata`` /
  ``_clear_volatile_device_metadata``. Inherits from
  ``controllers._device_builder_base.DeviceBuilderBase`` so
  ``self._db`` comes via ``super().__init__(device_builder)``;
  ``DevicesController`` inherits ``DeviceMetadataBase``
  linearly, no mixin protocol.
- ``reachability`` — per-device reachability streaming + the
  on-subscription mDNS A-record refresh loop.
- ``scan_change`` — scan-handler backbone: forwards ADDED /
  UPDATED / REMOVED scanner changes onto the event bus and
  fans out side effects (mDNS probe, regen scheduling,
  reachability cleanup).
- ``search`` — ``yaml/search`` WS command body
  (substring-search every device's raw YAML; cache-backed).
- ``state_callbacks`` — the six per-attribute mDNS callbacks
  (state / ip / version / mac / api_encryption / config_hash).
- ``validate`` — ``devices/validate`` WS command body
  (``esphome config`` subprocess + ANSI-conceal-SGR redaction).
- ``storage_regen`` — background ``--only-generate`` scheduler
  + the disk-stamp guard that keeps it from looping on a
  broken YAML.
- ``controller`` — ``DevicesController`` itself + the scan / state
  / MQTT bridge. Hosts thin bound-method delegates that the
  WS dispatch and the per-concern submodules call into.
"""

from __future__ import annotations

from .controller import DevicesController
from .helpers import friendly_name_slugify

__all__ = ["DevicesController", "friendly_name_slugify"]
