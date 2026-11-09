"""Persistent repository for compiled device firmware."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from aiohttp import web
from esphome.const import __version__ as _installed_esphome_version
from esphome.storage_json import StorageJSON

from ..helpers.api import CommandError, api_command
from ..helpers.async_ import run_in_executor
from ..helpers.atomic_io import atomic_write_preserving_mode
from ..helpers.device_yaml import _UNRESOLVED_SUBSTITUTION_RE, detect_platform_from_yaml
from ..helpers.network_interfaces import resolve_bind_host
from ..helpers.paths import resolve_under_root
from ..helpers.storage_path import resolve_storage_path
from ..models import ErrorCode
from ..models.boards import normalize_platform
from .devices.resolve import resolve_config, resolve_config_subprocess

_LOGGER = logging.getLogger(__name__)
_REPOSITORY_FILENAME = "repository.json"
_UPDATE_PORT = 6070
_DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z")


class FirmwareRepositoryController:
    """Manage repository artifacts and serve public device updates."""

    def __init__(self, device_builder: Any) -> None:
        self._db = device_builder
        self.root = device_builder.settings.config_dir / "repos"
        self._records: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Load repository metadata and bind the update listener."""
        await run_in_executor(self._load_index)
        app = web.Application()
        app.router.add_get("/update", self._http_update_manifest)
        app.router.add_get("/update/download", self._http_download)
        app.router.add_get("/update/md5", self._http_md5)
        runner = web.AppRunner(app)
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
        summary: str,
        release_url: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        firmware = self._db.firmware
        await firmware._validate_configuration_boundary(configuration)
        try:
            summary, release_url = _validate_release_metadata(summary, release_url)
        except (TypeError, ValueError) as err:
            raise CommandError(ErrorCode.INVALID_ARGS, str(err)) from err
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
            entry["type"]: entry["file"]
            for entry in downloads
            if entry.get("type") in {"ota", "factory"}
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

    async def _http_update_manifest(self, request: web.Request) -> web.Response:
        device = request.query.get("device", "")
        artifact_type = request.query.get("type")
        try:
            _validate_device(device)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device") from None
        if artifact_type is not None and artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, None)
        if record is None:
            raise web.HTTPNotFound
        try:
            manifest = _public_firmware_manifest(record, artifact_type)
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        return web.json_response(manifest, headers={"Cache-Control": "no-cache"})

    async def _http_download(self, request: web.Request) -> web.StreamResponse:
        device = request.query.get("device", "")
        version = request.query.get("version")
        artifact_type = request.query.get("type", "")
        try:
            _validate_device(device)
            if version is not None:
                _validate_version(version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_download_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        try:
            path = _repository_download_path(self.root, record, artifact_type)
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        if not await run_in_executor(path.is_file):
            raise web.HTTPNotFound
        filename = f"{device}-{record['version']}-{artifact_type}{path.suffix}"
        cache_control = "no-store" if version is None else "public, max-age=60"
        return web.FileResponse(
            path,
            headers={
                "Cache-Control": cache_control,
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "application/octet-stream",
            },
        )

    async def _http_md5(self, request: web.Request) -> web.Response:
        device = request.query.get("device", "")
        version = request.query.get("version")
        artifact_type = request.query.get("type", "")
        try:
            _validate_device(device)
            if version is not None:
                _validate_version(version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if artifact_type not in {"ota", "factory"}:
            raise web.HTTPBadRequest(text="type must be ota or factory")
        async with self._lock:
            record = await run_in_executor(self._resolve_download_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        try:
            path = _repository_download_path(self.root, record, artifact_type)
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        try:
            digest = await run_in_executor(_md5, path)
        except OSError:
            raise web.HTTPNotFound from None
        filename = f"{device}-{record['version']}-{artifact_type}.md5"
        return web.Response(
            text=digest,
            content_type="text/plain",
            headers={
                "Cache-Control": "no-store" if version is None else "public, max-age=60",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    async def _http_repository_download(self, request: web.Request) -> web.StreamResponse:
        device = request.query.get("device", "")
        version = request.query.get("version", "")
        file_type = request.query.get("file", "")
        try:
            _validate_key(device, version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if file_type not in {"yaml", "bin", "md5", "ota", "factory", "ota-md5", "factory-md5"}:
            raise web.HTTPBadRequest(text="Unsupported firmware repository file type")
        async with self._lock:
            record = await run_in_executor(self._get_record, device, version)
        if record is None:
            raise web.HTTPNotFound

        try:
            filename, path, digest = _resolve_repository_download(
                self.root, record, device, version, file_type
            )
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        headers = {
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        if digest is not None:
            return web.Response(
                text=digest,
                content_type="text/plain",
                headers=headers,
            )
        if path is None:
            raise web.HTTPNotFound
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
        self._records.update(_parse_repository_index(data))
        if self._migrate_manifest_metadata():
            self._write_index()
        self._sync_device_json_files()

    def _migrate_manifest_metadata(self) -> bool:
        changed = False
        records = tuple(self._records.values())
        for record in records:
            chip_family = _record_chip_family(self.root, record, records)
            if chip_family != "unknown" and record.get("platform") != chip_family:
                record["platform"] = chip_family
                changed = True
            friendly_name = _record_friendly_name(record, records)
            if record.get("friendly_name") != friendly_name:
                record["friendly_name"] = friendly_name
                changed = True
        return changed

    def _save_sync(
        self,
        configuration: str,
        config: dict[str, Any],
        artifacts: dict[str, str] | None = None,
        summary: str = "Firmware",
        release_url: str | None = None,
    ) -> dict[str, Any]:
        summary, release_url = _validate_release_metadata(summary, release_url)
        source_yaml = self._db.settings.config_dir / configuration
        esphome_config = config.get("esphome") if isinstance(config, dict) else None
        if not isinstance(esphome_config, dict):
            raise TypeError("Resolved YAML must define esphome.name")
        project = esphome_config.get("project", {})
        device = esphome_config.get("name")
        version = project.get("version") if isinstance(project, dict) else None
        if not isinstance(device, str):
            raise TypeError("YAML must define esphome.name")
        friendly_name = _repository_friendly_name(esphome_config, device)

        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None or storage.firmware_bin_path is None:
            raise ValueError("No compiled firmware found; compile the device first")
        source_bin = Path(storage.firmware_bin_path)
        if not source_bin.is_file():
            raise ValueError("Compiled firmware.bin was not found")
        chip_family = _repository_chip_family(storage, source_yaml, config)
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

        config_hash = build_info.get("config_hash")
        if isinstance(config_hash, int) and not isinstance(config_hash, bool):
            config_hash = f"{config_hash & 0xFFFFFFFF:08x}"
        else:
            config_hash = None
        build_time = build_info.get("build_time")
        compiled_at = _format_build_time(build_time)
        key = (device, version)
        existing = self._records.get(key)
        _validate_save_replacement(self._records, existing, device, version, compiled_at)

        device_dir = self.root / device
        device_dir.mkdir(parents=True, exist_ok=True)
        relative_yaml = Path(device) / f"{version}.yaml"
        default_artifact, artifact_metadata = _copy_repository_artifacts(
            self.root, device, version, artifact_sources
        )
        _atomic_copy(source_yaml, self.root / relative_yaml)
        record = {
            "device": device,
            "friendly_name": friendly_name,
            "version": version,
            "status": "saved",
            "yaml": relative_yaml.as_posix(),
            "bin": default_artifact[0],
            "md5": default_artifact[1],
            "saved_at": datetime.now(UTC).isoformat(),
            "compiled_at": compiled_at,
            "esphome_version": esphome_version,
            "config_hash": config_hash,
            "platform": chip_family,
            "summary": summary,
            "release_url": release_url,
            **artifact_metadata,
        }
        self._records[key] = record
        _remove_stale_artifact_files(self.root, existing, record)
        self._write_index()
        self._sync_device_json_files()
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
        self._sync_device_json_files()
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
        for relative_path in paths:
            (self.root / relative_path).unlink(missing_ok=True)
        self._write_index()
        self._sync_device_json_files()

    def _list_sync(self) -> list[dict[str, Any]]:
        return [dict(record) for record in sorted(self._records.values(), key=_sort_key)]

    def _get_record(self, device: str, version: str) -> dict[str, Any] | None:
        record = self._records.get((device, version))
        return dict(record) if record is not None else None

    def _resolve_public_record(self, device: str, version: str | None) -> dict[str, Any] | None:
        published = [
            record
            for (record_device, _), record in self._records.items()
            if record_device == device and record["status"] == "published"
        ]
        return dict(published[0]) if published else None

    def _resolve_download_record(self, device: str, version: str | None) -> dict[str, Any] | None:
        if version is not None:
            record = self._records.get((device, version))
            if record is None or record["status"] not in {"published", "obsolete"}:
                return None
            return dict(record)
        return self._resolve_public_record(device, None)

    def _write_index(self) -> None:
        data = {"firmwares": self._list_sync()}
        _atomic_write_json(self.root / _REPOSITORY_FILENAME, data)

    def _sync_device_json_files(self) -> None:
        devices = {device for device, _ in self._records}
        for device in devices:
            device_records = [
                record
                for (record_device, _), record in self._records.items()
                if record_device == device
            ]
            versions = [_private_repository_entry(record) for record in device_records]
            payload = {
                "device": device,
                "friendly_name": next(
                    (
                        record.get("friendly_name")
                        for record in reversed(device_records)
                        if record.get("friendly_name")
                    ),
                    device,
                ),
                "firmwares": versions,
            }
            _atomic_write_json(self.root / f"{device}.json", payload)
        for path in self.root.glob("*.json"):
            if path.name != _REPOSITORY_FILENAME and path.stem not in devices:
                path.unlink(missing_ok=True)


def _validate_key(device: str, version: str) -> None:
    _validate_device(device)
    _validate_version(version)


def _validate_device(device: str) -> None:
    if not isinstance(device, str) or not _DEVICE_RE.fullmatch(device) or device in {".", ".."}:
        raise ValueError("Invalid device name")


def _validate_version(version: str) -> None:
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version) or version in {".", ".."}:
        raise ValueError("Invalid firmware version")


def _validate_release_metadata(summary: str, release_url: str | None) -> tuple[str, str | None]:
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary is required")
    normalized_summary = summary.strip()
    if len(normalized_summary) > 500:
        raise ValueError("summary must be 500 characters or fewer")
    if release_url is None:
        return normalized_summary, None
    if not isinstance(release_url, str):
        raise TypeError("release_url must be a string")
    if not release_url.strip():
        return normalized_summary, None
    normalized_url = release_url.strip()
    parsed_url = urlsplit(normalized_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("release_url must be an absolute http or https URL")
    if len(normalized_url) > 2048:
        raise ValueError("release_url must be 2048 characters or fewer")
    return normalized_summary, normalized_url


def _repository_download_path(root: Path, record: dict[str, Any], artifact_type: str) -> Path:
    relative_path = record.get(f"{artifact_type}_bin")
    if relative_path is None and artifact_type == "ota" and record.get("factory_bin") is None:
        relative_path = record.get("bin")
    if not isinstance(relative_path, str):
        raise FileNotFoundError
    return resolve_under_root(root / relative_path, root)


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
    default = copied.get("ota") or copied.get("bin") or next(iter(copied.values()))
    return default, metadata


def _resolve_repository_download(
    root: Path,
    record: dict[str, Any],
    device: str,
    version: str,
    file_type: str,
) -> tuple[str, Path | None, str | None]:
    fields = {
        "ota": ("ota_bin", "ota_md5"),
        "factory": ("factory_bin", "factory_md5"),
        "ota-md5": (None, "ota_md5"),
        "factory-md5": (None, "factory_md5"),
        "bin": ("bin", "md5"),
        "md5": (None, "md5"),
        "yaml": ("yaml", None),
    }
    path_field, digest_field = fields[file_type]
    if file_type in {"md5", "ota-md5", "factory-md5"}:
        digest = record.get(digest_field) if digest_field is not None else None
        if digest is None and file_type in {"md5", "ota-md5"}:
            digest = record.get("md5") if record.get("factory_bin") is None else None
        if digest is None:
            raise FileNotFoundError
        flavor = file_type.removesuffix("-md5") if file_type != "md5" else ""
        filename = f"{device}-{version}{f'-{flavor}' if flavor else ''}.md5"
        return filename, None, digest

    relative_path = record.get(path_field) if path_field is not None else None
    if relative_path is None and file_type == "ota" and record.get("factory_bin") is None:
        relative_path = record["bin"]
    if not isinstance(relative_path, str):
        raise FileNotFoundError
    flavor = f"-{file_type}" if file_type in {"ota", "factory"} else ""
    filename = f"{device}-{version}{flavor}{Path(relative_path).suffix}"
    return filename, root / relative_path, None


def _parse_repository_index(data: Any) -> dict[tuple[str, str], dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("firmwares", []), list):
        raise TypeError("Invalid firmware repository index")
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for record in data.get("firmwares", []):
        if not isinstance(record, dict):
            raise TypeError("Invalid firmware repository entry")
        device, version = record["device"], record["version"]
        _validate_key(device, version)
        _validate_repository_record(record, device, version)
        records[(device, version)] = record
    return records


def _validate_repository_record(record: dict[str, Any], device: str, version: str) -> None:
    if record.get("status") not in {"saved", "published", "obsolete"}:
        raise ValueError("Invalid firmware status in repository index")
    if record.get("yaml") != f"{device}/{version}.yaml":
        raise ValueError("Invalid YAML path in repository index")
    valid_bin_paths = {f"{device}/{version}.bin"}
    for artifact_type in ("ota", "factory"):
        artifact_path = record.get(f"{artifact_type}_bin")
        if artifact_path is None:
            continue
        if artifact_path != f"{device}/{version}.{artifact_type}.bin":
            raise ValueError("Invalid typed firmware path in repository index")
        valid_bin_paths.add(artifact_path)
        artifact_md5 = record.get(f"{artifact_type}_md5")
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_md5 or ""):
            raise ValueError("Invalid typed firmware MD5 in repository index")
    if record.get("bin") not in valid_bin_paths:
        raise ValueError("Invalid firmware path in repository index")
    if not re.fullmatch(r"[a-f0-9]{32}", record.get("md5", "")):
        raise ValueError("Invalid MD5 in repository index")


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["device"], record["version"]


def _repository_friendly_name(esphome_config: dict[str, Any], device: str) -> str:
    value = esphome_config.get("friendly_name")
    return value if isinstance(value, str) and value.strip() else device


def _repository_chip_family(storage: StorageJSON, source_yaml: Path, config: dict[str, Any]) -> str:
    platform = storage.core_platform or detect_platform_from_yaml(
        source_yaml.read_text(encoding="utf-8"), config
    )
    if not platform:
        platform = storage.target_platform
    return _normalize_chip_family(platform)


def _normalize_chip_family(platform: str) -> str:
    normalized = platform.strip().lower()
    if normalized.startswith("esp32"):
        return "esp32"
    return str(normalize_platform(normalized)) if normalized else "unknown"


def _record_chip_family(
    root: Path, record: dict[str, Any], records: tuple[dict[str, Any], ...]
) -> str:
    chip_family = _normalize_chip_family(str(record.get("platform", "")))
    if chip_family != "unknown":
        return chip_family
    for sibling in records:
        if sibling["device"] == record["device"]:
            chip_family = _normalize_chip_family(str(sibling.get("platform", "")))
            if chip_family != "unknown":
                return chip_family
    return _yaml_chip_family(root, record)


def _record_friendly_name(record: dict[str, Any], records: tuple[dict[str, Any], ...]) -> str:
    device = str(record["device"])
    friendly_name = _repository_friendly_name(record, device)
    if friendly_name != device:
        return friendly_name
    for sibling in records:
        if sibling["device"] == device:
            friendly_name = _repository_friendly_name(sibling, device)
            if friendly_name != device:
                return friendly_name
    return device


def _yaml_chip_family(root: Path, record: dict[str, Any]) -> str:
    try:
        yaml_content = (root / record["yaml"]).read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    return _normalize_chip_family(detect_platform_from_yaml(yaml_content, None))


def _public_firmware_manifest(
    record: dict[str, Any], artifact_type: str | None = None
) -> dict[str, Any]:
    if artifact_type is not None and artifact_type not in {"ota", "factory"}:
        raise ValueError("Invalid public firmware artifact type")
    artifact_types = (artifact_type,) if artifact_type else ("ota", "factory")
    build: dict[str, Any] = {"chipFamily": _normalize_chip_family(record.get("platform", ""))}
    for current_type in artifact_types:
        relative_path = record.get(f"{current_type}_bin")
        digest = record.get(f"{current_type}_md5")
        if relative_path is None and current_type == "ota" and record.get("factory_bin") is None:
            relative_path = record.get("bin")
            digest = record.get("md5")
        if relative_path is None or digest is None:
            continue
        query = urlencode(
            {
                "device": record["device"],
                "version": record["version"],
                "type": current_type,
            }
        )
        build[current_type] = {
            "md5": digest,
            "path": f"/update/download?{query}",
            "summary": record.get("summary") or f"ESPHome {current_type} firmware",
        }
    if len(build) == 1:
        raise FileNotFoundError
    manifest: dict[str, Any] = {
        "name": _repository_friendly_name(record, record["device"]),
        "version": record["version"],
        "builds": [build],
    }
    if record.get("release_url"):
        manifest["release_url"] = record["release_url"]
    return manifest


def _private_repository_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "version",
            "status",
            "friendly_name",
            "platform",
            "yaml",
            "bin",
            "md5",
            "ota_bin",
            "ota_md5",
            "factory_bin",
            "factory_md5",
            "saved_at",
            "published_at",
            "compiled_at",
            "esphome_version",
            "config_hash",
            "summary",
            "release_url",
        )
    }


def _remove_stale_artifact_files(
    root: Path, previous: dict[str, Any] | None, current: dict[str, Any]
) -> None:
    if previous is None:
        return
    previous_paths = {
        value
        for value in (
            previous.get("bin"),
            previous.get("ota_bin"),
            previous.get("factory_bin"),
        )
        if isinstance(value, str)
    }
    current_paths = {
        value
        for value in (current.get("bin"), current.get("ota_bin"), current.get("factory_bin"))
        if isinstance(value, str)
    }
    for path in previous_paths - current_paths:
        (root / path).unlink(missing_ok=True)


def _validate_save_replacement(
    records: dict[tuple[str, str], dict[str, Any]],
    existing: dict[str, Any] | None,
    device: str,
    version: str,
    compiled_at: str | None,
) -> None:
    for (record_device, record_version), record in records.items():
        if record_device == device and record.get("compiled_at") == compiled_at:
            raise FileExistsError(
                f"Build date {compiled_at} for {device} already exists under version "
                f"{record_version}"
            )
    if existing is not None and existing["status"] != "saved":
        raise FileExistsError(
            f"Version {version} for {device} already exists with status {existing['status']}"
        )


def _has_unresolved_repository_identity(config: dict[str, Any]) -> bool:
    esphome_config = config.get("esphome")
    if not isinstance(esphome_config, dict):
        return False
    project = esphome_config.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    values = (esphome_config.get("name"), esphome_config.get("friendly_name"), version)
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
