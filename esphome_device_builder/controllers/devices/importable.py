"""Discovery / adoption helpers for the devices controller."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NamedTuple

from esphome import const
from esphome.storage_json import ignored_devices_storage_path

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.atomic_io import atomic_write_exclusive, atomic_write_preserving_mode
from ...helpers.device_yaml import (
    generate_adoption_yaml,
    get_ota_encryption_key,
    ota_encryption_block_unresolved,
)
from ...helpers.json import JSONDecodeError, dumps_indent, loads
from ...helpers.lazy_module import async_import_module
from ...helpers.yaml import (
    API_ENCRYPTION_KEY_PATH,
    YamlUpsertNotSupportedError,
    api_key_settled,
    component_block_present,
    generate_api_encryption_key,
    read_yaml_scalar,
    upsert_api_encryption_key,
)
from ...models import (
    AdoptableDevice,
    ErrorCode,
    EventType,
    ImportableDeviceAddedData,
    ImportableDeviceRemovedData,
)
from ..editor import IMPORT_VALIDATE_TIMEOUT, ValidatorUnavailableError
from .mutations_yaml import PackageWarning, packages_block_span
from .resolve import resolve_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

_UNRESOLVED_WARNING = (
    "The package could not be resolved during adoption, so no API "
    "encryption key was added; edit and install the device to "
    "add one, or let Home Assistant provision it."
)
_OWN_OTA_KEY_WARNING = (
    "The package gives the OTA platform its own encryption key, so no API "
    "encryption key was generated; edit the device to use one key for both."
)
_INHERIT_ERROR_MARK = "encryption key to inherit"
_STAYS_STORED = (
    "was not applied and stays stored; installing this config may cut Home Assistant off "
    "until it re-provisions."
)
_NOT_APPLIED_TAIL = f" The key Home Assistant provisioned {_STAYS_STORED}"
_LATE_PUSH_WARNING = (
    f"A key Home Assistant pushed while the config was being written {_STAYS_STORED}"
)


@dataclass(frozen=True, slots=True)
class _AdoptionKeyContext:
    """The adoption the key step works on: the validated unkeyed YAML and its shape."""

    controller: DevicesController
    name: str
    path: Path
    content: str
    full_config_import: bool

    @property
    def insert_api(self) -> bool:
        """Whether a splice may add the ``api:`` block; never on verbatim upstream YAML."""
        return not self.full_config_import

    def packages_span(self, text: str) -> tuple[int, int] | None:
        """Return the span of *text* whose errors count as package-confined, or none."""
        return None if self.full_config_import else packages_block_span(text)


class _KeyOutcome(NamedTuple):
    """What the key step reports, plus the YAML still to write and the pending key to consume."""

    validation_warning: PackageWarning | None
    key_warning: str | None
    to_write: str | None = None
    consume: str | None = None


class _SplicedKey(NamedTuple):
    """A freshly keyed YAML, or why the shape refused the splice."""

    keyed: str | None
    refusal: str | None


async def import_device(
    controller: DevicesController,
    *,
    name: str,
    project_name: str,
    package_import_url: str,
    friendly_name: str | None,
    encryption: str | None,
) -> dict:
    """Import / adopt a discovered device."""
    configuration = f"{name}.yaml"
    path = controller._db.settings.rel_path(configuration)
    # The adopt dialog always imports under the factory broadcast name
    # (frontend applies an edited name via the post-adopt rename flow),
    # so the name-keyed lookup is exact; an absent row falls through to
    # Wi-Fi.
    adoptable = controller.state.import_result.get(name)
    network = adoptable.network if adoptable and adoptable.network else const.CONF_WIFI
    full_config_import = "full_config" in package_import_url.partition("?")[2]
    async with _name_claimed(controller, name):
        # Peek, don't pop; a failed import must keep the key for retry.
        pending = controller._pending_keys.get(name)
        content: str | None = None
        try:
            if full_config_import:
                # A ``?full_config`` import downloads and rewrites the whole
                # upstream YAML; keep delegating those to esphome's
                # implementation. ``esphome.components.dashboard_import`` pulls
                # in ~14 MB of upstream code, loaded lazily off the loop.
                dashboard_import = await async_import_module("esphome.components.dashboard_import")
                await run_in_executor(
                    dashboard_import.import_config,
                    path,
                    name,
                    friendly_name,
                    project_name,
                    package_import_url,
                    network,
                    encryption,
                )
            else:
                content = generate_adoption_yaml(
                    name,
                    friendly_name,
                    project_name,
                    package_import_url,
                    network_provided=network != const.CONF_WIFI,
                    api_encryption=False,
                    api_encryption_key=pending["key"] if pending else None,
                )
                await run_in_executor(atomic_write_exclusive, path, content.encode("utf-8"))
        except FileExistsError as exc:
            msg = f"Configuration {configuration} already exists"
            raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

        async with _rolled_back_on_failure(path):
            if content is None:
                content = await controller._read_yaml_async(path)
            ctx = _AdoptionKeyContext(controller, name, path, content, full_config_import)
            # Adopt tolerates a validator timeout on a short budget: the config's
            # ``github://`` fetch can outlast a full validate.
            warning = await controller._validate_rewritten_yaml_or_raise(
                configuration,
                content,
                action="import",
                tolerate_unavailable=True,
                timeout=IMPORT_VALIDATE_TIMEOUT,
                packages_span=ctx.packages_span(content),
                failure_tail=". The import was rolled back; nothing was written.",
            )
            outcome = await _finalize_adoption_key(ctx, warning=warning, encryption=encryption)

    await controller._commit_history(configuration, f"Import {configuration}")

    # Post-write scan is best-effort; the next periodic scan
    # will catch the new YAML and failing here would mislead the
    # user into a retry that trips ``FileExistsError``.
    try:
        await controller._scanner.scan()
    except Exception:
        _LOGGER.exception("Scan after import failed; will pick up on next poll")

    _drop_importable_row_and_probe(controller, name)
    result = {"configuration": configuration}
    validation = outcome.validation_warning
    if warnings := [w for w in (validation.text if validation else None, outcome.key_warning) if w]:
        result["warning"] = "\n".join(warnings)
    return result


async def toggle_ignore(controller: DevicesController, *, name: str, ignore: bool) -> None:
    """Mark a discovered device as ignored / visible in the import list."""
    if ignore:
        controller.state.ignored_devices.add(name)
    else:
        controller.state.ignored_devices.discard(name)
    await run_in_executor(controller._save_ignored_devices)
    # Mirror the new flag onto the cached AdoptableDevice and
    # re-publish ADDED so subscribed frontends update the badge
    # without waiting for a full re-discovery cycle.
    existing = controller.state.import_result.get(name)
    if existing is not None and existing.ignored != ignore:
        on_importable_added(controller, replace(existing, ignored=ignore))


def on_importable_added(controller: DevicesController, device: AdoptableDevice) -> None:
    """Stash a newly-discovered importable device and notify subscribers."""
    controller.state.import_result[device.name] = device
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=device)
    )


def on_importable_removed(controller: DevicesController, name: str) -> None:
    """Forget an importable device that disappeared from mDNS."""
    if controller.state.import_result.pop(name, None) is None:
        return
    controller._db.bus.fire(
        EventType.IMPORTABLE_DEVICE_REMOVED, ImportableDeviceRemovedData(name=name)
    )


def get_importable_devices(controller: DevicesController) -> list[AdoptableDevice]:
    """Snapshot of importable devices, filtered against the configured-name set."""
    configured_names = {d.name for d in controller._scanner.devices}
    return [d for d in controller.state.import_result.values() if d.name not in configured_names]


def load_ignored_devices(controller: DevicesController) -> None:
    """Populate ``controller.state.ignored_devices`` from the on-disk JSON file."""
    storage_path = ignored_devices_storage_path()
    try:
        raw = storage_path.read_bytes()
    except FileNotFoundError:
        return
    try:
        data = loads(raw)
    except JSONDecodeError:
        # A corrupt file shouldn't tank controller bootstrap;
        # start with an empty ignored set and let the next
        # toggle_ignore call rewrite it cleanly.
        _LOGGER.warning(
            "Ignored-devices file at %s is corrupt; starting with an empty set",
            storage_path,
        )
        return
    if not isinstance(data, dict):
        _LOGGER.warning(
            "Ignored-devices file at %s isn't a JSON object; starting with an empty set",
            storage_path,
        )
        return
    # Mutate the set in place rather than replacing it. The
    # ``DeviceStateMonitor`` captures
    # ``state.ignored_devices.__contains__`` at controller
    # ``__init__`` time, before this loader runs in
    # ``start()``; replacing the set here would leave the
    # monitor checking a stale empty set forever.
    controller.state.ignored_devices.clear()
    ignored = data.get("ignored_devices", [])
    if not isinstance(ignored, list):
        _LOGGER.warning(
            "Ignored-devices file at %s has a non-list ``ignored_devices`` "
            "field; resetting to an empty set",
            storage_path,
        )
        return
    controller.state.ignored_devices.update(name for name in ignored if isinstance(name, str))


def save_ignored_devices(controller: DevicesController) -> None:
    """Persist ``controller.state.ignored_devices`` to the on-disk JSON file."""
    atomic_write_preserving_mode(
        ignored_devices_storage_path(),
        dumps_indent({"ignored_devices": sorted(controller.state.ignored_devices)}),
    )


@asynccontextmanager
async def _name_claimed(controller: DevicesController, name: str) -> AsyncIterator[None]:
    """Hold *name* against the key handoff and a second adopt for the block, rollback included."""
    if name in controller.state.adopting:
        raise CommandError(ErrorCode.INVALID_ARGS, f"Configuration {name}.yaml is being adopted")
    controller.state.adopting.add(name)
    try:
        yield
    finally:
        controller.state.adopting.discard(name)


@asynccontextmanager
async def _rolled_back_on_failure(path: Path) -> AsyncIterator[None]:
    """Discard *path* when the block fails; a file that stays behind is named in the error."""
    failure: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if failure is not None and not await _try_discard(path) and isinstance(failure, Exception):
            _LOGGER.error(
                "Adoption of %s failed and its YAML could not be removed",
                path.stem,
                exc_info=failure,
            )
            raise CommandError(
                ErrorCode.INTERNAL_ERROR,
                f"Adoption failed: {failure}. The partially written {path.name} could "
                "not be removed; delete it before retrying.",
            ) from failure


async def _try_discard(path: Path) -> bool:
    """Remove *path* off the loop, shielded; ``False`` when the file may still be there."""
    try:
        await asyncio.shield(run_in_executor(_discard, path))
    except Exception:
        _LOGGER.exception("Rolling the adoption back did not complete; %s may remain", path.name)
        return False
    return True


def _discard(path: Path) -> None:
    """Remove the adoption YAML."""
    path.unlink(missing_ok=True)


async def _finalize_adoption_key(
    ctx: _AdoptionKeyContext, *, warning: PackageWarning | None, encryption: str | None
) -> _KeyOutcome:
    """Land the right API key after validation; owns the write and the pending-key consumption."""
    # Re-peek: a push can land during the validate window, after the generate-time peek.
    fresh = _pending_key(ctx)
    if fresh is not None:
        outcome = await _splice_pending_key_validated(ctx, fresh, warning)
    elif encryption and ctx.insert_api:
        outcome = await _mint_key_unless_package_encrypts(ctx, warning)
    else:
        outcome = _KeyOutcome(warning, None)
    return await _write_keyed(ctx, outcome, handled=fresh)


async def _write_keyed(
    ctx: _AdoptionKeyContext, outcome: _KeyOutcome, *, handled: str | None
) -> _KeyOutcome:
    """Write the keyed YAML, a key pushed since *handled* winning, and consume the pending key."""
    pushed = _pending_key(ctx)
    if pushed is not None and pushed != handled:
        outcome = _prefer_pushed_key(ctx, outcome, pushed)
    if outcome.to_write is not None:
        await ctx.controller._write_yaml_atomic_async(ctx.path, outcome.to_write)
    if outcome.consume is not None:
        ctx.controller._pending_keys.pop_if(ctx.name, outcome.consume)
    if _pending_key(ctx) in (None, pushed):
        return outcome
    _LOGGER.warning("A key pushed for %s while its config was being written stays stored", ctx.name)
    return outcome._replace(
        key_warning=" ".join(filter(None, (outcome.key_warning, _LATE_PUSH_WARNING)))
    )


def _pending_key(ctx: _AdoptionKeyContext) -> str | None:
    """Return the key Home Assistant has pending for this adoption, if any."""
    entry = ctx.controller._pending_keys.get(ctx.name)
    return None if entry is None else entry["key"]


def _prefer_pushed_key(ctx: _AdoptionKeyContext, outcome: _KeyOutcome, key: str) -> _KeyOutcome:
    """Splice the pushed *key* into the YAML about to land; a refusal leaves it stored."""
    base = ctx.content if outcome.to_write is None else outcome.to_write
    splice = _splice_key(base, key, insert_api=ctx.insert_api)
    if splice.keyed is None:
        _LOGGER.warning(
            "Pushed key not applied to %s (%s); written key kept", ctx.path.name, splice.refusal
        )
        return outcome._replace(key_warning=f"{splice.refusal}{_NOT_APPLIED_TAIL}")
    return outcome._replace(to_write=splice.keyed, consume=key)


async def _mint_key_unless_package_encrypts(
    ctx: _AdoptionKeyContext, warning: PackageWarning | None
) -> _KeyOutcome:
    """Bake a fresh API key unless the resolved package already enables encryption."""
    config, resolved = await resolve_config(ctx.controller, ctx.path, spawn=warning is None)
    api_block = config.get("api") if config else None
    # Presence check, not get_api_encryption_block: a bare ``encryption:``
    # can resolve to null and must still count as package-provided.
    if isinstance(api_block, dict) and "encryption" in api_block:
        return _KeyOutcome(warning, None)
    # A package's own OTA key would have to match a baked api key; leave both out.
    if get_ota_encryption_key(config) or ota_encryption_block_unresolved(config):
        return _KeyOutcome(warning, _OWN_OTA_KEY_WARNING)
    if not resolved and (config is None or not _only_missing_inherited_key(warning)):
        _LOGGER.warning("Could not resolve %s; adopted without a generated API key", ctx.path.name)
        return _KeyOutcome(warning, _UNRESOLVED_WARNING)
    return await _mint_key(ctx, warning, resolved=resolved)


async def _mint_key(
    ctx: _AdoptionKeyContext, warning: PackageWarning | None, *, resolved: bool
) -> _KeyOutcome:
    """Splice a fresh key and re-check when the unkeyed YAML warned; strict when unresolved."""
    splice = _splice_key(ctx.content, generate_api_encryption_key(), insert_api=ctx.insert_api)
    if splice.keyed is None:
        _LOGGER.warning(
            "Could not splice a key into %s (%s); adopted without one",
            ctx.path.name,
            splice.refusal,
        )
        return _KeyOutcome(
            warning,
            f"A generated API encryption key could not be spliced in ({splice.refusal}); "
            "adopted without one.",
        )
    if warning is None:
        return _KeyOutcome(None, None, to_write=splice.keyed)
    recheck = await _revalidate_keyed(
        ctx, splice.keyed, warning, failure_tail=". Adopted without a key."
    )
    if recheck.key_warning is not None:
        return recheck
    if not resolved and recheck.validation_warning is not None:
        _LOGGER.warning(
            "Could not resolve %s; a key did not repair it (%s), adopted without one",
            ctx.path.name,
            recheck.validation_warning.text,
        )
        return _KeyOutcome(warning, _UNRESOLVED_WARNING)
    return _KeyOutcome(recheck.validation_warning, None, to_write=splice.keyed)


async def _splice_pending_key_validated(
    ctx: _AdoptionKeyContext, key: str, warning: PackageWarning | None
) -> _KeyOutcome:
    """Splice the HA-provisioned *key* and let esphome check it; a refusal keeps the key pending."""
    if api_key_settled(ctx.content, key):
        return _KeyOutcome(warning, None, consume=key)
    splice = _splice_key(ctx.content, key, insert_api=ctx.insert_api)
    if splice.keyed is None:
        return _KeyOutcome(warning, f"{splice.refusal}{_NOT_APPLIED_TAIL}")
    # An OTA block the line walker can't read may still hold a key the splice
    # can't reconcile; esphome decides before anything is written.
    recheck = await _revalidate_keyed(
        ctx, splice.keyed, warning, failure_tail=f".{_NOT_APPLIED_TAIL}"
    )
    if recheck.key_warning is not None:
        return recheck
    return recheck._replace(to_write=splice.keyed, consume=key)


async def _revalidate_keyed(
    ctx: _AdoptionKeyContext, keyed: str, warning: PackageWarning | None, *, failure_tail: str
) -> _KeyOutcome:
    """Re-check a keyed YAML; an outage keeps *warning*, a refusal lands in ``key_warning``."""
    try:
        verdict = await ctx.controller._validate_rewritten_yaml_or_raise(
            ctx.path.name,
            keyed,
            action="import",
            timeout=IMPORT_VALIDATE_TIMEOUT,
            packages_span=ctx.packages_span(keyed),
            failure_tail=failure_tail,
        )
    except CommandError as err:
        return _KeyOutcome(warning, err.message)
    except ValidatorUnavailableError as err:
        _LOGGER.warning(
            "Validator unavailable during the key re-check of %s (%r); warning kept",
            ctx.path.name,
            err,
        )
        return _KeyOutcome(warning, None)
    return _KeyOutcome(verdict, None)


def _only_missing_inherited_key(warning: PackageWarning | None) -> bool:
    """Whether every package complaint is the api key a bare ``ota: encryption:`` inherits."""
    return warning is not None and all(_INHERIT_ERROR_MARK in m for m in warning.messages)


def _splice_key(content: str, key: str, *, insert_api: bool) -> _SplicedKey:
    """Splice *key* into *content*; a missing ``api:`` block is added only with *insert_api*."""
    if (
        not insert_api
        and read_yaml_scalar(content, API_ENCRYPTION_KEY_PATH) is None
        and not component_block_present(content, "api")
    ):
        return _SplicedKey(
            None,
            "The imported config does not declare an api: block, so there "
            "is nowhere to put the Home Assistant provisioned key.",
        )
    try:
        spliced = upsert_api_encryption_key(content, key)
    except YamlUpsertNotSupportedError as exc:
        return _SplicedKey(None, str(exc))
    if spliced == content:
        return _SplicedKey(
            None,
            "The imported config supplies its own API encryption key via !secret, !include, or a "
            "substitution.",
        )
    if not api_key_settled(spliced, key):
        return _SplicedKey(None, "The imported config's shape defeated the key splice.")
    return _SplicedKey(spliced, None)


def _drop_importable_row_and_probe(controller: DevicesController, name: str) -> None:
    """Retire the adopted device's importable row and kick its first probe."""
    # Drop only the adopted name's row — a URL-wide sweep would retire
    # every sibling unit of the same product. The removal is a no-op
    # when the post-write scan already pruned it.
    controller._on_importable_removed(name)

    # No state seed — the real sources decide. Discovery is
    # mDNS-based, so the adopt claims ONLINE via the esphomelib
    # probe's cache hit in this same call.
    cached = controller._state_monitor.mdns.get_cached_addresses(f"{name}.local")
    if cached:
        controller._state_monitor.apply_ip_addresses(name, cached)
    controller._state_monitor.mdns.probe_device(name)
