"""``devices/import_bundle`` WS command body.

Lands an ``esphome bundle`` archive (``.esphomebundle.tar.gz``) as a
device: the main YAML plus its ``!include``s, local external components,
and a merged ``secrets.yaml``. Two-phase by design — the first call
reports any on-disk files the bundle would overwrite so the user picks
which to replace, then re-submits with ``overwrite`` set.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import EsphomeError
from esphome.helpers import write_file as atomic_write_file
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ...helpers.api import CommandError
from ...helpers.device_yaml import configuration_stem, parse_platform_from_yaml
from ...helpers.yaml import read_yaml_scalar
from ...models import ErrorCode, ImportBundleResponse
from .helpers import _validate_archive_configuration
from .mutations_create import init_device_storage

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

_SECRETS_FILENAME = "secrets.yaml"
# Compressed-upload cap. The 500 MB decompressed cap is enforced inside
# esphome.bundle.extract_bundle; this guards the base64 payload itself.
_MAX_BUNDLE_UPLOAD_BYTES = 64 * 1024 * 1024


async def import_bundle(
    controller: DevicesController,
    *,
    file_content_b64: str,
    overwrite: list[str] | None = None,
) -> ImportBundleResponse:
    """
    Import an ``esphome bundle`` archive as a device.

    Returns ``status="conflicts"`` (nothing written) when bundle files
    already exist and *overwrite* is ``None``; the caller re-submits the
    same bytes with the chosen paths in *overwrite*. ``secrets.yaml`` is
    always merged, never reported as a conflict.
    """
    config_dir = controller._db.settings.config_dir
    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        None, _stage_bundle, file_content_b64, config_dir, overwrite
    )
    if outcome.conflicts is not None:
        return ImportBundleResponse(
            status="conflicts",
            configuration=outcome.configuration,
            conflicts=outcome.conflicts,
            has_secrets=outcome.has_secrets,
            esphome_version=outcome.esphome_version,
        )

    # The YAML is on disk; register it the same way create_device does.
    await controller._register_new_device(
        outcome.configuration, f"Import bundle {outcome.configuration}"
    )
    return ImportBundleResponse(
        status="imported",
        configuration=outcome.configuration,
        conflicts=[],
        has_secrets=outcome.has_secrets,
        esphome_version=outcome.esphome_version,
    )


@dataclass
class _Outcome:
    """Result of the executor-side staging step.

    ``conflicts is None`` means the tree was placed; a list (possibly
    empty only on the resolved second pass) means nothing was written.
    """

    configuration: str
    conflicts: list[str] | None
    has_secrets: bool
    esphome_version: str


def _stage_bundle(file_content_b64: str, config_dir: Path, overwrite: list[str] | None) -> _Outcome:
    """Decode, extract to a temp dir, then plan or place the files (blocking)."""
    try:
        from esphome.bundle import (  # noqa: PLC0415
            MANIFEST_FILENAME,
            extract_bundle,
            read_bundle_manifest,
        )
    except ImportError as exc:  # pragma: no cover - pinned esphome ships bundle
        raise CommandError(
            ErrorCode.UNAVAILABLE,
            "This ESPHome version doesn't support config bundles.",
        ) from exc

    bundle_bytes = _decode_bundle(file_content_b64)
    overwrite_set = set(overwrite or [])

    with tempfile.TemporaryDirectory(prefix="esphb-import-") as tmp:
        tmp_path = Path(tmp)
        bundle_path = tmp_path / "upload.esphomebundle.tar.gz"
        bundle_path.write_bytes(bundle_bytes)

        try:
            manifest = read_bundle_manifest(bundle_path)
        except EsphomeError as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS, f"Not a valid ESPHome bundle: {exc}"
            ) from exc

        config_filename = manifest.config_filename
        _validate_archive_configuration(config_filename)

        staging = tmp_path / "staging"
        try:
            extract_bundle(bundle_path, staging)
        except EsphomeError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Couldn't extract bundle: {exc}") from exc

        placements = [
            (rel, src)
            for src in sorted(staging.rglob("*"))
            if src.is_file() and (rel := src.relative_to(staging).as_posix()) != MANIFEST_FILENAME
        ]
        conflicts = sorted(
            rel for rel, _ in placements if rel != _SECRETS_FILENAME and (config_dir / rel).exists()
        )
        if conflicts and overwrite is None:
            return _Outcome(
                configuration=config_filename,
                conflicts=conflicts,
                has_secrets=manifest.has_secrets,
                esphome_version=manifest.esphome_version,
            )

        for rel, src in placements:
            dest = config_dir / rel
            if rel == _SECRETS_FILENAME:
                _merge_secrets(src, dest)
                continue
            if dest.exists() and rel not in overwrite_set:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_file(dest, src.read_bytes())

        _init_bundle_storage(config_dir, config_filename)
        return _Outcome(
            configuration=config_filename,
            conflicts=None,
            has_secrets=manifest.has_secrets,
            esphome_version=manifest.esphome_version,
        )


def _decode_bundle(file_content_b64: str) -> bytes:
    """Base64-decode the upload; reject non-base64, oversize, or non-gzip."""
    try:
        raw = base64.b64decode(file_content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, "Bundle upload isn't valid base64.") from exc
    if len(raw) > _MAX_BUNDLE_UPLOAD_BYTES:
        limit_mb = _MAX_BUNDLE_UPLOAD_BYTES // (1024 * 1024)
        raise CommandError(
            ErrorCode.INVALID_ARGS, f"Bundle exceeds the {limit_mb} MB upload limit."
        )
    if raw[:2] != b"\x1f\x8b":
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "Upload isn't a .tar.gz bundle (missing gzip header).",
        )
    return raw


def _merge_secrets(src: Path, dest: Path) -> None:
    """Merge bundle secrets into *dest*, keeping existing keys; create if absent."""
    if not dest.exists():
        atomic_write_file(dest, src.read_bytes())
        return
    yaml = YAML()
    try:
        existing = yaml.load(dest.read_text("utf-8")) or {}
        incoming = yaml.load(src.read_text("utf-8")) or {}
    except (YAMLError, OSError, UnicodeDecodeError):
        # A non-YAML or tag-bearing secrets file; never risk clobbering
        # the user's secrets, so leave the existing file untouched.
        _LOGGER.warning("Couldn't parse secrets for merge; left %s untouched", dest)
        return
    if not isinstance(existing, dict) or not isinstance(incoming, dict):
        return
    added = False
    for key, value in incoming.items():
        if key not in existing:
            existing[key] = value
            added = True
    if not added:
        return
    buf = io.StringIO()
    yaml.dump(existing, buf)
    atomic_write_file(dest, buf.getvalue())


def _init_bundle_storage(config_dir: Path, config_filename: str) -> None:
    """Write a fresh StorageJSON sidecar from the imported config's own fields."""
    content = (config_dir / config_filename).read_text("utf-8", errors="replace")
    name = configuration_stem(config_filename)
    friendly = read_yaml_scalar(content, ("esphome", "friendly_name"))
    if friendly and "${" in friendly:
        friendly = None
    platform, _pio_board, _variant = parse_platform_from_yaml(content)
    init_device_storage(config_filename, name, friendly, platform)
