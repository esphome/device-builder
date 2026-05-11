"""
Locate + collect a build's flash artifacts on the dashboard's filesystem.

Wraps ESPHome's per-build manifest (``idedata.json``) into a
typed accessor that surfaces just the "files to flash" view
the dashboard needs. Used by the remote-build receiver-side
download path to pack the artifacts into a tarball, and by
future install-related flows that need to enumerate the same
set without re-running platformio.

Background:

ESPHome's compile pipeline writes two per-build records the
dashboard can read after the fact, both anchored on the
``CORE.data_dir`` deployment-mode logic (default mode at
``<config_dir>/.esphome/`` /  HA-addon mode at ``/data/`` /
``ESPHOME_DATA_DIR`` env override):

* ``<data_dir>/storage/<config>.json`` —
  :class:`esphome.storage_json.StorageJSON` sidecar with
  ``build_path`` + ``firmware_bin_path`` + framework /
  loaded-integrations metadata. Loaded via
  :func:`esphome.storage_json.ext_storage_path` +
  ``StorageJSON.load``.
* ``<data_dir>/idedata/<name>.json`` —
  :class:`esphome.platformio_api.IDEData` cache with the
  per-image flash offsets (``extra.flash_images``). esphome
  writes this on first compile + on every platformio.ini
  mtime bump.

The flash images that matter (platform-variation table):

* **ESP32**: ``bootloader.bin`` +
  ``partitions.bin`` + ``ota_data_initial.bin`` +
  ``firmware.bin`` (4 images at known offsets).
* **ESP8266**: just ``firmware.bin`` (eboot integrated).
* **Libretiny / RP2040**: ``firmware.bin`` (mass-storage
  install uses ``.uf2`` directly; out of scope here).
* **Native ESP-IDF**: similar to ESP32 with different
  paths inside ``build/`` rather than ``.pioenvs/``.

``idedata.extra_flash_images`` handles all of those —
platform-specific code lives upstream, we just consume the
manifest. ``StorageJSON.firmware_bin_path`` carries the
canonical "firmware image" path which is the same value
``idedata.firmware_bin_path`` reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from esphome.storage_json import StorageJSON

from .json import loads as json_loads
from .storage_path import resolve_idedata_path, resolve_storage_path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FlashArtifact:
    """One file to flash with its offset.

    ``offset`` is the offset string as it appears in
    ``idedata.json`` (typically lowercase hex with ``0x``
    prefix, e.g. ``"0x10000"``). The downstream consumer
    (esptool / Web Serial) wants strings — keeping them
    verbatim avoids the parse-and-stringify round-trip and
    matches the upstream call sites' shape.

    ``path`` is the absolute :class:`Path` on the
    dashboard's filesystem; the caller decides what to do
    with the bytes (stream them over a peer-link, hand them
    to local install, etc.).
    """

    path: Path
    offset: str


@dataclass(frozen=True)
class BuildArtifacts:
    """A built ESPHome config's flash artifacts + manifest.

    Bundles the discovered set: every file the install
    chain needs to flash a device, plus the
    ``idedata.json`` bytes for downstream consumers that
    want the upstream-canonical manifest (e.g. the
    remote-build offloader's frontend hands them to
    Web Serial / esptool with the same offsets the
    receiver-side install would have used).

    ``flash_images`` is ordered: ``firmware.bin`` first,
    then ``idedata.extra_flash_images`` in their declared
    order. This matches the order
    :func:`esphome.__main__.upload_using_esptool` constructs
    its flash-image list and is the order most install
    tools accept.

    ``idedata_bytes`` is the raw on-disk content of
    ``idedata.json``; consumers can ``json.loads`` it if
    they need a parsed view.
    """

    flash_images: list[FlashArtifact]
    idedata_bytes: bytes


def load_build_artifacts(configuration: str) -> BuildArtifacts:
    """Load and validate the flash-artifact set for *configuration*.

    *configuration* is the device-YAML filename (e.g.
    ``"kitchen.yaml"``) — the same handle the firmware
    controller and the rest of the dashboard use to refer
    to a device. The function resolves the matching
    :class:`StorageJSON` sidecar and ``idedata.json`` from
    ``CORE.data_dir``'s subdirectories, walks the
    flash-image manifest, and returns a typed
    :class:`BuildArtifacts` holding every existing image's
    path + offset plus the raw idedata bytes.

    Synchronous; meant to run inside an executor when called
    from an async context (every read here is a blocking
    filesystem operation).

    Raises :class:`FileNotFoundError` when:

    * The StorageJSON sidecar is missing (the YAML was
      never compiled, or its sidecar was cleaned up).
    * ``StorageJSON.firmware_bin_path`` is unset or points
      at a missing file (the build directory was wiped
      since compile completed).
    * ``idedata.json`` is missing for the device name
      (compile completed without writing the manifest —
      shouldn't happen in practice but a clean error here
      beats a silent half-result later).

    Raises :class:`ValueError` when ``idedata.json`` parses
    to something that isn't a JSON object (``null`` / list /
    scalar). Shouldn't happen in practice but keeps the
    ``.get("extra", ...)`` chain below from blowing up with
    ``AttributeError`` on a corrupt file — the caller's
    surface (e.g. ``pack_failed`` reject reason) is much more
    useful than an opaque traceback.

    Logs (at WARNING) and skips any
    :attr:`IDEData.extra_flash_images` entry whose path
    doesn't exist; matches upstream's
    :func:`esphome.__main__.upload_using_esptool` behaviour
    where a missing extra image is non-fatal (the platform
    declared it but the build target may not have emitted
    it).
    """
    storage_path = resolve_storage_path(configuration)
    storage = StorageJSON.load(storage_path)
    if storage is None:
        msg = f"StorageJSON sidecar missing for {configuration}: {storage_path}"
        raise FileNotFoundError(msg)
    if storage.firmware_bin_path is None:
        msg = f"firmware_bin_path unset in StorageJSON for {configuration}"
        raise FileNotFoundError(msg)
    firmware_bin = Path(storage.firmware_bin_path)
    if not firmware_bin.is_file():
        msg = f"firmware_bin_path missing for {configuration}: {firmware_bin}"
        raise FileNotFoundError(msg)

    idedata_path = resolve_idedata_path(configuration, name=storage.name)
    if not idedata_path.is_file():
        msg = f"idedata.json missing for {configuration}: {idedata_path}"
        raise FileNotFoundError(msg)
    idedata_bytes = idedata_path.read_bytes()
    idedata = json_loads(idedata_bytes)
    # Defensive dict-check — corrupt-but-parseable JSON
    # (``null`` / list / scalar) would otherwise blow up on
    # ``.get("extra", {})`` below with ``AttributeError``,
    # which the caller surfaces as an opaque ``pack_failed``.
    # Raising :class:`ValueError` here lets the receiver-side
    # ``ArtifactsDownloadSender`` catch it via the existing
    # ``Exception`` arm and surface a clean reject reason.
    if not isinstance(idedata, dict):
        msg = (
            f"idedata.json for {configuration} is not a JSON object (got {type(idedata).__name__})"
        )
        raise ValueError(msg)

    # ``firmware.bin`` is the only image consistently
    # reported by ``StorageJSON.firmware_bin_path`` upstream.
    # Its offset comes from idedata's ``flash_extra_images``
    # cousin — no, actually it's a separate field. Upstream's
    # ``upload_using_esptool`` derives it from
    # ``CORE.is_esp32`` (``0x10000``) vs ESP8266 (``0x0``).
    # Mirror that decision here to avoid pulling CORE into
    # the dashboard runtime — read ``target_platform`` from
    # the StorageJSON sidecar and pick the offset.
    firmware_offset = _firmware_offset_for_platform(storage.target_platform)
    flash_images: list[FlashArtifact] = [FlashArtifact(path=firmware_bin, offset=firmware_offset)]
    extra = idedata.get("extra")
    raw_flash_images = extra.get("flash_images", []) if isinstance(extra, dict) else []
    for entry in raw_flash_images:
        # Defensive isinstance gate — a corrupt-but-parseable
        # ``flash_images`` entry (string / null / nested array)
        # would otherwise blow up on ``.get("path")`` with
        # ``AttributeError``. Skip with a warning so a single
        # malformed entry doesn't fail the whole pack — matches
        # the existing "missing extra image is non-fatal" stance
        # below.
        if not isinstance(entry, dict):
            _LOGGER.warning(
                "skipping malformed flash image entry in idedata for %s: %r",
                configuration,
                entry,
            )
            continue
        path_str = entry.get("path")
        offset = entry.get("offset")
        if not path_str or not offset:
            continue
        extra_path = Path(path_str)
        if not extra_path.is_file():
            _LOGGER.warning(
                "skipping missing flash image declared in idedata for %s: %s",
                configuration,
                extra_path,
            )
            continue
        flash_images.append(FlashArtifact(path=extra_path, offset=offset))

    return BuildArtifacts(flash_images=flash_images, idedata_bytes=idedata_bytes)


def _firmware_offset_for_platform(target_platform: str) -> str:
    """Return the flash offset for the main firmware image on *target_platform*.

    ESP32 family devices flash the firmware partition at
    ``0x10000``; ESP8266 and everything else (libretiny,
    RP2040, native ESP-IDF where the image isn't a
    separately-flashed firmware) use ``0x0``. Matches
    :func:`esphome.__main__.upload_using_esptool`'s decision
    exactly so the offloader's install path can flash
    against the same offsets the receiver would have used
    locally.
    """
    if target_platform and target_platform.lower().startswith("esp32"):
        return "0x10000"
    return "0x0"
