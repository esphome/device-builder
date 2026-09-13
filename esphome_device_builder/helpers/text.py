"""Small user-facing text helpers."""

from __future__ import annotations

from collections.abc import Iterable


def summarise(messages: Iterable[str]) -> str:
    """Join up to three non-empty messages with a ``(+N more)`` count, period-trimmed."""
    shown_all = [msg for msg in messages if msg]
    shown = shown_all[:3]
    suffix = f" (+{len(shown_all) - len(shown)} more)" if len(shown_all) > len(shown) else ""
    # esphome messages often end with their own period; the caller's
    # tail brings the sentence break.
    return ("; ".join(shown) + suffix).removesuffix(".")
