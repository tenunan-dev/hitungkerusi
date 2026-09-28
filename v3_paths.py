"""V3 project-root configuration (P1.6).

Single source of truth for V3 absolute-path independence. Every module that
previously derived repository locations from ``__file__`` parent-counting or
sibling-directory assumptions resolves through :func:`project_root` /
:func:`data_root` here instead.

Design (recorded in PLAN.md P1.6):
- ``HITUNGKERUSI_ROOT`` environment variable overrides everything (tests).
- Otherwise the root is the first ancestor of this file containing
  ``requirements.txt`` (the V3 monorepo marker).
- ``data/`` lives inside the monorepo; the V2 sibling-``1_DATA`` layout is gone.
"""
from __future__ import annotations

import os
from pathlib import Path

_MARKER = "requirements.txt"


def project_root(anchor: "Path | str | None" = None) -> Path:
    """Return the V3 monorepo root, independent of cwd.

    Order: explicit anchor's marker-walk -> HITUNGKERUSI_ROOT env -> walk up
    from this file until requirements.txt is found.
    """
    env = os.environ.get("HITUNGKERUSI_ROOT")
    if env:
        return Path(env).resolve()

    start = Path(anchor).resolve() if anchor else Path(__file__).resolve()
    if start.is_file():
        start = start.parent
    for candidate in [start, *start.parents]:
        if (candidate / _MARKER).is_file():
            return candidate
    raise RuntimeError(
        "V3 project root not found (no {} above {}); set HITUNGKERUSI_ROOT".format(
            _MARKER, start
        )
    )


def data_root(anchor: "Path | str | None" = None) -> Path:
    """Return the canonical data directory (``<root>/data``)."""
    return project_root(anchor) / "data"


def analytics_root(anchor: "Path | str | None" = None) -> Path:
    """Return the analytics domain directory (``<root>/analytics``)."""
    return project_root(anchor) / "analytics"
