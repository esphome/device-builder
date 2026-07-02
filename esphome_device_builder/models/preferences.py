"""User preferences models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .common import DashboardModel


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


class EditorLayout(StrEnum):
    """Device editor pane layout: the form, the YAML pane, or both."""

    VISUAL = "visual"
    YAML = "yaml"
    BOTH = "both"


class SecretsEditorLayout(StrEnum):
    """Secrets editor layout: the form or the YAML pane, never both."""

    VISUAL = "visual"
    YAML = "yaml"


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
class UserPreferences(DashboardModel):
    """Per-user UI preferences.

    Stored in .device-builder.json under the _preferences key.
    All fields have sensible defaults so a fresh install works out of the box.
    """

    # Dashboard view
    dashboard_view: DashboardView = DashboardView.CARDS
    theme: Theme = Theme.SYSTEM

    # Device editor
    navigator_visible: bool = True
    # Which editor panes the user last had open, persisted so the choice
    # survives a new browser. The secrets editor has no split view, so a
    # dedicated enum makes "never both" a type rather than a comment.
    device_editor_layout: EditorLayout = EditorLayout.BOTH
    secrets_editor_layout: SecretsEditorLayout = SecretsEditorLayout.VISUAL

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

    # Auto-commit config edits to a git history. Off stops new commits
    # and skips repo creation; the toggle is an expert-only surface.
    version_history_enabled: bool = True

    # Highest onboarding-flow version the user has acknowledged.
    # Default 0 ⇒ never gone through onboarding; the dashboard
    # surfaces the wizard on next load. See
    # ``models/onboarding.ONBOARDING_VERSION`` for the server
    # side; bumping that constant when adding new steps re-prompts
    # users at lower versions.
    onboarding_completed_version: int = 0
