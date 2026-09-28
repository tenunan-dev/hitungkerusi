#!/usr/bin/env python3
"""Fail-closed GE16 five-stage contract runner.

Regular invocations validate and return a completed posture record without
executing a stage. Execution requires --live-run, --scheduled-fire, or an isolated --dry-run.
The dry-run interface is solely for isolated fixtures: every executable and
argument vector is code-owned and child writes are confined to its owner domain.
Bounded live execution requires a fresh external owner authorization, consumed
before one adapter runs. The 1.9.0 contract permits pinned Stages 1-4;
Stage 5 is isolated-only. Scheduler state and
deployment remain forbidden through this runner.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import base64
import hashlib
import pwd
import uuid
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from cron.sync_jobs import SyncError, validate_canonical_config
from validate_ops_contract import (
    ContractError, GATE_RECORD_SCHEMA, MANIFEST_RECORD_SCHEMA, validate_scheduled_job_binding,
    STAGE_FIVE_RELEASE_REQUIREMENTS, validate_contract, validate_bounded_live_execution,
    FIRE_KEY_DIRECTORY, BOUNDED_LIVE_POLICY, OPS_CONTRACT_VERSION, PINNED_MODULE_PATHS, COMMITTED_MODULE_SHA256,
    SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS as _VALIDATOR_SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS,
)

FIRE_KEY_DIRECTORY = pathlib.Path(FIRE_KEY_DIRECTORY)
CONTRACT_PATH = ROOT / "ops-contract.json"
JOBS_PATH = ROOT / "cron" / "ge16_jobs.json"
DOMAIN_NAMES = ("1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES")
MANIFEST_FIELDS = tuple(MANIFEST_RECORD_SCHEMA["required_fields"])
GATE_FIELDS = tuple(GATE_RECORD_SCHEMA["required_fields"])
FRESHNESS_SECONDS = 24 * 60 * 60
LIVE_AUTHORIZATION_SECONDS = 15 * 60
LIVE_CLAIMS_DIRECTORY = ".bounded-live-claims"
LIVE_LOCK_PATH = pathlib.Path("/private/tmp") / ("ge16-bounded-live-%d.lock" % os.geteuid())
SCHEDULER_REGISTRY_PATH = pathlib.Path(BOUNDED_LIVE_POLICY["scheduler_registry_read_only"])
CHILD_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
}

# Per-owner child interpreter override: 2_ANALYTICS adapters need numpy/pandas,
# which live in the 2_ANALYTICS venv, not in the interpreter that runs this
# runner. Other owners keep the runner's own interpreter.
OWNER_CHILD_INTERPRETER = {
    "2_ANALYTICS": pathlib.Path(REPOSITORY_ROOT) / "2_ANALYTICS" / ".venv" / "bin" / "python3",
    # Stage-3 children import 2_ANALYTICS build modules (pandas) inside a
    # sandbox that blocks reading exports from the analytics venv at runtime;
    # run them under the same pandas-capable interpreter directly.
    "3_OUTPUTS": pathlib.Path(REPOSITORY_ROOT) / "2_ANALYTICS" / ".venv" / "bin" / "python3",
}

# The single scheduled-fire job-record digest field list. The validator owns the
# digest computation; this runner copy exists so the boundary is explicit here
# too. Module import fails closed if the two ever drift.
SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS = (
    "id", "name", "schedule", "model", "provider", "state", "workdir",
    "context_from", "enabled_toolsets", "prompt", "skills", "skill",
    "script", "no_agent", "base_url", "monitor_script", "monitor_url",
    "origin",
)
if SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS != _VALIDATOR_SCHEDULER_JOB_DIGEST_REQUIRED_FIELDS:
    raise RuntimeError("scheduler job digest required-field drift between runner and validator")

# Contract data only selects one adapter name; it never supplies an executable,
# argument, shell fragment, environment entry, or working directory.
COMMAND_ALLOWLIST = {
    "data-collect-news": ("1_DATA/scripts/collect/track_ge16_news.py",),
    "data-collect-polls": ("1_DATA/scripts/collect/track_ge16_polls.py",),
    "data-collect-candidates": ("1_DATA/scripts/collect/track_ge16_candidates.py",),
    "data-commit-news": ("1_DATA/scripts/collect/track_ge16_news.py", "--commit"),
    "data-refresh-canonical": ("1_DATA/scripts/refresh_canonical_data.py",),
    "data-validate-canonical": ("1_DATA/scripts/validate_canonical_data.py", "--manifest", "1_DATA/canonical-data-provenance.json"),
    "analytics-forecast": ("2_ANALYTICS/02_FORECAST/engine/forecast_engine.py", "--iterations", "1"),
    "analytics-report-en": ("2_ANALYTICS/02_FORECAST/engine/report_builder.py", "--lang", "en"),
    "analytics-report-ms": ("2_ANALYTICS/02_FORECAST/engine/report_builder.py", "--lang", "ms"),
    "analytics-state-report-en": ("2_ANALYTICS/02_FORECAST/engine/state_report_builder.py", "--lang", "en", "--skip-unchanged"),
    "analytics-state-report-ms": ("2_ANALYTICS/02_FORECAST/engine/state_report_builder.py", "--lang", "ms", "--skip-unchanged"),
    "analytics-social-stage": ("2_ANALYTICS/05_AUTOMATION/publish_stage_manifests.py", "--stage", "4"),
    "analytics-ms-en-sections": ("2_ANALYTICS/automation/qa/audit_ms_en_sections.py", "--json"),
    "analytics-state-number-parity": ("2_ANALYTICS/automation/qa/audit_state_number_parity.py", "--json"),
    # The optional analytics-authoring step (OPTIONAL_CHAIN_STEPS, below) names
    # its executable in this allowlist too, so the runner never has to take an
    # executable from anywhere but a code-owned table. It gets nothing else: it
    # is not a command of any STAGE_ADAPTERS adapter, so it is never handed a
    # sandbox slot, and COMMAND_CHILDPOLICY leaves it in the default "deny"
    # class — this runner grants outbound TCP 443 to the data collectors only.
    # The weekly cron agent runs the step outside the sandbox, in the position
    # chain_steps(2) puts it in.
    "analytics-authoring": ("2_ANALYTICS/02_FORECAST/engine/author_reports.py", "--all-states", "--lang", "en"),
    # The baseline program's optional chain steps (OPTIONAL_CHAIN_STEPS below).
    # Same rule as the authoring step: the executable is named in this
    # code-owned table, the step is not a command of any STAGE_ADAPTERS adapter,
    # so it never gets a sandbox slot, and COMMAND_CHILDPOLICY leaves it at the
    # default "deny" — outbound TCP 443 stays with the three data collectors.
    # The weekly cron agent runs them outside the sandbox, in the positions
    # chain_steps(1) / chain_steps(2) put them in. The news-side step's judge
    # calls DeepSeek with the agent's own (non-sandboxed) network access.
    "data-baseline-news-delta": ("2_ANALYTICS/tools/baseline/rebuild_baseline.py",
                                 "--news", "--news-sweeps", "2",
                                 "--judge-backend", "deepseek"),
    "analytics-baseline-derived": ("2_ANALYTICS/tools/baseline/rebuild_baseline.py",
                                   "--events", "--graph", "--vdbs", "--figures"),
    "outputs-intake-seal": ("3_OUTPUTS/scripts/seal_release_intake.py",),
    "delivery-publish": ("4_DELIVERY/scripts/publish_delivery.py",),
    "websites-build-gate": ("5_WEBSITES/scripts/verify_release_gate.py",),
}
# Code-owned per-command permission; contract data cannot grant network access.
# HTTPS uses outbound TCP 443; DNS resolution uses only the system resolver.
COMMAND_CHILDPOLICY = {name: "https" if name in {
    "data-collect-news", "data-collect-polls", "data-collect-candidates",
} else "ro-git" if name in {"outputs-intake-seal", "delivery-publish"}
else "deny" for name in COMMAND_ALLOWLIST}
# Stage 1 runs in two runner invocations so the cron agent can judge collected
# news candidates between them: phase a only collects (no handoff), phase b
# commits the agent's judged output and only phase b emits the Stage 1 handoff.
STAGE_ADAPTERS = {
    "data-stage-1a": (1, "1_DATA", ("data-collect-news", "data-collect-polls", "data-collect-candidates")),
    "data-stage-1b": (1, "1_DATA", ("data-commit-news", "data-refresh-canonical", "data-validate-canonical")),
    "analytics-stage-2": (2, "2_ANALYTICS", ("analytics-forecast", "analytics-report-en", "analytics-report-ms", "analytics-state-report-en", "analytics-state-report-ms", "analytics-social-stage", "analytics-ms-en-sections", "analytics-state-number-parity")),
    "outputs-stage-3": (3, "3_OUTPUTS", ("outputs-intake-seal",)),
    "delivery-stage-4": (4, "4_DELIVERY", ("delivery-publish",)),
    "websites-stage-5": (5, "5_WEBSITES", ("websites-build-gate",)),
}
STAGE1_COMMAND_NAMES = STAGE_ADAPTERS["data-stage-1a"][2] + STAGE_ADAPTERS["data-stage-1b"][2]
# Phase b's precondition evidence: proof phase a collected in this cycle.
STAGE_ONE_CANDIDATES_PATH = "1_DATA/research/trackers/ge16-news-candidates.json"
STAGE_ONE_JUDGED_PATH = "1_DATA/research/trackers/ge16-news-judged.json"
STAGE2_COMMAND_NAMES = STAGE_ADAPTERS["analytics-stage-2"][2]

# --- optional chain steps ----------------------------------------------------
# The weekly chain has one OPTIONAL step after the deterministic stage-2 reports:
# `analytics-authoring`, the AI-authored narrative edition
# (2_ANALYTICS/02_FORECAST/engine/author_reports.py — see the pipeline figure in
# 2_ANALYTICS/tools/authoring/README.md). It is declared here so the chain's
# order is code-owned and testable, but it is NOT a sandboxed adapter command:
# a live authoring pass makes an outbound model call, and this runner grants
# outbound TCP 443 to the data collectors only (COMMAND_CHILDPOLICY, and the
# per-command network containment the OPS tests pin). The weekly cron agent runs
# the step in the position declared here, after `analytics-state-report-ms` and
# before Stage 3. It is non-blocking by contract: if the step fails, or no writer
# in a language's chain can verify a section, the deterministic editions stay the
# published path (a pass with nothing authored publishes no edition at all), the
# step's own manifest records what happened, and
# Stage 3 proceeds. The step never writes 03_REPORTS/federal or
# 03_REPORTS/states.
#
# The baseline program (2_ANALYTICS/tools/baseline/rebuild_baseline.py) joins the
# same chain as two more OPTIONAL, non-blocking steps, one per stage, so the
# weekly run rebuilds the baseline from the data the run just collected:
#   * stage 1 — `data-baseline-news-delta` (rebuild_baseline.py --news
#     --news-sweeps 2 --judge-backend deepseek), inserted right after
#     `data-commit-news`, i.e. once the stage has committed the agent's judged
#     items. It owns the 1_DATA news side only: it merges judged items into
#     1_DATA/research/trackers/ge16-news-accepted.json (staged, verified against
#     the staged blob, then swapped) and never touches the canonical data, the
#     reports, or any later domain.
#   * stage 2 — `analytics-baseline-derived` (rebuild_baseline.py --events
#     --graph --vdbs --figures), inserted after
#     `analytics-state-report-ms` and before the Stage 3 handoff is computed, so
#     the sealed outputs describe the freshly rebuilt events DB, graph and VDBs.
#     It writes 2_ANALYTICS/work/** only: `--polls` is deliberately NOT part of
#     it, because the poll tracker is a 1_DATA-owned file the stage-2 contract
#     pins as non-mutable (cross-domain mutation is forbidden) and stage 1
#     already refreshes it through `data-collect-polls`.
# Both are declared non_blocking (gate_impact "none"): a baseline refresh that
# fails leaves the deterministic artifacts of the run standing — the pre-rebuild
# build for the derived layers, the pre-merge owner record for the news side —
# records the failure in its own manifest, and Stage 3 proceeds.
OPTIONAL_CHAIN_STEPS = {
    1: (
        {
            "name": "data-baseline-news-delta",
            "after": "data-commit-news",
            "owner_domain": "1_DATA",
            "script": "2_ANALYTICS/tools/baseline/rebuild_baseline.py",
            "argv": ("--news", "--news-sweeps", "2", "--judge-backend", "deepseek"),
            "non_blocking": True,
            "gate_impact": "none",
            "sandboxed": False,
            "writes": ("1_DATA/research/trackers", "2_ANALYTICS/work/baseline"),
            "must_not_write": (
                "1_DATA/research/raw", "1_DATA/canonical-data-provenance.json",
                "2_ANALYTICS/02_FORECAST", "2_ANALYTICS/03_REPORTS",
                "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", "OPS",
            ),
        },
    ),
    2: (
        {
            "name": "analytics-authoring",
            "after": "analytics-state-report-ms",
            "owner_domain": "2_ANALYTICS",
            "script": "2_ANALYTICS/02_FORECAST/engine/author_reports.py",
            "argv": ("--all-states", "--lang", "en"),
            "argv_ms": ("--all-states", "--lang", "ms"),
            "non_blocking": True,
            "sandboxed": False,
            "writes": ("2_ANALYTICS/03_REPORTS/ai", "2_ANALYTICS/work/reports"),
            "must_not_write": (
                "2_ANALYTICS/03_REPORTS/federal",
                "2_ANALYTICS/03_REPORTS/states",
                "1_DATA", "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", "OPS",
            ),
        },
        {
            "name": "analytics-baseline-derived",
            "after": "analytics-state-report-ms",
            "owner_domain": "2_ANALYTICS",
            "script": "2_ANALYTICS/tools/baseline/rebuild_baseline.py",
            "argv": ("--events", "--graph", "--vdbs", "--figures"),
            "non_blocking": True,
            "gate_impact": "none",
            "sandboxed": False,
            "writes": ("2_ANALYTICS/work/baseline", "2_ANALYTICS/work/events",
                       "2_ANALYTICS/work/graph", "2_ANALYTICS/work/figures",
                       "2_ANALYTICS/work/scenarios", "2_ANALYTICS/work/graph-explorer"),
            "must_not_write": (
                "1_DATA",
                "2_ANALYTICS/03_REPORTS/federal",
                "2_ANALYTICS/03_REPORTS/states",
                "3_OUTPUTS", "4_DELIVERY", "5_WEBSITES", "OPS",
            ),
        },
    ),
}


def chain_steps(stage: int) -> Tuple[str, ...]:
    """The stage's weekly chain in execution order.

    Sandboxed adapter commands (``STAGE_ADAPTERS``) plus every optional step
    declared for that stage, inserted immediately after the command named in its
    ``after`` field. Callers that execute commands under the child sandbox must
    keep using ``STAGE_ADAPTERS``/``STAGE2_COMMAND_NAMES``: optional steps are
    agent-executed and are never granted a sandbox slot.
    """
    names: List[str] = []
    for _adapter_name, (number, _owner, commands) in STAGE_ADAPTERS.items():
        if number == stage:
            names.extend(name for name in commands if name not in names)
    for step in OPTIONAL_CHAIN_STEPS.get(stage, ()):
        anchor = step.get("after")
        if anchor in names:
            names.insert(names.index(anchor) + 1, step["name"])
        else:
            names.append(step["name"])
    return tuple(names)


STAGE2_CHAIN = chain_steps(2)


def optional_step(stage: int, name: str) -> Optional[dict]:
    """The optional step called ``name`` declared for ``stage``, if any."""
    for step in OPTIONAL_CHAIN_STEPS.get(stage, ()):
        if step.get("name") == name:
            return step
    return None


def advance_chain(stage: int, run_step: Callable[[str], None],
                  log: Optional[Callable[[str], None]] = None) -> List[str]:
    """Run one stage's chain in order; return the steps that completed.

    The sandboxed adapter commands stay fail-closed: the first one that raises
    stops the chain, exactly as before. A declared optional step is not — it is
    ``non_blocking`` by contract, so its failure is logged and the chain carries
    on. That is what keeps a failed or thin AI-authored pass from
    ever stopping the deterministic stages behind it, Stage 3 included.

    The caller supplies ``run_step`` (how this invocation executes one step), so
    the ordering and the non-blocking rule stay code-owned and testable without
    a live run.
    """
    advanced: List[str] = []
    for name in chain_steps(stage):
        try:
            run_step(name)
        except Exception as error:  # noqa: BLE001 - a non-blocking step may fail
            step = optional_step(stage, name)
            if step is None or step.get("non_blocking") is not True:
                raise
            if log is not None:
                log("optional step %s failed without blocking the chain: %s: %s"
                    % (name, type(error).__name__, error))
            continue
        advanced.append(name)
    return advanced


HANDOFFS = {
    1: ("1_DATA", "ge16-data-handoff.json", "ge16-cron-data-collection-validation", "1.4.0"),
    2: ("2_ANALYTICS", "ge16-analytics-handoff.json", "ge16-cron-analytics-forecast-reports-social", "1.4.0"),
    3: ("3_OUTPUTS", "ge16-release-seal.json", "ge16-cron-outputs-intake-sealing", "1.4.0"),
    4: ("4_DELIVERY", "ge16-delivery.json", "ge16-cron-delivery-publish-validation", "1.4.0"),
    5: ("5_WEBSITES", "ge16-release-gate.json", "ge16-cron-websites-build-release-gate", "1.5.0"),
}

class LiveValidationError(ValueError):
    """Only code-owned, non-secret reason codes may be raised here."""


@contextlib.contextmanager
def _exclusive_live_lock():
    """Serialize all cooperating runner processes; never unlink a lock inode.

    This advisory lock does not stop editors or other tools. There is no
    snapshot or rollback: failures may leave partial owner-domain mutations.
    """
    path = LIVE_LOCK_PATH
    if not _real_path(path) or _is_within(path, REPOSITORY_ROOT):
        raise LiveValidationError("unsafe-bounded-live-lock")
    parent = path.parent.stat()
    if parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
        raise LiveValidationError("unsafe-bounded-live-lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise LiveValidationError("unsafe-bounded-live-lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LiveValidationError("bounded-live-lock-already-held") from error
        current = path.stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise LiveValidationError("unsafe-bounded-live-lock")
        yield
    finally:
        os.close(fd)


class IsolatedRunError(RuntimeError):
    pass
class IsolatedRootError(ValueError):
    pass

def _emit(result: str, reason: str) -> None:
    print(json.dumps({"result": result, "reason": reason}, separators=(",", ":")))
def _is_within(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try: path.relative_to(parent); return True
    except ValueError: return False
def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest()
def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def _parse_utc(value: object, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"): raise IsolatedRunError("%s must be a UTC Z timestamp" % label)
    try: return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as error: raise IsolatedRunError("%s is not a valid UTC timestamp" % label) from error
def _assert_fresh(timestamp: dt.datetime, label: str) -> None:
    age = (dt.datetime.now(dt.timezone.utc) - timestamp).total_seconds()
    if age < -300 or age > FRESHNESS_SECONDS: raise IsolatedRunError("%s is stale or from the future" % label)

def _parse_locked_arguments(arguments: List[str]) -> Tuple[str, str]:
    if len(arguments) != 4 or arguments[0] != "--contract" or arguments[2] != "--version":
        raise ValueError("arguments must exactly be --contract <id> --version <version>")
    return arguments[1], arguments[3]
def _parse_stage_phase_tail(tail: List[str]) -> Tuple[Optional[int], Optional[str]]:
    """Trailing [--stage N] [--phase {a,b}], in that order; either may stand alone."""
    if len(tail) not in (0, 2, 4):
        raise ValueError("trailing arguments must be [--stage N] [--phase a|b]")
    stage: Optional[int] = None
    phase: Optional[str] = None
    if len(tail) == 2:
        flag, value = tail
        if flag == "--stage":
            if value not in {"1", "2", "3", "4", "5"}: raise ValueError("--stage must be 1, 2, 3, 4, or 5")
            stage = int(value)
        elif flag == "--phase":
            if value not in {"a", "b"}: raise ValueError("--phase must be a or b")
            phase = value
        else:
            raise ValueError("trailing arguments must be --stage N or --phase a|b")
    elif len(tail) == 4:
        if tail[0] != "--stage" or tail[1] not in {"1", "2", "3", "4", "5"} or tail[2] != "--phase" or tail[3] not in {"a", "b"}:
            raise ValueError("trailing arguments must be --stage N --phase a|b")
        stage, phase = int(tail[1]), tail[3]
    return stage, phase

def _parse_dry_run_arguments(arguments: List[str]) -> Tuple[str, str, pathlib.Path, Optional[int], Optional[str]]:
    if len(arguments) not in (7, 9, 11) or arguments[:5:2] != ["--contract", "--version", "--dry-run"] or arguments[5] != "--isolated-root":
        raise ValueError("dry-run arguments must be --contract <id> --version <version> --dry-run --isolated-root <dir> [--stage N] [--phase a|b]")
    stage, phase = _parse_stage_phase_tail(arguments[7:])
    return arguments[1], arguments[3], pathlib.Path(arguments[6]), stage, phase

def _parse_live_arguments(arguments: List[str]) -> Tuple[str, str, pathlib.Path, Optional[int], Optional[str]]:
    if len(arguments) not in (7, 9, 11) or arguments[:5:2] != ["--contract", "--version", "--live-run"] or arguments[5] != "--authorization":
        raise ValueError("live arguments must be --contract <id> --version <version> --live-run --authorization <file> [--stage N] [--phase a|b]")
    stage, phase = _parse_stage_phase_tail(arguments[7:])
    return arguments[1], arguments[3], pathlib.Path(arguments[6]), stage, phase

def _real_path(path: pathlib.Path) -> bool:
    return path.is_absolute() and ".." not in path.parts and not any(
        part.is_symlink() for part in (path, *path.parents)
    )

def _validated_live_root(candidate: pathlib.Path) -> pathlib.Path:
    if candidate != REPOSITORY_ROOT or candidate != ROOT.parent or not _real_path(candidate) or not candidate.is_dir():
        raise ValueError("live root must be the exact real repository directory")
    for name in DOMAIN_NAMES:
        domain = candidate / name
        if not _real_path(domain) or not domain.is_dir() or domain.resolve() != candidate / name:
            raise ValueError("live root requires five real domain directories")
    return candidate

def _exact_json_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("live authorization contains duplicate fields")
        result[key] = value
    return result

def _read_external_json(path: pathlib.Path, limit: int = 65536, *, owner_only: bool = False) -> Tuple[object, str]:
    """Read one stable owner-controlled regular file without exposing contents."""
    if not _real_path(path) or _is_within(path, REPOSITORY_ROOT) or not path.is_file():
        raise ValueError("live authorization must be a safe external regular file")
    for parent in path.parents:
        info = parent.stat()
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValueError("live authorization parent permissions are unsafe")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("live authorization file ownership or permissions are unsafe")
        if owner_only and (stat.S_IMODE(info.st_mode) != 0o600 or pwd.getpwuid(info.st_uid).pw_name != "faisal.muthalib"):
            raise ValueError("scheduled fire file requires owner ownership and mode 600")
        try:
            raw = source.read(limit + 1)
            if len(raw) > limit:
                raise ValueError("live authorization exceeds size limit")
            value = json.loads(raw, object_pairs_hook=_exact_json_object)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("live authorization is not valid JSON") from error
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink", "st_mode", "st_uid")
        if not _real_path(path) or any(
            getattr(current, field) != getattr(info, field)
            for current in (path.stat(), os.fstat(source.fileno())) for field in stable_fields
        ):
            raise ValueError("live authorization file changed while being read")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_scheduler_job(contract: dict, stage: int, *, scheduled: bool = False) -> str:
    """Read the exact registry, never a caller-selected substitute or API."""
    registry, digest = _read_external_json(SCHEDULER_REGISTRY_PATH, limit=8 * 1024 * 1024)
    jobs = registry.get("jobs") if isinstance(registry, dict) else registry
    if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
        raise LiveValidationError("bounded-live-scheduler-binding-invalid")
    expected = BOUNDED_LIVE_POLICY["scheduler_bindings"][stage - 1]
    state = expected["scheduled_fire"] if scheduled else expected
    matches = [job for job in jobs if job.get("id") == expected["id"]]
    binding = contract.get("scheduler_binding", {})
    if (len(matches) != 1 or binding.get("migration_job_id") != expected["id"]
            or (not scheduled and binding.get("enabled") is not False)
            or matches[0].get("enabled") is not state["enabled"] or matches[0].get("state") != state["state"]):
        raise LiveValidationError("bounded-live-scheduler-binding-invalid")
    if scheduled:
        return validate_scheduled_job_binding(matches[0], expected["id"])
    return digest


def _validate_live_authorization(path: pathlib.Path, contract: dict, stage: int, phase: Optional[str] = None) -> str:
    """Return a content identity without printing or modifying the approval.

    Timestamps are exact UTC seconds; approval age and total lifetime are each
    capped at 15 minutes with no future clock skew allowance. The external file
    must be owned by this OS user and must not be writable by group/others.

    On a two-phase stage the approval is bound to its exact phase. Phase a only
    collects; phase b commits the agent's judged output and is the only phase
    that emits the handoff. Without this binding an approval granted to collect
    could be redirected to the phase that publishes.
    """
    auth, _ = _read_external_json(path)
    expected = {
        "mode": "bounded-live", "approval_status": "approved",
        "execution_mode": "manual-one-stage", "contract_id": contract["id"],
        "contract_version": contract["version"],
        "scheduler_job_id": contract["scheduler_binding"]["migration_job_id"],
        "stage_number": stage, "one_shot": True,
        "ops_contract_version": OPS_CONTRACT_VERSION,
        "bounded_live_policy_version": BOUNDED_LIVE_POLICY["version"],
        "scheduler_job_enabled": False, "scheduler_job_state": "paused",
        "manual_one_shot_allowed": True,
        "scheduler_registry_sha256": _validate_scheduler_job(contract, stage),
        "contract_sha256": _sha256(CONTRACT_PATH),
        "canonical_jobs_sha256": _sha256(JOBS_PATH),
        "allow_deploy": False, "allow_gateway_start": False,
        "allow_schedule_enablement": False, "authorized_by": "owner",
    }
    if phase is not None:
        expected["stage_phase"] = phase
    if not isinstance(auth, dict) or set(auth) != set(expected) | {"authorized_at_utc", "expires_at_utc"}:
        raise ValueError("live authorization fields are not exact")
    if any(type(auth[key]) is not type(value) or auth[key] != value for key, value in expected.items()):
        raise ValueError("live authorization binding or permission is not exact")
    _validate_authorization_times(auth, "authorized_at_utc")
    return hashlib.sha256(_canonical_json_bytes(auth)).hexdigest()

def _read_owner_public_key(clause: dict) -> bytes:
    """Only public material is available to the runner; code pins are mandatory."""
    path = ROOT / "security/owner_ed25519.pub"
    if not _real_path(path) or not path.is_file():
        raise LiveValidationError("scheduled-fire-owner-public-key-missing-or-unsafe")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid() or info.st_mode & 0o022):
            raise LiveValidationError("scheduled-fire-owner-public-key-unsafe")
        raw = source.read(1024)
    key = clause["owner_public_key"]
    expected = ("-----BEGIN PUBLIC KEY-----\n" + key["public_key_b64"] +
                "\n-----END PUBLIC KEY-----\n").encode("ascii")
    if raw != expected or hashlib.sha256(raw).hexdigest() != key["key_sha256"]:
        raise LiveValidationError("scheduled-fire-owner-public-key-mismatch")
    return base64.b64decode(key["public_key_b64"], validate=True)


def _validate_fire_path(path: pathlib.Path) -> None:
    if (path.parent != FIRE_KEY_DIRECTORY or not _real_path(path)
            or _is_within(path, REPOSITORY_ROOT)):
        raise ValueError("scheduled fire requires the exact owner delivery directory")
    info = path.parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid() or pwd.getpwuid(info.st_uid).pw_name != "faisal.muthalib"):
        raise ValueError("scheduled fire delivery directory requires owner and mode 700")


def _canonical_base64(value: object, label: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(label + " must be canonical base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError(label + " must be canonical base64") from error
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError(label + " must be canonical base64")
    return raw


def _scheduled_fire_binding(contract: dict, stage: int, phase: Optional[str]) -> dict:
    clause = validate_bounded_live_execution(json.loads(CONTRACT_PATH.read_text(encoding="utf-8")))
    if clause["manual_execution_enabled"] is False:
        raise LiveValidationError("bounded-live-execution-revoked")
    if type(stage) is not int or stage not in clause["allowed_stages"]:
        raise LiveValidationError("bounded-live-stage-not-authorized")
    adapter = contract.get("isolated_adapter")
    if (isinstance(adapter, list) and phase not in ("a", "b")) or (not isinstance(adapter, list) and phase is not None):
        raise ValueError("scheduled fire requires the exact adapter phase")
    expected = {
        "mode": "scheduled-fire", "scheduler_job_id": contract["scheduler_binding"]["migration_job_id"],
        "stage_number": stage, "authorized_by": "owner", "one_shot": True,
        "owner_key_sha256": clause["owner_public_key"]["key_sha256"],
        "contract_id": contract["id"], "contract_version": contract["version"],
        "ops_contract_version": OPS_CONTRACT_VERSION,
        "bounded_live_policy_version": BOUNDED_LIVE_POLICY["version"],
        "contract_sha256": _sha256(CONTRACT_PATH), "canonical_jobs_sha256": _sha256(JOBS_PATH),
        "scheduler_binding": {
            "id": BOUNDED_LIVE_POLICY["scheduler_bindings"][stage - 1]["id"],
            "enabled": True, "state": "scheduled",
        },
        "scheduler_job_binding_sha256": _validate_scheduler_job(contract, stage, scheduled=True),
        "scheduler_job_enabled": True, "scheduler_job_state": "scheduled",
        "allow_deploy": False, "allow_gateway_start": False, "allow_schedule_enablement": False,
    }
    if phase is not None:
        expected["stage_phase"] = phase
    return expected


def _validate_scheduled_fire(path: pathlib.Path, contract: dict, stage: int, phase: Optional[str] = None) -> str:
    _validate_fire_path(path)
    envelope, _ = _read_external_json(path, owner_only=True)
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "payload_b64", "signature"}:
        raise ValueError("scheduled fire envelope fields are not exact")
    payload = envelope["payload"]
    expected = _scheduled_fire_binding(contract, stage, phase)
    if not isinstance(payload, dict) or set(payload) != set(expected) | {"key_id", "approved_at_utc", "expires_at_utc"}:
        raise ValueError("scheduled fire payload fields are not exact")
    if any(type(payload[key]) is not type(value) or payload[key] != value for key, value in expected.items()):
        raise ValueError("scheduled fire binding or permission is not exact")
    # Dict equality treats integer 1 as True; nested state must be exact too.
    if _canonical_json_bytes(payload["scheduler_binding"]) != _canonical_json_bytes(expected["scheduler_binding"]):
        raise ValueError("scheduled fire scheduler binding is not exact")
    try:
        key_id = uuid.UUID(payload["key_id"])
        if key_id.version != 4 or str(key_id) != payload["key_id"]:
            raise ValueError("not a canonical UUID4")
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("scheduled fire key_id must be UUID4") from error
    canonical = _canonical_json_bytes(payload)
    identity = hashlib.sha256(canonical).hexdigest()
    if _canonical_base64(envelope["payload_b64"], "payload_b64") != canonical:
        raise ValueError("scheduled fire canonical payload mismatch")
    clause = validate_bounded_live_execution(json.loads(CONTRACT_PATH.read_text(encoding="utf-8")))
    public_der = _read_owner_public_key(clause)
    signature = _canonical_base64(envelope["signature"], "signature")
    if len(signature) != 64:
        raise ValueError("scheduled fire signature length mismatch")
    from cron.ed25519_support import verify
    verify(public_der, signature, canonical)
    _validate_authorization_times(payload, "approved_at_utc")
    return identity


def _validate_authorization_times(auth: dict, approved_field: str) -> None:
    if any(not isinstance(auth[key], str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", auth[key]) is None
           for key in (approved_field, "expires_at_utc")):
        raise ValueError("live authorization timestamps are not exact UTC seconds")
    try:
        authorized = _parse_utc(auth[approved_field], "live authorization time")
        expires = _parse_utc(auth["expires_at_utc"], "live authorization expiry")
    except IsolatedRunError as error:
        raise ValueError("live authorization timestamps are invalid") from error
    now = dt.datetime.now(dt.timezone.utc)
    if not (authorized <= now < expires and
            0 < (expires - authorized).total_seconds() <= LIVE_AUTHORIZATION_SECONDS and
            (now - authorized).total_seconds() <= LIVE_AUTHORIZATION_SECONDS):
        raise ValueError("live authorization is future, expired, or exceeds the 15-minute lifetime")


def _claim_live_authorization(root: pathlib.Path, owner: str, identity: str) -> None:
    """Atomic durable claim: failures consume approval too; never auto-remove.

    Claims live in the owner domain, protected from all adapter writes by the
    native sandbox. Canonical JSON identity prevents replay via renamed copies.
    """
    directory = root / owner / LIVE_CLAIMS_DIRECTORY
    if not _real_path(directory) or (directory.exists() and not directory.is_dir()):
        raise ValueError("unsafe live authorization claim directory")
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(directory / identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as error:
        raise ValueError("live authorization already consumed") from error
    with os.fdopen(descriptor, "w") as record:
        record.write("consumed\n")
        record.flush()
        os.fsync(record.fileno())
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def _load_validated_request(contract_id: str, version: str) -> dict:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    jobs = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
    validate_contract(contract); validate_canonical_config(jobs, contract["migration"], contract["cron_contracts"])
    matches = [item for item in contract["cron_contracts"] if item["id"] == contract_id]
    if len(matches) != 1 or matches[0]["version"] != version: raise ValueError("unknown contract or version")
    job = next(item for item in jobs["jobs"] if item["contract_id"] == contract_id)
    if job["contract_version"] != version: raise ValueError("canonical job contract version mismatch")
    return matches[0]

def _validated_isolated_root(candidate: pathlib.Path) -> pathlib.Path:
    if not candidate.exists() or candidate.is_symlink() or not candidate.is_dir(): raise IsolatedRootError("isolated root does not exist as a real directory")
    root = candidate.resolve()
    protected = (REPOSITORY_ROOT.resolve(), ROOT.resolve()) + tuple((REPOSITORY_ROOT / name).resolve() for name in DOMAIN_NAMES)
    if any(_is_within(root, live) or _is_within(live, root) for live in protected): raise IsolatedRootError("isolated root overlaps live OPS or domain tree")
    for name in DOMAIN_NAMES:
        child = root / name
        if child.is_symlink() or (child.exists() and not _is_within(child.resolve(), root)): raise IsolatedRootError("isolated root contains unsafe domain link %s" % name)
    return root

def _safe_regular_file(root: pathlib.Path, path: pathlib.Path, label: str) -> None:
    if path.is_symlink() or not path.is_file() or not _is_within(path.resolve(), root): raise IsolatedRunError("missing or unsafe %s" % label)
    parent = path.parent
    while parent != root:
        if parent.is_symlink() or not _is_within(parent.resolve(), root): raise IsolatedRunError("unsafe parent for %s" % label)
        parent = parent.parent
def _assert_fresh_file(root: pathlib.Path, path: pathlib.Path, label: str) -> None:
    _safe_regular_file(root, path, label)
    _assert_fresh(dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc), label)
def _inventory(root: pathlib.Path) -> Dict[str, str]:
    """Capture all fixture paths, including non-domain top-level siblings."""
    result = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[key] = "symlink:" + os.readlink(path)
        elif path.is_dir():
            result[key] = "directory"
        elif path.is_file():
            result[key] = "sha256:" + _sha256(path)
    return result
def _assert_only_owner_changed(before: Dict[str, str], root: pathlib.Path, owner: str) -> None:
    after = _inventory(root)
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    permitted = (owner + "/",)
    forbidden = [key for key in changed if not key.startswith(permitted)]
    if forbidden:
        raise IsolatedRunError("cross-domain or sibling mutation detected: %s" % ", ".join(forbidden))

def _resolve_evidence_ref(root: pathlib.Path, value: object, label: str) -> pathlib.Path:
    """Resolve an immutable content ref (or an explicit fixture ref) safely."""
    if not isinstance(value, str):
        raise IsolatedRunError("%s evidence_ref is not a string" % label)
    relative, separator, fragment = value.partition("#")
    if not separator or not relative or not fragment:
        raise IsolatedRunError("%s evidence_ref is malformed" % label)
    path = root / relative
    _safe_regular_file(root, path, "%s evidence" % label)
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", fragment)
    if match is None:
        raise IsolatedRunError("%s evidence_ref must use #sha256:<digest>" % label)
    if _sha256(path) != match.group(1):
        raise IsolatedRunError("%s evidence_ref digest does not match content" % label)
    return path

# A Python audit hook is installed before adapter code runs.  It forbids
# writes outside the owner domain even when an adapter uses an absolute path,
# and permits only code-owned read-only Git subprocesses for release adapters.
# Only collector commands may connect over TCP 443.
_AUDITED_PYTHON = r'''
import hashlib, importlib.machinery, json, os, pathlib, runpy, socket, sys, types
root = pathlib.Path(sys.argv[1]).resolve()
owner = root / sys.argv[2]
claims = owner / ".bounded-live-claims"
git_directory = owner / ".git"
script = (root / sys.argv[3]).resolve()
network = sys.argv[5]
def resolve(value):
    if isinstance(value, int): return None
    p = pathlib.Path(value)
    return (p if p.is_absolute() else pathlib.Path.cwd() / p).resolve(strict=False)
def check(value):
    p = resolve(value)
    if p is not None:
        try: p.relative_to(owner)
        except ValueError: raise PermissionError("GE16 runner forbids writes outside owner domain")
        if p == owner or p == claims or claims in p.parents:
            raise PermissionError("GE16 runner forbids writes to authorization claims")
        if p == git_directory or git_directory in p.parents:
            raise PermissionError("GE16 runner forbids writes to owner Git metadata")
def readonly_git(executable, argv):
    if network != "ro-git" or executable not in {"git", "/usr/bin/git", "/Library/Developer/CommandLineTools/usr/bin/git"}:
        return False
    if not isinstance(argv, (list, tuple)) or len(argv) < 2 or argv[0] != "git":
        return False
    if not all(isinstance(value, str) for value in argv):
        return False
    forbidden = ("commit", "push", "add", "checkout", "reset", "rebase", "merge",
                 "tag", "remote", "rm", "mv", "apply", "prune", "gc", "fsck",
                 "am", "cherry-pick", "revert", "bisect", "worktree")
    if any(word == value or word in value.split(" ") for value in argv for word in forbidden):
        return False
    index = 1
    if argv[1] == "-C":
        if len(argv) < 4:
            return False
        directory = pathlib.Path(argv[2])
        if not directory.is_absolute():
            return False
        try: directory.resolve().relative_to(root)
        except ValueError: return False
        index = 3
    if argv[index] not in {"rev-parse", "status", "show", "log", "cat-file", "ls-tree", "diff", "describe"}:
        return False
    if "-C" in argv[index + 1:]:
        return False
    # Read verbs may expose output files and external driver execution.
    if any(value.startswith(("--output", "--ext-diff", "--textconv", "--filters")) for value in argv):
        return False
    return True

def audit(event, args):
    if event == "open":
        p = resolve(args[0])
        if p is not None and (p == root / "OPS/security" or root / "OPS/security" in p.parents):
            raise PermissionError("GE16 runner forbids child reads of owner keys")
        mode = args[1] if len(args) > 1 else "r"; flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if (isinstance(mode, str) and any(x in mode for x in "wax+")) or flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC): check(args[0])
    elif event in {"os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "os.utime", "os.truncate"}: check(args[0])
    elif event in {"os.rename", "os.replace", "os.link"}: check(args[0]); check(args[1])
    elif event == "os.symlink": check(args[1])
    elif event == "socket.connect":
        sock, address = args
        if (network != "https" or sock.family not in (socket.AF_INET, socket.AF_INET6)
                or sock.type != socket.SOCK_STREAM or not isinstance(address, tuple)
                or len(address) < 2 or address[1] != 443):
            raise PermissionError("GE16 runner forbids this network connection")
    elif event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        if network != "https": raise PermissionError("GE16 runner forbids network resolution")
    elif event in {"subprocess.Popen", "os.exec", "os.posix_spawn"}:
        argv = None
        env = None
        cwd = None
        if event == "subprocess.Popen":
            argv = args[0] if isinstance(args[0], (list, tuple)) else (args[1] if len(args) > 1 and isinstance(args[1], (list, tuple)) else None)
            env = None
            cwd = None
            for slot in args[2:4]:
                if isinstance(slot, dict) and env is None:
                    env = slot
                elif isinstance(slot, str) and cwd is None:
                    cwd = slot
        elif event in {"os.posix_spawn", "os.exec"}:
            argv = args[1] if len(args) > 1 and isinstance(args[1], (list, tuple)) else None
            cwd = None
        if env is not None:
            if any(os.fsdecode(key).startswith("GIT_") for key in env):
                raise PermissionError("GE16 runner forbids child Git environment overrides")
        if isinstance(args[0], str) and isinstance(argv, (list, tuple)):
            ok = readonly_git(args[0], argv)
        elif isinstance(args[0], (list, tuple)):
            ok = readonly_git(args[0][0], args[0])
        else:
            ok = False
        if cwd is not None:
            ok = ok and resolve(cwd) == root
        if pathlib.Path.cwd() != root or not ok:
            raise PermissionError("GE16 runner forbids this child process")
    elif event in {"os.system", "os.fork", "os.forkpty", "socket.bind", "socket.sendto", "socket.sendmsg"}: raise PermissionError("GE16 runner forbids child processes and network access")
# Runner-owned pins arrive as immutable bootstrap arguments, not child metadata.
meta = json.loads(sys.argv[6])
modules_pinned = meta["modules_pinned"]
committed_modules_pinned = meta["committed_module_sha256"]
def verify_module(relative, source):
    if hashlib.sha256(source).hexdigest() != modules_pinned[relative]:
        raise PermissionError("GE16 pinned module hash mismatch: " + relative)
    return source
original_get_code = importlib.machinery.SourceFileLoader.get_code
def pinned_get_code(loader, fullname):
    # Match the lexical load path before resolving: a swapped symlink must
    # never turn a pinned import into an unpinned fallback.
    path = pathlib.Path(os.path.abspath(loader.path))
    relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
    if relative in modules_pinned:
        if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
            raise PermissionError("GE16 pinned module path is unsafe")
        # Bypass cached bytecode and execute exactly the bytes verified here.
        return compile(verify_module(relative, path.read_bytes()), str(path), "exec")
    return original_get_code(loader, fullname)
importlib.machinery.SourceFileLoader.get_code = pinned_get_code
# Both release wrappers also execute validator bytes obtained with git show.
# Audit compile checks those bytes before any module top-level code can execute.
def pinned_compile(event, args):
    if event == "compile" and modules_pinned:
        source, filename = args
        if isinstance(filename, str) and filename.endswith((":validate_release_intake.py", ":scripts/validate_release_intake.py")):
            expected = committed_modules_pinned.get(filename, modules_pinned["3_OUTPUTS/scripts/validate_release_intake.py"])
            source = source.encode() if isinstance(source, str) else source
            if hashlib.sha256(source).hexdigest() != expected:
                raise PermissionError("GE16 pinned committed module hash mismatch")
sys.addaudithook(pinned_compile)
sys.addaudithook(audit)
expected_hash = sys.argv[4]
sys.argv = [str(script)] + sys.argv[7:]
if expected_hash == "isolated":
    runpy.run_path(str(script), run_name="__main__")
else:
    source = script.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_hash:
        raise PermissionError("GE16 live script hash mismatch")
    # Sibling imports use only the verified, code-allowlisted script's parent.
    # -I/-S still exclude caller-supplied Python paths and site setup.
    sys.path.insert(0, str(script.parent))
    # Execute precisely the bytes hashed above; path replacement cannot swap
    # the entry point between hash validation and execution.
    module = types.ModuleType("__main__")
    module.__dict__.update({"__file__": str(script), "__package__": "",
                            "__spec__": None, "__cached__": None})
    sys.modules["__main__"] = module
    exec(compile(source, str(script), "exec"), module.__dict__)
'''
def _run_allowlisted_command(root: pathlib.Path, name: str, owner: str, live_script_hashes: Optional[dict] = None, stage_arguments: Tuple[str, ...] = ()) -> None:
    command = COMMAND_ALLOWLIST.get(name)
    if command is None: raise IsolatedRunError("adapter command is not allowlisted: %s" % name)
    if owner not in DOMAIN_NAMES or pathlib.PurePosixPath(command[0]).parts[0] != owner:
        raise IsolatedRunError("allowlisted command does not belong to owner")
    if stage_arguments:
        if name not in {"outputs-intake-seal", "delivery-publish"}:
            raise IsolatedRunError("stage arguments only belong to release adapters")
        command = (*command, *stage_arguments)
    owner_root = root / owner
    if not _real_path(owner_root) or not owner_root.is_dir():
        raise IsolatedRunError("adapter owner is not a real directory")
    # A pre-existing hard link could alias a sibling or an external file while
    # appearing owner-local. Children cannot create such links across the fence.
    if any(path.is_file() and not path.is_symlink() and path.stat().st_nlink != 1 for path in owner_root.rglob("*")):
        raise IsolatedRunError("owner domain contains unsafe hard-linked file")
    _safe_regular_file(root, root / command[0], "allowlisted command %s" % name)
    sandbox = pathlib.Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file() or not os.access(sandbox, os.X_OK):
        raise IsolatedRunError("native macOS sandbox-exec is unavailable")
    # Python needs its standard library, hence unrestricted reads; default deny
    # still blocks network except the collector rules, and writes are owner-confined.
    interpreter = OWNER_CHILD_INTERPRETER.get(owner) or sys.executable
    if not pathlib.Path(interpreter).is_file():
        raise IsolatedRunError("owner child interpreter is missing: %s" % interpreter)
    executable = pathlib.Path(interpreter).resolve()
    exec_rules = [
        '(allow process-exec (literal %s))' % json.dumps(sys.executable),
        '(allow process-exec (literal %s))' % json.dumps(str(executable)),
    ]
    framework = next((path for path in executable.parents if path.name.endswith('.framework')), None)
    if framework is not None:
        exec_rules.append('(allow process-exec (subpath %s))' % json.dumps(str(framework)))
    network = COMMAND_CHILDPOLICY.get(name, "deny")
    if network == "ro-git":
        exec_rules.append('(allow process-exec (literal "/usr/bin/git"))')
        # macOS git(1) from PATH may resolve through the Xcode CLT shim.
        exec_rules.append('(allow process-exec (literal "/Library/Developer/CommandLineTools/usr/bin/git"))')
        exec_rules.append('(allow process-fork)')
        # git opens /dev/null for reading and writing (pager/config fallbacks).
        exec_rules.extend(['(allow file-read* (literal "/dev/null"))',
                           '(allow file-write* (literal "/dev/null"))'])
    network_rules = []
    if network == "https":
        network_rules = [
            '(allow network-outbound (remote tcp "*:443"))',
            '(allow system-socket (require-all (socket-domain AF_SYSTEM) (socket-protocol 2)))',
            '(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration"))',
            '(allow mach-lookup (global-name "com.apple.dnssd.service"))',
            '(allow network-outbound (literal "/private/var/run/mDNSResponder"))',
        ]
    profile = '\n'.join([
        '(version 1)',
        '(deny default)',
        '(allow file-read*)',
        '(allow sysctl-read)',
        '(deny file-read* (subpath %s))' % json.dumps(str(root / 'OPS/security')),
        *exec_rules,
        *network_rules,
        '(allow file-write* (subpath %s))' % json.dumps(str(owner_root)),
        '(deny file-write* (subpath %s))' % json.dumps(str(owner_root / LIVE_CLAIMS_DIRECTORY)),
        '(deny file-write* (subpath %s))' % json.dumps(str(owner_root / '.git')),
        '(deny file-write-unlink (literal %s))' % json.dumps(str(owner_root)),
        '',
    ])
    modules_pinned = {}
    if live_script_hashes is not None and network == "ro-git":
        modules_pinned = {path: live_script_hashes.get(path) for path in PINNED_MODULE_PATHS}
        for path, digest in modules_pinned.items():
            _safe_regular_file(root, root / path, "pinned release module")
            if not isinstance(digest, str) or _sha256(root / path) != digest:
                raise LiveValidationError("bounded-live-module-hash-mismatch")
    refs_before = _refs_snapshot(owner_root)
    git_inputs_before = _git_execution_inputs(owner_root)
    environment = dict(CHILD_ENVIRONMENT)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="ge16-sandbox-", suffix=".sb", delete=False) as handle:
        handle.write(profile); profile_path = handle.name
    try:
        expected_hash = "isolated"
        if live_script_hashes is not None:
            expected_hash = live_script_hashes.get(command[0])
            if (owner == "5_WEBSITES" or not isinstance(expected_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
                    or _sha256(root / command[0]) != expected_hash):
                raise LiveValidationError("bounded-live-script-hash-mismatch")
        result = subprocess.run((str(sandbox), "-f", profile_path, str(interpreter), "-I", "-c", _AUDITED_PYTHON, str(root), owner, command[0], expected_hash, network, json.dumps({"modules_pinned": modules_pinned, "committed_module_sha256": COMMITTED_MODULE_SHA256}), *command[1:]), cwd=str(root), env=environment, shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    finally:
        try:
            _refs_unchanged(owner_root, refs_before)
            if _git_execution_inputs(owner_root) != git_inputs_before:
                raise IsolatedRunError("child changed Git execution inputs")
        finally:
            pathlib.Path(profile_path).unlink(missing_ok=True)
    if result.returncode:
        # Reads are unrestricted: adapter diagnostics may contain credentials or
        # authorization contents, so never forward stdout/stderr to the caller.
        raise IsolatedRunError("allowlisted command failed (%s), exit %d" % (name, result.returncode))

def _handoff_path(root: pathlib.Path, stage: int) -> pathlib.Path:
    domain, filename, _, _ = HANDOFFS[stage]; return root / domain / "manifest" / filename
def _write_json(root: pathlib.Path, path: pathlib.Path, value: object) -> None:
    if not _is_within(path, root) or not _real_path(path) or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise IsolatedRunError("unsafe handoff path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
def _write_handoff(root: pathlib.Path, contract: dict, stage: int, run_id: str, refs: List[str], gate_records: Optional[List[dict]] = None) -> None:
    handoff = _handoff_path(root, stage)
    manifest = {"run_id": run_id, "contract_id": contract["id"], "contract_version": contract["version"], "owner_domain": contract["owner_domain"], "input_manifest_refs": refs, "output_manifest_refs": contract["output_manifest_refs"], "created_at_utc": _utc_now()}
    if set(manifest) != set(MANIFEST_FIELDS): raise IsolatedRunError("internal manifest record schema mismatch")
    gates = gate_records if gate_records is not None else [{"run_id": run_id, "gate_id": gate, "status": "passed", "checked_at_utc": _utc_now(), "evidence_ref": refs[0]} for gate in contract["gate_records_required"]]
    if any(set(gate) != set(GATE_FIELDS) for gate in gates): raise IsolatedRunError("internal gate record schema mismatch")
    _write_json(root, handoff, manifest); _write_json(root, handoff.with_name(handoff.stem + ".gates.json"), gates)

def _read_handoff(root: pathlib.Path, stage: int) -> Tuple[dict, str]:
    domain, filename, contract_id, contract_version = HANDOFFS[stage]
    handoff = root / domain / "manifest" / filename
    gates_path = handoff.with_name(handoff.stem + ".gates.json")
    _assert_fresh_file(root, handoff, "Stage %d %s manifest" % (stage, domain))
    _assert_fresh_file(root, gates_path, "Stage %d %s gates" % (stage, domain))
    try: manifest, gates = json.loads(handoff.read_text(encoding="utf-8")), json.loads(gates_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error: raise IsolatedRunError("invalid Stage %d %s handoff JSON" % (stage, domain)) from error
    if not isinstance(manifest, dict) or set(manifest) != set(MANIFEST_FIELDS): raise IsolatedRunError("Stage %d %s manifest fields are not exact" % (stage, domain))
    if manifest.get("contract_id") != contract_id or manifest.get("contract_version") != contract_version or manifest.get("owner_domain") != domain or not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]: raise IsolatedRunError("Stage %d %s manifest does not bind the immediate upstream contract" % (stage, domain))
    refs = manifest.get("input_manifest_refs")
    if not isinstance(refs, list) or len(refs) != (2 if stage in (3, 4) else 1) or not isinstance(refs[0], str) or "#sha256:" not in refs[0]: raise IsolatedRunError("Stage %d %s manifest input lineage is invalid" % (stage, domain))
    for ref in refs:
        _resolve_evidence_ref(root, ref, "Stage %d %s manifest" % (stage, domain))
    if not isinstance(manifest.get("output_manifest_refs"), list) or not manifest["output_manifest_refs"] or manifest["output_manifest_refs"] != _load_validated_request(contract_id, contract_version)["output_manifest_refs"]: raise IsolatedRunError("Stage %d %s manifest output lineage is not exactly contract-bound" % (stage, domain))
    _assert_fresh(_parse_utc(manifest["created_at_utc"], "Stage manifest"), "Stage %d %s manifest" % (stage, domain))
    expected = set(_load_validated_request(contract_id, contract_version)["gate_records_required"])
    if not isinstance(gates, list) or len(gates) != len(expected): raise IsolatedRunError("Stage %d %s gates are missing" % (stage, domain))
    found = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != set(GATE_FIELDS) or gate.get("gate_id") in found or gate.get("run_id") != manifest["run_id"] or gate.get("status") != "passed" or gate.get("evidence_ref") != refs[0]: raise IsolatedRunError("Stage %d %s gate is not bound to manifest lineage" % (stage, domain))
        _resolve_evidence_ref(root, gate["evidence_ref"], "Stage %d %s gate" % (stage, domain))
        _assert_fresh(_parse_utc(gate.get("checked_at_utc"), "Stage gate"), "Stage %d %s gate" % (stage, domain)); found.add(gate["gate_id"])
    if found != expected: raise IsolatedRunError("Stage %d %s required gates have not all passed" % (stage, domain))
    return manifest, _sha256(handoff)

def _stage_input(root: pathlib.Path, stage: int) -> Tuple[List[str], str]:
    if stage == 1:
        path = root / "1_DATA/canonical-data-provenance.json"; _assert_fresh_file(root, path, "1_DATA source provenance evidence")
        digest = _sha256(path); return ["1_DATA/canonical-data-provenance.json#sha256:" + digest], "isolated-data-" + digest[:16]
    upstream, digest = _read_handoff(root, stage - 1)
    ref = _handoff_path(root, stage - 1).relative_to(root).as_posix() + "#sha256:" + digest
    if stage == 5:
        if re.fullmatch(r"H-[0-9]{8}-[0-9]{2}", upstream["run_id"]) is None: raise IsolatedRunError("Stage 4 4_DELIVERY manifest must provide an exact delivery ID")
        return [ref], upstream["run_id"]
    labels = {2: "analytics", 3: "outputs", 4: "delivery"}
    if stage == 4:
        return [ref], upstream["run_id"]
    if stage == 3:
        return [ref], "H-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d") + "-" + ("%02d" % (int(digest[:4], 16) % 100))
    return [ref], "isolated-%s-%s" % (labels[stage], digest[:16])

STAGE_FIVE_EVIDENCE_FIELDS = {
    "delivery_id", "delivery_manifest_sha256", "vercel_git_sha",
    "github_repo", "github_ref", "github_mirror_sha", "gate_id", "status",
}
STAGE_FIVE_READBACK_EVIDENCE_FIELDS = STAGE_FIVE_EVIDENCE_FIELDS | {
    "deployment_id", "url",
}
STAGE_FIVE_GITHUB_REPO = "tenunan-dev/hitungkerusi222"
STAGE_FIVE_GITHUB_REF = "main"


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def fixture_stage_five_evidence(
    delivery_id: str, delivery_manifest_sha256: str, vercel_git_sha: str,
    gate_id: str, status: str,
) -> dict:
    """Structured fixture evidence for tests; it never authorizes deployment."""
    evidence = {
        "delivery_id": delivery_id,
        "delivery_manifest_sha256": delivery_manifest_sha256,
        "vercel_git_sha": vercel_git_sha,
        "github_repo": STAGE_FIVE_GITHUB_REPO,
        "github_ref": STAGE_FIVE_GITHUB_REF,
        "github_mirror_sha": vercel_git_sha,
        "gate_id": gate_id,
        "status": status,
    }
    if gate_id == "vercel-post-deploy-readback-recorded" and status == "recorded":
        evidence.update({"deployment_id": "fixture-vercel-deployment", "url": "https://fixture.vercel.app"})
    return evidence


def _fixture_evidence_ref(relative: str, evidence: dict) -> str:
    return relative + "#sha256:" + hashlib.sha256(_canonical_json_bytes(evidence)).hexdigest()


def fixture_release_authorization(delivery_id: str, delivery_manifest_sha256: str) -> dict:
    """Exact fixture record used only by unit tests; it never enables deploy."""
    vercel_git_sha = "b" * 40
    gates = []
    for gate_id in STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"]:
        evidence = fixture_stage_five_evidence(
            delivery_id, delivery_manifest_sha256, vercel_git_sha, gate_id, "passed"
        )
        gates.append({
            "run_id": delivery_id, "gate_id": gate_id, "status": "passed",
            "checked_at_utc": _utc_now(),
            "evidence_ref": _fixture_evidence_ref("5_WEBSITES/evidence/%s.json" % gate_id, evidence),
        })
    readback = fixture_stage_five_evidence(
        delivery_id, delivery_manifest_sha256, vercel_git_sha,
        "vercel-post-deploy-readback-recorded", "recorded",
    )
    return {"approval_status": "approved", "delivery_id": delivery_id, "target": "5_WEBSITES", "vercel_git_sha": vercel_git_sha, "delivery_manifest_sha256": delivery_manifest_sha256, "authorization_state": "exact-release-record-required", "required_gates": STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"], "direct_vercel_cli": "required", "vercel_post_deploy_readback": "recorded-required", "github_mirror_parity": "exact-required", "git_integration_deployment": "forbidden", "gate_evidence": gates, "vercel_readback_evidence": {"status": "recorded", "evidence_ref": _fixture_evidence_ref("5_WEBSITES/evidence/vercel-readback.json", readback)}}


def _validate_stage_five_evidence(
    root: pathlib.Path, evidence_ref: object, label: str, delivery_id: str,
    delivery_sha: str, vercel_git_sha: str, gate_id: str, status: str,
    readback: bool = False,
) -> None:
    anchor = re.fullmatch(r"[^#]+#sha256:([0-9a-f]{64})", evidence_ref) if isinstance(evidence_ref, str) else None
    if anchor is None:
        raise IsolatedRunError("%s evidence_ref must use #sha256:<digest>" % label)
    path = _resolve_evidence_ref(root, evidence_ref, label)
    try:
        raw_evidence = path.read_bytes()
        expected_digest = anchor.group(1)
        if hashlib.sha256(raw_evidence).hexdigest() != expected_digest:
            raise IsolatedRunError("%s changed while being validated" % label)
        evidence = json.loads(raw_evidence)
    except json.JSONDecodeError as error:
        raise IsolatedRunError("%s is not valid JSON" % label) from error
    required_fields = STAGE_FIVE_READBACK_EVIDENCE_FIELDS if readback else STAGE_FIVE_EVIDENCE_FIELDS
    if not isinstance(evidence, dict) or set(evidence) != required_fields:
        raise IsolatedRunError("%s schema is not exact" % label)
    expected = {
        "delivery_id": delivery_id,
        "delivery_manifest_sha256": delivery_sha,
        "vercel_git_sha": vercel_git_sha,
        "github_repo": STAGE_FIVE_GITHUB_REPO,
        "github_ref": STAGE_FIVE_GITHUB_REF,
        "github_mirror_sha": vercel_git_sha,
        "gate_id": gate_id,
        "status": status,
    }
    if any(evidence.get(field) != value for field, value in expected.items()):
        raise IsolatedRunError("%s does not bind the authorized Stage 5 release" % label)
    if readback and (not isinstance(evidence["deployment_id"], str) or not evidence["deployment_id"] or not isinstance(evidence["url"], str) or not evidence["url"]):
        raise IsolatedRunError("%s lacks Vercel deployment identity" % label)


def _validate_stage_five_authorization(root: pathlib.Path, delivery_id: str, delivery_sha: str) -> List[dict]:
    auth_path, vercel_path = root/"5_WEBSITES/release-authorization.json", root/"5_WEBSITES/vercel-manifest.json"
    _assert_fresh_file(root, auth_path, "5_WEBSITES exact release authorization"); _assert_fresh_file(root, vercel_path, "5_WEBSITES Vercel manifest")
    try: auth = json.loads(auth_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error: raise IsolatedRunError("invalid 5_WEBSITES release authorization JSON") from error
    fields = {"approval_status","delivery_id","target","vercel_git_sha","delivery_manifest_sha256","authorization_state","required_gates","direct_vercel_cli","vercel_post_deploy_readback","github_mirror_parity","git_integration_deployment","gate_evidence","vercel_readback_evidence"}
    if not isinstance(auth, dict) or set(auth) != fields: raise IsolatedRunError("5_WEBSITES release authorization fields are not exact")
    if auth.get("approval_status") != "approved" or auth.get("target") != "5_WEBSITES" or auth.get("delivery_id") != delivery_id or auth.get("delivery_manifest_sha256") != delivery_sha: raise IsolatedRunError("5_WEBSITES authorization does not exactly bind 4_DELIVERY and Vercel manifest")
    for field in ("authorization_state","direct_vercel_cli","vercel_post_deploy_readback","github_mirror_parity","git_integration_deployment"):
        if auth.get(field) != STAGE_FIVE_RELEASE_REQUIREMENTS[field]: raise IsolatedRunError("5_WEBSITES authorization requirement %s is not exact" % field)
    if auth.get("required_gates") != STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"] or re.fullmatch(STAGE_FIVE_RELEASE_REQUIREMENTS["vercel_git_sha_pattern"], auth.get("vercel_git_sha","")) is None: raise IsolatedRunError("5_WEBSITES authorization gate list or Vercel Git SHA is not exact")
    try: commit_manifest = json.loads(vercel_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error: raise IsolatedRunError("invalid 5_WEBSITES Vercel manifest JSON") from error
    if not isinstance(commit_manifest, dict) or set(commit_manifest) != {"git_sha", "delivery_manifest_sha256"} or commit_manifest.get("git_sha") != auth["vercel_git_sha"] or commit_manifest.get("delivery_manifest_sha256") != delivery_sha:
        raise IsolatedRunError("5_WEBSITES Vercel manifest is not bound to the authorized delivery")
    gates, required = auth.get("gate_evidence"), set(STAGE_FIVE_RELEASE_REQUIREMENTS["required_gates"])
    if not isinstance(gates, list) or len(gates) != len(required): raise IsolatedRunError("5_WEBSITES authorization gate records are missing")
    found = set()
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) != set(GATE_FIELDS) or gate.get("gate_id") in found or gate.get("run_id") != delivery_id or gate.get("status") != "passed": raise IsolatedRunError("5_WEBSITES authorization gate record is invalid")
        _validate_stage_five_evidence(root, gate.get("evidence_ref"), "5_WEBSITES authorization gate", delivery_id, delivery_sha, auth["vercel_git_sha"], gate["gate_id"], "passed")
        _assert_fresh(_parse_utc(gate.get("checked_at_utc"), "5_WEBSITES authorization gate"), "5_WEBSITES authorization gate"); found.add(gate["gate_id"])
    if found != required: raise IsolatedRunError("5_WEBSITES authorization has not passed every release gate")
    for name, gate_id in (("vercel_readback_evidence","vercel-post-deploy-readback-recorded"),):
        record, gate = auth.get(name), next(item for item in gates if item["gate_id"] == gate_id)
        if not isinstance(record, dict) or set(record) != {"status","evidence_ref"} or record.get("status") != "recorded": raise IsolatedRunError("5_WEBSITES %s is not separately recorded" % name)
        _validate_stage_five_evidence(root, record["evidence_ref"], "5_WEBSITES %s" % name, delivery_id, delivery_sha, auth["vercel_git_sha"], gate_id, "recorded", readback=True)
    return gates

def _resolve_isolated_adapter(contract: dict, phase: Optional[str]) -> Tuple[Tuple[int, str, Tuple[str, ...]], bool]:
    """Resolve the (stage, owner, commands) adapter and whether it writes handoff.

    A list isolated_adapter is a two-phase stage: index 0 is phase a (collect
    only, never writes handoff), index 1 is phase b (commits the agent's
    judged output and is the only phase that may emit the stage handoff). A
    string isolated_adapter is a single-phase stage and rejects any --phase.
    """
    raw = contract.get("isolated_adapter")
    if isinstance(raw, list):
        if len(raw) != 2 or not all(isinstance(name, str) for name in raw):
            raise IsolatedRunError("contract isolated_adapter phases are not exact")
        if phase not in ("a", "b"):
            raise IsolatedRunError("contract stage requires an explicit --phase a or --phase b")
        name = raw[0] if phase == "a" else raw[1]
        write_handoff = phase == "b"
    elif isinstance(raw, str):
        if phase is not None:
            raise IsolatedRunError("contract stage does not support phase selection")
        name = raw
        write_handoff = True
    else:
        raise IsolatedRunError("contract isolated_adapter is not allowlisted")
    adapter = STAGE_ADAPTERS.get(name)
    if adapter is None:
        raise IsolatedRunError("contract isolated_adapter is not allowlisted")
    return adapter, write_handoff

def _refs_snapshot(owner_root: pathlib.Path) -> dict:
    git = owner_root / ".git"
    paths = [git / "HEAD", git / "packed-refs", *(git / "refs/heads").rglob("*")]
    result = {}
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            continue
        if path.exists() or path.is_symlink():
            _safe_regular_file(owner_root, path, "Git reference")
            result[path.relative_to(git).as_posix()] = path.read_bytes()
    return result


def _refs_unchanged(owner_root: pathlib.Path, before: dict) -> None:
    if _refs_snapshot(owner_root) != before:
        raise IsolatedRunError("child changed owner Git refs")


def _git_execution_inputs(owner_root: pathlib.Path) -> str:
    """Inspect raw Git inputs without invoking Git or accepting config includes."""
    paths = {owner_root / ".git/config", owner_root / ".git/config.worktree",
             owner_root / ".git/info/attributes"}
    paths.update(owner_root.rglob(".gitattributes"))
    contents = []
    for path in sorted(paths):
        if not path.exists() and not path.is_symlink():
            continue
        _safe_regular_file(owner_root, path, "Git execution input")
        source = path.read_bytes()
        section = ""
        try:
            lines = source.decode("utf-8", "strict").splitlines()
        except UnicodeError as error:
            raise IsolatedRunError("unreadable Git execution input") from error
        for line in lines:
            value = line.strip()
            if not value or value.startswith(("#", ";")):
                continue
            match = re.match(r'\[\s*([A-Za-z]+)(?:\s+"[^"\n]*"|\.[^]\n]+)?\s*\]', value)
            if match:
                section = match[1].lower()
                # Includes can hide arbitrary executable configuration.
                if section in {"include", "includeif"}:
                    raise IsolatedRunError("unsafe Git execution configuration")
                value = value[match.end():].strip()
            key = value.partition("=")[0].strip().lower()
            setting = value.partition("=")[2].strip()
            if (re.search(r"\b(filter|textconv|diff)\.[A-Za-z0-9_.-]+\s*=\s*\S", value, re.I)
                    or (section == "filter" and key in {"clean", "smudge", "process"} and setting)
                    or (section in {"diff", "textconv"} and key in {"command", "textconv"} and setting)
                    or ((section == "color" or key.startswith("color."))
                        and re.search(r"[!;|&`$<>\\]|(?:^|\s)(?:/\S+|sh|bash|zsh)(?:\s|$)", setting))
                    or key in {"core.pager", "core.fsmonitor", "core.fsmonitordaemon"}
                    or (section == "core" and key in {"pager", "fsmonitor", "fsmonitordaemon"})
                    or key.startswith(("include.", "includeif."))):
                raise IsolatedRunError("unsafe Git execution configuration")
        contents.append((path.relative_to(owner_root).as_posix(), source.hex()))
    return hashlib.sha256(json.dumps(contents, separators=(",", ":")).encode()).hexdigest()


_PARENT_GIT_INPUTS = {}


@contextlib.contextmanager
def _parent_git_input_guard(root):
    before = dict(_PARENT_GIT_INPUTS)
    try:
        for domain in DOMAIN_NAMES[:4]:
            repository = root / domain
            _PARENT_GIT_INPUTS[repository] = _git_execution_inputs(repository)
        yield
    finally:
        _PARENT_GIT_INPUTS.clear()
        _PARENT_GIT_INPUTS.update(before)


def _parent_git(root: pathlib.Path, domain: str, *arguments: str) -> str:
    repository = root / domain
    if domain not in DOMAIN_NAMES[:4] or not _real_path(repository):
        raise IsolatedRunError("unsafe parent Git repository")
    snapshot = _git_execution_inputs(repository)
    if repository in _PARENT_GIT_INPUTS and snapshot != _PARENT_GIT_INPUTS[repository]:
        raise IsolatedRunError("Git execution inputs changed since child staging")
    environment = {**CHILD_ENVIRONMENT, "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_ATTR_NOSYSTEM": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                   "GIT_AUTHOR_NAME": "GE16 Pipeline", "GIT_AUTHOR_EMAIL": "pipeline@tenunan.com",
                   "GIT_COMMITTER_NAME": "GE16 Pipeline", "GIT_COMMITTER_EMAIL": "pipeline@tenunan.com"}
    if domain == "3_OUTPUTS":
        environment["GIT_AUTHOR_NAME"] = environment["GIT_COMMITTER_NAME"] = "GE16 Stage 3 Pipeline"
    result = subprocess.run(["git", "-C", str(repository), "-c", "core.hooksPath=/dev/null",
                             "-c", "commit.gpgsign=false", "-c", "core.fsmonitor=false",
                             "-c", "core.attributesFile=/dev/null", "-c", "core.fsmonitorHookVersion=0", *arguments],
                            cwd=str(root), env=environment, capture_output=True, text=True, check=True)
    return result.stdout.rstrip("\n")


def _local_repository(root: pathlib.Path, domain: str) -> None:
    path = root / domain
    if not _real_path(path / ".git") or not (path / ".git").is_dir():
        raise IsolatedRunError("parent step requires a local domain Git repository: " + domain)
    if pathlib.Path(_parent_git(root, domain, "rev-parse", "--show-toplevel")) != path:
        raise IsolatedRunError("parent Git root mismatch")


def _dirty_paths(root: pathlib.Path, domain: str) -> List[str]:
    raw = _parent_git(root, domain, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    records = iter(raw.split("\0"))
    paths = []
    for record in records:
        if not record:
            continue
        paths.append(record[3:])
        if "R" in record[:2] or "C" in record[:2]:
            paths.append(next(records))
    return paths


def _commit_upstream_manifests(root: pathlib.Path, guard=None) -> Dict[str, str]:
    """P1: check BOTH trees first; never absorb unrelated staged changes."""
    dirty = {}
    for domain in ("1_DATA", "2_ANALYTICS"):
        _local_repository(root, domain)
        dirty[domain] = _dirty_paths(root, domain)
        if any(not path.startswith("manifest/") for path in dirty[domain]):
            raise IsolatedRunError("non-manifest dirt blocks Stage 3: " + domain)
        for relative in dirty[domain]:
            path = root / domain / relative
            if not _real_path(path) or (path.is_file() and path.stat().st_nlink != 1):
                raise IsolatedRunError("unsafe upstream manifest path")
    heads = {}
    for domain, paths in dirty.items():
        if guard is not None:
            guard()
        if _dirty_paths(root, domain) != paths:
            raise IsolatedRunError("upstream changed during parent preflight")
        if paths:
            _parent_git(root, domain, "add", "--", *paths)
            _parent_git(root, domain, "commit", "-q", "-m", "GE16 pipeline handoff provenance")
        if _dirty_paths(root, domain):
            raise IsolatedRunError("upstream is not clean after parent commit")
        heads[domain] = _parent_git(root, domain, "rev-parse", "HEAD")
    return heads


def _initialize_isolated_repositories(root: pathlib.Path) -> None:
    # This check is mandatory even when called directly from a fixture helper.
    _validated_isolated_root(root)
    for domain in DOMAIN_NAMES[:4]:
        repository = root / domain
        repository.mkdir(exist_ok=True)
        if not (repository / ".git").exists():
            _parent_git(root, domain, "init", "-q")
            _parent_git(root, domain, "add", "--", ".")
            _parent_git(root, domain, "commit", "-q", "--allow-empty", "-m", "GE16 isolated fixture")
        _local_repository(root, domain)


def _release_child(root, name, owner, arguments, guard, hashes):
    if guard is not None:
        guard()
    before = _inventory(root)
    _run_allowlisted_command(root, name, owner, live_script_hashes=hashes, stage_arguments=tuple(arguments))
    _assert_only_owner_changed(before, root, owner)


def _release_binding(root, run_id):
    path = root / "3_OUTPUTS/manifest/ge16-release-seal-input.json"
    _safe_regular_file(root, path, "OUTPUTS seal input")
    value = json.loads(path.read_text())
    manifest = root / "3_OUTPUTS/releases" / run_id / "manifest.json"
    _safe_regular_file(root, manifest, "sealed release manifest")
    if (set(value) != {"schema", "release_id", "sealed_commit", "sealed_at_utc", "manifest_sha256"}
            or value["schema"] != "outputs.release-seal-input.v1" or value["release_id"] != run_id
            or not isinstance(value["sealed_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["sealed_commit"])
            or value["manifest_sha256"] != _sha256(manifest)):
        raise IsolatedRunError("OUTPUTS seal binding mismatch")
    return path, value


def _run_release_stage(contract, root, stage, owner, refs, run_id, guard, hashes):
    if guard is None:
        _initialize_isolated_repositories(root)
    with _parent_git_input_guard(root):
        return _run_guarded_release_stage(contract, root, stage, owner, refs, run_id, guard, hashes)


def _run_guarded_release_stage(contract, root, stage, owner, refs, run_id, guard, hashes):
    if stage == 3:
        heads = _commit_upstream_manifests(root, guard)
        # P1 changes only commit provenance; upstream handoff bytes remain bound.
        if _stage_input(root, stage) != (refs, run_id):
            raise IsolatedRunError("upstream lineage changed during parent preparation")
        _local_repository(root, "3_OUTPUTS")
        # Never let an unrelated pre-staged path enter the release commit.
        if _parent_git(root, "3_OUTPUTS", "diff", "--cached", "--name-only"):
            raise IsolatedRunError("OUTPUTS index must be clean before staging")
        _release_child(root, "outputs-intake-seal", owner,
                       ("--stage", "write", "--release-id", run_id,
                        "--analytics-sha", heads["2_ANALYTICS"], "--data-sha", heads["1_DATA"]), guard, hashes)
        if guard is not None:
            guard()
        if _parent_git(root, "3_OUTPUTS", "diff", "--cached", "--name-only"):
            raise IsolatedRunError("child unexpectedly changed OUTPUTS index")
        manifest_dir = root / "3_OUTPUTS/manifest"
        if not _real_path(manifest_dir):
            raise IsolatedRunError("unsafe OUTPUTS manifest directory")
        manifest_dir.mkdir(exist_ok=True)
        release = root / "3_OUTPUTS/releases" / run_id
        release_files = sorted(path.relative_to(root / "3_OUTPUTS").as_posix()
                               for path in release.rglob("*") if path.is_file())
        for relative in release_files:
            _safe_regular_file(root, root / "3_OUTPUTS" / relative, "release commit file")
        if not release_files:
            raise IsolatedRunError("empty release")
        _parent_git(root, "3_OUTPUTS", "add", "--", *release_files)
        _parent_git(root, "3_OUTPUTS", "commit", "-q", "-m", "GE16 Stage 3 release " + run_id)
        sealed_commit = _parent_git(root, "3_OUTPUTS", "rev-parse", "HEAD")
        _release_child(root, "outputs-intake-seal", owner,
                       ("--stage", "seal", "--release-id", run_id, "--sealed-commit", sealed_commit), guard, hashes)
        binding_path, binding = _release_binding(root, run_id)
        if binding["sealed_commit"] != sealed_commit:
            raise IsolatedRunError("seal child did not bind the parent commit")
    else:
        _, seal = _release_binding(root, run_id)
        _release_child(root, "delivery-publish", owner, ("--release-id", run_id), guard, hashes)
        binding_path = root / "4_DELIVERY/manifest/ge16-delivery-input.json"
        _safe_regular_file(root, binding_path, "DELIVERY gate input")
        binding = json.loads(binding_path.read_text())
        release = root / "4_DELIVERY/releases" / run_id
        delivery_manifest = release / "DELIVERY.json"
        _safe_regular_file(root, delivery_manifest, "DELIVERY manifest")
        if (binding.get("delivery_id") != run_id or binding.get("outputs_release_id") != run_id
                or binding.get("outputs_sealed_commit") != seal["sealed_commit"]
                or binding.get("delivery_manifest_sha256") != _sha256(delivery_manifest)
                or "delivery_commit" not in binding or binding["delivery_commit"] is not None):
            raise IsolatedRunError("DELIVERY gate input mismatch")
        document = json.loads(delivery_manifest.read_text())
        for relative, meta in document["files"].items():
            path = release / relative
            _safe_regular_file(root, path, "DELIVERY artifact")
            if not _is_within(path.resolve(), release) or _sha256(path) != meta["sha256"] or path.stat().st_size != meta["bytes"]:
                raise IsolatedRunError("DELIVERY artifact hash mismatch")
        if not (root / "4_DELIVERY/.git").is_dir():
            marker = root / "4_DELIVERY/manifest/.needs-git-init"
            _safe_regular_file(root, marker, "DELIVERY needs-git-init marker")
            # Included in the final step record, not a second JSON stdout line.
    if _stage_input(root, stage) != (refs, run_id):
        raise IsolatedRunError("stage input changed during release execution")
    if guard is not None:
        guard()
    _write_handoff(root, contract, stage, run_id,
                   [*refs, binding_path.relative_to(root).as_posix() + "#sha256:" + _sha256(binding_path)])
    return run_id


def _run_isolated(contract: dict, root: pathlib.Path, requested_stage: Optional[int], phase: Optional[str] = None, live_guard=None, live_script_hashes=None) -> str:
    adapter, write_handoff = _resolve_isolated_adapter(contract, phase)
    stage, owner, commands = adapter
    if stage == 5 and (live_guard is not None or live_script_hashes is not None):
        raise LiveValidationError("stage-5-live-execution-forbidden")
    if requested_stage is not None and requested_stage != stage: raise IsolatedRunError("requested stage does not match contract adapter")
    if contract.get("owner_domain") != owner: raise IsolatedRunError("adapter owner does not match contract owner")
    if stage == 1:
        # Stage 1 owns and refreshes this evidence; stale input must not block
        # collection. Freshness is enforced after refresh and validation.
        _safe_regular_file(root, root / "1_DATA/canonical-data-provenance.json", "1_DATA source provenance evidence")
        if phase == "b":
            # Evidence phase a actually ran in THIS cycle, not mere convention.
            # A filesystem timestamp alone is insufficient: candidates from a
            # previous cycle, or from a phase a that failed after collecting,
            # would qualify. Require a phase-a completion receipt bound to the
            # digest of the candidates it produced.
            candidates = root / STAGE_ONE_CANDIDATES_PATH
            _assert_fresh_file(root, candidates, "1_DATA news candidates evidence")
            receipt = _read_phase_a_receipt(root)
            candidates_digest = _sha256(candidates)
            if (receipt.get("candidates_sha256") != candidates_digest
                    or receipt.get("stage") != 1 or receipt.get("phase") != "a"):
                raise IsolatedRunError(
                    "1_DATA phase-b precondition not met: no current-cycle phase-a "
                    "receipt bound to these candidates; run phase a first"
                )
            # Phase b must not publish a cycle where judging never happened. A
            # missing or empty judged file means the agent skipped judging; the
            # digest binding also rejects judged output left from an earlier cycle.
            judged = root / STAGE_ONE_JUDGED_PATH
            _assert_fresh_file(root, judged, "1_DATA news judged evidence")
            try:
                judged_items = json.loads(judged.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise IsolatedRunError("1_DATA judged evidence is not valid JSON") from error
            if isinstance(judged_items, dict):
                judged_items = judged_items.get("accepted") or judged_items.get("items") or []
            if not isinstance(judged_items, list) or not judged_items:
                raise IsolatedRunError(
                    "1_DATA judged evidence contains no accepted items; judging must "
                    "run between phase a and phase b (an explicit zero-acceptance "
                    "cycle must record that outcome, e.g. {\"accepted\": []}, and is "
                    "still bound to the current candidates)"
                )
            judged_digest = _sha256(judged)
            # Phase A clears any leftover judged evidence, so its receipt
            # records '' for this cycle; a NON-empty receipt hash means the
            # receipt predates the clearing fix (or was replayed) — reject
            # unless it exactly re-matches (defends competitive re-execution).
            bound = receipt.get("judged_sha256")
            if bound and bound != judged_digest:
                raise IsolatedRunError(
                    "1_DATA judged evidence does not match this cycle's phase-a "
                    "receipt; judge the current candidates before phase b"
                )
        refs, run_id = [], ""
    else:
        refs, run_id = _stage_input(root, stage)
    delivery_sha = ""
    stage_five_gates = None
    if stage == 5:
        delivery_sha = "sha256:" + refs[0].rsplit("sha256:", 1)[1]
        stage_five_gates = _validate_stage_five_authorization(root, run_id, delivery_sha)
    # Reject any initially changed pin before the first child can mutate data.
    # Each entry point is also rehashed immediately before and inside execution.
    if live_script_hashes is not None:
        for name in commands:
            relative = COMMAND_ALLOWLIST[name][0]
            _safe_regular_file(root, root / relative, "pinned adapter")
            if live_script_hashes.get(relative) != _sha256(root / relative):
                raise LiveValidationError("bounded-live-script-hash-mismatch")
    if stage in (3, 4):
        return _run_release_stage(contract, root, stage, owner, refs, run_id, live_guard, live_script_hashes)
    before = _inventory(root)
    for command in commands:
        if live_guard is not None: live_guard()
        if live_script_hashes is None:
            _run_allowlisted_command(root, command, owner)
        else:
            _run_allowlisted_command(root, command, owner, live_script_hashes=live_script_hashes)
    _assert_only_owner_changed(before, root, owner)
    # The adapter must not invalidate the evidence checked before it ran.
    # Phase a never writes handoff and never refreshes provenance, so it has
    # no post-execution lineage to (re)compute; only phase b (or a single-
    # phase stage) needs current, fresh evidence here.
    if not (stage == 1 and phase == "a"):
        current_refs, current_run_id = _stage_input(root, stage)
        if stage == 1:
            refs, run_id = current_refs, current_run_id
        elif current_refs != refs or current_run_id != run_id:
            raise IsolatedRunError("stage input changed during adapter execution")
    if stage == 5:
        stage_five_gates = _validate_stage_five_authorization(root, run_id, delivery_sha)
        vercel = root / "5_WEBSITES" / "vercel-manifest.json"
        refs.append("5_WEBSITES/vercel-manifest.json#sha256:" + _sha256(vercel))
    if live_guard is not None: live_guard()
    if write_handoff:
        _write_handoff(root, contract, stage, run_id, refs, stage_five_gates)
    _assert_only_owner_changed(before, root, owner)
    if stage == 1 and phase == "a":
        # Completion receipt for phase b: binds this cycle's candidates and the
        # judged evidence the agent produces afterwards. Phase b refuses to run
        # unless this receipt still matches what is on disk, so candidates from
        # an earlier cycle — or a phase a that later failed — never qualify.
        candidates = root / STAGE_ONE_CANDIDATES_PATH
        judged = root / STAGE_ONE_JUDGED_PATH
        # A leftover judged file from a previous cycle would make phase A pin
        # the OLD hash; any in-cycle judging then changes the file and phase B
        # can never match. Phase A therefore clears the leftover judged
        # evidence: its ownership is the agent's judging step between
        # phases, and the last committed cycle's judged output lives on in
        # the committed manifests (phase B archives it into the cycle's
        # input manifest before the agent writes the fresh one).
        if judged.is_file() and live_script_hashes is not None:
            # LIVE mode only: a leftover judged file from a previous cycle
            # would be pinned by phase A and then changed by in-cycle judging,
            # making phase B's binding unsatisfiable. In dry-run fixtures the
            # judged file is pre-seeded and phase A must leave it in place.
            judged.unlink()
        receipt = {
            "stage": 1,
            "phase": "a",
            "candidates_sha256": _sha256(candidates),
            # Phase B rejects this placeholder unless the file at phase-b
            # time is a valid judged output produced THIS cycle (nonempty,
            # keyed to the current candidates digest).
            "judged_sha256": "",
        }
        _write_phase_a_receipt(root, receipt)
    return run_id

STAGE_ONE_RECEIPT_PATH = "1_DATA/manifest/ge16-stage1-phase-a-receipt.json"

def _write_phase_a_receipt(root: pathlib.Path, receipt: dict) -> None:
    path = root / STAGE_ONE_RECEIPT_PATH
    if not _is_within(path, root) or not _real_path(path) or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise IsolatedRunError("unsafe phase-a receipt path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

def _read_phase_a_receipt(root: pathlib.Path) -> dict:
    path = root / STAGE_ONE_RECEIPT_PATH
    _safe_regular_file(root, path, "1_DATA phase-a receipt")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise IsolatedRunError("1_DATA phase-a receipt is not valid JSON") from error
    if not isinstance(receipt, dict) or set(receipt) != {"stage", "phase", "candidates_sha256", "judged_sha256"}:
        raise IsolatedRunError("1_DATA phase-a receipt fields are not exact")
    return receipt

def _run_live(contract_id: str, version: str, authorization: pathlib.Path, requested_stage: Optional[int], phase: Optional[str], *, scheduled: bool = False) -> dict:
    with _exclusive_live_lock():
        return _run_live_locked(contract_id, version, authorization, requested_stage, phase, scheduled=scheduled)


def _run_live_locked(contract_id: str, version: str, authorization: pathlib.Path, requested_stage: Optional[int], phase: Optional[str], *, scheduled: bool = False) -> dict:
    digests = (_sha256(CONTRACT_PATH), _sha256(JOBS_PATH))
    contract = _load_validated_request(contract_id, version)
    ops_contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    clause = validate_bounded_live_execution(ops_contract)
    if clause["manual_execution_enabled"] is False:
        raise LiveValidationError("bounded-live-execution-revoked")
    # Fail before authorization/claim, mirroring the stage pre-check below;
    # _resolve_isolated_adapter is not reused here so a bad --phase surfaces
    # as validation-error (pre-flight), not execution-error (mid-adapter).
    raw_adapter = contract.get("isolated_adapter")
    if isinstance(raw_adapter, list):
        if len(raw_adapter) != 2 or not all(isinstance(name, str) for name in raw_adapter) or phase not in ("a", "b"):
            raise ValueError("live adapter or phase is not allowlisted")
        adapter_name = raw_adapter[0] if phase == "a" else raw_adapter[1]
    elif isinstance(raw_adapter, str):
        if phase is not None:
            raise ValueError("live adapter does not support phase selection")
        adapter_name = raw_adapter
    else:
        raise ValueError("live adapter or owner is not allowlisted")
    adapter = STAGE_ADAPTERS.get(adapter_name)
    if adapter is None or contract.get("owner_domain") != adapter[1]:
        raise ValueError("live adapter or owner is not allowlisted")
    stage, owner, _ = adapter
    if stage == 5:
        raise LiveValidationError("stage-5-live-execution-forbidden")
    if stage not in clause["allowed_stages"] or contract.get("mutable_writes") != [owner]:
        raise LiveValidationError("bounded-live-stage-not-authorized")
    root = _validated_live_root(REPOSITORY_ROOT)
    if requested_stage is not None and requested_stage != stage:
        raise ValueError("requested stage does not match contract adapter")
    validate_authorization = _validate_scheduled_fire if scheduled else _validate_live_authorization
    identity = validate_authorization(authorization, contract, stage, phase)

    def guard() -> None:
        _validated_live_root(root)
        if digests != (_sha256(CONTRACT_PATH), _sha256(JOBS_PATH)) or identity != validate_authorization(authorization, contract, stage, phase):
            raise ValueError("live authorization or contract changed during execution")

    guard()
    _claim_live_authorization(root, owner, identity)
    run_id = _run_isolated(contract, root, stage, phase=phase, live_guard=guard, live_script_hashes=clause["script_sha256"])
    return {**({"delivery_commit": None, "delivery_commit_note": "4_DELIVERY commit: none (no local repo yet)",
                "needs_git_init": "4_DELIVERY/manifest/.needs-git-init"} if stage == 4 else {}),
            "result": "completed", "mode": "scheduled-fire" if scheduled else "bounded-live", "stage": stage,
            "contract_id": contract_id, "contract_version": version, "run_id": run_id}

def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        if "--scheduled-fire" in arguments:
            if "--live-run" in arguments or "--authorization" in arguments or "--dry-run" in arguments:
                raise ValueError("scheduled-fire and manual/dry-run modes are mutually exclusive")
            if len(arguments) not in (7, 9, 11) or arguments[:5:2] != ["--contract", "--version", "--scheduled-fire"] or arguments[5] != "--fire-key":
                raise ValueError("scheduled arguments must be --contract <id> --version <version> --scheduled-fire --fire-key <file> [--stage N] [--phase a|b]")
            stage, phase = _parse_stage_phase_tail(arguments[7:])
            record = _run_live(arguments[1], arguments[3], pathlib.Path(arguments[6]), stage, phase, scheduled=True)
            print(json.dumps(record, separators=(",", ":")))
            return 0
        if "--live-run" in arguments:
            record = _run_live(*_parse_live_arguments(arguments))
            print(json.dumps(record, separators=(",", ":")))
            return 0
        elif "--dry-run" not in arguments:
            contract_id, version = _parse_locked_arguments(arguments); _load_validated_request(contract_id, version)
        else:
            contract_id, version, candidate, requested_stage, phase = _parse_dry_run_arguments(arguments)
            _run_isolated(_load_validated_request(contract_id, version), _validated_isolated_root(candidate), requested_stage, phase=phase)
    except LiveValidationError as error:
        _emit("validation-error", str(error)); return 1
    except (OSError, json.JSONDecodeError, ContractError, SyncError, ValueError, StopIteration, RecursionError) as error:
        _emit("validation-error", "bounded-live-validation-failed" if {"--live-run", "--scheduled-fire"} & set(arguments) else str(error)); return 1
    except (IsolatedRunError, subprocess.SubprocessError) as error:
        _emit("execution-error", "bounded-live-execution-failed" if {"--live-run", "--scheduled-fire"} & set(arguments) else str(error)); return 1
    if "--dry-run" not in arguments:
        # "posture", not "completed": nothing executed. A downstream agent must
        # never read this record as a stage run.
        print(json.dumps({"result": "posture", "mode": "posture-only",
                          "contract_id": contract_id, "contract_version": version,
                          "ops_contract_version": OPS_CONTRACT_VERSION, "lock_retired": True},
                         separators=(",", ":")))
        return 0
    _emit("completed", "isolated-dry-run-complete"); return 0
if __name__ == "__main__":
    raise SystemExit(main())
