#!/usr/bin/env python3
"""P2.6 — source checkpoints, the complete accepted-news archive, and the
baseline/incremental mode layer.

Design brief: evidence/P2/P2.6-design-brief.md. Not placed under
``data/scripts/collect/`` on purpose: that directory is collector-only
(track_ge16_*.py + their tightly-scoped helpers); this module drives those
collectors from the outside as subprocesses, exactly like
``refresh_canonical_data.py``'s P2.3 run layer does, and must never itself be
mistaken for a collector.

## §2.1 Source checkpoints (open question: per-source vs one bundle file)

Decision: **one JSON file per source** under ``data/canonical/checkpoints/``
(``<source_id>.json``), not a single bundle. Reasons: (a) sources evolve on
independent cadences (news is continuous, polls/candidates are bursty) so a
shared file would force one writer to serialize around every source's
cadence and complicates the collision-refusal story; (b) P2.9's coverage doc
(named in the brief as a consumer) reads one source at a time; (c) it keeps
the "content-addressed, no-op runs don't touch it" property (below) scoped
per file instead of forcing a whole-bundle rewrite whenever any one source's
run is a no-op.

Each file holds the single LATEST checkpoint for that source (not a JSONL
history) — a checkpoint's whole purpose is "where do I resume from", and
carrying old checkpoints forward would make that lookup ambiguous. History
of every past run is already preserved in the run directories
(``data/work/<run_id>/``, P2.3) and the edition chain; the checkpoint file
itself only needs to name the run that produced it (``run_id``).

Idempotency (boundary #5): a checkpoint write is skipped entirely — not even
``collected_at`` moves — when the run found nothing to change
(``items_seen == 0 and items_accepted == 0``) and a checkpoint already
exists. This makes a true no-op incremental produce ZERO byte changes,
stronger than "only collected_at moves". The documented exception applies
only when a run DOES have something to record (baseline runs always write;
incrementals with any observed/accepted items write) but the resulting
window happens to match a prior checkpoint's — there ``collected_at`` is the
only field expected to differ.

## §2.2 Complete accepted archive

Decision: ``data/canonical/archive/news-accepted/accepted.jsonl`` (append
-only JSONL, one line per accepted item, oldest-first). Chosen over the
brief's literal ``news-accepted/`` example because a single append-only file
is the simplest collision-refusing structure (P2.4/P2.5 precedent: append
-only files, never rewritten) and needs no sharding at this corpus size
(thousands, not millions, of rows).

Identity for dedup/idempotent-append: normalized-link identity ALONE
undercounts here — the live corpus has 148 pairs of genuinely distinct
accepted entries that share one normalized link (the same story re-queued
and re-judged at a different ``judged_at``, sometimes with a retitled
Google-News headline). Deduping on link alone would seed the archive with
2,219 rows, undershooting the acceptance bar (count >= 2,367). So the
identity key hashes the FULL item with its ``link`` field replaced by its
normalized form (``normalize_link.v1``): distinct accepted entries stay
distinct, while re-merging the exact same item (same link post
-normalization + same everything else) is recognized as already-seen and
skipped (a no-op, counted, never appended twice).

## §2.3 Mode layer

``compute_window`` implements baseline (declared start -> now) vs
incremental (checkpoint's ``window_end`` -> now) per brief §2.1. Driving a
collector without editing it: ``track_ge16_news.py`` already exposes
``GE16_NEWS_MAX_DAYS`` (an existing P1/P2.3-era env knob, not new) as the
lookback-days override; ``run_news_collection`` computes the window and
converts it to a day count for that knob, then invokes the collector exactly
as ``refresh_canonical_data.py`` does (subprocess, ``work_paths.apply_env``
wiring so tracker files land in the run dir under staged mode). No file
under ``data/scripts/collect/`` is read for anything but this pre-existing
knob; ``git diff data/scripts/collect/`` stays empty (deviation protocol not
needed).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_ROOT = Path(__file__).resolve().parent
DATA_ROOT = SCRIPTS_ROOT.parent
CANONICAL_ROOT = DATA_ROOT / "canonical"
COLLECT_ROOT = SCRIPTS_ROOT / "collect"
REPOSITORY_ROOT = DATA_ROOT.parent

CHECKPOINTS_DIR = CANONICAL_ROOT / "checkpoints"
ARCHIVE_DIR = CANONICAL_ROOT / "archive" / "news-accepted"
ARCHIVE_FILE = ARCHIVE_DIR / "accepted.jsonl"
ACCEPTED_JSON = CANONICAL_ROOT / "research" / "trackers" / "ge16-news-accepted.json"

CHECKPOINT_SCHEMA = "ge16.source-checkpoint.v1"
#: R08 default baseline start (brief §2.1): epoch of the source's earliest
#: item, or this date, whichever the caller does not override.
DEFAULT_BASELINE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

sys.path.insert(0, str(SCRIPTS_ROOT / "import"))
try:
    from normalize_link import normalize_link  # noqa: E402
finally:
    sys.path.remove(str(SCRIPTS_ROOT / "import"))

sys.path.insert(0, str(SCRIPTS_ROOT))
try:
    import work_paths  # noqa: E402
finally:
    sys.path.remove(str(SCRIPTS_ROOT))


# --------------------------------------------------------------- checkpoints

def _checkpoint_path(source_id, checkpoints_dir=CHECKPOINTS_DIR):
    return Path(checkpoints_dir) / f"{source_id}.json"


def read_checkpoint(source_id, checkpoints_dir=CHECKPOINTS_DIR):
    """The latest checkpoint for ``source_id``, or None if never collected."""
    path = _checkpoint_path(source_id, checkpoints_dir)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def compute_window(source_id, mode, now=None, checkpoints_dir=CHECKPOINTS_DIR,
                    baseline_start=None):
    """(window_start, window_end) as timezone-aware datetimes.

    baseline: from ``baseline_start`` (default DEFAULT_BASELINE_START, R08) to
    ``now``. incremental: from the existing checkpoint's ``window_end`` to
    ``now`` — falls back to a baseline window when no checkpoint exists yet
    (an incremental run before any baseline has nothing to resume from).
    """
    now = now or datetime.now(timezone.utc)
    if mode not in ("baseline", "incremental"):
        raise ValueError(f"mode must be 'baseline' or 'incremental', got {mode!r}")
    if mode == "baseline":
        start = baseline_start or DEFAULT_BASELINE_START
        return start, now
    checkpoint = read_checkpoint(source_id, checkpoints_dir)
    if checkpoint is None:
        start = baseline_start or DEFAULT_BASELINE_START
        return start, now
    start = datetime.fromisoformat(checkpoint["window_end"])
    return start, now


def write_checkpoint(source_id, window_start, window_end, items_seen, items_accepted,
                      run_id, mode, checkpoints_dir=CHECKPOINTS_DIR, now=None,
                      force=False):
    """Write ``<source_id>.json``; skipped entirely (no bytes touched) for a
    genuine no-op incremental (see module docstring §2.1 idempotency note).
    Returns the written dict, or the untouched existing dict when skipped.
    """
    checkpoints_dir = Path(checkpoints_dir)
    existing = read_checkpoint(source_id, checkpoints_dir)
    if (not force and mode == "incremental" and items_seen == 0
            and items_accepted == 0 and existing is not None):
        return existing
    record = {
        "schema": CHECKPOINT_SCHEMA,
        "source_id": source_id,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "items_seen": items_seen,
        "items_accepted": items_accepted,
        "collected_at": _iso(now or datetime.now(timezone.utc)),
        "run_id": run_id,
        "mode": mode,
    }
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    path = _checkpoint_path(source_id, checkpoints_dir)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                     encoding="utf-8")
    return record


# ------------------------------------------------------------------- archive

def _archive_identity(item):
    """sha256 of the item with its link normalized — see module docstring
    §2.2 for why link-only identity undercounts the live corpus."""
    normalized = dict(item)
    if normalized.get("link"):
        normalized["link"] = normalize_link(normalized["link"])
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_archive_identities(archive_file):
    identities = set()
    if not Path(archive_file).is_file():
        return identities
    with open(archive_file, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            identities.add(json.loads(line)["_archive_id"])
    return identities


def archive_count(archive_file=ARCHIVE_FILE):
    if not Path(archive_file).is_file():
        return 0
    with open(archive_file, "r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def append_items(items, archive_file=ARCHIVE_FILE):
    """Append every item in ``items`` not already present (by archive
    identity). Collision-refusing: never rewrites an existing line, only
    ever opens in append mode. Returns (appended_count, skipped_count).
    """
    archive_file = Path(archive_file)
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_archive_identities(archive_file)
    appended = 0
    skipped = 0
    with open(archive_file, "a", encoding="utf-8") as handle:
        for item in items:
            identity = _archive_identity(item)
            if identity in existing:
                skipped += 1
                continue
            record = dict(item)
            record["_archive_id"] = identity
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            existing.add(identity)
            appended += 1
    return appended, skipped


def seed_archive_from_corpus(accepted_json=ACCEPTED_JSON, archive_file=ARCHIVE_FILE):
    """Baseline seed: every item currently in the rolling accepted corpus,
    deduped by archive identity, appended (idempotent — running twice adds
    nothing the second time)."""
    document = json.loads(Path(accepted_json).read_text(encoding="utf-8"))
    items = document.get("items", [])
    appended, skipped = append_items(items, archive_file)
    return {"source_items": len(items), "appended": appended, "skipped": skipped,
            "archive_count": archive_count(archive_file)}


# --------------------------------------------------------------- mode layer

NEWS_SOURCE_ID = "news"
NEWS_COLLECTOR = COLLECT_ROOT / "track_ge16_news.py"


class CollectionCycleError(RuntimeError):
    """A collection cycle failed; the window stays open (no checkpoint).

    Carries the collector's exit code and the tail of its stderr so the
    orchestrator/cron agent can log and retry the SAME window.
    """

    def __init__(self, message, returncode=None, stderr_tail=""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


def _window_days(window_start, window_end):
    delta = window_end - window_start
    return max(1, int(delta.total_seconds() // 86400) + 1)


def run_news_collection(mode, run=None, now=None, checkpoints_dir=CHECKPOINTS_DIR,
                        archive_file=ARCHIVE_FILE, accepted_json=ACCEPTED_JSON,
                        env=None, extra_env=None):
    """Drive one baseline/incremental collection cycle.

    Steps: (a) read the checkpoint, (b) compute the window, (c) run the
    EXISTING collector unmodified via its ``GE16_NEWS_MAX_DAYS`` knob
    (no new collector knob; the only collector change in P2.6 is the
    adjudicated candidates-collector staging fix), (d) after a SUCCESSFUL
    collect, re-seed the archive from the rolling accepted corpus
    (idempotent — append_items dedupes, so items accepted by any pipeline
    cycle between runs land exactly once), and (e) write the checkpoint.

    Failure policy (packet criterion: failures do not advance checkpoints):
    when the collector subprocess exits non-zero, NO checkpoint is written
    and the archive is left untouched — the next run recomputes the same
    window from the prior ``window_end`` and retries it. The exception is
    re-raised as ``CollectionCycleError`` (carrying the collector's
    returncode and stderr tail) so callers/CI see the failure loudly.

    Acceptance-path note: the collector writes ``ge16-news-accepted.json``
    only in its ``--commit`` step (judgment happens between collect and
    commit, often by the cron LLM agent). Rather than duplicate that
    pipeline here, step (d) reconciles the archive against whatever the
    accepted corpus contains at cycle end — so an item committed any time
    before the next cycle is captured by that cycle, and idempotent
    re-seeding makes this safe to repeat (BLOCKER 2 remediation,
    re-review 2026-09-29).

    ``run`` (a work_paths.RunPaths) is optional: when given, the collector's
    tracker writes are staged into the run dir (work_paths.apply_env), same
    as refresh_canonical_data.py's run layer; when omitted the collector
    reads/writes the live canonical tracker files directly.
    """
    now = now or datetime.now(timezone.utc)
    window_start, window_end = compute_window(NEWS_SOURCE_ID, mode, now=now,
                                              checkpoints_dir=checkpoints_dir)
    max_days = _window_days(window_start, window_end)
    process_env = dict(os.environ if env is None else env)
    if run is not None:
        work_paths.apply_env(run, env=process_env)
    process_env["GE16_NEWS_MAX_DAYS"] = str(max_days)
    if extra_env:
        process_env.update(extra_env)

    completed = subprocess.run(
        [sys.executable, str(NEWS_COLLECTOR)],
        capture_output=True, text=True, env=process_env, cwd=str(REPOSITORY_ROOT),
    )
    if completed.returncode != 0:
        # Failure policy: the window stays open. No checkpoint, no archive
        # append — the failed window is retried from the same window_end.
        raise CollectionCycleError(
            f"collector failed (exit {completed.returncode}); checkpoint NOT "
            f"advanced; window {window_start.isoformat()}..{window_end.isoformat()} "
            f"stays open for retry",
            returncode=completed.returncode,
            stderr_tail=completed.stderr[-2000:],
        )

    items_seen = 0
    candidates_path = CANONICAL_ROOT / "research" / "trackers" / "ge16-news-candidates.json"
    if run is not None:
        candidates_path = run.trackers / "ge16-news-candidates.json"
    if candidates_path.is_file():
        payload = json.loads(candidates_path.read_text(encoding="utf-8"))
        items_seen = payload.get("count", len(payload.get("items", [])))

    # Archive reconciliation: append whatever the rolling accepted corpus
    # holds that the archive does not yet have. Idempotent by construction
    # (append_items dedupes on archive identity), and correct across the
    # async collect -> judge -> commit pipeline: anything committed by the
    # time this cycle runs is captured here.
    seed = seed_archive_from_corpus(accepted_json=accepted_json,
                                    archive_file=archive_file)
    appended, skipped = seed["appended"], seed["skipped"]

    run_id = run.run_id if run is not None else now.strftime("%Y%m%dT%H%M%SZ") + "-00000000"
    checkpoint = write_checkpoint(
        NEWS_SOURCE_ID, window_start, window_end, items_seen=items_seen,
        items_accepted=appended, run_id=run_id, mode=mode,
        checkpoints_dir=checkpoints_dir, now=now)
    return {
        "mode": mode, "window_start": _iso(window_start), "window_end": _iso(window_end),
        "max_days": max_days, "collector_returncode": completed.returncode,
        "collector_stdout": completed.stdout, "collector_stderr": completed.stderr,
        "items_seen": items_seen, "items_newly_accepted": appended,
        "archive_appended": appended, "archive_skipped": skipped,
        "checkpoint": checkpoint,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed-archive", "run"])
    parser.add_argument("--mode", choices=["baseline", "incremental"], default="incremental")
    args = parser.parse_args()
    if args.command == "seed-archive":
        print(json.dumps(seed_archive_from_corpus(), indent=2, sort_keys=True))
    else:
        print(json.dumps(run_news_collection(args.mode), indent=2, sort_keys=True,
                         default=str))
