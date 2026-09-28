#!/usr/bin/env python3
"""Staging knob for the GE16 tracker collectors (dry-run purity + staged merges).

``GE16_TRACKER_OUT_DIR`` reroutes a collector's tracker-file WRITES into a staging
directory (the baseline rebuild's run stage, ``work/baseline/stage/<run_id>/``) so
that a dry run, or a staged merge, cannot touch the live owner state:

  * ``w(path)``     the write target: ``<out>/<basename>`` when the knob is set and
                    the path belongs to the live tracker directory, else the path.
  * ``r(path)``     the read source: the staged copy when it exists (this run's own
                    evidence), else the live file.
  * ``a(path)``     the append target: the staged copy, seeded from the live file
                    first, so the staged file is a complete replacement for it.
  * ``g(pattern)``  glob: staged matches when the knob is set, else live matches. A
                    staged run therefore never deletes live evidence.
  * ``ensure()``    create the staging directory.

Set only by ``rebuild_baseline.py`` (P4 remap pending). With the knob unset
every function returns its input unchanged, so the standalone cron/agent flows
behave exactly as before.

Owner directive (26 Sep 2026, baseline-rebuild review MED-1/MED-2): a ``--dry-run``
must change zero bytes outside ``work/baseline/**``, and the accepted owner record
must be verified as a STAGED blob before it is atomically swapped into place.
"""
from __future__ import annotations

import glob as _glob
import os
import shutil

#: The env knob the baseline rebuild sets for a staged run.
OUT_ENV = "GE16_TRACKER_OUT_DIR"

_HERE = os.path.dirname(os.path.abspath(__file__))
#: V3 (P1.7): data/scripts/collect -> data/canonical
_DATA_ROOT = os.path.dirname(os.path.dirname(_HERE))
#: The live tracker directory these collectors own.
LIVE_DIR = os.path.join(_DATA_ROOT, "canonical", "research", "trackers")
#: ``research/trackers`` as a path suffix: the layout ``owns()`` matches on.
_LAYOUT = os.path.join("research", "trackers")


def out_dir():
    """The staging directory, or None when the collector writes live."""
    value = os.environ.get(OUT_ENV)
    if not value:
        return None
    return os.path.abspath(os.path.expanduser(value))


def owns(path) -> bool:
    """True when ``path`` is a file in a ``research/trackers`` directory.

    Layout-based on purpose. An absolute-root test silently disabled staging
    entirely when the root constant was off by one directory (the knob then became
    a no-op and a ``--dry-run`` wrote live tracker files — MED-1). Keying on the
    ``research/trackers`` layout means a collector cannot escape staging through a
    differently-rooted tracker directory, and a wrong root is caught by the pin
    test rather than by a silent live write.
    """
    if not path:
        return False
    parent = os.path.dirname(os.path.abspath(os.fspath(path)))
    return parent.endswith(os.sep + _LAYOUT)


def _staged(path) -> str:
    return os.path.join(out_dir(), os.path.basename(os.fspath(path)))


def w(path):
    """The path to WRITE ``path`` to (staged when the knob is set)."""
    if out_dir() and owns(path):
        os.makedirs(out_dir(), exist_ok=True)
        return _staged(path)
    return os.fspath(path)


def r(path):
    """The path to READ ``path`` from (this run's staged copy, else live)."""
    if out_dir() and owns(path):
        staged = _staged(path)
        if os.path.exists(staged):
            return staged
    return os.fspath(path)


def a(path):
    """The path to APPEND to: the staged copy, seeded with the live bytes first.

    Seeding matters: an append-only file staged without its history would make the
    post-verify swap DELETE that history (the owner log is append-only).
    """
    if out_dir() and owns(path):
        staged = _staged(path)
        live = os.fspath(path)
        os.makedirs(out_dir(), exist_ok=True)
        if not os.path.exists(staged) and os.path.exists(live):
            shutil.copy2(live, staged)
        return staged
    return os.fspath(path)


def g(pattern):
    """Glob: staged matches when the knob is set, else live matches."""
    if out_dir():
        return sorted(_glob.glob(os.path.join(out_dir(), os.path.basename(pattern))))
    return sorted(_glob.glob(pattern))


def staged(name):
    """``<out>/<name>`` when the knob is set, else None (callers fall back to live)."""
    if not out_dir():
        return None
    return os.path.join(out_dir(), name)


def ensure() -> None:
    """Create the staging directory (no-op when writing live)."""
    if out_dir():
        os.makedirs(out_dir(), exist_ok=True)
