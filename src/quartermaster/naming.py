"""Canonical item-name normalization.

This lives apart from the domain services so that migrations can apply exactly
the same rule the runtime applies. SQLite's own `lower()` is ASCII-only and has
no way to collapse internal whitespace, so normalization has to be Python's.
"""

from __future__ import annotations


def normalize_name(name: str) -> str:
    """Collapse whitespace and case-fold so equal items share one stack identity."""
    return " ".join(name.split()).casefold()
