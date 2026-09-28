#!/usr/bin/env python3
"""Run-scoped working paths (P2.3): one refresh/collect execution = one run.

Working state (tracker staging, self-heal ledgers, judge scratch, logs) lives
under ``data/work/<run_id>/`` and is never the durable record — canonical
editions are. Run dirs are retained forever: there is no cleanup, TTL or
retention logic anywhere in this module, on purpose (imported audit artifacts
are kept; the working tree is reproducible from the editions).

Auto-wiring: the orchestrator calls :func:`apply_env` once after :func:`new_run`.
The EXISTING collector knobs — ``GE16_TRACKER_OUT_DIR`` (ge16_tracker_outdir.py)
and ``GE16_SELFHEAL_STATE`` (ge16_selfheal_state.py), built in P1.7 — then route
every collector write into the run directory with zero per-collector changes.
With no run active, :func:`apply_env` is a no-op and every collector resolves
exactly the live canonical paths it resolved before P2.3.
"""
from __future__ import annotations

import json
import os
import platform
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

#: Env knob orchestrators set to scope a process tree to one run.
RUN_ENV = "GE16_RUN_DIR"
#: The pre-existing collector knobs apply_env() derives from the run (P1.7).
TRACKER_OUT_ENV = "GE16_TRACKER_OUT_DIR"
SELFHEAL_STATE_ENV = "GE16_SELFHEAL_STATE"

RUN_JSON = "run.json"
#: ``YYYYMMDDTHHMMSSZ`` + "-" + 8 lowercase hex (V2 stage-dir convention).
RUN_ID_PATTERN = r"\d{8}T\d{6}Z-[0-9a-f]{8}"
RUN_SUBDIRS = ("trackers", "judge", "state", "logs")

_HERE = Path(__file__).resolve()
if str(_HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[2]))
try:
    import v3_paths
finally:
    sys.path.remove(str(_HERE.parents[2]))


def work_root(data_root=None) -> Path:
    """``<data>/work`` — parent of every run directory."""
    base = Path(data_root) if data_root else v3_paths.data_root(_HERE)
    return Path(base) / "work"


def new_run_id(now=None) -> str:
    """UTC second stamp + 8 hex of entropy, e.g. ``20260929T045219Z-ba972a1a``."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


class RunPaths:
    """Absolute paths of one run directory (all fields under ``root``).

    A plain class on purpose: every module in this tree is loaded by path via
    importlib without a sys.modules entry, which the dataclasses decorator
    cannot survive.
    """

    __slots__ = ("run_id", "root", "trackers", "judge", "state", "logs")

    def __init__(self, run_id, root, trackers, judge, state, logs):
        self.run_id = run_id
        self.root = Path(root)
        self.trackers = Path(trackers)
        self.judge = Path(judge)
        self.state = Path(state)
        self.logs = Path(logs)

    @property
    def run_json(self) -> Path:
        return self.root / RUN_JSON

    @property
    def selfheal_state(self) -> Path:
        return self.state / "selfheal-state.json"

    def subdirs(self):
        return (self.trackers, self.judge, self.state, self.logs)

    def __eq__(self, other):
        if not isinstance(other, RunPaths):
            return NotImplemented
        return (self.run_id, self.root) == (other.run_id, other.root)

    def __repr__(self):
        return f"RunPaths(run_id={self.run_id!r}, root={str(self.root)!r})"


def _paths(run_id: str, root: Path) -> RunPaths:
    return RunPaths(run_id=run_id, root=root,
                    **{name: root / name for name in RUN_SUBDIRS})


def new_run(label="", data_root=None, now=None, host=None, inputs_edition=None) -> RunPaths:
    """Create ``<data>/work/<run_id>/`` with its four subdirs and ``run.json``.

    Never sets environment variables: wiring a process tree to the run is the
    orchestrator's explicit :func:`apply_env` call, so a bare ``new_run`` in a
    library context cannot silently reroute collectors.
    """
    moment = now or datetime.now(timezone.utc)
    run_id = new_run_id(moment)
    run = _paths(run_id, work_root(data_root) / run_id)
    for directory in run.subdirs():
        directory.mkdir(parents=True, exist_ok=False)
    document = {
        "run_id": run.run_id,
        "label": label or "",
        "started_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "host": host if host is not None else platform.node(),
    }
    if inputs_edition:
        document["inputs_edition"] = inputs_edition
    run.run_json.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    return run


def current_run(env=None) -> "RunPaths | None":
    """Resolve the active run from ``GE16_RUN_DIR``, or None when unset.

    Reads only: never creates anything. ``run_id`` prefers run.json (the dir
    name is the fallback for a hand-placed run directory).
    """
    environment = os.environ if env is None else env
    value = environment.get(RUN_ENV)
    if not value:
        return None
    root = Path(os.path.abspath(os.path.expanduser(value)))
    run_id = root.name
    manifest = root / RUN_JSON
    if manifest.is_file():
        try:
            loaded = json.loads(manifest.read_text(encoding="utf-8"))
            run_id = loaded.get("run_id") or run_id
        except (OSError, ValueError):
            pass
    return _paths(run_id, root)


def apply_env(run=None, env=None):
    """Wire the existing collector knobs to the run; return the active run.

    ``run`` defaults to :func:`current_run`, so an orchestrator that only set
    ``GE16_RUN_DIR`` can call ``apply_env()`` bare. With neither argument
    resolving a run, nothing is exported and every collector keeps today's
    live-path resolution — no silent behavior change for unscoped processes.
    """
    environment = os.environ if env is None else env
    active = run if run is not None else current_run(env=environment)
    if active is None:
        return None
    environment[RUN_ENV] = str(active.root)
    environment[TRACKER_OUT_ENV] = str(active.trackers)
    environment[SELFHEAL_STATE_ENV] = str(active.selfheal_state)
    return active
