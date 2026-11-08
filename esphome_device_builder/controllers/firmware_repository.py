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

from aiohttp import web
from esphome.const import __version__ as _installed_esphome_version
from esphome.storage_json import StorageJSON

from ..helpers.api import CommandError, api_command
from ..helpers.async_ import run_in_executor
from ..helpers.atomic_io import atomic_write_preserving_mode
from ..helpers.network_interfaces import resolve_bind_host
from ..helpers.storage_path import resolve_storage_path
from ..helpers.yaml import load_yaml_fast_then_esphome
from ..models import ErrorCode

_LOGGER = logging.getLogger(__name__)
_REPOSITORY_FILENAME = "repository.json"
_UPDATE_PORT = 8080
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
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @api_command("firmware_repository/list")
    async def list_firmwares(self, **kwargs: Any) -> list[dict[str, Any]]:
        async with self._lock:
            return await run_in_executor(self._list_sync)

    @api_command("firmware_repository/save")
    async def save_firmware(self, *, configuration: str, **kwargs: Any) -> dict[str, Any]:
        firmware = self._db.firmware
        await firmware._validate_configuration_boundary(configuration)
        async with self._lock:
            try:
                return await run_in_executor(self._save_sync, configuration)
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
        device = request.query.get("device", "")
        try:
            _validate_key(device, request.query.get("version", "latest"))
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        version = request.query.get("version")
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        if version is None:
            path = self.root / f"{device}.json"
            if not await run_in_executor(path.is_file):
                raise web.HTTPNotFound
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        path = self.root / record["bin"]
        if not await run_in_executor(path.is_file):
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={"Cache-Control": "public, max-age=60"})

    async def _http_md5(self, request: web.Request) -> web.Response:
        device = request.query.get("device", "")
        version = request.query.get("version", "")
        try:
            _validate_key(device, version)
        except ValueError:
            raise web.HTTPBadRequest(text="Invalid device or version") from None
        async with self._lock:
            record = await run_in_executor(self._resolve_public_record, device, version)
        if record is None:
            raise web.HTTPNotFound
        return web.Response(text=record["md5"], content_type="text/plain")

    def _load_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index_path = self.root / _REPOSITORY_FILENAME
        if not index_path.exists():
            self._write_index()
            return
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for record in data.get("firmwares", []):
            device, version = record["device"], record["version"]
            _validate_key(device, version)
            if record.get("status") not in {"saved", "published", "obsolete"}:
                raise ValueError("Invalid firmware status in repository index")
            if record.get("yaml") != f"{device}/{version}.yaml":
                raise ValueError("Invalid YAML path in repository index")
            if record.get("bin") != f"{device}/{version}.bin":
                raise ValueError("Invalid firmware path in repository index")
            if not re.fullmatch(r"[a-f0-9]{32}", record.get("md5", "")):
                raise ValueError("Invalid MD5 in repository index")
            self._records[(device, version)] = record
        self._sync_device_json_files()

    def _save_sync(self, configuration: str) -> dict[str, Any]:
        source_yaml = self._db.settings.config_dir / configuration
        config = load_yaml_fast_then_esphome(source_yaml)
        esphome_config = config.get("esphome") if isinstance(config, dict) else None
        if not isinstance(esphome_config, dict):
            raise TypeError("YAML must define esphome.name and esphome.project.version")
        project = esphome_config.get("project", {})
        device = esphome_config.get("name")
        version = project.get("version") if isinstance(project, dict) else None
        if version is None or version == "":
            version = _installed_esphome_version
        if not isinstance(device, str) or not isinstance(version, str):
            raise TypeError("YAML must define esphome.name")
        _validate_key(device, version)

        key = (device, version)
        existing = self._records.get(key)
        if existing is not None and existing["status"] != "saved":
            raise FileExistsError(
                f"Version {version} for {device} already exists with status {existing['status']}"
            )

        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None or storage.firmware_bin_path is None:
            raise ValueError("No compiled firmware found; compile the device first")
        source_bin = Path(storage.firmware_bin_path)
        if not source_bin.is_file():
            raise ValueError("Compiled firmware.bin was not found")

        device_dir = self.root / device
        device_dir.mkdir(parents=True, exist_ok=True)
        relative_bin = Path(device) / f"{version}.bin"
        relative_yaml = Path(device) / f"{version}.yaml"
        _atomic_copy(source_bin, self.root / relative_bin)
        _atomic_copy(source_yaml, self.root / relative_yaml)
        record = {
            "device": device,
            "version": version,
            "status": "saved",
            "yaml": relative_yaml.as_posix(),
            "bin": relative_bin.as_posix(),
            "md5": _md5(self.root / relative_bin),
            "saved_at": datetime.now(UTC).isoformat(),
        }
        self._records[key] = record
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
        (self.root / record["bin"]).unlink(missing_ok=True)
        (self.root / record["yaml"]).unlink(missing_ok=True)
        self._write_index()
        self._sync_device_json_files()

    def _list_sync(self) -> list[dict[str, Any]]:
        return [dict(record) for record in sorted(self._records.values(), key=_sort_key)]

    def _resolve_public_record(self, device: str, version: str | None) -> dict[str, Any] | None:
        if version is not None:
            record = self._records.get((device, version))
            return dict(record) if record is not None and record["status"] == "published" else None
        published = [
            record
            for (record_device, _), record in self._records.items()
            if record_device == device and record["status"] == "published"
        ]
        return dict(published[0]) if published else None

    def _write_index(self) -> None:
        data = {"firmwares": self._list_sync()}
        _atomic_write_json(self.root / _REPOSITORY_FILENAME, data)

    def _sync_device_json_files(self) -> None:
        published_devices: set[str] = set()
        for (device, _), record in self._records.items():
            if record["status"] != "published":
                continue
            published_devices.add(device)
            payload = {
                "device": device,
                "version": record["version"],
                "firmware": f"/update/firmware?device={device}&version={record['version']}",
                "md5": record["md5"],
                "published_at": record.get("published_at", ""),
            }
            _atomic_write_json(self.root / f"{device}.json", payload)
        for path in self.root.glob("*.json"):
            if path.name != _REPOSITORY_FILENAME and path.stem not in published_devices:
                path.unlink(missing_ok=True)


def _validate_key(device: str, version: str) -> None:
    if not isinstance(device, str) or not _DEVICE_RE.fullmatch(device) or device in {".", ".."}:
        raise ValueError("Invalid device name")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version) or version in {".", ".."}:
        raise ValueError("Invalid firmware version")


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    return record["device"], record["version"]


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
