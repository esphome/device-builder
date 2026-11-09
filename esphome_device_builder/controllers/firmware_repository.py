"""Persistent repository for compiled device firmware."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from aiohttp import web
from esphome.const import __version__ as _installed_esphome_version
from esphome.storage_json import StorageJSON

from ..helpers.api import CommandError, api_command
from ..helpers.async_ import run_in_executor
from ..helpers.atomic_io import atomic_write, atomic_write_preserving_mode
from ..helpers.device_yaml import _UNRESOLVED_SUBSTITUTION_RE, detect_platform_from_yaml
from ..helpers.network_interfaces import resolve_bind_host
from ..helpers.paths import resolve_under_root
from ..helpers.storage_path import resolve_storage_path
from ..models import ErrorCode
from .devices.resolve import resolve_config, resolve_config_subprocess

_LOGGER = logging.getLogger(__name__)
_REPOSITORY_FILENAME = "repository.json"
_ALLOWLIST_FILENAME = ".device-builder-firmware-allowlist.json"
_UPDATE_PORT = 6070
_DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


class FirmwareRepositoryController:
    """Manage repository artifacts and serve public device updates."""

    def __init__(self, device_builder: Any) -> None:
        self._db = device_builder
        self.root = device_builder.settings.config_dir / "repos"
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._access_tokens: dict[str, dict[str, str]] = {}
        self._authenticate_public = True
        self._lock = asyncio.Lock()
        self._listener_lock = asyncio.Lock()
        self._index_loaded = False
        self._runner: web.AppRunner | None = None

    async def start(
        self, *, access_enabled: bool = False, authenticate_public: bool = True
    ) -> None:
        """Load repository metadata and bind the listener when enabled."""
        await run_in_executor(self._load_index)
        self._access_tokens = await run_in_executor(self._load_access_tokens_sync)
        self._index_loaded = True
        await self.configure_public_access(
            enabled=access_enabled, authenticate_public=authenticate_public
        )

    async def configure_public_access(self, *, enabled: bool, authenticate_public: bool) -> None:
        """Reconcile the listener and token-authentication preference."""
        async with self._listener_lock:
            self._authenticate_public = authenticate_public
            if not enabled:
                await self._stop_public_listener()
                return
            if self._runner is not None:
                return
            if not self._index_loaded:
                await run_in_executor(self._load_index)
                self._index_loaded = True
            await self._start_public_listener()

    async def _start_public_listener(self) -> None:
        app = web.Application()
        app.router.add_get("/update", self._http_update_manifest)
        app.router.add_get("/update/download", self._http_download)
        app.router.add_get("/update/firmware", self._http_firmware)
        app.router.add_get("/update/md5", self._http_md5)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            for host in resolve_bind_host(self._db.settings.host):
                site = web.TCPSite(runner, host, _UPDATE_PORT)
                await site.start()
                _LOGGER.info("Firmware update site listening on %s:%d", host, _UPDATE_PORT)
        except Exception:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        """Stop the unauthenticated update listener."""
        async with self._listener_lock:
            await self._stop_public_listener()

    async def _stop_public_listener(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @api_command("firmware_repository/list")
    async def list_firmwares(self, **kwargs: Any) -> list[dict[str, Any]]:
        async with self._lock:
            return await run_in_executor(self._list_sync)

    @api_command("firmware_repository/save")
    async def save_firmware(
        self,
        *,
        configuration: str,
        summary: str = "",
        release_url: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        firmware = self._db.firmware
        await firmware._validate_configuration_boundary(configuration)
        devices = self._db.devices
        config, resolved = await resolve_config(
            devices, self._db.settings.config_dir / configuration
        )
        if resolved and config is not None and _has_unresolved_repository_identity(config):
            config = await resolve_config_subprocess(
                devices, self._db.settings.config_dir / configuration
            )
            resolved = config is not None
        if not resolved or config is None:
            raise CommandError(ErrorCode.UNAVAILABLE, "Could not fully resolve device YAML")
        downloads = await firmware.get_binaries(configuration=configuration)
        artifacts = {
            item["type"]: item["file"]
            for item in downloads
            if item.get("type") in {"ota", "factory"}
        }
        async with self._lock:
            try:
                return await run_in_executor(
                    self._save_sync, configuration, config, artifacts, summary, release_url
                )
            except FileExistsError as err:
                raise CommandError(ErrorCode.ALREADY_EXISTS, str(err)) from err
            except (OSError, TypeError, ValueError) as err:
                raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err

    @api_command("firmware_repository/publish")
    async def publish_firmware(self, *, device: str, version: str, **kwargs: Any) -> dict[str, Any]:
        try:
            _validate_key(device, version)
        except ValueError as err:
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
        async with self._lock:
            try:
                return await run_in_executor(self._publish_sync, device, version)
            except KeyError as err:
                raise CommandError(ErrorCode.NOT_FOUND, "Firmware version not found") from err
            except OSError as err:
                raise CommandError(ErrorCode.INTERNAL_ERROR, "Could not publish firmware") from err

    @api_command("firmware_repository/delete")
    async def delete_firmware(self, *, device: str, version: str, **kwargs: Any) -> None:
        try:
            _validate_key(device, version)
        except ValueError as err:
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
        async with self._lock:
            try:
                await run_in_executor(self._delete_sync, device, version)
            except KeyError as err:
                raise CommandError(ErrorCode.NOT_FOUND, "Firmware version not found") from err
            except OSError as err:
                raise CommandError(ErrorCode.INTERNAL_ERROR, "Could not delete firmware") from err

    async def rename_configuration(
        self,
        old_configuration: str,
        new_configuration: str,
        *,
        new_esphome_name: str | None = None,
        new_friendly_name: str | None = None,
    ) -> None:
        old_device = Path(old_configuration).stem
        new_device = Path(new_configuration).stem
        _validate_key(new_device, "rename")
        async with self._lock:
            await run_in_executor(
                self._rename_configuration_sync,
                old_device,
                new_device,
                new_esphome_name,
                new_friendly_name,
            )

    async def list_access_tokens(self) -> list[dict[str, str]]:
        """List token labels and display hints without exposing credentials."""
        async with self._lock:
            return [
                {key: value for key, value in entry.items() if key != "digest"}
                for entry in sorted(self._access_tokens.values(), key=lambda item: item["id"])
            ]

    async def add_access_token(self, *, name: str) -> dict[str, str]:
        """Create a token and return its secret once with its display metadata."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("name must contain 1 to 80 characters")
        token = secrets.token_urlsafe(32)
        token_id = secrets.token_urlsafe(9)
        entry = {
            "id": token_id,
            "name": name.strip(),
            "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "last_four": token[-4:],
            "created_at": datetime.now(UTC).isoformat(),
        }
        async with self._lock:
            updated = {**self._access_tokens, token_id: entry}
            await run_in_executor(self._write_access_tokens_sync, updated)
            self._access_tokens = updated
        return {key: value for key, value in entry.items() if key != "digest"} | {"token": token}

    async def delete_access_token(self, *, token_id: str) -> None:
        """Delete an allowlisted token by its public identifier."""
        if not isinstance(token_id, str) or not token_id:
            raise ValueError("token_id is required")
        async with self._lock:
            if token_id not in self._access_tokens:
                raise KeyError(token_id)
            updated = {key: value for key, value in self._access_tokens.items() if key != token_id}
            await run_in_executor(self._write_access_tokens_sync, updated)
            self._access_tokens = updated

    async def _http_firmware(self, request: web.Request) -> web.StreamResponse:
        return await self._http_download(request)

    async def _http_download(self, request: web.Request) -> web.StreamResponse:
        access_token = await self._authorize_public_request(request)
        device = request.query.get("device", "")
        version = request.query.get("version")
        artifact_type = request.query.get("type", "")
        try:
            _validate_key(device, "latest" if version is None else version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        relative_bin = record.get(f"{artifact_type}_bin")
        if relative_bin is None and artifact_type == "ota" and record.get("factory_bin") is None:
            relative_bin = record.get("bin")
        if relative_bin is None:
            raise web.HTTPNotFound
        path = self.root / relative_bin
        if not await run_in_executor(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(
            path,
            headers={
                "Cache-Control": "no-store" if access_token else "public, max-age=60",
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "Content-Type": "application/octet-stream",
            },
        )

    async def _http_update_manifest(self, request: web.Request) -> web.Response:
        access_token = await self._authorize_public_request(request)
        device = request.query.get("device", "")
        artifact_type = request.query.get("type")
        if not _DEVICE_RE.fullmatch(device) or device in {".", ".."}:
            raise web.HTTPBadRequest(text="Invalid device")
        if artifact_type is not None and artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, None)
        if record is None:
            raise web.HTTPNotFound
        try:
            if record.get("platform") in {None, "", "unknown"}:
                yaml_path = resolve_under_root(self.root / record["yaml"], self.root)
                platform = await run_in_executor(_platform_from_yaml_file, yaml_path)
                if platform:
                    record["platform"] = platform
            manifest = _public_firmware_manifest(record, artifact_type, access_token)
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        cache_control = "no-store" if access_token else "no-cache"
        return web.json_response(manifest, headers={"Cache-Control": cache_control})

    async def _http_md5(self, request: web.Request) -> web.Response:
        access_token = await self._authorize_public_request(request)
        device = request.query.get("device", "")
        version = request.query.get("version")
        artifact_type = request.query.get("type", "")
        try:
            _validate_key(device, "latest" if version is None else version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        relative_bin = record.get(f"{artifact_type}_bin")
        if relative_bin is None and artifact_type == "ota" and record.get("factory_bin") is None:
            relative_bin = record.get("bin")
        if relative_bin is None:
            raise web.HTTPNotFound
        path = self.root / relative_bin
        if not await run_in_executor(path.is_file):
            raise web.HTTPNotFound
        digest = await run_in_executor(_md5, path)
        filename = f"{device}-{record['version']}-{artifact_type}.md5"
        return web.Response(
            text=digest,
            content_type="text/plain",
            headers={
                "Cache-Control": "no-store" if access_token else "public, max-age=60",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    async def _authorize_public_request(self, request: web.Request) -> str | None:
        if self._authenticate_public:
            return None
        token = request.query.get("token", "")
        if not token or len(token) > 512:
            raise web.HTTPUnauthorized(text="A valid token is required")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with self._lock:
            allowed = any(
                hmac.compare_digest(entry["digest"], digest)
                for entry in self._access_tokens.values()
            )
        if not allowed:
            raise web.HTTPUnauthorized(text="A valid token is required")
        return token

    async def _http_repository_download(self, request: web.Request) -> web.StreamResponse:
        device = request.query.get("device", "")
        version = request.query.get("version", "")
        file_type = request.query.get("file", "")
        try:
            _validate_key(device, version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if file_type not in {"yaml", "bin", "ota", "factory", "md5"}:
            raise web.HTTPBadRequest(text="file must be yaml, bin, ota, factory, or md5")
        async with self._lock:
            record = await run_in_executor(self._get_record, device, version)
        if record is None:
            raise web.HTTPNotFound

        filename = f"{device}-{version}.{file_type}"
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if file_type == "md5":
            return web.Response(
                text=record["md5"],
                content_type="text/plain",
                headers=headers,
            )

        artifact_field = f"{file_type}_bin" if file_type in {"ota", "factory"} else file_type
        relative_path = record.get(artifact_field)
        if relative_path is None and file_type == "ota" and record.get("factory_bin") is None:
            relative_path = record.get("bin")
        if relative_path is None:
            raise web.HTTPNotFound
        path = self.root / relative_path
        if not await run_in_executor(path.is_file):
            raise web.HTTPNotFound
        headers["Content-Type"] = "application/octet-stream"
        return web.FileResponse(path, headers=headers)

    def _load_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index_path = self.root / _REPOSITORY_FILENAME
        if not index_path.exists():
            self._write_index()
            return
        data = json.loads(index_path.read_text(encoding="utf-8"))
        records = data.get("firmwares", [])
        for record in records:
            device, version = _validate_repository_record(record)
            self._records[(device, version)] = record
        if _migrate_legacy_repository_metadata(self.root, self._records):
            self._write_index()

    def _load_access_tokens_sync(self) -> dict[str, dict[str, str]]:
        path = self._db.settings.config_dir / _ALLOWLIST_FILENAME
        if not path.exists():
            return {}
        invalid_reason = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as err:
            data = {}
            invalid_reason = str(err)
        entries = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            invalid_reason = invalid_reason or "missing tokens mapping"
        if invalid_reason:
            _LOGGER.warning("Ignoring invalid firmware token allowlist: %s", invalid_reason)
            try:
                path.replace(path.with_name(path.name + ".corrupt"))
            except OSError:
                _LOGGER.warning("Could not preserve invalid token allowlist at %s", path)
            return {}
        return {
            token_id: entry
            for token_id, entry in entries.items()
            if isinstance(token_id, str)
            and isinstance(entry, dict)
            and all(isinstance(value, str) for value in entry.values())
            and entry.get("id") == token_id
            and re.fullmatch(r"[a-f0-9]{64}", entry.get("digest", ""))
        }

    def _write_access_tokens_sync(self, tokens: dict[str, dict[str, str]]) -> None:
        path = self._db.settings.config_dir / _ALLOWLIST_FILENAME
        data = json.dumps({"tokens": tokens}, sort_keys=True, indent=2).encode("utf-8")
        atomic_write(path, data, mode=0o600, make_parents=True)

    def _save_sync(
        self,
        configuration: str,
        config: dict[str, Any],
        artifacts: dict[str, str] | None = None,
        summary: str = "",
        release_url: str = "",
    ) -> dict[str, Any]:
        source_yaml = self._db.settings.config_dir / configuration
        esphome_config = config.get("esphome") if isinstance(config, dict) else None
        if not isinstance(esphome_config, dict):
            raise TypeError("Resolved YAML must define esphome.name")
        project = esphome_config.get("project", {})
        esphome_name = esphome_config.get("name")
        device = Path(configuration).stem
        version = project.get("version") if isinstance(project, dict) else None
        if not isinstance(esphome_name, str):
            raise TypeError("YAML must define esphome.name")
        friendly_name = _repository_friendly_name(esphome_config, esphome_name)

        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None or storage.firmware_bin_path is None:
            raise ValueError("No compiled firmware found; compile the device first")
        source_bin = Path(storage.firmware_bin_path)
        if not source_bin.is_file():
            raise ValueError("Compiled firmware.bin was not found")
        artifact_sources = _resolve_repository_artifacts(source_bin, artifacts or {})
        build_info = _read_build_info(storage.build_path)
        esphome_version = build_info.get("esphome_version")
        if not isinstance(esphome_version, str) or not esphome_version:
            esphome_version = storage.esphome_version or _installed_esphome_version
        if version is None or version == "":
            version = esphome_version
        if not isinstance(version, str):
            raise TypeError("Resolved esphome.project.version must be a string")
        _validate_key(device, version)

        key = (device, version)
        existing = self._records.get(key)
        if existing is not None and existing["status"] != "saved":
            raise FileExistsError(
                f"Version {version} for {device} already exists with status {existing['status']}"
            )

        config_hash = build_info.get("config_hash")
        if isinstance(config_hash, int) and not isinstance(config_hash, bool):
            config_hash = f"{config_hash & 0xFFFFFFFF:08x}"
        else:
            config_hash = None
        build_time = build_info.get("build_time")
        compiled_at = _format_build_time(build_time)
        _ensure_unique_build_date(self._records, key, compiled_at)
        platform = _repository_platform(source_yaml, config, storage)

        device_dir = self.root / device
        device_dir.mkdir(parents=True, exist_ok=True)
        relative_yaml = Path(device) / f"{version}.yaml"
        default_artifact, variant_metadata = _copy_repository_artifacts(
            self.root, device, version, artifact_sources
        )
        _atomic_copy(source_yaml, self.root / relative_yaml)
        record = {
            "device": device,
            "esphome_name": esphome_name,
            "friendly_name": friendly_name,
            "platform": platform.strip().lower(),
            "summary": summary,
            "release_url": release_url or None,
            "version": version,
            "status": "saved",
            "yaml": relative_yaml.as_posix(),
            "bin": default_artifact[0],
            "md5": default_artifact[1],
            "saved_at": datetime.now(UTC).isoformat(),
            "compiled_at": compiled_at,
            "esphome_version": esphome_version,
            "config_hash": config_hash,
            **variant_metadata,
        }
        self._records[key] = record
        _remove_stale_artifact_files(self.root, existing, record)
        self._write_index()
        return dict(record)

    def _publish_sync(self, device: str, version: str) -> dict[str, Any]:
        target = self._records.get((device, version))
        if target is None:
            raise KeyError((device, version))
        updated = {key: dict(value) for key, value in self._records.items()}
        for (record_device, _), record in updated.items():
            if record_device == device and record["status"] == "published":
                record["status"] = "obsolete"
        updated[(device, version)]["status"] = "published"
        updated[(device, version)]["published_at"] = datetime.now(UTC).isoformat()
        self._records = updated
        self._write_index()
        return dict(self._records[(device, version)])

    def _delete_sync(self, device: str, version: str) -> None:
        record = self._records.pop((device, version), None)
        if record is None:
            raise KeyError((device, version))
        paths = {record["bin"], record["yaml"]}
        paths.update(
            path for path in (record.get("ota_bin"), record.get("factory_bin")) if path is not None
        )
        for path in paths:
            (self.root / path).unlink(missing_ok=True)
        self._write_index()

    def _rename_configuration_sync(
        self,
        old_device: str,
        new_device: str,
        new_esphome_name: str | None,
        new_friendly_name: str | None,
    ) -> None:
        source_records = {
            key: record for key, record in self._records.items() if key[0] == old_device
        }
        if not source_records:
            return
        replacements, moves = _prepare_repository_rename(
            self.root,
            self._records,
            source_records,
            old_device,
            new_device,
            new_esphome_name,
            new_friendly_name,
        )
        previous_records = self._records
        updated_records = {
            key: record for key, record in self._records.items() if key not in source_records
        }
        updated_records.update(replacements)
        moved: list[tuple[Path, Path]] = []
        try:
            moved = _move_repository_files(moves)
            self._records = updated_records
            self._write_index()
        except Exception:
            self._records = previous_records
            _rollback_repository_files(moved)
            raise
        old_directory = self.root / old_device
        with contextlib.suppress(OSError):
            old_directory.rmdir()

    def _list_sync(self) -> list[dict[str, Any]]:
        return [dict(record) for record in sorted(self._records.values(), key=_sort_key)]

    def _get_record(self, device: str, version: str) -> dict[str, Any] | None:
        record = self._records.get((device, version))
        return dict(record) if record is not None else None

    def _resolve_public_record(self, device: str, version: str | None) -> dict[str, Any] | None:
        if version is not None:
            record = self._records.get((device, version))
            return (
                dict(record)
                if record is not None and record["status"] in {"published", "obsolete"}
                else None
            )
        published = [
            record
            for (record_device, _), record in self._records.items()
            if record_device == device and record["status"] == "published"
        ]
        return dict(published[0]) if published else None

    def _write_index(self) -> None:
        data = {"firmwares": self._list_sync()}
        _atomic_write_json(self.root / _REPOSITORY_FILENAME, data)


def _validate_key(device: str, version: str) -> None:
    if not isinstance(device, str) or not _DEVICE_RE.fullmatch(device) or device in {".", ".."}:
        raise ValueError("Invalid device name")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version) or version in {".", ".."}:
        raise ValueError("Invalid firmware version")


def _ensure_unique_build_date(
    records: dict[tuple[str, str], dict[str, Any]],
    key: tuple[str, str],
    compiled_at: str | None,
) -> None:
    if compiled_at is None:
        return
    if any(
        existing_key[0] == key[0]
        and existing_key != key
        and record.get("compiled_at") == compiled_at
        for existing_key, record in records.items()
    ):
        raise FileExistsError(f"Build date {compiled_at} already exists for {key[0]}")


def _migrate_legacy_repository_metadata(
    root: Path, records: dict[tuple[str, str], dict[str, Any]]
) -> bool:
    changed = False
    for record in records.values():
        sibling = max(
            (
                candidate
                for candidate in records.values()
                if candidate["device"] == record["device"] and candidate is not record
            ),
            key=_repository_record_recency,
            default=None,
        )
        for field in ("friendly_name", "platform"):
            if record.get(field) not in (None, ""):
                continue
            value = sibling.get(field) if sibling is not None else None
            if value not in (None, ""):
                record[field] = value
                changed = True
        if record.get("platform") in (None, ""):
            yaml_path = root / record["yaml"]
            if yaml_path.is_file():
                platform = _platform_from_yaml_file(yaml_path)
                if platform:
                    record["platform"] = platform
                    changed = True
    return changed


def _prepare_repository_rename(
    root: Path,
    records: dict[tuple[str, str], dict[str, Any]],
    source_records: dict[tuple[str, str], dict[str, Any]],
    old_device: str,
    new_device: str,
    new_esphome_name: str | None,
    new_friendly_name: str | None,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[Path, Path]]:
    replacements: dict[tuple[str, str], dict[str, Any]] = {}
    moves: dict[Path, Path] = {}
    for (_, version), record in source_records.items():
        new_key = (new_device, version)
        if new_key in records and new_key not in source_records:
            raise FileExistsError(f"Firmware repository already contains {new_device} {version}")
        updated = dict(record)
        updated["device"] = new_device
        if new_esphome_name is not None:
            updated["esphome_name"] = new_esphome_name
        if new_friendly_name is not None or new_esphome_name is not None:
            updated["friendly_name"] = new_friendly_name or new_esphome_name
        for field in ("yaml", "bin", "ota_bin", "factory_bin"):
            relative_path = record.get(field)
            if isinstance(relative_path, str) and relative_path.startswith(f"{old_device}/"):
                renamed_path = f"{new_device}/{relative_path[len(old_device) + 1 :]}"
                updated[field] = renamed_path
                moves[root / relative_path] = root / renamed_path
        replacements[new_key] = updated
    return replacements, moves


def _move_repository_files(moves: dict[Path, Path]) -> list[tuple[Path, Path]]:
    source_paths = set(moves)
    for destination in moves.values():
        if destination.exists() and destination not in source_paths:
            raise FileExistsError(f"Firmware repository file already exists: {destination}")
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in moves.items():
            if not source.is_file():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
            moved.append((source, destination))
    except Exception:
        _rollback_repository_files(moved)
        raise
    return moved


def _rollback_repository_files(moved: list[tuple[Path, Path]]) -> None:
    for source, destination in reversed(moved):
        source.parent.mkdir(parents=True, exist_ok=True)
        destination.replace(source)


def _validate_repository_record(record: Any) -> tuple[str, str]:
    if not isinstance(record, dict):
        raise TypeError("Invalid firmware repository entry")
    device, version = record["device"], record["version"]
    _validate_key(device, version)
    if record.get("status") not in {"saved", "published", "obsolete"}:
        raise ValueError("Invalid firmware status in repository index")
    if record.get("yaml") != f"{device}/{version}.yaml":
        raise ValueError("Invalid YAML path in repository index")
    allowed_paths = {f"{device}/{version}.bin"}
    for artifact_type in ("ota", "factory"):
        artifact_path = record.get(f"{artifact_type}_bin")
        if artifact_path is None:
            continue
        if artifact_path != f"{device}/{version}.{artifact_type}.bin":
            raise ValueError("Invalid typed firmware path in repository index")
        allowed_paths.add(artifact_path)
        artifact_md5 = record.get(f"{artifact_type}_md5")
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_md5 or ""):
            raise ValueError("Invalid typed firmware MD5 in repository index")
    if record.get("bin") not in allowed_paths:
        raise ValueError("Invalid firmware path in repository index")
    if not re.fullmatch(r"[a-f0-9]{32}", record.get("md5", "")):
        raise ValueError("Invalid MD5 in repository index")
    return device, version


def _resolve_repository_artifacts(
    firmware_bin_path: Path, artifacts: dict[str, str]
) -> dict[str, Path]:
    build_dir = firmware_bin_path.parent
    resolved: dict[str, Path] = {}
    for artifact_type in ("ota", "factory"):
        filename = artifacts.get(artifact_type)
        if filename is not None:
            path = resolve_under_root(build_dir / filename, build_dir)
            if not path.is_file():
                raise ValueError(f"Compiled {artifact_type} firmware was not found")
            resolved[artifact_type] = path
    return resolved or {"bin": firmware_bin_path}


def _copy_repository_artifacts(
    root: Path, device: str, version: str, sources: dict[str, Path]
) -> tuple[tuple[str, str], dict[str, str | None]]:
    metadata: dict[str, str | None] = {
        "ota_bin": None,
        "ota_md5": None,
        "factory_bin": None,
        "factory_md5": None,
    }
    copied: dict[str, tuple[str, str]] = {}
    for artifact_type, source in sources.items():
        suffix = f".{artifact_type}" if artifact_type in {"ota", "factory"} else ""
        relative = Path(device) / f"{version}{suffix}.bin"
        destination = root / relative
        _atomic_copy(source, destination)
        digest = _md5(destination)
        copied[artifact_type] = (relative.as_posix(), digest)
        if artifact_type in {"ota", "factory"}:
            metadata[f"{artifact_type}_bin"] = relative.as_posix()
            metadata[f"{artifact_type}_md5"] = digest
    default_artifact = copied.get("ota") or copied.get("bin") or next(iter(copied.values()))
    return default_artifact, metadata


def _remove_stale_artifact_files(
    root: Path, previous: dict[str, Any] | None, current: dict[str, Any]
) -> None:
    if previous is None:
        return
    previous_paths = {
        path
        for path in (previous.get("bin"), previous.get("ota_bin"), previous.get("factory_bin"))
        if isinstance(path, str)
    }
    current_paths = {
        path
        for path in (current.get("bin"), current.get("ota_bin"), current.get("factory_bin"))
        if isinstance(path, str)
    }
    for relative_path in previous_paths - current_paths:
        (root / relative_path).unlink(missing_ok=True)


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["device"], record["version"]


def _repository_record_recency(record: dict[str, Any]) -> str:
    return str(
        record.get("published_at") or record.get("saved_at") or record.get("compiled_at") or ""
    )


def _public_firmware_manifest(
    record: dict[str, Any], artifact_type: str | None, access_token: str | None = None
) -> dict[str, Any]:
    artifact_types = (artifact_type,) if artifact_type is not None else ("ota", "factory")
    chip_family = str(record.get("platform", "unknown")).strip().lower()
    if chip_family.startswith("esp32"):
        chip_family = "esp32"
    build: dict[str, Any] = {"chipFamily": chip_family or "unknown"}
    for selected_type in artifact_types:
        relative_bin = record.get(f"{selected_type}_bin")
        digest = record.get(f"{selected_type}_md5")
        if relative_bin is None and selected_type == "ota" and record.get("factory_bin") is None:
            relative_bin = record.get("bin")
            digest = record.get("md5")
        if relative_bin is None or digest is None:
            continue
        query_params = {
            "device": record["device"],
            "version": record["version"],
            "type": selected_type,
        }
        if access_token is not None:
            query_params["token"] = access_token
        query = urlencode(query_params)
        artifact = {
            "md5": digest,
            "path": f"/update/firmware?{query}",
            "summary": record.get("summary") or f"ESPHome {selected_type} firmware",
        }
        if selected_type == "ota" and record.get("release_url"):
            artifact["release_url"] = record["release_url"]
        build[selected_type] = artifact
    if len(build) == 1:
        raise FileNotFoundError
    name = record.get("friendly_name") or record.get("esphome_name") or record["device"]
    manifest: dict[str, Any] = {
        "name": name,
        "version": record["version"],
        "builds": [build],
    }
    return manifest


def _platform_from_yaml_file(path: Path) -> str:
    yaml_content = path.read_text(encoding="utf-8")
    return detect_platform_from_yaml(yaml_content, None)


def _repository_platform(source_yaml: Path, config: dict[str, Any], storage: Any) -> str:
    platform = detect_platform_from_yaml(source_yaml.read_text(encoding="utf-8"), config)
    if not platform:
        platform = str(
            getattr(storage, "core_platform", None)
            or getattr(storage, "target_platform", None)
            or "unknown"
        )
    return platform.strip().lower()


def _repository_friendly_name(esphome_config: dict[str, Any], esphome_name: str) -> str:
    friendly_name = esphome_config.get("friendly_name")
    if isinstance(friendly_name, str) and friendly_name.strip():
        return friendly_name
    return esphome_name


def _has_unresolved_repository_identity(config: dict[str, Any]) -> bool:
    esphome_config = config.get("esphome")
    if not isinstance(esphome_config, dict):
        return False
    project = esphome_config.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    values = (esphome_config.get("name"), version)
    return any(
        isinstance(value, str) and _UNRESOLVED_SUBSTITUTION_RE.search(value) for value in values
    )


def _read_build_info(build_path: Path | None) -> dict[str, Any]:
    if build_path is None:
        return {}
    try:
        data = json.loads((build_path / "build_info.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _format_build_time(build_time: Any) -> str | None:
    if not isinstance(build_time, (int, float)) or isinstance(build_time, bool):
        return None
    try:
        return datetime.fromtimestamp(build_time, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


async def http_repository_download(request: web.Request) -> web.StreamResponse:
    db = request.app["device_builder"]
    controller: FirmwareRepositoryController | None = db.firmware_repository
    if controller is None:
        raise web.HTTPServiceUnavailable
    return await controller._http_repository_download(request)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    encoded = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    atomic_write_preserving_mode(path, encoded)


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
            temp_path = temp.name
            with source.open("rb") as source_file:
                shutil.copyfileobj(source_file, temp)
        Path(temp_path).replace(destination)
    finally:
        if temp_path is not None:
            Path(temp_path).unlink(missing_ok=True)


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as firmware_file:
        for chunk in iter(lambda: firmware_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
