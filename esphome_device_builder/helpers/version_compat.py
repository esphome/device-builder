"""Version-match policy helpers for the offloader's compat gate."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import assert_never

_DIGITS_PREFIX_RE = re.compile(r"^(\d+)")

# Pragmatic PEP 440 matcher, not the full grammar: esphome only ever
# emits release / pre / post / dev / local forms. A regex keeps the
# cold-import floor clean — ``packaging`` is too heavy to import here.
_PEP440_RE = re.compile(
    r"""
    v?                                                       # optional leading v
    (?:\d+!)?                                                 # epoch
    \d+(?:\.\d+)*                                             # release segment
    (?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?\d*)?   # pre-release
    (?:(?:[-_.]?post[-_.]?\d*)|-\d+)?                         # post-release
    (?:[-_.]?dev[-_.]?\d*)?                                   # dev-release
    (?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?                       # local version
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_pep440_version(version: str) -> bool:
    """Return whether *version* is a well-formed PEP 440 version string.

    The single gate for every esphome version string crossing a trust
    boundary (peer-link advert, provisioning target) so a malformed or
    injected value never reaches storage or a ``pip install`` argument.
    """
    return _PEP440_RE.fullmatch(version) is not None


# A plain dotted-numeric release (no epoch / pre / post / dev / local segment).
_RELEASE_RE = re.compile(r"\d+(?:\.\d+)*")


def is_release_version(version: str) -> bool:
    """Return whether *version* is a final release, not a pre / dev / local build.

    Stricter than :func:`is_pep440_version`: only a plain release pins to a
    reproducible ``pip install esphome==<version>``, so the provisioner
    refuses anything else (a ``-dev`` build can't be pinned to an exact commit).
    """
    return _RELEASE_RE.fullmatch(version) is not None


class VersionMatchPolicy(StrEnum):
    """How strictly the offloader filters peers by ESPHome version.

    ``EXACT_REQUIRED`` tightens ``EXACT`` in two ways: the
    scheduler hard-fails (``NO_COMPATIBLE_PEER``) instead of
    falling back to LOCAL when no peer survives the filter, AND
    a peer that hasn't broadcast its ``esphome_version`` yet is
    treated as ineligible. Under the laxer policies an unknown
    peer-version is allowed through (LOCAL fallback catches any
    post-handshake surprise), but under ``EXACT_REQUIRED`` there
    is no fallback — the policy's "no compatible peer ⇒ refuse"
    promise would otherwise leak on a peer with no version yet.
    """

    ANY = "any"
    RELEASE = "release"
    EXACT = "exact"
    EXACT_REQUIRED = "exact_required"


def version_satisfies_policy(local: str, peer: str, policy: VersionMatchPolicy) -> bool:
    """Return whether *peer* survives the *policy*-level filter against *local*."""
    if policy is VersionMatchPolicy.ANY:
        return True
    if policy is VersionMatchPolicy.RELEASE:
        return major_versions_match(local, peer)
    if policy is VersionMatchPolicy.EXACT:
        return versions_match_exactly(local, peer)
    if policy is VersionMatchPolicy.EXACT_REQUIRED:
        # No LOCAL fallback — unknown peer version is a no-match;
        # see :class:`VersionMatchPolicy` for the rationale.
        return bool(local) and bool(peer) and local == peer
    # Fail loudly on a new policy member that hasn't been wired
    # in — silent fallthrough would have a fresh strict policy
    # behaving like EXACT and the regression wouldn't surface
    # until an operator-reproduced bug report. Unreachable by
    # construction (mypy / py-type would catch a missing branch);
    # the call is a runtime safety net for ``# type: ignore`` slip.
    assert_never(policy)  # pragma: no cover


def major_versions_match(local: str, peer: str) -> bool:
    """Return ``True`` when *local* and *peer* share a ``YYYY.MM`` release line.

    Empty strings on either side match so a fresh APPROVED
    pairing isn't filtered before its first session-open.
    """
    if not local or not peer:
        return True
    if local == peer:
        return True
    return _release_key(local) == _release_key(peer)


def versions_match_exactly(local: str, peer: str) -> bool:
    """Return ``True`` when *local* and *peer* are identical (or either is empty)."""
    if not local or not peer:
        return True
    return local == peer


def _release_key(version: str) -> str:
    """Year + month prefix used for cross-release comparison."""
    parts = version.split(".")
    year = parts[0] if parts else ""
    month_raw = parts[1] if len(parts) > 1 else ""
    match = _DIGITS_PREFIX_RE.match(month_raw)
    month = match.group(1) if match else month_raw
    return f"{year}.{month}"
