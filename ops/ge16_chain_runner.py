#!/usr/bin/env python3
"""GE16 completion-triggered chain orchestrator (owner-side listener).

The weekly GE16 chain used to advance on a *clock*: five scheduled jobs at
22:00/01:00/03:00/05:00/05:30. Clock chaining is fragile in both directions --
a fast stage still waits for its slot, and a slow stage fires the next job on
top of an unfinished predecessor.

This script makes the same chain advance on *completion*. It is deliberately
small, deterministic and non-agentic:

- It runs from the OWNER's launchd context, never from a cron agent. The cron
  prompt keeps forbidding the agent from minting keys; the mint stays here.
- It reads the predecessor handoff manifest, binds it by SHA-256 (never by
  timestamp), and only then mints the next stage's one-shot owner fire key.
- Every stage execution still goes through the untouched
  `OPS/cron/run_stage.py` scheduled-fire path with the exact contract, exact
  version and a fresh <=15-minute owner key. No stage logic is duplicated, no
  scheduler-binding check is bypassed, and the registry is only ever read.
- Stage 1 stays judgement work (the chain job's agent collects, judges and
  commits phases a/b) and Stage 5 stays release-authorization work (its own
  job); the daemon drives the deterministic single-phase adapters, Stages 2-4,
  and observes Stage 1/5 completion.

Failure policy: a failed stage stops the chain. Nothing is retried and nothing
is auto-advanced; the next weekly run resumes at the first un-completed stage,
and the five stage jobs stay scheduled as the recovery floor.

State: ~/.hermes/profiles/coding/cron/ge16-chain-state.json (owner-side).
Evidence: OPS/logs/chain/ge16-chain-<run_id>.json (OPS-local, gitignored).
Log: ~/.hermes/logs/ge16-chain.log.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

OPS_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = OPS_ROOT.parent
if str(OPS_ROOT) not in sys.path:
    # Standalone use (`python3 OPS/cron/ge16_chain_runner.py --poll`) has no OPS
    # root on sys.path.
    sys.path.insert(0, str(OPS_ROOT))

from cron import run_stage  # noqa: E402  code-owned: exact handoff paths + owner key directory

SCHEMA = "ge16.chain-state.v1"
CHAIN_ORDER = (1, 2, 3, 4, 5)
# The daemon drives the deterministic single-phase adapters. Stage 1 is
# judgement work and Stage 5 is owner-authorized release work: both are
# agent-driven and the daemon only waits for their artifact. `run_stage`
# agrees: bounded-live execution allows stages 1-4 and Stage 5 is
# isolated-dry-run-only.
DRIVEN_STAGES = (2, 3, 4)
AGENT_STAGES = (1, 5)
#: The only fire keys the owner-side daemon may mint: Stage 1's two phases (the
#: chain trigger surfaces them for the chain job's agent) and the single-phase
#: driven stages. Stage 5 is never minted for: bounded-live execution stops at
#: Stage 4 and Stage 5 is isolated-dry-run-only.
MINTABLE_FIRE_KEYS = ((1, "a"), (1, "b"), (2, None), (3, None), (4, None))
OWNER_DOMAINS = ("1_DATA", "2_ANALYTICS", "3_OUTPUTS", "4_DELIVERY")
HANDOFF_FILES = {
    stage: REPO_ROOT / run_stage.HANDOFFS[stage][0] / "manifest" / run_stage.HANDOFFS[stage][1]
    for stage in CHAIN_ORDER
}
# The wait window the daemon enforces for each stage, in minutes, from the chain
# design (Stage 1 240, Stage 2 600, Stage 3 30, Stage 4 120). Stage 5 is
# owner-authorized release work that runs on its own 05:30 schedule after the
# Stage 4 handoff, so its wait is bounded by the run deadline instead of a
# 45-minute window that a healthy week could never meet.
STAGE_WINDOW_MINUTES: Dict[int, Optional[int]] = {1: 240, 2: 600, 3: 30, 4: 120, 5: None}
MAX_RUN_HOURS = 12
KEY_TTL_MINUTES = 14  # the runner caps every authorization at 15 minutes
POLL_MINUTES = 2
STATE_PATH = pathlib.Path("/Users/faisal.muthalib/.hermes/profiles/coding/cron/ge16-chain-state.json")
EVIDENCE_DIR = OPS_ROOT / "logs" / "chain"
LOG_PATH = pathlib.Path("/Users/faisal.muthalib/.hermes/logs/ge16-chain.log")
MINT_SCRIPT = OPS_ROOT / "cron" / "mint_fire_key.py"
RUNNER_SCRIPT = OPS_ROOT / "cron" / "run_stage.py"
KEY_DIRECTORY = pathlib.Path(run_stage.FIRE_KEY_DIRECTORY)
CHILD_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "HOME": str(pathlib.Path.home()), "PYTHONDONTWRITEBYTECODE": "1"}
MAX_EVENTS = 200
MYT = dt.timezone(dt.timedelta(hours=8), "MYT")
CHAIN_ANCHOR_WEEKDAY = 4  # Friday, matches the 0 22 * * 5 chain schedule
CHAIN_ANCHOR_HOUR = 22


# --------------------------------------------------------------------------
# time, hashing and state helpers
# --------------------------------------------------------------------------
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stamp(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def anchor_for(now: dt.datetime) -> str:
    """Return the Friday 22:00 Asia/Kuala_Lumpur anchor of the current chain week."""
    local = now.astimezone(MYT)
    days_since = (local.weekday() - CHAIN_ANCHOR_WEEKDAY) % 7
    candidate = (local - dt.timedelta(days=days_since)).replace(
        hour=CHAIN_ANCHOR_HOUR, minute=0, second=0, microsecond=0)
    if candidate > local:
        candidate -= dt.timedelta(days=7)
    return candidate.strftime("%Y-%m-%dT%H%M%z")


def sha256_file(path: pathlib.Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_manifest(path: pathlib.Path) -> Tuple[Optional[dict], Optional[str]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, hashlib.sha256(raw).hexdigest()


def _manifest_label(path: pathlib.Path, repo_root: pathlib.Path = REPO_ROOT) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _step(stage: int, handoffs: Optional[Dict[int, pathlib.Path]] = None,
          repo_root: pathlib.Path = REPO_ROOT) -> Dict[str, Any]:
    manifest = (handoffs or HANDOFF_FILES)[stage]
    return {
        "stage": stage,
        "mode": "agent" if stage in AGENT_STAGES else "runner",
        "status": "pending",
        "attempts": 0,
        "manifest_sha256": None,
        "manifest_path": _manifest_label(manifest, repo_root),
        "key_id": None,
        "key_file": None,
        "pid": None,
        "started_at_utc": None,
        "finished_at_utc": None,
        "window_minutes": STAGE_WINDOW_MINUTES[stage],
        "reason": None,
    }


def new_run_state(run_id: str, now: dt.datetime,
                  handoffs: Optional[Dict[int, pathlib.Path]] = None,
                  repo_root: pathlib.Path = REPO_ROOT) -> Dict[str, Any]:
    handoffs = handoffs or HANDOFF_FILES
    baseline = {str(stage): sha256_file(handoffs[stage]) for stage in CHAIN_ORDER}
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "status": "running",
        "stop_reason": None,
        "created_at_utc": stamp(now),
        "started_at_utc": stamp(now),
        "updated_at_utc": stamp(now),
        "deadline_utc": stamp(now + dt.timedelta(hours=MAX_RUN_HOURS)),
        "polls": 0,
        "baseline": baseline,
        "steps": {str(stage): _step(stage, handoffs, repo_root) for stage in CHAIN_ORDER},
        "events": [],
    }


def event(state: Dict[str, Any], now: dt.datetime, name: str, **fields: Any) -> None:
    record = {"at_utc": stamp(now), "event": name}
    record.update({key: value for key, value in fields.items() if value is not None})
    state.setdefault("events", []).append(record)
    state["events"] = state["events"][-MAX_EVENTS:]
    state["updated_at_utc"] = stamp(now)


def load_state(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def save_state(state: Dict[str, Any], path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def write_evidence(state: Dict[str, Any], directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "ge16.chain-record.v1",
        "run_id": state["run_id"],
        "status": state["status"],
        "stop_reason": state["stop_reason"],
        "written_at_utc": state["updated_at_utc"],
        "owner_domain": "OPS",
        "writes": ["OPS/logs/chain"],
        "gate_records": [
            {
                "run_id": state["run_id"],
                "gate_id": "chain-run-complete-recorded",
                "status": "passed" if state["status"] == "complete" else "pending",
                "checked_at_utc": state["updated_at_utc"],
                "evidence_ref": "OPS/logs/chain/ge16-chain-%s.json#sha256" % state["run_id"],
            }
        ],
        "chain": state,
    }
    target = directory / ("ge16-chain-%s.json" % state["run_id"])
    target.write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")
    return target


def log(message: str, path: pathlib.Path = LOG_PATH, limit: int = 200_000) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > limit:
            path.rename(path.with_suffix(path.suffix + ".1"))
        with open(path, "a") as handle:
            handle.write("%s %s\n" % (stamp(utc_now()), message))
    except OSError:
        pass


# --------------------------------------------------------------------------
# completion detection (content hashes, never timestamps)
# --------------------------------------------------------------------------
def completed_manifest(state: Dict[str, Any], stage: int,
                       handoffs: Optional[Dict[int, pathlib.Path]] = None
                       ) -> Tuple[Optional[dict], Optional[str]]:
    """Return the stage manifest only if it is new for this run and chained.

    A manifest counts only when it is (a) present, (b) different from the
    baseline captured when the run started -- so last week's artifact can never
    satisfy this week -- and (c) for every stage after the first, carrying an
    input reference to the exact SHA-256 of the recorded predecessor manifest.
    """
    data, digest = read_manifest((handoffs or HANDOFF_FILES)[stage])
    if data is None or digest is None:
        return None, None
    if digest == state["baseline"].get(str(stage)):
        return None, None
    if stage > 1:
        predecessor = state["steps"][str(stage - 1)]["manifest_sha256"]
        if not predecessor:
            return None, None
        needle = "#sha256:" + predecessor
        refs = data.get("input_manifest_refs")
        if not isinstance(refs, list) or not any(
                isinstance(ref, str) and ref.endswith(needle) for ref in refs):
            return None, None
    return data, digest


# --------------------------------------------------------------------------
# owner-side effects (the only two, both injectable for tests)
# --------------------------------------------------------------------------
def mint_key(spec: Dict[str, Any], key_directory: pathlib.Path = KEY_DIRECTORY,
             timeout: int = 90) -> Dict[str, Any]:
    """Mint one one-shot owner fire key through the mints-script PTY path.

    The mint script requires an owner terminal; the daemon obtains one exactly
    the way the existing remint daemon does, with `/usr/bin/script -q /dev/null`.
    """
    stage = int(spec["stage"])
    if (stage, spec.get("phase")) not in MINTABLE_FIRE_KEYS:
        raise ValueError("the chain daemon mints only the single-phase driven stages and Stage 1's two phases")
    key_file = pathlib.Path(spec["key_file"])
    key_directory.mkdir(parents=True, exist_ok=True)
    os.chmod(key_directory, 0o700)
    command = ["/usr/bin/script", "-q", "/dev/null", "/usr/bin/python3", str(MINT_SCRIPT),
               "--stage", str(stage), "--ttl-minutes", str(KEY_TTL_MINUTES),
               "--out", str(key_file), "--confirm-owner"]
    result = subprocess.run(command, capture_output=True, timeout=timeout,
                            env=dict(CHILD_ENVIRONMENT))
    if result.returncode != 0 or not key_file.exists():
        raise RuntimeError("owner key mint failed for stage %d" % stage)
    payload = json.loads(key_file.read_text(encoding="utf-8")).get("payload") or {}
    return {
        "key_file": str(key_file),
        "key_id": payload.get("key_id"),
        "expires_at_utc": payload.get("expires_at_utc"),
        "sha256": sha256_file(key_file),
    }


def launch_stage(command: List[str], log_path: pathlib.Path) -> int:
    """Start one exact `run_stage.py --scheduled-fire` argv, detached."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=handle,
                                   stdin=subprocess.DEVNULL, cwd=str(REPO_ROOT),
                                   env=dict(CHILD_ENVIRONMENT), start_new_session=True)
    return process.pid


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stage_command(stage: int, key_file: pathlib.Path) -> List[str]:
    domain, filename, contract_id, contract_version = run_stage.HANDOFFS[stage]
    del domain, filename
    return ["/usr/bin/python3", str(RUNNER_SCRIPT), "--contract", contract_id,
            "--version", contract_version, "--scheduled-fire",
            "--fire-key", str(key_file), "--stage", str(stage)]


def key_path_for(stage: int, now: dt.datetime, key_directory: pathlib.Path,
                 phase: Optional[str] = None) -> pathlib.Path:
    name = "%s-%s" % (stage, phase) if phase else str(stage)
    return key_directory / ("%s-%sZ.json" % (name, now.strftime("%Y%m%dT%H%M%S")))


def key_payload(path: pathlib.Path) -> Optional[dict]:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    return payload if isinstance(payload, dict) else None


def key_identity(payload: dict) -> str:
    """The runner's one-shot consumption identity for a scheduled-fire payload."""
    return hashlib.sha256(run_stage._canonical_json_bytes(payload)).hexdigest()


def key_claimed(payload: dict, now: dt.datetime,
                repo_root: pathlib.Path = REPO_ROOT) -> bool:
    """True when the key is already consumed or no longer valid."""
    try:
        expires = dt.datetime.strptime(payload["expires_at_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (KeyError, TypeError, ValueError):
        return True
    if expires <= now:
        return True
    identity = key_identity(payload)
    claims = run_stage.LIVE_CLAIMS_DIRECTORY
    return any((repo_root / domain / claims / identity).exists()
               for domain in OWNER_DOMAINS)


def stage_one_key_available(stage: int, phase: str, now: dt.datetime,
                           key_directory: pathlib.Path = KEY_DIRECTORY,
                           repo_root: pathlib.Path = REPO_ROOT) -> bool:
    """True when Stage 1 already has an unconsumed, unexpired key for a phase."""
    for path in sorted(key_directory.glob("%d-%s-*.json" % (stage, phase))):
        payload = key_payload(path)
        if payload is not None and not key_claimed(payload, now, repo_root):
            return True
    return False


# --------------------------------------------------------------------------
# the state machine
# --------------------------------------------------------------------------
class ChainRunner:
    """Advance one chain run as far as this poll can, and never guess."""

    def __init__(
        self,
        *,
        state_path: pathlib.Path = STATE_PATH,
        evidence_dir: pathlib.Path = EVIDENCE_DIR,
        key_directory: pathlib.Path = KEY_DIRECTORY,
        stage_log_dir: Optional[pathlib.Path] = None,
        handoffs: Optional[Dict[int, pathlib.Path]] = None,
        repo_root: pathlib.Path = REPO_ROOT,
        mint: Callable[..., Dict[str, Any]] = mint_key,
        launch: Callable[..., int] = launch_stage,
        alive: Callable[[int], bool] = process_alive,
        now_fn: Callable[[], dt.datetime] = utc_now,
        log_fn: Callable[[str], None] = log,
    ) -> None:
        self.state_path = state_path
        self.evidence_dir = evidence_dir
        self.key_directory = key_directory
        self.stage_log_dir = stage_log_dir or state_path.parent / "ge16-chain-logs"
        self.handoffs = dict(handoffs or HANDOFF_FILES)
        self.repo_root = repo_root
        self._mint = mint
        self._launch = launch
        self._alive = alive
        self._now = now_fn
        self._log = log_fn

    # -- run lifecycle -----------------------------------------------------
    def start(self, run_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """Start (or resume) the chain run for an anchor. Idempotent."""
        now = self._now()
        anchor = run_id or anchor_for(now)
        state = load_state(self.state_path)
        if state is None or state["run_id"] != anchor or force:
            state = new_run_state(anchor, now, self.handoffs, self.repo_root)
            event(state, now, "run-created" if force else "run-started", run_id=anchor)
        else:
            event(state, now, "run-resumed", run_id=anchor)
        self.surface_stage_one_keys(state, now)
        save_state(state, self.state_path)
        return state

    def status(self) -> Optional[Dict[str, Any]]:
        return load_state(self.state_path)

    # -- the poll ----------------------------------------------------------
    def poll(self) -> Optional[Dict[str, Any]]:
        state = load_state(self.state_path)
        if state is None:
            self._log("poll: no chain state yet; the Friday chain trigger has not started a run")
            return None
        now = self._now()
        material = ("status", "stop_reason", "steps", "events", "baseline")
        before = json.dumps({key: state.get(key) for key in material}, sort_keys=True)
        state["polls"] = int(state.get("polls") or 0) + 1
        self.surface_stage_one_keys(state, now)
        self._advance(state, now)
        after = json.dumps({key: state.get(key) for key in material}, sort_keys=True)
        save_state(state, self.state_path)
        if before != after:
            write_evidence(state, self.evidence_dir)
        return state

    def surface_stage_one_keys(self, state: Dict[str, Any], now: dt.datetime) -> None:
        """Keep a fresh unconsumed Stage 1 key for each phase while it is waiting.

        The chain trigger is how Stage 1's key is surfaced (the packet's
        on-demand trigger). It never over-mints: a phase that already has an
        unconsumed, unexpired key in the one-shot directory is skipped, which is
        the same idempotence rule the scheduled-fire remint daemon uses.
        """
        step = state["steps"]["1"]
        if step["status"] not in ("pending", "awaiting"):
            return
        for phase in ("a", "b"):
            if stage_one_key_available(1, phase, now, self.key_directory, self.repo_root):
                continue
            key_file = key_path_for(1, now, self.key_directory, phase)
            try:
                minted = self._mint({"stage": 1, "phase": phase, "key_file": str(key_file),
                                     "ttl_minutes": KEY_TTL_MINUTES, "trigger": "chain-start"},
                                    key_directory=self.key_directory)
            except Exception as error:  # a missing key stops the chain job, never fakes one
                self._log("stage-1 key surfacing failed for phase %s: %s" % (phase, type(error).__name__))
                event(state, now, "stage-1-key-surface-failed", phase=phase,
                      error=type(error).__name__)
                continue
            event(state, now, "stage-1-key-surfaced", phase=phase, key_id=minted.get("key_id"),
                  expires_at_utc=minted.get("expires_at_utc"))

    def _advance(self, state: Dict[str, Any], now: dt.datetime) -> None:
        if state["status"] != "running":
            return
        started = parse_stamp(state["started_at_utc"])
        if now - started > dt.timedelta(hours=MAX_RUN_HOURS):
            self._fail(state, now, "run-deadline-exceeded", {"age_hours": round((now - started).total_seconds() / 3600.0, 3)})
            return
        for stage in CHAIN_ORDER:
            step = state["steps"][str(stage)]
            if step["status"] == "complete":
                continue
            _, digest = completed_manifest(state, stage, self.handoffs)
            if digest is not None:
                self._complete(state, now, stage, digest, state["steps"][str(stage)].get("pid"))
                continue
            if stage in AGENT_STAGES:
                self._await_agent(state, now, stage)
                return
            self._drive(state, now, stage)
            return
        self._finish(state, now)

    def _complete(self, state: Dict[str, Any], now: dt.datetime, stage: int,
                  digest: str, pid: Optional[int]) -> None:
        step = state["steps"][str(stage)]
        step["manifest_sha256"] = digest
        step["status"] = "complete"
        step["finished_at_utc"] = stamp(now)
        step["pid"] = None
        event(state, now, "stage-complete", stage=stage, manifest_sha256=digest,
              mode=step["mode"], previous_pid=pid)

    def _await_agent(self, state: Dict[str, Any], now: dt.datetime, stage: int) -> None:
        step = state["steps"][str(stage)]
        if step["status"] == "pending":
            step["status"] = "awaiting"
            step["started_at_utc"] = stamp(now)
            event(state, now, "stage-awaiting", stage=stage, mode="agent")
        window = STAGE_WINDOW_MINUTES.get(stage)
        if window is None:
            # Stage 5 is owner-authorized release work whose own job window is
            # 45 minutes once started; the run deadline bounds the wait.
            return
        started = parse_stamp(step["started_at_utc"])
        if now - started > dt.timedelta(minutes=window):
            self._fail(state, now, "stage-%d-handoff-missing-within-window" % stage,
                       {"stage": stage, "window_minutes": window, "mode": "agent"})

    def _drive(self, state: Dict[str, Any], now: dt.datetime, stage: int) -> None:
        step = state["steps"][str(stage)]
        if step["status"] == "running":
            started = parse_stamp(step["started_at_utc"])
            window = STAGE_WINDOW_MINUTES[stage] or MAX_RUN_HOURS * 60
            elapsed = now - started
            pid = step.get("pid")
            if pid is not None and self._alive(int(pid)) and elapsed <= dt.timedelta(minutes=window):
                return  # the launched runner is still working; wait
            if pid is not None and self._alive(int(pid)):
                self._fail(state, now, "stage-%d-exceeded-window-while-running" % stage,
                           {"stage": stage, "window_minutes": window, "pid": int(pid)})
                return
            self._fail(state, now, "stage-%d-runner-exited-without-handoff" % stage,
                       {"stage": stage, "pid": pid, "key_id": step.get("key_id")})
            return

        predecessor = state["steps"][str(stage - 1)]
        if predecessor["status"] != "complete":
            self._fail(state, now, "predecessor-not-complete", {"stage": stage})
            return
        key_file = key_path_for(stage, now, self.key_directory)
        step["attempts"] = int(step["attempts"]) + 1
        step["started_at_utc"] = stamp(now)
        try:
            minted = self._mint({"stage": stage, "phase": None, "key_file": str(key_file),
                                 "ttl_minutes": KEY_TTL_MINUTES, "predecessor_sha256": predecessor["manifest_sha256"]},
                                key_directory=self.key_directory)
        except Exception as error:  # mint failure stops the chain; never invent a key
            step["status"] = "pending"
            step["reason"] = "mint-failed"
            self._fail(state, now, "stage-%d-key-mint-failed" % stage,
                       {"stage": stage, "error": type(error).__name__})
            return
        command = stage_command(stage, pathlib.Path(minted["key_file"]))
        step["key_id"] = minted.get("key_id")
        step["key_file"] = minted.get("key_file")
        try:
            pid = self._launch(command, self.stage_log_dir / ("stage-%d-%s.log" % (
                stage, now.strftime("%Y%m%dT%H%M%SZ"))))
        except Exception as error:
            step["reason"] = "launch-failed"
            self._fail(state, now, "stage-%d-launch-failed" % stage,
                       {"stage": stage, "error": type(error).__name__})
            return
        step["status"] = "running"
        step["pid"] = int(pid)
        event(state, now, "stage-launched", stage=stage, pid=int(pid), key_id=minted.get("key_id"),
              expires_at_utc=minted.get("expires_at_utc"), argv=" ".join(command))

    def _fail(self, state: Dict[str, Any], now: dt.datetime, reason: str, extra: Dict[str, Any]) -> None:
        state["status"] = "failed"
        state["stop_reason"] = reason
        state["finished_at_utc"] = stamp(now)
        event(state, now, "run-failed", reason=reason, **extra)
        self._log("chain run %s failed: %s" % (state["run_id"], reason))

    def _finish(self, state: Dict[str, Any], now: dt.datetime) -> None:
        state["status"] = "complete"
        state["finished_at_utc"] = stamp(now)
        event(state, now, "run-complete")
        self._log("chain run %s complete" % state["run_id"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _lock(path: pathlib.Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="GE16 owner-side completion-triggered chain orchestrator")
    parser.add_argument("--state", type=pathlib.Path, default=STATE_PATH)
    parser.add_argument("--evidence-dir", type=pathlib.Path, default=EVIDENCE_DIR)
    parser.add_argument("--start", action="store_true",
                        help="owner on-demand trigger: start or resume this chain week's run")
    parser.add_argument("--run-id", default=None, help="explicit run id (anchor) for --start")
    parser.add_argument("--force", action="store_true", help="with --start: begin a fresh run for the anchor")
    parser.add_argument("--poll", action="store_true", help="one polling pass (default)")
    parser.add_argument("--status", action="store_true", help="print the read-only chain state")
    args = parser.parse_args(argv)

    runner = ChainRunner(state_path=args.state, evidence_dir=args.evidence_dir)
    if args.status:
        state = runner.status()
        if state is None:
            print(json.dumps({"schema": SCHEMA, "status": "no-run", "state_file": str(args.state)}, indent=1))
            return 0
        print(json.dumps(state, indent=1, sort_keys=True))
        return 0

    handle = _lock(args.state.with_suffix(".lock"))
    if handle is None:
        return 0  # another tick is already running
    try:
        if args.start:
            runner.start(run_id=args.run_id, force=args.force)
        elif not args.poll:
            parser.error("one of --start, --poll or --status is required")
        state = runner.poll()
        if state is not None:
            print(json.dumps({"run_id": state["run_id"], "status": state["status"],
                              "stop_reason": state["stop_reason"],
                              "steps": {key: value["status"] for key, value in state["steps"].items()}},
                             indent=1, sort_keys=True))
        return 0
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
