"""``devices/import_bundle`` WS command body.

Lands an ``esphome bundle`` archive (``.esphomebundle.tar.gz``) as a
device: the main YAML plus its ``!include``s, local external components,
and a merged ``secrets.yaml``. Two-phase by design: the first call
reports any on-disk files the bundle would overwrite so the user picks
which to replace, then re-submits with ``overwrite`` set.
"""

from __future__ import annotations

import base64
import binascii
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import EsphomeError
from esphome.helpers import write_file as atomic_write_file

from ...constants import SECRETS_FILENAME
from ...helpers.api import CommandError
from ...helpers.device_yaml import configuration_stem, parse_platform_from_yaml
from ...helpers.secrets_state import merge_secrets_file
from ...helpers.yaml import read_yaml_scalar
from ...models import ErrorCode, ImportBundleResponse
from .helpers import _validate_archive_configuration
from .mutations_create import init_device_storage

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

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
    always merged, never reported as a conflict. On ``status="imported"``
    the response carries ``written`` (files placed) and ``kept`` (existing
    files the caller left untouched), so a partial import is never masked
    as a full one.
    """
    if overwrite is not None and (
        not isinstance(overwrite, list) or not all(isinstance(p, str) for p in overwrite)
    ):
        # The WS layer doesn't coerce JSON types, so a malformed
        # ``overwrite`` would otherwise reach ``set(...)`` and corrupt the
        # keep/replace decision.
        raise CommandError(ErrorCode.INVALID_ARGS, "overwrite must be a list of strings")

    config_dir = controller._db.settings.config_dir
    # Staging writes the bundle's files (secrets, !include fragments, packages,
    # external_components). Funnel through the shared lock + editor-cache drop so
    # any of those overwrites refreshes an open editor's lint, not just secrets.
    outcome = await controller._db.write_secrets_locked(
        _stage_bundle, file_content_b64, config_dir, overwrite
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
    # Overwriting a live device preserves its labels / comment / board_id.
    await controller._register_new_device(
        outcome.configuration,
        f"Import bundle {outcome.configuration}",
        clear_metadata=not outcome.main_existed,
    )
    return ImportBundleResponse(
        status="imported",
        configuration=outcome.configuration,
        conflicts=[],
        written=outcome.written,
        kept=outcome.kept,
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
    # True when the main config already existed (overwrite of a live
    # device); its metadata + StorageJSON are then preserved.
    main_existed: bool = False
    # Files placed (created or overwritten) and existing files left
    # untouched on the resolved pass. ``kept`` is non-empty exactly when
    # the import was partial, so the response can say so honestly.
    written: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


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
        main_existed = (config_dir / config_filename).exists()

        staging = tmp_path / "staging"
        try:
            extract_bundle(bundle_path, staging)
        except EsphomeError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, f"Couldn't extract bundle: {exc}") from exc

        # Refuse a non-text main config before anything is placed, so a
        # corrupt bundle is a clean error rather than a half-written tree
        # with a degraded sidecar.
        try:
            (staging / config_filename).read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"The bundle's main config {config_filename} isn't valid UTF-8 text.",
            ) from exc

        placements = [
            (rel, src)
            for src in sorted(staging.rglob("*"))
            if src.is_file() and (rel := src.relative_to(staging).as_posix()) != MANIFEST_FILENAME
        ]
        conflicts = sorted(
            rel for rel, _ in placements if rel != SECRETS_FILENAME and (config_dir / rel).exists()
        )
        if conflicts and overwrite is None:
            return _Outcome(
                configuration=config_filename,
                conflicts=conflicts,
                has_secrets=manifest.has_secrets,
                esphome_version=manifest.esphome_version,
                main_existed=main_existed,
            )

        written: list[str] = []
        kept: list[str] = []
        for rel, src in placements:
            dest = config_dir / rel
            if rel == SECRETS_FILENAME:
                merge_secrets_file(src, dest)
                continue
            # A conflicting file the user didn't pick is left as-is; record
            # it so the caller can tell a partial import from a full one.
            if dest.exists() and rel not in overwrite_set:
                kept.append(rel)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_file(dest, src.read_bytes())
            written.append(rel)

        # A new device gets a fresh sidecar; overwriting a live one keeps
        # its existing StorageJSON (build state) and dashboard metadata.
        if not main_existed:
            _init_bundle_storage(config_dir, config_filename)
        return _Outcome(
            configuration=config_filename,
            conflicts=None,
            written=sorted(written),
            kept=sorted(kept),
            has_secrets=manifest.has_secrets,
            esphome_version=manifest.esphome_version,
            main_existed=main_existed,
        )


def _decode_bundle(file_content_b64: str) -> bytes:
    """Base64-decode the upload; reject non-base64, oversize, or non-gzip."""
    limit_mb = _MAX_BUNDLE_UPLOAD_BYTES // (1024 * 1024)
    oversize = CommandError(
        ErrorCode.INVALID_ARGS, f"Bundle exceeds the {limit_mb} MB upload limit."
    )
    # base64 inflates ~4/3, so reject an obviously-oversize payload by its
    # encoded length before materialising the decoded bytes in memory.
    if len(file_content_b64) > (_MAX_BUNDLE_UPLOAD_BYTES // 3 + 1) * 4:
        raise oversize
    try:
        raw = base64.b64decode(file_content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, "Bundle upload isn't valid base64.") from exc
    if len(raw) > _MAX_BUNDLE_UPLOAD_BYTES:
        raise oversize
    if raw[:2] != b"\x1f\x8b":
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            "Upload isn't a .tar.gz bundle (missing gzip header).",
        )
    return raw


def _init_bundle_storage(config_dir: Path, config_filename: str) -> None:
    """Write a fresh StorageJSON sidecar from the imported config's own fields."""
    try:
        content = (config_dir / config_filename).read_text("utf-8")
    except (OSError, UnicodeDecodeError) as err:
        # Safety net only: the main config was validated as UTF-8 before
        # placement, so this is unreachable in the normal flow. Skip seeding
        # rather than persist a degraded (empty-platform) sidecar or crash
        # an import whose files already landed; the scanner derives the
        # metadata from the YAML on its next pass.
        _LOGGER.warning("Couldn't read %s to seed its sidecar (%s); skipping", config_filename, err)
        return
    friendly = read_yaml_scalar(content, ("esphome", "friendly_name"))
    if friendly and "${" in friendly:
        friendly = None
    platform, _pio_board, _variant = parse_platform_from_yaml(content)
    init_device_storage(config_filename, configuration_stem(config_filename), friendly, platform)
