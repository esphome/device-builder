"""Receiver-side remote-build settings persistence backed by the metadata sidecar."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ...models import RemoteBuildSettings
from .metadata import _load_metadata, metadata_transaction

_LOGGER = logging.getLogger(__name__)

_REMOTE_BUILD_KEY = "_remote_build"

_REMOTE_BUILD_FAIL_SAFE = RemoteBuildSettings(enabled=False)


def _settings_from_raw(raw: Any) -> RemoteBuildSettings:
    """
    Decode a ``_remote_build`` blob, failing safe on shape mismatch.

    With the default ``RemoteBuildSettings.enabled=True``, a
    malformed blob can no longer silently fall through to
    "defaults" — that path would enable the listener on a
    corrupted sidecar without any operator opt-in. Instead:

    * A non-dict raw value (None / list / scalar from a hand-
      edit or partial-write) returns ``enabled=False``.
    * A dict that fails ``from_dict`` decode (schema break,
      type-incompatible value on a known field) returns
      ``enabled=False``.

    Both paths log a warning at the call site so the operator
    can spot the corrupted sidecar.

    Legacy ``tokens`` / ``manual_hosts`` / ``peers`` entries
    on older ``.device-builder.json`` files are silently
    dropped — mashumaro's ``DataClassORJSONMixin`` ignores
    unknown keys by default. The ``tokens`` field went with
    the pre-Noise bearer machinery; ``manual_hosts`` was
    removed once the pair dialog started typing hostnames
    straight into ``request_pair``; ``peers`` moved to its
    own per-file ``Store`` at ``.receiver_peers.json``.
    """
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Malformed ``_remote_build`` block in metadata "
            "(expected dict, got %s); failing safe to enabled=False. "
            "Fix or remove the block to recover default behaviour.",
            type(raw).__name__,
        )
        return _REMOTE_BUILD_FAIL_SAFE
    # Drop the legacy ``tokens`` key explicitly so a corrupt
    # token row in an older sidecar can't crash the whole
    # ``from_dict`` decode of an otherwise-valid blob.
    cleaned = {k: v for k, v in raw.items() if k != "tokens"}
    try:
        return RemoteBuildSettings.from_dict(cleaned)
    except Exception:
        _LOGGER.exception(
            "Failed to decode ``_remote_build`` block in metadata; "
            "failing safe to enabled=False. Fix or remove the block "
            "to recover default behaviour."
        )
        return _REMOTE_BUILD_FAIL_SAFE


def load_remote_build_settings(config_dir: Path) -> RemoteBuildSettings:
    """
    Load the receiver-side remote-build settings.

    Returns defaults (``RemoteBuildSettings()``, i.e.
    ``enabled=True``) when the metadata file is missing or the
    ``_remote_build`` key isn't present (fresh install). A
    present-but-malformed block fails safe to
    ``enabled=False`` rather than silently inheriting the
    permissive default — see :func:`_settings_from_raw` for
    the corruption-path rationale.

    HA-addon callers that need to suppress the auto-bind on a
    fresh install should pair this with
    :func:`has_remote_build_settings_persisted` and gate
    accordingly — the load function returns the dataclass
    semantically; the deployment-mode rule lives at the bind
    site so the toggle's "operator opted in" signal isn't
    lost.
    """
    metadata = _load_metadata(config_dir)
    if _REMOTE_BUILD_KEY not in metadata:
        return RemoteBuildSettings()
    return _settings_from_raw(metadata[_REMOTE_BUILD_KEY])


def has_remote_build_settings_persisted(config_dir: Path) -> bool:
    """
    Return ``True`` when ``_remote_build`` has been explicitly written.

    Distinguishes "fresh install, never touched the toggle"
    (returns ``False``) from "operator deliberately set a value,
    even if that value matches the dataclass default" (returns
    ``True``). The HA-addon default-off rule keys on this so a
    fresh addon install doesn't bind port 6055 (the container
    doesn't expose it anyway) but an operator who flips the
    toggle in Settings still gets the receiver bound regardless
    of deployment mode.

    The block must also have the expected on-disk shape (a
    dict). A malformed ``_remote_build`` value (list, scalar,
    null) doesn't count as opt-in — ``set_settings`` writes
    ``RemoteBuildSettings.to_dict()`` which is always a dict,
    so any non-dict value reached the sidecar via a hand-edit
    or partial-write, not an operator interaction with the
    toggle. Returning ``False`` for that shape keeps the
    HA-addon gate consistent with the fail-safe shape in
    :func:`_settings_from_raw`.
    """
    return isinstance(_load_metadata(config_dir).get(_REMOTE_BUILD_KEY), dict)


def save_remote_build_settings(config_dir: Path, settings: RemoteBuildSettings) -> None:
    """Persist the receiver-side remote-build settings."""
    with metadata_transaction(config_dir) as data:
        data[_REMOTE_BUILD_KEY] = settings.to_dict()


@contextmanager
def remote_build_settings_transaction(
    config_dir: Path,
) -> Iterator[RemoteBuildSettings]:
    """
    Atomic read-modify-write context for the remote-build settings.

    Yields the current :class:`RemoteBuildSettings` (defaults if
    missing or corrupt). Mutate it in place; on a clean exit the
    changes are persisted under the same ``metadata_transaction``
    lock, so the whole RMW is atomic against concurrent
    transactions. Exceptions raised inside the block discard the
    pending mutation.

    Use this whenever an operation "depends on the current state
    to compute the next state": add / remove a manual host, flip
    ``enabled`` while preserving the rest. A bare ``load + save``
    pair is racy because two concurrent callers can both read the
    same starting value and the second save wipes the first's
    change.
    """
    with metadata_transaction(config_dir) as data:
        settings = _settings_from_raw(data.get(_REMOTE_BUILD_KEY, {}))
        yield settings
        data[_REMOTE_BUILD_KEY] = settings.to_dict()
