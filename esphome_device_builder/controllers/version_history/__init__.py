"""Git-backed version history for the dashboard config directory."""

from __future__ import annotations

from .controller import VersionHistoryController
from .git_repo import GIT_COMMIT_ERRORS

__all__ = ["GIT_COMMIT_ERRORS", "VersionHistoryController"]
