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
    yaml_diff_button: bool = False

    # Table view settings
    table_page_size: int = 25
    table_column_visibility: dict[str, bool] = field(default_factory=dict)
    table_sort_column: str | None = None
    table_sort_direction: SortDirection | None = None

    # Highest onboarding-flow version the user has acknowledged.
    # Default 0 ⇒ never gone through onboarding; the dashboard
    # surfaces the wizard on next load. See
    # ``models/onboarding.ONBOARDING_VERSION`` for the server
    # side; bumping that constant when adding new steps re-prompts
    # users at lower versions.
    onboarding_completed_version: int = 0
