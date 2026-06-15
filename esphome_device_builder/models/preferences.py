"""User preferences models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro.mixins.orjson import DataClassORJSONMixin


class DashboardView(StrEnum):
    """Dashboard device list view mode."""

    CARDS = "cards"
    TABLE = "table"


class Theme(StrEnum):
    """UI theme."""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class SortDirection(StrEnum):
    """Table sort direction."""

    ASC = "asc"
    DESC = "desc"


class ExperienceLevel(StrEnum):
    """
    How much ESPHome the user knows; tailors UI weight.

    Chosen in onboarding, changeable any time via the Settings expert-mode
    toggle. ``EXPERT`` unlocks the power-user surfaces (editor diff, navigator
    and YAML search). ``None`` (a fresh install that hasn't picked) is handled
    separately; a pre-existing install migrates to ``EXPERT``.
    """

    BEGINNER = "beginner"
    EXPERT = "expert"


@dataclass
class UserPreferences(DataClassORJSONMixin):
    """Per-user UI preferences.

    Stored in .device-builder.json under the _preferences key.
    All fields have sensible defaults so a fresh install works out of the box.
    """

    # Dashboard view
    dashboard_view: DashboardView = DashboardView.CARDS
    theme: Theme = Theme.SYSTEM

    # Device editor
    navigator_visible: bool = True

    # Table view settings
    table_page_size: int = 25
    table_column_visibility: dict[str, bool] = field(default_factory=dict)
    table_sort_column: str | None = None
    table_sort_direction: SortDirection | None = None

    # Experience level chosen in onboarding (None = not yet chosen).
    # ``EXPERT`` unlocks the power-user editor and search surfaces.
    experience_level: ExperienceLevel | None = None
    # This install is only a remote build node: onboarding skips the
    # Wi-Fi step and device-creation entry points are hidden.
    remote_compute_only: bool = False

    # Highest onboarding-flow version the user has acknowledged.
    # Default 0 ⇒ never gone through onboarding; the dashboard
    # surfaces the wizard on next load. See
    # ``models/onboarding.ONBOARDING_VERSION`` for the server
    # side; bumping that constant when adding new steps re-prompts
    # users at lower versions.
    onboarding_completed_version: int = 0
