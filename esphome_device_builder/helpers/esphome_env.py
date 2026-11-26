"""The environment every ``esphome`` subprocess that loads a device config runs under."""

from __future__ import annotations

import os
from collections.abc import Mapping

from esphome.const import ENV_NOGITIGNORE

from .config_layout import in_devices_dir
from .remote_build_layout import parse_from_configuration as parse_remote_build_path
from .storage_path import resolve_data_dir


def esphome_subprocess_env(
    configuration: str = "", extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """
    Return ``os.environ`` + *extra* with ``ESPHOME_DATA_DIR`` pinned. Blocking.

    The pin is :func:`resolve_data_dir` of *configuration*, so a device below
    ``devices/`` builds into the dashboard's data dir rather than one beside its YAML,
    and its compile writes no ``.gitignore`` into its folder.
    """
    env = {**os.environ, **(extra or {})}
    env["ESPHOME_DATA_DIR"] = str(resolve_data_dir(configuration))
    if in_devices_dir(configuration):
        env[ENV_NOGITIGNORE] = "1"
    if parse_remote_build_path(configuration) is not None:
        # esphome's CORE.data_dir prefers is_ha_addon() over ESPHOME_DATA_DIR; an
        # add-on receiver would write artefacts to /data and ignore the override.
        env.pop("ESPHOME_IS_HA_ADDON", None)
    return env
