"""
Dashboard onboarding state models.

The dashboard tracks first-run setup the user needs to complete
before the editor is fully usable. Currently one step (Wi-Fi
credentials); designed to grow as we add more guidance (Home
Assistant addon hand-off, encryption-key defaults, …).

Step status is **data-derived** — the controller computes
``pending`` / ``done`` from the actual on-disk state every time
``get_state`` is called, rather than persisting per-step
completion. This keeps the badge accurate even when the user
configures something outside the dashboard (manual ``secrets.yaml``
edit, etc.).

Acknowledgement is tracked separately via
``onboarding_completed_version`` in user preferences. When a future
release adds a new step we bump :data:`ONBOARDING_VERSION`; existing
users with a lower ``completed_version`` flow through onboarding
again to see the new step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .common import DashboardModel


class OnboardingStepId(StrEnum):
    """Stable identifiers for onboarding steps.

    Don't rename — the frontend keys its UI off these strings.
    Acknowledgement is tracked via ``onboarding_completed_version``
    (an int), not per-step, so the only stability requirement is
    the wire-format string the frontend dispatches on.
    """

    USE_CASE = "use_case"
    EXPERIENCE_LEVEL = "experience_level"


class OnboardingStepStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


@dataclass
class OnboardingStep(DashboardModel):
    """One step in the onboarding flow.

    ``status`` is computed by the controller from live data on
    each ``get_state`` call — never persisted, never derived from
    user prefs. The frontend uses ``status == "pending"`` to
    decide whether to surface the step (dialog + menu badge).
    """

    id: OnboardingStepId
    status: OnboardingStepStatus


# Bump this when new onboarding steps are added. Existing users
# whose ``onboarding_completed_version`` is lower will see the
# onboarding dialog again with the new steps; a user who's
# already at the current version doesn't get re-prompted unless
# a step is data-derived-pending (e.g. they manually deleted
# ``wifi_ssid`` from ``secrets.yaml``).
ONBOARDING_VERSION: int = 2


@dataclass
class OnboardingState(DashboardModel):
    """Full onboarding snapshot the dashboard pulls on app load.

    ``current_version`` is the onboarding-flow version the server
    knows about; ``completed_version`` is what the user last
    acknowledged. Frontend gating combines:

    1. ``any(step.status == "pending" for step in steps)`` —
       a real-data signal that something needs doing.
    2. ``completed_version < current_version`` — the user hasn't
       seen this version of onboarding yet, even if all current
       steps look done from data alone (covers steps that are
       informational, not data-derived).
    """

    current_version: int
    completed_version: int
    steps: list[OnboardingStep] = field(default_factory=list)
