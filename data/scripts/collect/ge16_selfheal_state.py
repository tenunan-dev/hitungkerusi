#!/usr/bin/env python3
"""GE16 Stage-1 pipeline self-heal state — one small helper, one state file.

WHY THIS EXISTS
    The Stage 1 loop is collect (runner) -> judge (cron agent) -> commit
    (runner). It runs once a week from the scheduler and nobody watches it live.
    A transient fetch error, an agent that exhausts its idle budget mid-batch, or
    a phase B failure otherwise costs a whole week of news, silently. This helper
    records the outcome of every step so the NEXT scheduled run - the live cron
    agent, which reads stdout - can see what failed and rerun exactly that step.

    There is NO daemon, NO extra cron job and NO scheduler change. Healing is
    purely on-disk state consumed by the next scheduled run:
      * collect failed  -> the candidate keys stay in ge16-news-seen-pending.json,
                           so the next collect re-presents them (never burned).
      * judge failed or was partial -> the next run's --judge-input regenerates
                           every batch file and the agent judges the missing
                           batches first.
      * commit failed   -> commit is all-or-nothing; the next --commit replays the
                           same judged evidence and nothing was half-committed.

STATE FILE (the documented single contract)
    data/canonical/research/trackers/ge16-selfheal-state.json
    {
      "schema": "ge16.selfheal-state.v1",
      "updated_at": "<utc iso>",
      "steps": {
        "collect": STEP, "judge": STEP, "commit": STEP
      }
    }
    STEP = {"step": "collect|judge|commit", "outcome": "ok|partial|fail",
            "failure_class": "operational|design|", "ts": "<utc iso>",
            "detail": "<one line>", "cycle": "<collection_id>"}

FAILURE CLASSES
    operational  fetch error, timeout, partial batch, missing key or batch file,
                 unreadable evidence -> safe to retry automatically.
    design       validation failed after well-formed judged output, contract
                 precondition, stale evidence -> reported, never re-judged blindly.
    Anything unmatched defaults to operational, because the retry path is
    idempotent (collect re-presents, --judge-input regenerates, --commit is
    all-or-nothing) and a missed retry costs a week.

BANNER
    banner() emits the one line the live agent must see when a step is still
    unfinished:
      "SELFHEAL: previous cycle failed at <step> (<detail>) - this run will retry
       it automatically"
    Only the last outcome per step is kept, so the banner advertises a failure
    until that same step records a success - no daemon and no manual clearing.
"""
import json
import os
import tempfile
from datetime import datetime, timezone

SCHEMA = "ge16.selfheal-state.v1"
STEPS = ("collect", "judge", "commit")
OPERATIONAL = "operational"
DESIGN = "design"

BANNER_PREFIX = "SELFHEAL: previous cycle failed at %s (%s) \u2014 this run will retry it automatically"

# Tokens are matched case-insensitively against "<ExceptionType>: <exception> <message>".
# DESIGN is checked first: a validation failure that happens to mention a missing
# field is still a design failure, not a fetch problem.
DESIGN_SIGNALS = (
    "validation", "contract", "precondition", "digest", "schema", "malformed",
    "mismatch", "invalid", "refus", "stale",
)
OPERATIONAL_SIGNALS = (
    "timed out", "timeout", "connection", "network", "urlopen", "urlerror",
    "httperror", "http error", "temporary failure", "name or service",
    "reset by peer", "remote end closed", "ssl", "fetch error", "partial",
    "missing", "not found", "no such file", "key file", "unreadable",
    "incomplete", "interrupted",
)

# Timestamp of the last write, used to pick the most recent outstanding failure.
_EMPTY_STATE = {"schema": SCHEMA, "updated_at": "", "steps": {}}

#: Env knob for a staged run (see state_path).
SELFHEAL_STATE_ENV = "GE16_SELFHEAL_STATE"


def state_path(data_root=None):
    """Resolve the single state file under a DATA root (default: this repo).

    ``GE16_SELFHEAL_STATE`` overrides the path so a staged run (the baseline
    rebuild's dry run and staged commit) records its outcome without writing the
    live tracker directory. Unset -> the documented live path.
    """
    override = os.environ.get(SELFHEAL_STATE_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if data_root is None:
        data_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(str(data_root), "canonical", "research", "trackers", "ge16-selfheal-state.json")


# Module-level default; track_ge16_news.py and the tests both read this name.
STATE = state_path()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify(message, exc=None):
    """Return OPERATIONAL or DESIGN for a failure message/exception."""
    text = " ".join(str(part) for part in (type(exc).__name__ if exc else "", exc or "", message or ""))
    lowered = text.lower()
    for signal in DESIGN_SIGNALS:
        if signal in lowered:
            return DESIGN
    for signal in OPERATIONAL_SIGNALS:
        if signal in lowered:
            return OPERATIONAL
    return OPERATIONAL


def load_state(path=None):
    """Read the state file; a missing or corrupt file heals to the empty state."""
    target = path or STATE
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return json.loads(json.dumps(_EMPTY_STATE))
    if not isinstance(data, dict):
        return json.loads(json.dumps(_EMPTY_STATE))
    steps = data.get("steps")
    data["steps"] = steps if isinstance(steps, dict) else {}
    data.setdefault("schema", SCHEMA)
    data.setdefault("updated_at", "")
    return data


def record(step, outcome, detail="", failure_class="", cycle="", path=None):
    """Atomically record the last outcome of one step. Returns the written state."""
    if step not in STEPS:
        raise ValueError("unknown self-heal step: %r" % (step,))
    if outcome not in ("ok", "partial", "fail"):
        raise ValueError("unknown self-heal outcome: %r" % (outcome,))
    target = path or STATE
    state = load_state(target)
    state["schema"] = SCHEMA
    state["updated_at"] = utc_now()
    state["steps"][step] = {
        "step": step,
        "outcome": outcome,
        "failure_class": failure_class or ("" if outcome == "ok" else classify(detail)),
        "ts": utc_now(),
        "detail": " ".join(str(detail or "").split())[:300],
        "cycle": cycle or "",
    }
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".selfheal-", dir=directory or ".")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    return state


def outstanding(state, current_cycle=""):
    """Failed/partial steps still awaiting a successful retry, most recent first.

    The state keeps only the LAST outcome per step, so a failure stays advertised
    until that same step records a success - that is what makes the banner
    self-clearing without a daemon. (current_cycle is accepted for call-site
    clarity and to label the banner; it never suppresses a pending retry, so a
    re-run inside the same cycle still reports what is unfinished.)
    """
    steps = state.get("steps") or {}
    failed = []
    for step in STEPS:
        entry = steps.get(step)
        if not isinstance(entry, dict):
            continue
        if entry.get("outcome") not in ("partial", "fail"):
            continue
        failed.append(entry)
    failed.sort(key=lambda entry: (entry.get("ts") or "", entry.get("step") or ""), reverse=True)
    return failed


def banner(state, current_cycle=""):
    """The stdout banner the live cron agent reads, or '' when nothing is pending."""
    failed = outstanding(state, current_cycle)
    if not failed:
        return ""
    entry = failed[0]
    detail = entry.get("detail") or ""
    failure_class = entry.get("failure_class") or ""
    if failure_class:
        detail = ("%s [%s]" % (detail, failure_class)) if detail else failure_class
    return BANNER_PREFIX % (entry.get("step"), detail or "no detail recorded")
