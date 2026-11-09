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
from urllib.parse import urlencode

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
        self._listener_lock = asyncio.Lock()
        self._index_loaded = False
        self._runner: web.AppRunner | None = None

    async def start(self, *, public_enabled: bool = False) -> None:
        """Load repository metadata and bind the listener when enabled."""
        await run_in_executor(self._load_index)
        self._index_loaded = True
        if public_enabled:
            await self.set_public_enabled(enabled=True)

    async def set_public_enabled(self, *, enabled: bool) -> None:
        """Reconcile the unauthenticated update listener with the preference."""
        async with self._listener_lock:
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

    async def _http_firmware(self, request: web.Request) -> web.StreamResponse:
        return await self._http_download(request)

    async def _http_download(self, request: web.Request) -> web.StreamResponse:
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
                "Cache-Control": "public, max-age=60",
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "Content-Type": "application/octet-stream",
            },
        )

    async def _http_update_manifest(self, request: web.Request) -> web.Response:
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
            manifest = _public_firmware_manifest(record, artifact_type)
        except FileNotFoundError:
            raise web.HTTPNotFound from None
        return web.json_response(manifest, headers={"Cache-Control": "no-cache"})

    async def _http_md5(self, request: web.Request) -> web.Response:
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
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def _http_repository_download(self, request: web.Request) -> web.StreamResponse:
        device = request.query.get("device", "")
        version = request.query.get("version", "")
        file_type = request.query.get("file", "")
        try:
            _validate_key(device, version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        if file_type not in {"yaml", "bin", "md5"}:
            raise web.HTTPBadRequest(text="file must be yaml, bin, or md5")
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

        path = self.root / record[file_type]
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
        for record in data.get("firmwares", []):
            device, version = _validate_repository_record(record)
            self._records[(device, version)] = record

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


def _public_firmware_manifest(record: dict[str, Any], artifact_type: str | None) -> dict[str, Any]:
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
        query = urlencode(
            {"device": record["device"], "version": record["version"], "type": selected_type}
        )
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
