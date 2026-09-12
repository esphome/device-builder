"""Discovery / adoption helpers for the devices controller."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NamedTuple

from esphome import const
from esphome.storage_json import ignored_devices_storage_path

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.atomic_io import atomic_write_exclusive
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
    write_user_yaml,
)
from ...models import (
    AdoptableDevice,
    ErrorCode,
    EventType,
    ImportableDeviceAddedData,
    ImportableDeviceRemovedData,
)
from ..editor import IMPORT_VALIDATE_TIMEOUT, ValidatorUnavailableError
from .mutations_yaml import packages_block_span
from .resolve import resolve_config

if TYPE_CHECKING:
    from collections.abc import Callable
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


@dataclass(frozen=True, slots=True)
class _AdoptionKeyContext:
    """The adoption the key step works on: the validated unkeyed YAML and its shape."""

    controller: DevicesController
    name: str
    path: Path
    content: str
    full_config_import: bool


class _KeyOutcome(NamedTuple):
    """What the key step reports, plus the YAML still to write and the pending key to consume."""

    validation_warning: str | None
    key_warning: str | None
    to_write: str | None = None
    consume: str | None = None


class _KeyedRecheck(NamedTuple):
    """Verdict of re-validating a keyed YAML: the new warning, or why the key was refused."""

    warning: str | None
    refusal: str | None


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
    # Peek, don't pop — a failed import must keep the key for retry.
    pending = controller._pending_keys.get(name)
    try:
        if full_config_import:
            # A ``?full_config`` import downloads and rewrites the whole
            # upstream YAML — keep delegating those to esphome's
            # implementation. ``esphome.components.dashboard_import`` pulls
            # in ~14 MB of upstream code; load it through
            # ``async_import_module`` so the first such adoption pays the
            # cost on the dedicated import thread (no event loop block, no
            # concurrent-import race) and sessions that never need it skip
            # the load entirely.
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

            def _write_exclusive() -> None:
                # Staged exclusive-create, matching ``import_config``'s
                # FileExistsError contract for a concurrent writer.
                atomic_write_exclusive(path, content.encode("utf-8"))

            await run_in_executor(_write_exclusive)
    except FileExistsError as exc:
        msg = f"Configuration {configuration} already exists"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc

    # Validate the freshly-written YAML; on a genuine failure the cleanup
    # callback unlinks it so a retry doesn't trip ``FileExistsError``. Adopt
    # tolerates a validator timeout on a short budget: the config's
    # ``github://`` fetch can outlast a full validate.
    def _read() -> str:
        return path.read_text(encoding="utf-8")

    def _cleanup() -> None:
        path.unlink(missing_ok=True)

    try:
        content = await run_in_executor(_read)
    except (OSError, UnicodeDecodeError):
        await run_in_executor(_cleanup)
        raise
    warning = await controller._validate_rewritten_yaml_or_raise(
        configuration,
        content,
        action="import",
        on_error_cleanup=_cleanup,
        tolerate_unavailable=True,
        timeout=IMPORT_VALIDATE_TIMEOUT,
        # The delegated full-config path writes verbatim upstream YAML;
        # only our generated adoption shape gets the keep-with-warning
        # classification.
        packages_span=None if full_config_import else packages_block_span(content),
        failure_tail=". The import was rolled back; nothing was written.",
    )

    outcome = await _land_adoption_key(
        _AdoptionKeyContext(controller, name, path, content, full_config_import),
        warning=warning,
        pending=pending,
        encryption=encryption,
        cleanup=_cleanup,
    )

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
    if warnings := [w for w in (outcome.validation_warning, outcome.key_warning) if w]:
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
        updated = replace(existing, ignored=ignore)
        controller.state.import_result[name] = updated
        controller._db.bus.fire(
            EventType.IMPORTABLE_DEVICE_ADDED, ImportableDeviceAddedData(device=updated)
        )


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
    ignored = data.get("ignored_devices", [])
    if not isinstance(ignored, list):
        _LOGGER.warning(
            "Ignored-devices file at %s has a non-list ``ignored_devices`` "
            "field; resetting to an empty set",
            storage_path,
        )
        controller.state.ignored_devices.clear()
        return
    controller.state.ignored_devices.clear()
    controller.state.ignored_devices.update(name for name in ignored if isinstance(name, str))


def save_ignored_devices(controller: DevicesController) -> None:
    """Persist ``controller.state.ignored_devices`` to the on-disk JSON file."""
    storage_path = ignored_devices_storage_path()
    storage_path.write_bytes(
        dumps_indent({"ignored_devices": sorted(controller.state.ignored_devices)}),
    )


async def _land_adoption_key(
    ctx: _AdoptionKeyContext,
    *,
    warning: str | None,
    pending: dict[str, str] | None,
    encryption: str | None,
    cleanup: Callable[[], None],
) -> _KeyOutcome:
    """Run the key step; a failure past validation rolls the adoption back so a retry works."""
    succeeded = False
    try:
        outcome = await _finalize_adoption_key(
            ctx, warning=warning, pending=pending, encryption=encryption
        )
        succeeded = True
    finally:
        if not succeeded:
            try:
                # Shielded so a cancelled task still finishes the unlink before a retry.
                await asyncio.shield(run_in_executor(_roll_back, cleanup))
            except BaseException:
                _LOGGER.exception("Rolling the adoption back did not complete")
    return outcome


def _roll_back(cleanup: Callable[[], None]) -> None:
    """Run *cleanup*, logging a failure so the original error stays the one surfaced."""
    try:
        cleanup()
    except Exception:
        _LOGGER.exception("Rolling the adoption back failed; original error kept")


async def _finalize_adoption_key(
    ctx: _AdoptionKeyContext,
    *,
    warning: str | None,
    pending: dict[str, str] | None,
    encryption: str | None,
) -> _KeyOutcome:
    """Land the right API key after validation; owns the write and the pending-key consumption."""
    # Re-peek: a push can land during the validate window, after the
    # generate-time peek; minting over it would bake a competing key. The
    # generate-time value counts only while it is baked into the content: an
    # entry the configured-device handoff consumed meanwhile put a newer key
    # in the file, which must not be overwritten.
    fresh = ctx.controller._pending_keys.get(ctx.name)
    if fresh is None and pending is not None and api_key_settled(ctx.content, pending["key"]):
        fresh = pending
    if fresh:
        outcome = await _splice_pending_key_validated(ctx, fresh["key"], warning)
    elif encryption and not ctx.full_config_import:
        outcome = await _mint_key_unless_package_encrypts(ctx, warning)
    else:
        return _KeyOutcome(warning, None)
    refusal = await _land_key(ctx, outcome)
    return outcome._replace(key_warning=outcome.key_warning or refusal)


async def _land_key(ctx: _AdoptionKeyContext, outcome: _KeyOutcome) -> str | None:
    """Write the keyed YAML, a key pushed meanwhile winning; returns a refused swap's warning."""
    to_write, consume, refusal = outcome.to_write, outcome.consume, None
    if (
        to_write is not None
        and consume is not None
        and not ctx.controller._pending_keys.get(ctx.name)
    ):
        # The entry went while the key was checked: the handoff wrote this file, or a
        # duplicate-name sibling consumed it. Only a key already on disk says which.
        if await _key_on_disk(ctx):
            _LOGGER.warning(
                "Pending key for %s landed through the handoff; write skipped", ctx.name
            )
            return None
        consume = None
    if to_write is not None:
        to_write, consume, refusal = _prefer_pushed_key(ctx, to_write, consume)
        await run_in_executor(write_user_yaml, ctx.path, to_write)
    if consume is not None:
        ctx.controller._pending_keys.pop_if(ctx.name, consume)
    return refusal


async def _key_on_disk(ctx: _AdoptionKeyContext) -> bool:
    """Whether the adoption YAML on disk already carries an api key."""
    on_disk = await run_in_executor(ctx.path.read_text, "utf-8")
    return read_yaml_scalar(on_disk, API_ENCRYPTION_KEY_PATH) is not None


def _prefer_pushed_key(
    ctx: _AdoptionKeyContext, keyed: str, consume: str | None
) -> tuple[str, str | None, str | None]:
    """Swap a key pushed meanwhile into *keyed*; ``(to_write, consume, refusal)``."""
    pushed = ctx.controller._pending_keys.get(ctx.name)
    if pushed is None or pushed["key"] == consume:
        return keyed, consume, None
    splice = _splice_pending_key(keyed, pushed["key"], insert_api=not ctx.full_config_import)
    if splice.keyed is None:
        _LOGGER.warning(
            "Pushed key not applied to %s (%s); written key kept", ctx.path.name, splice.refusal
        )
        return keyed, consume, f"{splice.refusal} The key Home Assistant pushed stays stored."
    return splice.keyed, pushed["key"], None


async def _mint_key_unless_package_encrypts(
    ctx: _AdoptionKeyContext, warning: str | None
) -> _KeyOutcome:
    """Bake a fresh API key unless the resolved package already enables encryption."""
    # Bounded only by resolve_config's per-leg ceiling: adoption is user-triggered,
    # and getting a key beats dialog latency.
    config, resolved = await resolve_config(ctx.controller, ctx.path, spawn=warning is None)
    api_block = config.get("api") if config else None
    # Presence check, not get_api_encryption_block: a bare ``encryption:``
    # can resolve to null and must still count as package-provided.
    if isinstance(api_block, dict) and "encryption" in api_block:
        return _KeyOutcome(warning, None)
    # A package's own OTA key would have to match a baked api key; leave both out.
    # A whole ``encryption:`` the loader left as a bare string is read the same way.
    if get_ota_encryption_key(config) or ota_encryption_block_unresolved(config):
        return _KeyOutcome(warning, _OWN_OTA_KEY_WARNING)
    if not resolved and (config is None or _INHERIT_ERROR_MARK not in (warning or "")):
        _LOGGER.warning("Could not resolve %s; adopted without a generated API key", ctx.path.name)
        return _KeyOutcome(warning, _UNRESOLVED_WARNING)
    return await _mint_key(ctx, warning, resolved=resolved)


async def _mint_key(
    ctx: _AdoptionKeyContext, warning: str | None, *, resolved: bool
) -> _KeyOutcome:
    """Splice a fresh key and re-check when the unkeyed YAML warned; strict when unresolved."""
    spliced = _splice_fresh_key(ctx.content, ctx.path.name)
    if spliced.keyed is None:
        return _KeyOutcome(warning, spliced.refusal)
    if warning is not None:
        recheck = await _revalidate_keyed(
            ctx, spliced.keyed, warning, failure_tail=". Adopted without a key."
        )
        if recheck.refusal is not None:
            return _KeyOutcome(warning, recheck.refusal)
        if not resolved and recheck.warning is not None:
            _LOGGER.warning(
                "Could not resolve %s; a key did not repair it (%s), adopted without one",
                ctx.path.name,
                recheck.warning,
            )
            return _KeyOutcome(warning, _UNRESOLVED_WARNING)
        warning = recheck.warning
    return _KeyOutcome(warning, None, to_write=spliced.keyed)


async def _splice_pending_key_validated(
    ctx: _AdoptionKeyContext, key: str, warning: str | None
) -> _KeyOutcome:
    """Splice the HA-provisioned *key* and let esphome check it; a refusal keeps the key pending."""
    not_applied_tail = (
        " The key Home Assistant provisioned was not applied and stays "
        "stored; installing this config may cut Home Assistant off "
        "until it re-provisions."
    )
    if api_key_settled(ctx.content, key):
        return _KeyOutcome(warning, None, consume=key)
    splice = _splice_pending_key(ctx.content, key, insert_api=not ctx.full_config_import)
    if splice.keyed is None:
        return _KeyOutcome(warning, f"{splice.refusal}{not_applied_tail}")
    # An OTA block the line walker can't read may still hold a key the splice
    # can't reconcile; esphome decides before anything is written.
    recheck = await _revalidate_keyed(
        ctx, splice.keyed, warning, failure_tail=f".{not_applied_tail}"
    )
    if recheck.refusal is not None:
        return _KeyOutcome(warning, recheck.refusal)
    return _KeyOutcome(recheck.warning, None, to_write=splice.keyed, consume=key)


async def _revalidate_keyed(
    ctx: _AdoptionKeyContext, keyed: str, warning: str | None, *, failure_tail: str
) -> _KeyedRecheck:
    """Re-check a keyed YAML; an outage keeps *warning*, a refusal carries *failure_tail*."""
    try:
        verdict = await ctx.controller._validate_rewritten_yaml_or_raise(
            ctx.path.name,
            keyed,
            action="import",
            timeout=IMPORT_VALIDATE_TIMEOUT,
            packages_span=None if ctx.full_config_import else packages_block_span(keyed),
            failure_tail=failure_tail,
        )
    except CommandError as err:
        return _KeyedRecheck(warning, err.message)
    except ValidatorUnavailableError as err:
        _LOGGER.warning(
            "Validator unavailable during the key re-check of %s (%r); warning kept",
            ctx.path.name,
            err,
        )
        return _KeyedRecheck(warning, None)
    return _KeyedRecheck(verdict, None)


def _splice_fresh_key(content: str, config_name: str) -> _SplicedKey:
    """Splice a freshly minted key into *content*; ``(None, warning)`` when the shape refuses it."""
    new_key = generate_api_encryption_key()
    reason = ""
    try:
        keyed = upsert_api_encryption_key(content, new_key)
    except YamlUpsertNotSupportedError as exc:
        keyed, reason = "", f" ({exc})"
    if not keyed or not api_key_settled(keyed, new_key):
        _LOGGER.warning(
            "Could not splice a key into %s%s; adopted without one", config_name, reason
        )
        return _SplicedKey(
            None,
            f"A generated API encryption key could not be spliced in{reason}; adopted without one.",
        )
    return _SplicedKey(keyed, None)


def _splice_pending_key(content: str, key: str, *, insert_api: bool) -> _SplicedKey:
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
